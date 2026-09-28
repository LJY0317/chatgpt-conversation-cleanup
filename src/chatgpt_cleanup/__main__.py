from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import sqlite3
import subprocess
import sys
import unicodedata

from . import __version__
from .appserver import AppServerError
from .core import (
    SafetyError,
    catalog,
    chatgpt_catalog,
    evict_catalog,
    maintain_recovery,
    purge_product_data,
    retire_installed_copy,
    recovery_root,
    restore_catalog,
    sessions,
)
from .codex_delete import execute_codex_delete_plan, preview_codex_delete
from .desktop_probe import Presence, probe_conversation_presence
from .evidence import (
    SAFE_CLEANUP_ERROR_CODES,
    desktop_failure_evidence,
    desktop_failure_evidence_many,
)
from .locking import operation_lock
from .identity import enrich_identities
from .platforms import PlatformError, current_platform
from .profiles import Profile, discover_profiles
from .reconcile import (
    CatalogAssessment,
    ReconcileStatus,
    assess_catalog_rows,
    assessment_counts,
)


def display(value):
    return "".join(
        character
        if not unicodedata.category(character).startswith("C")
        else " "
        for character in str(value)
    )[:160]


def _date(value):
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        text = str(value)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return None


def show(rows, *, kind):
    if kind == "catalog" and rows:
        print(
            "These chats look safe to clean up from ChatGPT Desktop on this computer. "
            "Some may already have disappeared from the sidebar.\n"
        )
    for index, row in enumerate(rows, 1):
        ident = row.get("id", row.get("thread_id"))
        title = row.get("title", row.get("display_title", "")) or "Untitled conversation"
        if kind == "archived":
            status = "Archived Codex chat"
            updated = _date(row.get("updated_at"))
        else:
            status = (
                "Deleted or not found in ChatGPT"
                if row.get("classification") == "confirmed-missing"
                else "Could not be loaded from ChatGPT"
                if row.get("classification") == "not-loadable"
                else "Marked missing by ChatGPT Desktop"
                if row.get("classification") == "missing-chatgpt-entry"
                else "Local leftover"
            )
            updated = _date(row.get("source_updated_at"))
        suffix = f" · {updated}" if updated else ""
        print(f"{index:>4}. {display(title)}")
        print(f"      {status}{suffix} · ID {display(str(ident)[:12])}…")
    if kind == "catalog":
        print(f"{len(rows)} local ChatGPT Desktop entr{'y' if len(rows) == 1 else 'ies'} shown for review.")
    else:
        print(f"{len(rows)} archived chat(s).")


def _prompt_default_all(count, *, mode):
    while True:
        if mode == "cleanup":
            prompt = (
                f"All {count} chats are selected for cleanup. "
                f"Press Enter or A to continue with all {count}, "
                "type numbers to clean up only those chats, or C to cancel: "
            )
        elif mode == "delete":
            prompt = (
                f"All {count} archived chats are selected for permanent deletion. "
                f"Press Enter or A to continue with all {count}, "
                "type numbers to delete only those chats, or C to cancel: "
            )
        else:
            prompt = (
                f"All {count} profiles are selected. "
                f"Press Enter or A to continue with all {count}, "
                "type profile numbers to use only those, or C to cancel: "
            )
        text = input(prompt).strip()
        lowered = text.lower()
        if not text or lowered in {"a", "all", "*"}:
            return "all", []
        if lowered == "c":
            return "cancel", []
        if "\x1b" in text:
            print("Arrow keys are not used here. Press Enter or A for all, type numbers like 1,3, or C to cancel.")
            continue
        try:
            indexes = [int(token.strip()) - 1 for token in text.split(",")]
        except ValueError:
            print("Enter numbers like 1,3, press Enter or A for all, or C to cancel.")
            continue
        if not (
            len(indexes) == len(set(indexes))
            and all(0 <= index < count for index in indexes)
        ):
            print("That selection is not valid. Enter numbers like 1,3, press Enter or A for all, or C to cancel.")
            continue
        return "subset", indexes


