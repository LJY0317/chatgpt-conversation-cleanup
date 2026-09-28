from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .appserver import AppServerError, AppServerSession
from .core import SafetyError, require, safe_path, sessions
from .platforms import PlatformAdapter, PlatformError
from .profiles import Profile


@dataclass(frozen=True)
class CodexDeletePlan:
    requested_ids: tuple[str, ...]
    ordered_ids: tuple[str, ...]
    rows: dict[str, dict]
    parents: dict[str, frozenset[str]]

    @property
    def dependent_ids(self) -> tuple[str, ...]:
        requested = set(self.requested_ids)
        return tuple(thread_id for thread_id in self.ordered_ids if thread_id not in requested)


def _rollout_parent_ids(profile: Profile, row: dict) -> set[str]:
    raw_path = row.get("rollout_path")
    if not isinstance(raw_path, str) or not raw_path:
        return set()
    try:
        path = safe_path(Path(raw_path), profile.root)
    except (SafetyError, OSError) as error:
        raise SafetyError(
            "A Codex rollout path could not be verified. Dependency planning stopped."
        ) from error
    require(path.is_file(), "A Codex rollout is missing. Dependency planning stopped.")
    try:
        with path.open("r", encoding="utf-8") as stream:
            first = stream.readline(256 * 1024)
        record = json.loads(first)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise SafetyError(
            "A Codex rollout header could not be read. Dependency planning stopped."
        ) from error
    require(
        isinstance(record, dict) and record.get("type") == "session_meta",
        "A Codex rollout has no readable session metadata. Dependency planning stopped.",
    )
    payload = record.get("payload")
    require(
        isinstance(payload, dict),
        "A Codex rollout has invalid session metadata. Dependency planning stopped.",
    )
    parents: set[str] = set()
    forked = payload.get("forked_from_id")
    if isinstance(forked, str) and forked:
        parents.add(forked)
    history_base = payload.get("history_base")
    if isinstance(history_base, dict):
        parent = history_base.get("thread_id")
        if isinstance(parent, str) and parent:
            parents.add(parent)
    return parents


def _list_appserver_threads(session: AppServerSession) -> list[dict]:
    rows: list[dict] = []
    for archived in (False, True):
        cursor = None
        seen_cursors: set[str] = set()
        pages = 0
        while True:
            require(pages < 1000, "Official Codex thread listing exceeded the safety page limit.")
            page = session.list_threads(
                archived=archived,
                cursor=cursor,
                include_derived=True,
                use_state_db_only=False,
            )
            data = page.get("data")
            require(isinstance(data, list), "Official Codex thread list returned an unexpected response.")
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    row = dict(item)
                    row["_listed_archived"] = archived
                    rows.append(row)
            next_cursor = page.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            require(next_cursor not in seen_cursors, "Official Codex thread pagination repeated a cursor.")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            pages += 1
    return rows


def _dependency_map(
    profile: Profile,
    db_rows: dict[str, dict],
    app_rows: list[dict],
) -> tuple[dict[str, frozenset[str]], dict[str, bool]]:
    parents: dict[str, set[str]] = {
        thread_id: _rollout_parent_ids(profile, row)
        for thread_id, row in db_rows.items()
    }
    app_archived: dict[str, bool] = {}
    for item in app_rows:
        thread_id = item.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            continue
        listed_archived = item.get("_listed_archived")
        if isinstance(listed_archived, bool):
            previous = app_archived.get(thread_id)
            require(
                previous is None or previous == listed_archived,
                "Official Codex inventory listed one thread as both active and archived.",
            )
            app_archived[thread_id] = listed_archived
        target = parents.setdefault(thread_id, set())
        for key in ("parentThreadId", "forkedFromId"):
            parent = item.get(key)
            if isinstance(parent, str) and parent:
                target.add(parent)
    return (
        {thread_id: frozenset(value) for thread_id, value in parents.items()},
        app_archived,
    )


def _closure_and_order(
    selected_ids: set[str],
    db_rows: dict[str, dict],
    parents: dict[str, frozenset[str]],
    app_archived: dict[str, bool],
) -> tuple[set[str], list[str]]:
    closure = set(selected_ids)
    changed = True
    while changed:
        changed = False
        for child, parent_ids in parents.items():
            if child in closure or not (parent_ids & closure):
                continue
            row = db_rows.get(child)
            if row is None:
                raise SafetyError(
                    "The official Codex inventory reports a dependent child/fork "
                    "that is missing from the local database snapshot. Nothing was deleted."
                )
            if row.get("archived") != 1 or app_archived.get(child) is False:
                raise SafetyError(
                    "An active Codex child/fork still depends on a selected archived chat. "
                    "Archive or detach that child before deleting its parent."
                )
            closure.add(child)
            changed = True

    children: dict[str, set[str]] = {thread_id: set() for thread_id in closure}
    for child in closure:
        for parent in parents.get(child, frozenset()):
            if parent in closure:
                children.setdefault(parent, set()).add(child)

    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(thread_id: str):
        if thread_id in visited:
            return
        if thread_id in visiting:
            raise SafetyError("A Codex thread dependency cycle was detected. Nothing was deleted.")
        visiting.add(thread_id)
        for child in sorted(children.get(thread_id, set())):
            visit(child)
        visiting.remove(thread_id)
        visited.add(thread_id)
        ordered.append(thread_id)

    for thread_id in sorted(closure):
        visit(thread_id)
    return closure, ordered


