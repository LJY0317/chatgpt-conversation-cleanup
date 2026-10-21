"""A thin, owner-verified client for Codex's own permanent thread deletion.

Only the bundled app-server deletes conversations, spawned agents, rollout
files, and indexes. This module selects roots, protects active spawned workers,
isolates definitive fork-reference refusals, and verifies the native result.
It never implements Codex's rollout dependency or deletion algorithms.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3

from .appserver import AppServerError, AppServerSession
from .core import SafetyError, columns, database, require, safe_path, sessions
from .platforms import PlatformAdapter, PlatformError, require_profile_stopped
from .profiles import Profile


_ROLLOUT_NAME = re.compile(
    r"^rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-"
    r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})"
    r"(?:_([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}))?"
    r"\.jsonl(?:\.zst)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CodexDeletePlan:
    requested_ids: tuple[str, ...]
    root_ids: tuple[str, ...]
    descendants_by_root: dict[str, frozenset[str]]
    protected: dict[str, str]
    rows: dict[str, dict]
    spawn_edges: tuple[tuple[str, str, str], ...]
    official_states: dict[str, bool]

    @property
    def affected_ids(self) -> frozenset[str]:
        result = set(self.root_ids)
        for descendants in self.descendants_by_root.values():
            result.update(descendants)
        return frozenset(result)

    @property
    def dependent_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.affected_ids - set(self.requested_ids)))


@dataclass(frozen=True)
class CodexDeleteResult:
    deleted_ids: tuple[str, ...]
    native_calls: tuple[str, ...]
    protected: dict[str, str]


def verified_rollout_path(profile: Profile, row: dict) -> Path:
    """Read-only diagnostic for a stored path or its exact .jsonl.zst sibling."""
    raw_path = row.get("rollout_path")
    require(isinstance(raw_path, str) and bool(raw_path), "A Codex rollout path is missing.")
    try:
        path = safe_path(Path(raw_path), profile.root, must_exist=False)
        if path.is_file():
            return path
        if path.suffix == ".jsonl":
            compressed = safe_path(Path(str(path) + ".zst"), profile.root, must_exist=False)
            if compressed.is_file():
                return compressed
    except (SafetyError, OSError) as error:
        raise SafetyError(
            "A Codex rollout path could not be verified. No changes were made."
        ) from error
    raise SafetyError("A Codex rollout path could not be verified. No changes were made.")


def _local_snapshot(profile: Profile) -> tuple[dict[str, dict], tuple[tuple[str, str, str], ...]]:
    rows = {row["id"]: row for row in sessions(profile)}
    with database(profile.root / "state_5.sqlite", profile.root) as db:
        require(
            {"parent_thread_id", "child_thread_id", "status"}
            <= columns(db, "thread_spawn_edges"),
            "Unsupported Codex spawned-thread index. No changes were made.",
        )
        edges = tuple(
            sorted(
                (str(row["parent_thread_id"]), str(row["child_thread_id"]), str(row["status"]))
                for row in db.execute(
                    "SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges"
                )
            )
        )
    return rows, edges


def _official_inventory(
    server: AppServerSession,
) -> tuple[dict[str, bool], tuple[tuple[str, str, str], ...]]:
    """Read an independent native inventory without scan-and-repair writes.

    Some upstream builds omit empty-preview archived children. Local state is
    therefore also needed for user-visible selection, but a positive native
    'active' result remains a veto.
    """
    result: dict[str, bool] = {}
    native_spawn_edges: set[tuple[str, str, str]] = set()
    for archived in (False, True):
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(1000):
            response = server.list_threads(
                archived=archived,
                cursor=cursor,
                include_derived=True,
                use_state_db_only=True,
            )
            data = response.get("data")
            require(isinstance(data, list), "Official Codex returned an invalid thread list.")
            for entry in data:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                    continue
                thread_id = entry["id"]
                require(
                    thread_id not in result or result[thread_id] == archived,
                    "Official Codex reported conflicting archived states. No changes were made.",
                )
                result[thread_id] = archived
                native_parent = entry.get("parentThreadId")
                if isinstance(native_parent, str) and native_parent:
                    native_spawn_edges.add((native_parent, thread_id, "native"))
            cursor = response.get("nextCursor")
            if not cursor:
                break
            require(
                isinstance(cursor, str) and cursor not in seen,
                "Official Codex repeated a listing cursor. No changes were made.",
            )
            seen.add(cursor)
        else:
            raise SafetyError("Official Codex listing exceeded the page limit.")
    return result, tuple(sorted(native_spawn_edges))


def _spawn_children(
    edges: tuple[tuple[str, str, str], ...],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    children: dict[str, set[str]] = defaultdict(set)
    parents: dict[str, set[str]] = defaultdict(set)
    for parent, child, _status in edges:
        children[parent].add(child)
        parents[child].add(parent)
    return children, parents


def _descendants(start: str, children: dict[str, set[str]]) -> set[str]:
    found: set[str] = set()
    waiting = list(children.get(start, ()))
    while waiting:
        thread_id = waiting.pop()
        require(
            thread_id != start,
            "Codex reported a spawned-thread cycle. No changes were made.",
        )
        if thread_id in found:
            continue
        found.add(thread_id)
        waiting.extend(children.get(thread_id, ()))
    return found


def _build_plan(
    requested: tuple[str, ...],
    rows: dict[str, dict],
    edges: tuple[tuple[str, str, str], ...],
    states: dict[str, bool],
) -> CodexDeletePlan:
    children, parents = _spawn_children(edges)
    protected: dict[str, str] = {}
    for thread_id in requested:
        row = rows.get(thread_id)
        require(
            row is not None and row.get("archived") == 1,
            "A selected archived thread changed. No changes were made.",
        )
        if states.get(thread_id) is False:
            protected[thread_id] = "Native Codex lists this thread as active"
            continue
        spawned = _descendants(thread_id, children)
        if any(
            child not in rows
            or rows[child].get("archived") != 1
            or states.get(child) is False
            for child in spawned
        ):
            protected[thread_id] = "An active or unknown spawned descendant must be preserved"

    eligible = set(requested) - set(protected)
    # Calling native delete on an eligible root already deletes its spawned
    # descendants. Do not issue duplicate deletes for explicitly selected
    # children. A child of a protected root can still be an independent root.
    roots: list[str] = []
    for thread_id in requested:
        if thread_id not in eligible:
            continue
        pending = list(parents.get(thread_id, ()))
        ancestors: set[str] = set()
        while pending:
            parent = pending.pop()
            if parent in ancestors:
                continue
            ancestors.add(parent)
            pending.extend(parents.get(parent, ()))
        require(thread_id not in ancestors, "Codex spawned-thread cycle detected.")
        if not ancestors.intersection(eligible):
            roots.append(thread_id)

    spawned_by_root = {
        root: frozenset(_descendants(root, children)) for root in roots
    }
    return CodexDeletePlan(
        requested_ids=requested,
        root_ids=tuple(roots),
        descendants_by_root=spawned_by_root,
        protected=protected,
        rows=rows,
        spawn_edges=edges,
        official_states=states,
    )


def preview_codex_delete(
    profile: Profile,
    selected: list[dict],
    platform: PlatformAdapter,
    *,
    session_factory=AppServerSession,
) -> CodexDeletePlan:
    require(selected, "No archived Codex chats were selected.")
    requested = tuple(row.get("id") for row in selected)
    require(
        all(isinstance(thread_id, str) and thread_id for thread_id in requested)
        and len(set(requested)) == len(requested),
        "Each selected Codex thread must have a unique ID.",
    )
    rows, edges = _local_snapshot(profile)
    for chosen in selected:
        require(
            chosen.get("archived") == 1 and rows.get(chosen["id"]) == chosen,
            "A selected Codex chat changed after review. No changes were made.",
        )
    try:
        with session_factory(profile, platform, timeout=15) as server:
            states, native_edges = _official_inventory(server)
    except (PlatformError, AppServerError) as error:
        raise SafetyError(str(error)) from error
    return _build_plan(requested, rows, tuple(sorted(set(edges) | set(native_edges))), states)


def _rollout_versions(profile: Profile, logical_ids: set[str]) -> tuple[set[str], set[Path]]:
    """Read-only native-artifact evidence, never a list of files to delete."""
    versions: set[str] = set()
    paths: set[Path] = set()
    for directory in ("sessions", "archived_sessions"):
        root = profile.root / directory
        require(not root.is_symlink(), "Codex rollout directory is a symlink. Stopped.")
        if not root.is_dir():
            continue
        for path in root.rglob("rollout-*.jsonl*"):
            if not path.is_file() or path.is_symlink():
                continue
            match = _ROLLOUT_NAME.fullmatch(path.name)
            if match and match.group(1) in logical_ids:
                paths.add(path)
                versions.add(match.group(2) or match.group(1))
    return versions, paths


def _history_residual(profile: Profile, versions: set[str]) -> bool:
    history = profile.root / "thread_history_1.sqlite"
    if not history.is_file() or not versions:
        return False
    with database(history, profile.root) as db:
        for table in ("thread_history_projection_state", "thread_turns", "thread_items"):
            require(
                "thread_id" in columns(db, table),
                "Native Codex history index schema is unrecognized. Verification stopped.",
            )
            sequence = sorted(versions)
            for index in range(0, len(sequence), 200):
                items = sequence[index:index + 200]
                args = ",".join("?" for _ in items)
                if db.execute(
                    f"SELECT 1 FROM {table} WHERE thread_id IN ({args}) LIMIT 1", items
                ).fetchone():
                    return True
    return False


def _reference_refusal(error: AppServerError) -> bool:
    return (
        error.code == -32600
        and "forked history still references it" in str(error).lower()
    )


def execute_codex_delete_plan(
    profile: Profile,
    plan: CodexDeletePlan,
    platform: PlatformAdapter,
    *,
    session_factory=AppServerSession,
) -> CodexDeleteResult:
    """Invoke only native thread/delete; isolate proven fork-reference refusals.

    A timeout, unrecognized refusal, data change or incomplete native cleanup
    stops the batch without guessing whether a destructive request completed.
    """
    try:
        require_profile_stopped(platform, profile)
    except PlatformError as error:
        raise SafetyError(str(error)) from error
    verified: set[str] = set()
    calls: list[str] = []
    refused: dict[str, str] = dict(plan.protected)
    try:
        with session_factory(profile, platform, timeout=15) as server:
            rows, edges = _local_snapshot(profile)
            states, native_edges = _official_inventory(server)
            current = _build_plan(
                plan.requested_ids, rows,
                tuple(sorted(set(edges) | set(native_edges))), states,
            )
            require(
                current == plan,
                "Codex archived selection or spawned-thread state changed. Nothing was deleted.",
            )
            attempts = list(plan.root_ids)
            # Retry only an explicit, deterministic fork-reference refusal after
            # other targets were verified deleted. Never retry uncertain RPCs.
            for pass_number in range(2):
                progress = 0
                pending: list[str] = []
                for root in attempts:
                    before, before_edges = _local_snapshot(profile)
                    if root not in before:
                        require(
                            root in verified,
                            "A planned Codex thread vanished unexpectedly. Stopped.",
                        )
                        refused.pop(root, None)
                        continue
                    require(
                        before[root] == plan.rows[root],
                        "A selected Codex thread changed. Remaining work stopped.",
                    )
                    expected = ({root} | set(plan.descendants_by_root[root])) & set(before)
                    require(
                        all(
                            before[item] == plan.rows[item] and before[item].get("archived") == 1
                            for item in expected
                        ),
                        "A native spawned descendant changed. Remaining work stopped.",
                    )
                    versions, _ = _rollout_versions(profile, expected)
                    calls.append(root)
                    try:
                        server.delete_thread(root)
                    except AppServerError as error:
                        if _reference_refusal(error):
                            refused_rows, refused_edges = _local_snapshot(profile)
                            require(
                                refused_rows == before and refused_edges == before_edges,
                                "Official Codex refused deletion but local state changed. "
                                "Remaining work stopped.",
                            )
                            refused[root] = "Official Codex protects referenced fork history"
                            pending.append(root)
                            continue
                        raise
                    after, after_edges = _local_snapshot(profile)
                    removed = set(before) - set(after)
                    require(
                        removed == expected,
                        "Native Codex reported success but removed an unexpected set of threads. Stopped.",
                    )
                    _, remaining_files = _rollout_versions(profile, expected)
                    require(
                        not remaining_files and not _history_residual(profile, versions),
                        "Native Codex left rollout/history records after reporting success. "
                        "No direct repair was attempted.",
                    )
                    require(
                        not any(parent in expected or child in expected for parent, child, _ in after_edges),
                        "Native Codex left spawned-thread index entries after deletion. "
                        "No direct repair was attempted.",
                    )
                    verified.update(removed)
                    progress += len(removed)
                    refused.pop(root, None)
                if pass_number or not pending or not progress:
                    break
                attempts = pending
    except (PlatformError, AppServerError, SafetyError, sqlite3.Error, OSError) as error:
        raise SafetyError(
            f"Official Codex deletion stopped; {len(verified)} chat(s) were verified removed. "
            "Recheck the profile before another attempt. "
            f"{error}"
        ) from error

    return CodexDeleteResult(
        deleted_ids=tuple(sorted(verified)),
        native_calls=tuple(calls),
        protected=refused,
    )


def delete_archived_codex(
    profile: Profile,
    selected: list[dict],
    platform: PlatformAdapter,
    *,
    session_factory=AppServerSession,
) -> CodexDeleteResult:
    try:
        require_profile_stopped(platform, profile)
    except PlatformError as error:
        raise SafetyError(str(error)) from error
    plan = preview_codex_delete(
        profile, selected, platform, session_factory=session_factory,
    )
    return execute_codex_delete_plan(
        profile, plan, platform, session_factory=session_factory,
    )