def choose(rows, *, kind, default_all=False):
    show(rows, kind=kind)
    if not rows:
        return []
    if default_all:
        action, indexes = _prompt_default_all(
            len(rows),
            mode="delete" if kind == "archived" else "cleanup",
        )
        if action == "all":
            return list(rows)
        if action == "cancel":
            return []
        return [rows[index] for index in indexes]
    else:
        text = input("Type numbers like 1,3, or press Enter to cancel: ").strip()
        if not text:
            return []
    try:
        indexes = [int(token.strip()) - 1 for token in text.split(",")]
    except ValueError as error:
        raise SafetyError("Enter numbers like 1,3.") from error
    if not (
        len(indexes) == len(set(indexes))
        and all(0 <= index < len(rows) for index in indexes)
    ):
        raise SafetyError("That selection is not valid.")
    return [rows[index] for index in indexes]


def _profile_detail(profile: Profile, *, show_routing_id=False):
    identity = profile.identity
    if not identity:
        return None
    routing_hint = None
    if show_routing_id and identity.workspace_account_id:
        routing_hint = "Workspace/account …" + identity.workspace_account_id[-8:]
    plan = identity.plan.replace("_", " ").title() if identity.plan else None
    parts = [
        identity.email,
        identity.workspace_name,
        identity.workspace_kind,
        plan,
        routing_hint,
    ]
    return " · ".join(part for part in parts if part) or None


def select_profiles(available: list[Profile], *, multiple=True):
    if not available:
        raise SafetyError("No supported ChatGPT/Codex profile was found.")
    if len(available) == 1:
        return list(available)
    print("\nChoose profiles\n")
    identity_keys = {}
    for profile in available:
        identity = profile.identity
        if not identity:
            continue
        key = (
            identity.email,
            identity.workspace_name,
            identity.workspace_kind,
            identity.plan,
        )
        identity_keys[key] = identity_keys.get(key, 0) + 1
    for index, profile in enumerate(available, 1):
        print(f"  {index}. {profile.display_name}")
        identity = profile.identity
        show_routing_id = False
        if identity:
            key = (
                identity.email,
                identity.workspace_name,
                identity.workspace_kind,
                identity.plan,
            )
            show_routing_id = identity_keys.get(key, 0) > 1 or not any(key)
        detail = _profile_detail(profile, show_routing_id=show_routing_id)
        if detail:
            print(f"     {detail}")
    if multiple:
        print()
        action, indexes = _prompt_default_all(len(available), mode="profiles")
        if action == "all":
            return list(available)
        if action == "cancel":
            return []
        return [available[index] for index in indexes]
    else:
        text = input("\nChoose one profile, or press Enter to cancel: ").strip()
    if not text:
        return []
    try:
        indexes = [int(token.strip()) - 1 for token in text.split(",")]
    except ValueError as error:
        raise SafetyError("Enter a profile number.") from error
    if not multiple and len(indexes) != 1:
        raise SafetyError("Choose exactly one profile.")
    if not (
        len(indexes) == len(set(indexes))
        and all(0 <= index < len(available) for index in indexes)
    ):
        raise SafetyError("That profile selection is not valid.")
    return [available[index] for index in indexes]


def _batch_summary(action, work, *, delete_plans=None):
    print("\nReady to continue\n")
    total = 0
    used_profiles = 0
    for profile, rows in work:
        if not rows:
            continue
        used_profiles += 1
        if action == "delete" and delete_plans is not None:
            plan = delete_plans[profile.id]
            count = len(plan.ordered_ids)
            dependent = len(plan.dependent_ids)
            total += count
            suffix = f" (+{dependent} dependent archived fork(s))" if dependent else ""
            print(f"{profile.display_name}: {count}{suffix}")
        else:
            total += len(rows)
            manual = sum(row.get("classification") == "manual-local-cleanup" for row in rows)
            suffix = f" ({manual} manually reviewed)" if manual else ""
            print(f"{profile.display_name}: {len(rows)}{suffix}")
    if action == "delete":
        print(
            "\nThese archived Codex chats will be permanently deleted through the "
            "official Codex app-server. Dependent archived forks are deleted before "
            "their parents. This cannot be undone."
        )
        phrase = f"DELETE {total} CHATS"
    else:
        print(
            "\nThis removes only stale ChatGPT Desktop entries from this Mac. "
            "It does not delete chats from ChatGPT servers."
        )
        phrase = f"CLEAN {total} CHATS"
    entered = input(f"Type [{phrase}] to continue, or press Enter to cancel: ").strip()
    return entered == phrase


def _pause_for_main_menu():
    input("\nPress Enter to return to the main menu: ")


def _assessments_from_sources(rows, failures, presence):
    return assess_catalog_rows(rows, failures, presence)