def preview_codex_delete(
    profile: Profile,
    selected: list[dict],
    platform: PlatformAdapter,
    *,
    session_factory=AppServerSession,
) -> CodexDeletePlan:
    require(selected, "No archived Codex chats were selected.")
    requested_ids = [row.get("id") for row in selected]
    require(all(isinstance(item, str) and item for item in requested_ids), "A selected Codex chat has no valid ID.")
    require(len(set(requested_ids)) == len(requested_ids), "Duplicate Codex delete targets are not allowed.")
    require(all(row.get("archived") == 1 for row in selected), "Only archived Codex chats can be permanently deleted here.")

    current_rows = {row["id"]: row for row in sessions(profile)}
    for selected_row in selected:
        current = current_rows.get(selected_row["id"])
        require(current == selected_row, "A selected Codex chat changed after preview. Review it again.")

    try:
        with session_factory(profile, platform, timeout=15) as appserver:
            app_rows = _list_appserver_threads(appserver)
    except (PlatformError, AppServerError) as error:
        raise SafetyError(str(error)) from error

    parents, app_archived = _dependency_map(profile, current_rows, app_rows)
    closure, ordered = _closure_and_order(
        set(requested_ids),
        current_rows,
        parents,
        app_archived,
    )
    rows = {thread_id: current_rows[thread_id] for thread_id in closure}
    return CodexDeletePlan(
        requested_ids=tuple(requested_ids),
        ordered_ids=tuple(ordered),
        rows=rows,
        parents={thread_id: parents.get(thread_id, frozenset()) for thread_id in closure},
    )


def delete_archived_codex(
    profile: Profile,
    selected: list[dict],
    platform: PlatformAdapter,
    *,
    session_factory=AppServerSession,
) -> CodexDeletePlan:
    try:
        platform.require_desktop_stopped()
    except PlatformError as error:
        raise SafetyError(str(error)) from error

    plan = preview_codex_delete(
        profile,
        selected,
        platform,
        session_factory=session_factory,
    )
    execute_codex_delete_plan(
        profile,
        plan,
        platform,
        session_factory=session_factory,
    )
    return plan


def execute_codex_delete_plan(
    profile: Profile,
    plan: CodexDeletePlan,
    platform: PlatformAdapter,
    *,
    session_factory=AppServerSession,
) -> None:
    try:
        platform.require_desktop_stopped()
    except PlatformError as error:
        raise SafetyError(str(error)) from error

    completed: list[str] = []
    try:
        with session_factory(profile, platform, timeout=15) as appserver:
            # Rebuild the dependency view after the user's final confirmation
            # and before the first irreversible call. A new hidden fork or an
            # archive-state change must invalidate the entire preview rather
            # than being discovered halfway through deletion.
            current_rows = {row["id"]: row for row in sessions(profile)}
            for thread_id, preview_row in plan.rows.items():
                require(
                    current_rows.get(thread_id) == preview_row,
                    "A Codex chat changed after the delete preview. Nothing was deleted.",
                )
            app_rows = _list_appserver_threads(appserver)
            current_parents, current_app_archived = _dependency_map(
                profile,
                current_rows,
                app_rows,
            )
            current_closure, current_order = _closure_and_order(
                set(plan.requested_ids),
                current_rows,
                current_parents,
                current_app_archived,
            )
            require(
                current_closure == set(plan.rows)
                and tuple(current_order) == plan.ordered_ids
                and {
                    thread_id: current_parents.get(thread_id, frozenset())
                    for thread_id in current_closure
                }
                == plan.parents,
                "Codex dependency state changed after preview. Nothing was deleted.",
            )
            for thread_id in plan.ordered_ids:
                current = {row["id"]: row for row in sessions(profile)}.get(thread_id)
                require(
                    current == plan.rows[thread_id],
                    "A Codex chat changed while deletion was in progress. Remaining work was stopped.",
                )
                appserver.delete_thread(thread_id)
                remaining = any(row["id"] == thread_id for row in sessions(profile))
                require(
                    not remaining,
                    "Codex reported deletion success but the chat still exists locally. Remaining work was stopped.",
                )
                completed.append(thread_id)
    except (PlatformError, AppServerError, SafetyError) as error:
        raise SafetyError(
            f"Codex deletion stopped after {len(completed)} of {len(plan.ordered_ids)} "
            "planned chat(s) were verified deleted. Review current state before retrying. "
            f"{error}"
        ) from error
