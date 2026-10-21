"""Source-only CLI: let the official ChatGPT/Codex runtimes do the cleanup."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import sqlite3
import sys
import time

from . import __version__
from .appserver import AppServerError
from .codex_delete import (
    execute_codex_delete_plan,
    preview_codex_delete,
    verified_rollout_path,
)
from .core import (
    SafetyError,
    catalog_rows,
    chatgpt_catalog,
    database,
    recovery_root,
    require,
    sessions,
    write_schema,
)
from .desktop_refresh import (
    _host_token,
    _workspace_host,
    _workspace_hosts,
    desktop_catalog_refresh_progress,
    desktop_catalog_refresh_result,
    prepare_desktop_catalog_refresh,
    require_desktop_catalog_refresh,
)
from .identity import IdentityError, resolve_identity
from .lifecycle import (
    capture_initial_desktops,
    restore_initial_desktops,
    wait_for_desktop_to_close,
)
from .locking import operation_lock
from .platforms import PlatformError, current_platform
from .profiles import Profile, discover_profiles


def _targets(profiles: list[Profile], chosen: str | None, all_profiles: bool):
    require(not (chosen and all_profiles), "Choose --profile or --all-profiles, not both.")
    if chosen:
        matches = [profile for profile in profiles if profile.id == chosen]
        require(len(matches) == 1, "No verified profile has that ID.")
        return matches
    return profiles if all_profiles else profiles[:1]


def doctor_data(profile: Profile):
    entries = sessions(profile)
    archives = [row for row in entries if row["archived"] == 1]
    missing = 0
    for row in archives:
        try:
            verified_rollout_path(profile, row)
        except SafetyError:
            missing += 1
    try:
        catalog_count = len(chatgpt_catalog(profile))
        catalog_readable = True
    except (SafetyError, sqlite3.Error, OSError):
        catalog_count = 0
        catalog_readable = False
    return {
        "profile_id": profile.id,
        "native_conversations": len(entries),
        "archived_conversations": len(archives),
        "archived_rollout_files_missing": missing,
        "desktop_catalog_readable": catalog_readable,
        "desktop_catalog_chats": catalog_count,
    }


def list_archived(profile: Profile):
    return [
        {
            "id": row["id"],
            "title": (row.get("name") or row.get("title") or "")[:140],
        }
        for row in sessions(profile) if row["archived"] == 1
    ]


def delete_archived_cli(
    profile: Profile, platform, *, ids: str, confirm: str | None = None,
):
    """All irreversible deletion is performed by official Codex thread/delete."""
    archived = {row["id"]: row for row in sessions(profile) if row["archived"] == 1}
    chosen = [item.strip() for item in ids.split(",")]
    require(
        all(item and item in archived for item in chosen)
        and len(set(chosen)) == len(chosen),
        "Every --ids value must uniquely identify an archived thread.",
    )
    plan = preview_codex_delete(profile, [archived[item] for item in chosen], platform)
    count = len(plan.affected_ids)
    print(
        f"{profile.id}: {len(chosen)} selected; {count} eligible logical thread(s); "
        f"{len(plan.root_ids)} official deletion request(s); {len(plan.protected)} protected."
    )
    for thread_id, reason in plan.protected.items():
        print(f"  protected {thread_id}: {reason}")
    if not count:
        print("Official deletion is unavailable for this selection; nothing changed.")
        return
    phrase = f"DELETE {count} CHATS"
    if confirm is None:
        print(f"Preview only. To proceed, rerun with --confirm '{phrase}'.")
        return
    require(confirm == phrase, "Confirmation does not match the current preview.")
    with operation_lock(recovery_root(platform)):
        result = execute_codex_delete_plan(profile, plan, platform)
    print(
        f"Official Codex verified {len(result.deleted_ids)} removed via "
        f"{len(result.native_calls)} request(s); {len(result.protected)} protected."
    )
    for thread_id, reason in result.protected.items():
        print(f"  protected {thread_id}: {reason}")


def _refresh_preview(profile: Profile, platform, *, host_token: str | None = None):
    """Find the exact Desktop catalog host without guessing across workspaces."""
    require_desktop_catalog_refresh(profile, platform)
    identity = resolve_identity(profile, platform)
    require(
        identity is not None and bool(identity.workspace_account_id),
        "Official Codex could not identify the signed-in ChatGPT workspace.",
    )
    scoped = replace(profile, identity=identity)
    with database(profile.root / "sqlite/codex-dev.db", profile.root) as db:
        write_schema(db)
        hosts = _workspace_hosts(db, identity.workspace_account_id)
        require(hosts, "No local ChatGPT catalog matches the signed-in account.")
        candidates = []
        for host_id in hosts:
            row = db.execute(
                "SELECT count(*) FROM local_thread_catalog "
                "WHERE host_id=? AND source_kind='chatgpt'",
                (host_id,),
            ).fetchone()
            candidates.append((_host_token(host_id), int(row[0])))
        if host_token is None and len(hosts) > 1:
            print(
                f"{profile.id}: {len(hosts)} different ChatGPT catalog hosts "
                "match the signed-in account."
            )
            for token, count in candidates:
                print(f"  --host-token {token}: {count} locally indexed chats")
            print(
                "Read-only only: select the intended host explicitly with "
                "--host-token. Do not guess which list the user means."
            )
            return None
        host_id = _workspace_host(db, identity.workspace_account_id, host_token=host_token)
    catalog = [row for row in chatgpt_catalog(scoped) if row["host_id"] == host_id]
    return scoped, host_id, {row["thread_id"] for row in catalog}


def refresh_cli(
    profile: Profile, platform, *, confirm: str | None, host_token: str | None = None,
    wait_seconds: float = 180, sleep=time.sleep, clock=time.monotonic,
):
    """Schedule official Desktop full reconciliation. Never evict catalog rows."""
    preview = _refresh_preview(profile, platform, host_token=host_token)
    if preview is None:
        return
    scoped, host_id, before_ids = preview
    resolved_token = _host_token(host_id)
    phrase = f"REFRESH {profile.id} {resolved_token}"
    print(
        f"{profile.id}: {len(before_ids)} local ChatGPT catalog entries; "
        f"host token {resolved_token}. The official Desktop will decide "
        "which entries still exist on the server; this command does NOT "
        "delete any live server-side chats."
    )
    if confirm is None:
        print(
            f"Preview only. To schedule full Desktop reconciliation, rerun with "
            f"--confirm '{phrase}'. A normal app quit/reopen may be needed."
        )
        return
    require(confirm == phrase, "Confirmation does not match the refresh target.")
    initial = capture_initial_desktops(platform, [profile])
    try:
        with operation_lock(recovery_root(platform)):
            if not wait_for_desktop_to_close(
                platform,
                purpose="refresh",
                profile=profile,
                approved_normal_quit=True,
            ):
                print("Refresh cancelled. The local catalog was not changed.")
                return
            request = prepare_desktop_catalog_refresh(
                scoped, scoped.identity.workspace_account_id, platform,
                host_token=resolved_token,
            )
        launched, reason = platform.launch_profile_desktop(profile)
        if not launched:
            print(
                f"Official Desktop full reconciliation was queued. "
                f"Open this profile using its normal app controls ({reason})."
            )
            return
        print("Official ChatGPT Desktop reopened; waiting for its full reconciliation.")
        deadline = clock() + wait_seconds
        previous_stage = None
        while clock() < deadline:
            result = desktop_catalog_refresh_result(scoped, request, platform)
            if result is not None:
                after_ids = {
                    row["thread_id"] for row in chatgpt_catalog(scoped)
                    if row["host_id"] == request.host_id
                }
                print(
                    f"Official Desktop refresh finished: {len(before_ids)} → "
                    f"{len(after_ids)} local entries; {len(before_ids-after_ids)} "
                    f"removed by Desktop; {len(result.missing_ids & after_ids)} "
                    "entries still marked missing."
                )
                return
            progress = desktop_catalog_refresh_progress(scoped, request)
            if progress.stage != previous_stage:
                print(f"Desktop reconciliation: {progress.stage}.")
                previous_stage = progress.stage
            sleep(2)
        print(
            "Refresh was queued but was not confirmed complete within the wait "
            "window. ChatGPT may continue it; no manual catalog deletion occurred."
        )
    finally:
        restore_initial_desktops(platform, [profile], initial)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="chatgpt-cleanup",
        description=(
            "Two distinct repairs: repair-chat-list schedules official Desktop "
            "refresh for already-deleted normal Chat entries; "
            "delete-archived-codex invokes native deletion of Work/Codex archives. "
            "Neither is a generic server-side Chat deletion command."
        ),
        epilog=(
            "Natural language: 'normal Chat deleted on server but still visible' "
            "=> repair-chat-list. 'Can't delete a chat' without archive context "
            "=> inspect normal Chat stale-list issue read-only first. "
            "'Archived Work/Codex chat won't delete' => delete-archived-codex. "
            "Generic 'delete my chat' is ambiguous: ask before any action. "
            "Never substitute refresh for server-side deletion."
        ),
    )
    parser.add_argument(
        "command", nargs="?",
        choices=("doctor", "list-archived", "repair-chat-list", "delete-archived-codex"),
    )
    parser.add_argument("--profile", help="Exact ID from doctor, e.g. default or profile2.")
    parser.add_argument("--all-profiles", action="store_true", help="Only for read-only commands.")
    parser.add_argument("--json", action="store_true", help="Machine-readable read-only inventory.")
    parser.add_argument("--ids", help="Comma-separated exact archived thread IDs to delete.")
    parser.add_argument(
        "--host-token", help="Exact opaque ChatGPT catalog host token from repair preview."
    )
    parser.add_argument("--confirm", help="Exact phrase printed by the preceding preview.")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return
    if args.command not in ("doctor", "list-archived"):
        require(
            args.profile and not args.all_profiles and not args.json,
            "Write-capable commands require one explicit --profile and no --json.",
        )
    if args.command != "delete-archived-codex":
        require(args.ids is None, "--ids is valid only for delete-archived-codex.")
    if args.command != "repair-chat-list":
        require(args.host_token is None, "--host-token is valid only for repair-chat-list.")
    if args.command not in ("delete-archived-codex", "repair-chat-list"):
        require(args.confirm is None, "--confirm is valid only for deletion or list repair.")
    if args.command == "delete-archived-codex":
        require(args.ids, "Specify exact archived IDs with --ids.")
    platform = current_platform()
    available = discover_profiles(platform)
    require(available, "No verified ChatGPT/Codex profile was found.")
    targets = _targets(available, args.profile, args.all_profiles)
    if args.command == "doctor":
        output = {"profiles": [doctor_data(profile) for profile in targets]}
    elif args.command == "list-archived":
        output = {"profiles": [
            {"profile_id": profile.id, "archived": list_archived(profile)}
            for profile in targets
        ]}
    elif args.command == "repair-chat-list":
        refresh_cli(
            targets[0], platform, confirm=args.confirm,
            host_token=args.host_token,
        )
        return
    else:
        delete_archived_cli(
            targets[0], platform, ids=args.ids, confirm=args.confirm,
        )
        return
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for entry in output["profiles"]:
            if args.command == "doctor":
                print(
                    f"{entry['profile_id']}: {entry['native_conversations']} Codex "
                    f"conversations; {entry['archived_conversations']} archived; "
                    f"{entry['archived_rollout_files_missing']} missing rollouts; "
                    f"{entry['desktop_catalog_chats']} ChatGPT catalog entries."
                )
            else:
                print(f"{entry['profile_id']}: {len(entry['archived'])} archived Codex chats")
                for row in entry["archived"]:
                    print(f"  {row['id']} {row['title']}")


def entrypoint():
    try:
        main()
    except (SafetyError, AppServerError, IdentityError, PlatformError, sqlite3.Error, OSError, EOFError) as error:
        print(f"Stopped safely: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    entrypoint()