def _candidates_from_sources(rows, failures, presence):
    return [
        item.selected_row()
        for item in _assessments_from_sources(rows, failures, presence)
        if item.default_selected
    ]


def _assessment_label(item: CatalogAssessment) -> str:
    return {
        ReconcileStatus.CONFIRMED: "Confirmed leftover",
        ReconcileStatus.SUSPECTED: "Possible leftover",
        ReconcileStatus.UNKNOWN: "Not verified",
        ReconcileStatus.PROTECTED: "Protected — access issue",
        ReconcileStatus.PRESENT: "Still available in ChatGPT",
    }[item.status]


def _show_assessments(assessments: list[CatalogAssessment]):
    for index, item in enumerate(assessments, 1):
        row = item.row
        title = row.get("display_title") or "Untitled conversation"
        updated = _date(row.get("source_updated_at"))
        suffix = f" · {updated}" if updated else ""
        lock = " · protected" if not item.selectable else ""
        print(f"{index:>4}. {display(title)}")
        print(
            f"      {_assessment_label(item)}{lock}{suffix} · "
            f"ID {display(str(row['thread_id'])[:12])}…"
        )


def _parse_number_selection(text: str, count: int) -> list[int] | None:
    indexes: list[int] = []
    try:
        for raw in text.split(","):
            token = raw.strip()
            if not token:
                return None
            if "-" in token:
                first, last = token.split("-", 1)
                start = int(first.strip())
                end = int(last.strip())
                if start > end:
                    return None
                indexes.extend(range(start - 1, end))
            else:
                indexes.append(int(token) - 1)
    except ValueError:
        return None
    if not (
        indexes
        and len(indexes) == len(set(indexes))
        and all(0 <= index < count for index in indexes)
    ):
        return None
    return indexes


def choose_cleanup_assessments(assessments: list[CatalogAssessment]):
    counts = assessment_counts(assessments)
    confirmed = [item for item in assessments if item.default_selected]
    reviewable = [item for item in assessments if item.selectable]
    print(
        f"{counts[ReconcileStatus.CONFIRMED]} confirmed leftover(s), "
        f"{counts[ReconcileStatus.SUSPECTED]} possible leftover(s), "
        f"{counts[ReconcileStatus.UNKNOWN]} not verified."
    )
    protected = counts[ReconcileStatus.PROTECTED] + counts[ReconcileStatus.PRESENT]
    if protected:
        print(f"{protected} chat(s) are protected because they may still be available.")

    if confirmed:
        print("\nConfirmed leftovers\n")
        _show_assessments(confirmed)
        while True:
            answer = input(
                f"\nPress Enter or A to clean these {len(confirmed)}, "
                f"R to review all {len(assessments)} local ChatGPT entries, "
                "or C to cancel: "
            ).strip().lower()
            if not answer or answer in {"a", "all"}:
                return [item.selected_row() for item in confirmed]
            if answer == "c":
                return []
            if answer == "r":
                break
            print("Choose Enter/A, R, or C.")
    else:
        answer = input(
            "\nNo leftovers could be confirmed automatically. "
            "Press R to review the local Desktop list manually, or C to cancel: "
        ).strip().lower()
        if answer != "r":
            return []

    if not reviewable:
        print("No local entries are eligible for manual cleanup.")
        return []

    print("\nReview local ChatGPT Desktop entries\n")
    print(
        "Removing an unverified entry only changes this computer's Desktop list. "
        "It does not delete the server conversation, and an undo point is saved.\n"
    )
    _show_assessments(assessments)
    selectable_indexes = {
        index for index, item in enumerate(assessments) if item.selectable
    }
    while True:
        answer = input(
            "Type the entries to remove (example: 1-8,11,14-18), or C to cancel: "
        ).strip().lower()
        if answer == "c" or not answer:
            return []
        indexes = _parse_number_selection(answer, len(assessments))
        if indexes is None or any(index not in selectable_indexes for index in indexes):
            print("Choose only unprotected entry numbers/ranges, or C to cancel.")
            continue
        return [assessments[index].selected_row(manual=True) for index in indexes]


def _probe_presence_for_failures(
    profile,
    conversation_ids,
    platform,
    failures,
    presence_probe,
):
    inaccessible_ids = {
        thread_id
        for thread_id, evidence in failures.items()
        if evidence.error_code == "conversation_inaccessible"
    }
    return presence_probe(
        profile,
        conversation_ids,
        platform,
        skip_ids=inaccessible_ids,
    )


