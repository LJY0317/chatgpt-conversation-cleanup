from __future__ import annotations

from dataclasses import dataclass
import time

from .core import database, require, require_catalog_write, write_schema
from .platforms import PlatformAdapter
from .profiles import Profile


@dataclass(frozen=True)
class DesktopCatalogRefreshRequest:
    profile_id: str
    host_id: str
    requested_at_ms: int
    starting_observation_sequence: int
    previous_last_full_reconciled_at: int | None


@dataclass(frozen=True)
class DesktopCatalogRefreshResult:
    host_id: str
    completed_at_ms: int
    observation_sequence: int
    present_ids: frozenset[str]
    missing_ids: frozenset[str]


def require_desktop_catalog_refresh(profile: Profile, platform: PlatformAdapter) -> None:
    require_catalog_write(platform)
    gate = getattr(platform, "profile_catalog_refresh_gate", None)
    if gate is None:
        supported, reason = platform.catalog_refresh_gate()
    else:
        supported, reason = gate(profile)
    require(supported, reason)


def _require_profile_stopped(profile: Profile, platform: PlatformAdapter) -> None:
    checker = getattr(platform, "require_profile_desktop_stopped", None)
    if checker is None:
        platform.require_desktop_stopped()
    else:
        checker(profile)


def _workspace_host(db, account_id: str) -> str:
    hosts = []
    for row in db.execute(
        "SELECT host_id FROM local_thread_catalog_hosts WHERE host_kind='chatgpt'"
    ):
        host_id = row["host_id"]
        if not isinstance(host_id, str):
            continue
        parts = host_id.split(":", 2)
        if len(parts) == 3 and parts[0] == "chatgpt" and parts[1] == account_id:
            hosts.append(host_id)
    require(
        len(hosts) == 1,
        "Could not identify exactly one local ChatGPT catalog for the signed-in workspace.",
    )
    return hosts[0]


def prepare_desktop_catalog_refresh(
    profile: Profile,
    account_id: str,
    platform: PlatformAdapter,
) -> DesktopCatalogRefreshRequest:
    """Queue one Desktop-owned full ChatGPT catalog reconciliation.

    The utility does not manufacture server results or rewrite conversation rows.
    It only clears the build-verified scheduling marker and stale scan state while
    Desktop is stopped. On the next Desktop start, Desktop remains the sole owner
    of the authenticated history requests and of any catalog changes they imply.
    """

    require_desktop_catalog_refresh(profile, platform)
    require(bool(account_id), "The signed-in ChatGPT workspace could not be identified.")
    _require_profile_stopped(profile, platform)

    path = profile.root / "sqlite/codex-dev.db"
    with database(path, profile.root, write=True) as db:
        write_schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            # Re-check while holding the write transaction. This narrows the
            # process/SQLite race: if Desktop appeared after the outer check,
            # the scheduling-state mutation is rolled back instead of racing it.
            _require_profile_stopped(profile, platform)
            host_id = _workspace_host(db, account_id)
            state = db.execute(
                "SELECT observation_sequence,last_full_reconciled_at "
                "FROM local_thread_catalog_sync_state WHERE host_id=?",
                (host_id,),
            ).fetchone()
            require(state is not None, "ChatGPT catalog sync state is unavailable.")
            starting_sequence = state["observation_sequence"]
            previous_full = state["last_full_reconciled_at"]
            require(
                isinstance(starting_sequence, int) and starting_sequence >= 0,
                "ChatGPT catalog observation sequence is invalid.",
            )
            require(
                previous_full is None or isinstance(previous_full, int),
                "ChatGPT catalog full-refresh timestamp is invalid.",
            )
            requested_at_ms = int(time.time() * 1000)

            # This mirrors Desktop's own migration contract for scheduling a
            # ChatGPT full catalog backfill, narrowed to this verified host. A
            # stale incremental checkpoint must be cleared as well; otherwise
            # Desktop can resume it ahead of the requested full reconciliation.
            changed = db.execute(
                "UPDATE local_thread_catalog_sync_state "
                "SET last_full_reconciled_at=NULL WHERE host_id=?",
                (host_id,),
            )
            require(changed.rowcount == 1, "ChatGPT catalog sync state changed unexpectedly.")
            db.execute(
                "DELETE FROM local_thread_catalog_scan_checkpoints WHERE host_id=?",
                (host_id,),
            )
            db.execute(
                "DELETE FROM local_thread_catalog_scan_entries WHERE host_id=?",
                (host_id,),
            )
            require(
                db.execute("PRAGMA quick_check").fetchone()[0] == "ok",
                "Database verification failed while queuing Desktop refresh.",
            )
            _require_profile_stopped(profile, platform)
            db.commit()
        except BaseException:
            db.rollback()
            raise

    return DesktopCatalogRefreshRequest(
        profile_id=profile.id,
        host_id=host_id,
        requested_at_ms=requested_at_ms,
        starting_observation_sequence=starting_sequence,
        previous_last_full_reconciled_at=previous_full,
    )


def desktop_catalog_refresh_result(
    profile: Profile,
    request: DesktopCatalogRefreshRequest,
    platform: PlatformAdapter,
) -> DesktopCatalogRefreshResult | None:
    """Return authoritative Desktop evidence only after a newer full scan ends."""

    require_desktop_catalog_refresh(profile, platform)
    require(request.profile_id == profile.id, "Desktop refresh request belongs to another profile.")

    path = profile.root / "sqlite/codex-dev.db"
    with database(path, profile.root) as db:
        write_schema(db)
        state = db.execute(
            "SELECT observation_sequence,last_full_reconciled_at,initial_build_complete "
            "FROM local_thread_catalog_sync_state WHERE host_id=?",
            (request.host_id,),
        ).fetchone()
        if state is None:
            return None
        sequence = state["observation_sequence"]
        completed_at = state["last_full_reconciled_at"]
        is_complete = state["initial_build_complete"]
        if not (
            isinstance(sequence, int)
            and sequence > request.starting_observation_sequence
            and isinstance(completed_at, int)
            and completed_at >= request.requested_at_ms
            and completed_at != request.previous_last_full_reconciled_at
            and is_complete != 0
        ):
            return None

        rows = list(
            db.execute(
                "SELECT thread_id,missing_candidate,observation_sequence "
                "FROM local_thread_catalog "
                "WHERE host_id=? AND source_kind='chatgpt'",
                (request.host_id,),
            )
        )

    present = frozenset(
        row["thread_id"] for row in rows if row["missing_candidate"] == 0
    )
    # Historical missing_candidate=1 is only suspicion. Promote a missing row to
    # strong evidence when this refresh itself advanced that row's observation
    # sequence while marking it missing.
    missing = frozenset(
        row["thread_id"]
        for row in rows
        if row["missing_candidate"] == 1
        and isinstance(row["observation_sequence"], int)
        and row["observation_sequence"] > request.starting_observation_sequence
    )
    return DesktopCatalogRefreshResult(
        host_id=request.host_id,
        completed_at_ms=completed_at,
        observation_sequence=sequence,
        present_ids=present,
        missing_ids=missing,
    )
