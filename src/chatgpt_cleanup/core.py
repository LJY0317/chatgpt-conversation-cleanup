"""Read-only native inventories and the verified Desktop refresh schema.

No function here deletes or rewrites conversation/catalog rows.
Only the official Desktop performs catalog reconciliation.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import sqlite3

from .platforms import PlatformAdapter
from .profiles import Profile


class SafetyError(RuntimeError):
    pass


CATALOG_COLUMNS = {
    "host_id", "thread_id", "display_title", "source_created_at",
    "source_updated_at", "cwd", "source_kind", "source_detail",
    "model_provider", "git_branch", "observation_sequence",
    "missing_candidate", "thread_source", "source_recency_at",
    "pending_observed_title", "project_id", "conversation_origin",
}


def require(condition, message):
    if not condition:
        raise SafetyError(message)


def safe_path(path: Path, root: Path, *, must_exist=True):
    path, root = Path(path).absolute(), Path(root).absolute()
    require(path.is_relative_to(root), "Path is outside the allowed root.")
    for part in [path, *path.parents]:
        require(not part.is_symlink(), f"Refusing symlinked path: {part.name}")
        if part == root:
            break
    require(not must_exist or path.exists(), f"Missing file or directory: {path.name}")
    return path


@contextlib.contextmanager
def database(path: Path, root: Path, *, write=False):
    safe_path(path, root)
    for suffix in ("-wal", "-shm", "-journal"):
        safe_path(Path(str(path) + suffix), root, must_exist=False)
    require(path.is_file(), "Expected a database file.")
    mode = "rw" if write else "ro"
    db = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def columns(db, table):
    return {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}


def sessions(profile: Profile):
    root = profile.root
    with database(root / "state_5.sqlite", root) as db:
        available = columns(db, "threads")
        required = {"id", "title", "archived", "updated_at", "rollout_path"}
        require(required <= available, "Unsupported Native conversation database schema.")
        optional = [
            name for name in ("creator_user_id", "creator_account_id", "name")
            if name in available
        ]
        selected = ["id", "title", "archived", "updated_at", "rollout_path", *optional]
        sql = "SELECT " + ",".join(f'"{name}"' for name in selected)
        sql += " FROM threads ORDER BY updated_at DESC"
        return [dict(row) for row in db.execute(sql)]


def catalog_rows(profile: Profile):
    root = profile.root
    with database(root / "sqlite/codex-dev.db", root) as db:
        require(
            {"host_id", "thread_id", "source_kind", "missing_candidate"}
            <= columns(db, "local_thread_catalog"),
            "ChatGPT Desktop catalog is not readable.",
        )
        require(
            {"host_id", "host_kind"} <= columns(db, "local_thread_catalog_hosts"),
            "ChatGPT Desktop host catalog is not readable.",
        )
        return [
            dict(row) for row in db.execute(
                "SELECT c.*, h.host_kind FROM local_thread_catalog c "
                "LEFT JOIN local_thread_catalog_hosts h USING(host_id)"
            )
        ]


def chatgpt_catalog(profile: Profile):
    return [
        row for row in catalog_rows(profile)
        if row["host_kind"] == "chatgpt" and row["source_kind"] == "chatgpt"
    ]


def write_schema(db):
    """Only the known native full-refresh scheduling schema may be updated."""
    require(
        db.execute("PRAGMA user_version").fetchone()[0] == 34,
        "Unsupported catalog version. No changes were made.",
    )
    available = columns(db, "local_thread_catalog")
    extras = available - CATALOG_COLUMNS
    require(
        CATALOG_COLUMNS <= available
        and extras <= {"trial_conversation_type", "chatgpt_async_status"},
        "Catalog columns do not match a verified Desktop layout.",
    )
    expected = {
        "local_thread_catalog_hosts": {"host_id", "host_kind"},
        "local_thread_catalog_metadata": {"id", "catalog_revision"},
        "local_thread_catalog_scan_entries": {"host_id", "thread_id", "removed"},
        "local_thread_catalog_sync_state": {
            "host_id", "watermark_updated_at", "initial_build_complete",
            "observation_sequence", "last_full_reconciled_at",
        },
        "local_thread_catalog_scan_checkpoints": {"host_id", "checkpoint", "failed_at"},
    }
    for table, names in expected.items():
        require(columns(db, table) == names, f"Catalog {table} schema is unsupported.")
    require(
        db.execute("SELECT count(*) FROM local_thread_catalog_metadata WHERE id=1").fetchone()[0] == 1,
        "Catalog revision row is missing.",
    )
    require(
        db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name IN "
            "('local_thread_catalog','local_thread_catalog_metadata','local_thread_catalog_scan_entries')"
        ).fetchone()[0] == 0,
        "Unexpected catalog trigger detected.",
    )
    return available


def require_catalog_write(platform: PlatformAdapter):
    supported, reason = platform.catalog_write_gate()
    require(supported, reason)


def recovery_root(platform: PlatformAdapter):
    """Existing private operation-lock directory; old undo files remain untouched."""
    path = platform.recovery_root
    safe_path(path, platform.home, must_exist=False)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path