def _current_workspace_ids(profile, rows):
    account_id = (
        profile.identity.workspace_account_id
        if profile.identity is not None
        else None
    )
    if not account_id:
        return set()
    matched = set()
    for row in rows:
        host_id = row.get("host_id")
        if not isinstance(host_id, str):
            continue
        parts = host_id.split(":", 2)
        if len(parts) == 3 and parts[0] == "chatgpt" and parts[1] == account_id:
            matched.add(row["thread_id"])
    return matched


def _cleanup_candidates(
    profile,
    platform,
    *,
    evidence_reader=desktop_failure_evidence,
    presence_probe=probe_conversation_presence,
    announce=True,
):
    rows = chatgpt_catalog(profile)
    if not rows:
        if announce:
            print("No ChatGPT chats are stored in the local Desktop list.")
        return []
    if announce:
        print(f"Checking {len(rows)} chats saved by ChatGPT Desktop...")
    ids = {row["thread_id"] for row in rows}
    failures = evidence_reader(profile, ids, platform)
    presence = _probe_presence_for_failures(
        profile,
        ids,
        platform,
        failures,
        presence_probe,
    )
    candidates = _candidates_from_sources(rows, failures, presence)
    if announce and failures:
        safe = sum(
            evidence.error_code in SAFE_CLEANUP_ERROR_CODES
            for evidence in failures.values()
        )
        inaccessible = sum(
            evidence.error_code == "conversation_inaccessible"
            for evidence in failures.values()
        )
        print(
            f"{safe} chat(s) were previously confirmed deleted or missing."
        )
        if inaccessible:
            print(
                f"{inaccessible} chat(s) were left alone because ChatGPT reported "
                "an access problem rather than a confirmed deletion."
            )
    return candidates


def _cleanup_assessments_batch(
    profiles,
    platform,
    *,
    evidence_many_reader=desktop_failure_evidence_many,
    presence_probe=probe_conversation_presence,
    announce=True,
):
    rows_by_profile = {
        profile.id: chatgpt_catalog(profile) for profile in profiles
    }
    ids_by_profile = {
        profile.id: {row["thread_id"] for row in rows}
        for profile, rows in (
            (profile, rows_by_profile[profile.id]) for profile in profiles
        )
    }
    if announce:
        total = sum(len(rows) for rows in rows_by_profile.values())
        print(f"\nChecking {total} chats saved by ChatGPT Desktop...")
    failures_by_profile = evidence_many_reader(
        list(profiles),
        ids_by_profile,
        platform,
    )
    presence_by_profile = {}
    for profile in profiles:
        rows = rows_by_profile[profile.id]
        live_ids = _current_workspace_ids(profile, rows)
        if announce and live_ids:
            print(f"Checking {profile.display_name} against ChatGPT...")
        presence_by_profile[profile.id] = (
            _probe_presence_for_failures(
                profile,
                live_ids,
                platform,
                failures_by_profile.get(profile.id, {}),
                presence_probe,
            )
            if live_ids
            else None
        )
        if announce and len(live_ids) < len(rows):
            print(
                f"{profile.display_name}: {len(rows) - len(live_ids)} chat(s) were "
                "not eligible for automatic live verification, but they remain available "
                "for manual local review."
            )
    if announce:
        for profile in profiles:
            failures = failures_by_profile.get(profile.id, {})
            presence = presence_by_profile.get(profile.id)
            if presence is None:
                print(
                    f"{profile.display_name}: ChatGPT could not be checked right now. "
                    "Confirmed leftovers will be preselected; every local Desktop entry "
                    "can still be reviewed manually."
                )
                inaccessible = sum(
                    evidence.error_code == "conversation_inaccessible"
                    for evidence in failures.values()
                )
                safe_ids = {
                    thread_id
                    for thread_id, evidence in failures.items()
                    if evidence.error_code in SAFE_CLEANUP_ERROR_CODES
                }
                list_missing_only = sum(
                    row.get("missing_candidate") == 1
                    and row["thread_id"] not in safe_ids
                    for row in rows_by_profile[profile.id]
                )
                if inaccessible:
                    print(
                        f"{profile.display_name}: {inaccessible} chat(s) are protected "
                        "because ChatGPT reported an access problem, not a deletion."
                    )
                if list_missing_only:
                    print(
                        f"{profile.display_name}: {list_missing_only} chat(s) are marked "
                        "as possible leftovers by Desktop and will appear in manual review."
                    )
            else:
                counts = {
                    state: sum(value == state for value in presence.values())
                    for state in Presence
                }
                print(
                    f"{profile.display_name}: {counts[Presence.PRESENT]} available, "
                    f"{counts[Presence.MISSING]} no longer available, "
                    f"{counts[Presence.UNVERIFIED]} could not be checked."
                )
    return {
        profile.id: _assessments_from_sources(
            rows_by_profile[profile.id],
            failures_by_profile.get(profile.id, {}),
            presence_by_profile.get(profile.id),
        )
        for profile in profiles
    }


def _cleanup_candidates_batch(
    profiles,
    platform,
    *,
    evidence_many_reader=desktop_failure_evidence_many,
    presence_probe=probe_conversation_presence,
    announce=True,
):
    assessments = _cleanup_assessments_batch(
        profiles,
        platform,
        evidence_many_reader=evidence_many_reader,
        presence_probe=presence_probe,
        announce=announce,
    )
    return {
        profile.id: [
            item.selected_row()
            for item in assessments.get(profile.id, [])
            if item.default_selected
        ]
        for profile in profiles
    }


def _collect_work(
    action,
    profiles,
    platform,
    *,
    evidence_many_reader=desktop_failure_evidence_many,
    presence_probe=probe_conversation_presence,
):
    work = []
    catalog_assessments = (
        _cleanup_assessments_batch(
            profiles,
            platform,
            evidence_many_reader=evidence_many_reader,
            presence_probe=presence_probe,
            announce=True,
        )
        if action == "catalog"
        else None
    )
    for profile in profiles:
        print(f"\n— {profile.display_name} —\n")
        if action == "delete":
            rows = [row for row in sessions(profile) if row["archived"] == 1]
            selected = choose(rows, kind="archived", default_all=True)
        else:
            assessments = catalog_assessments.get(profile.id, [])
            selected = choose_cleanup_assessments(assessments)
        work.append((profile, selected))
    return work


def _wait_for_desktop_to_close(platform):
    first = True
    while True:
        try:
            platform.require_desktop_stopped()
            return True
        except PlatformError:
            if first:
                print(
                    "\nQuit ChatGPT before the cleanup is applied. "
                    "Your selection is ready."
                )
                first = False
            answer = input(
                "When ChatGPT is fully closed, press Enter to continue, "
                "or C to cancel: "
            ).strip().lower()
            if answer == "c":
                return False


def apply_batch(
    action,
    profiles,
    platform,
    *,
    evidence_many_reader=desktop_failure_evidence_many,
    presence_probe=probe_conversation_presence,
):
    work = _collect_work(
        action,
        profiles,
        platform,
        evidence_many_reader=evidence_many_reader,
        presence_probe=presence_probe,
    )
    selected_work = [(profile, rows) for profile, rows in work if rows]
    if not selected_work:
        print("Nothing selected. Nothing was changed.")
        return
    if not _wait_for_desktop_to_close(platform):
        print("Cancelled. Nothing was changed.")
        return

    delete_plans = None
    if action == "delete":
        delete_plans = {}
        for profile, rows in selected_work:
            delete_plans[profile.id] = preview_codex_delete(profile, rows, platform)

    if not _batch_summary(action, selected_work, delete_plans=delete_plans):
        print("Cancelled. Nothing was changed.")
        return
    directory = recovery_root(platform)
    completed = []
    with operation_lock(directory):
        for profile, rows in selected_work:
            try:
                if action == "delete":
                    plan = delete_plans[profile.id]
                    execute_codex_delete_plan(profile, plan, platform)
                    detail = f"{len(plan.ordered_ids)} archived Codex chat(s) permanently deleted"
                else:
                    backup = evict_catalog(
                        profile,
                        rows,
                        recovery=directory,
                        platform=platform,
                    )
                    detail = f"{len(rows)} stale Desktop chat entr{'y' if len(rows) == 1 else 'ies'} removed"
                completed.append((profile.display_name, detail))
            except Exception:
                if completed:
                    print("\nFinished before the problem occurred:")
                    for name, detail in completed:
                        print(f"  ✓ {name}: {detail}")
                raise
    print("\nCleanup finished")
    for name, detail in completed:
        print(f"  ✓ {name}: {detail}")
    if action == "catalog":
        print("  An undo point was saved in case you need to restore these local entries.")


def doctor_data(profile, platform):
    rows = sessions(profile)
    data = {
        "profile_id": profile.id,
        "provider": profile.provider,
        "native_conversations": len(rows),
        "archived_conversations": sum(row["archived"] == 1 for row in rows),
        "catalog_readable": False,
        "missing_chatgpt_entries": None,
        "local_orphans": None,
    }
    try:
        entries = catalog(profile)
        data["catalog_readable"] = True
        data["missing_chatgpt_entries"] = sum(
            row["classification"] == "missing-chatgpt-entry" for row in entries
        )
        data["local_orphans"] = sum(
            row["classification"] == "local-orphan" for row in entries
        )
    except (SafetyError, OSError, sqlite3.Error):
        pass
    writable, reason = platform.catalog_write_gate()
    data["catalog_write_enabled"] = writable
    data["catalog_write_gate"] = reason
    return data


def doctor(profile, platform):
    data = doctor_data(profile, platform)
    print(f"\n{profile.display_name}")
    print(f"  Codex chats on this computer: {data['native_conversations']}")
    print(f"  Archived Codex chats: {data['archived_conversations']}")
    if data["catalog_readable"]:
        print(f"  ChatGPT entries marked missing: {data['missing_chatgpt_entries']}")
        print(f"  Other local leftovers: {data['local_orphans']}")
    else:
        print("  ChatGPT Desktop list check: unavailable")
    print(f"  Local cleanup: {'available' if data['catalog_write_enabled'] else 'read-only'}")
    if not data["catalog_write_enabled"]:
        print(f"  Why: {data['catalog_write_gate']}")


def recovery_menu(profile, platform):
    directory = recovery_root(platform)
    inventory = maintain_recovery(directory)
    records = [
        record
        for record in inventory["records"]
        if record["data"].get("profile_root") == str(profile.root)
    ]
    if not records:
        print("No saved undo points for this profile.")
        return
    print("\nSaved undo points\n")
    for index, record in enumerate(records, 1):
        data = record["data"]
        created = _date(data.get("created_at")) or "unknown date"
        count = len(data.get("items") or [])
        state = "ready to restore" if record["state"] == "active" else "already restored"
        print(f"{index}. {created} · {count} item(s) · {state}")
    text = input("Choose an undo point, or press Enter to go back: ").strip()
    if not text:
        return
    if not text.isdigit() or not 1 <= int(text) <= len(records):
        raise SafetyError("That undo point is not valid.")
    file = records[int(text) - 1]["path"]
    action = input("1 Restore these entries / 2 Delete this undo point / Enter to go back: ").strip()
    if action == "1":
        if input(f"Type [{profile.display_name} RESTORE] to continue: ").strip() != (
            f"{profile.display_name} RESTORE"
        ):
            return
        with operation_lock(directory):
            marked = restore_catalog(profile, file, platform=platform)
        print("Local entries restored.")
        if not marked:
            print("The entries were restored, but the undo point could not be marked as used.")
    elif action == "2":
        if input("Type [DELETE RECORD] to continue: ").strip() == "DELETE RECORD":
            with operation_lock(directory):
                file.unlink()
            print("Undo point deleted.")


def retire_install_interactive(platform):
    print(
        "This removes older installed copies of this app. "
        "Your saved undo points are kept."
    )
    if input("Type [REMOVE OLD COPY] to continue: ").strip() != "REMOVE OLD COPY":
        print("Cancelled. Nothing was removed.")
        return False
    removed = retire_installed_copy(platform)
    print(
        f"Removed {removed} old app item(s)."
        if removed
        else "No old installed copy was present."
    )
    return bool(removed)


def purge_data_interactive(platform):
    print(
        "This deletes this app's saved undo points and other local app data. "
        "It does not delete ChatGPT or Codex conversations."
    )
    if input("Type [PURGE CLEANUP DATA] to continue: ").strip() != "PURGE CLEANUP DATA":
        print("Cancelled. Nothing was removed.")
        return False
    removed = purge_product_data(platform)
    print("This app's local data was removed." if removed else "This app had no saved local data to remove.")
    return removed


def interactive(platform, available):
    while True:
        print("\nChatGPT Conversation Cleanup\n")
        print("1 Review and clean up ChatGPT Desktop leftovers")
        print("2 Permanently delete archived Codex chats")
        print("M More options")
        action = input("\nChoose 1, 2, or M. Press Enter to exit: ").strip()
        if not action:
            return
        if action in ("1", "2"):
            try:
                if action == "1" or len(available) > 1:
                    available = enrich_identities(available, platform)
                profiles = select_profiles(available, multiple=True)
                if not profiles:
                    print("Cancelled. Nothing was changed.")
                elif action == "1":
                    apply_batch("catalog", profiles, platform)
                else:
                    apply_batch("delete", profiles, platform)
            except (SafetyError, PlatformError, AppServerError) as error:
                print(f"\nCouldn’t continue: {error}")
                print("The operation stopped before it could finish. Review any completed items shown above.")
            _pause_for_main_menu()
            continue
        if action.lower() != "m":
            print("Choose 1, 2, or M.")
            continue

        print("\nMore options\n")
        print("3 Check this computer")
        print("4 Undo a previous cleanup")
        print("5 Remove an older installed copy")
        print("6 Delete this app's saved local data")
        more = input("\nChoose 3, 4, 5, or 6. Press Enter to go back: ").strip()
        if not more:
            continue
        try:
            if more == "3":
                if len(available) > 1:
                    available = enrich_identities(available, platform)
                profiles = select_profiles(available, multiple=True)
                if not profiles:
                    print("Cancelled.")
                else:
                    for profile in profiles:
                        doctor(profile, platform)
            elif more == "4":
                if len(available) > 1:
                    available = enrich_identities(available, platform)
                profiles = select_profiles(available, multiple=False)
                if not profiles:
                    print("Cancelled.")
                else:
                    recovery_menu(profiles[0], platform)
            elif more == "5":
                retire_install_interactive(platform)
            elif more == "6":
                purge_data_interactive(platform)
                print("Delete the portable app/executable to finish removing the program.")
            else:
                print("Choose 3, 4, 5, or 6.")
                continue
        except (SafetyError, PlatformError, AppServerError) as error:
            print(f"\nCouldn’t continue: {error}")
        _pause_for_main_menu()


def _resolve_cli_profiles(available, profile_id, all_profiles):
    if all_profiles:
        return available
    if profile_id:
        matches = [profile for profile in available if profile.id == profile_id]
        if not matches:
            raise SafetyError(f"Unknown profile: {profile_id}")
        return matches
    return available if len(available) == 1 else [available[0]]


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        prog="chatgpt-cleanup",
        description="Safely inspect and clean stale ChatGPT Desktop conversation state.",
    )
    parser.add_argument("command", nargs="?", choices=("doctor", "list-archived", "retire-install", "purge-data"))
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--profile")
    parser.add_argument("--all-profiles", action="store_true")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit privacy-safe machine-readable diagnostics (doctor only).",
    )
    args = parser.parse_args()
    platform = current_platform()
    if args.command in ("retire-install", "purge-data"):
        if args.json or args.profile or args.all_profiles:
            raise SafetyError(f"{args.command} does not accept profile or JSON options.")
        if args.command == "retire-install":
            retire_install_interactive(platform)
        else:
            purge_data_interactive(platform)
        return
    available = discover_profiles(platform)
    if not available:
        raise SafetyError("No supported ChatGPT/Codex profile was found.")
    if args.command:
        targets = _resolve_cli_profiles(available, args.profile, args.all_profiles)
        if args.command == "doctor":
            if args.json:
                print(
                    json.dumps(
                        {
                            "platform": platform.key,
                            "profiles": [
                                doctor_data(profile, platform) for profile in targets
                            ],
                        },
                        indent=2,
                    )
                )
            else:
                for profile in targets:
                    doctor(profile, platform)
        else:
            if args.json:
                raise SafetyError("--json is currently supported only with doctor.")
            for profile in targets:
                print(f"\n{profile.display_name}")
                show(
                    [row for row in sessions(profile) if row["archived"] == 1],
                    kind="archived",
                )
        return
    if args.json:
        raise SafetyError("--json requires the doctor command.")
    if not sys.stdin.isatty():
        raise SafetyError("Interactive cleanup requires a terminal.")
    interactive(platform, available)


def entrypoint():
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user. If a cleanup was already being applied, check its result before trying again.", file=sys.stderr)
        return 130
    except (
        SafetyError,
        AppServerError,
        PlatformError,
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as error:
        print(f"Couldn’t continue: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(entrypoint())
