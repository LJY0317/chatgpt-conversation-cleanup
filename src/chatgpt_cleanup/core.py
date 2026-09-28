from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import shutil
import uuid

from .platforms import PlatformAdapter, PlatformError
from .profiles import Profile


CATALOG_COLUMNS = {
    "host_id",
    "thread_id",
    "display_title",
    "source_created_at",
    "source_updated_at",
    "cwd",
    "source_kind",
    "source_detail",
    "model_provider",
    "git_branch",
    "observation_sequence",
    "missing_candidate",
    "thread_source",
    "source_recency_at",
    "pending_observed_title",
    "project_id",
    "conversation_origin",
}
CATALOG_COLUMNS_WITH_TRIAL = CATALOG_COLUMNS | {"trial_conversation_type"}
VERIFIED_CATALOG_LAYOUTS = {
    frozenset(CATALOG_COLUMNS),
    frozenset(CATALOG_COLUMNS_WITH_TRIAL),
}
ACTIVE_RECOVERY_MAX_FILES = 50
ACTIVE_RECOVERY_MAX_BYTES = 20 * 1024 * 1024
ACTIVE_RECOVERY_MAX_AGE_DAYS = 90
RESTORED_RECOVERY_RETENTION_DAYS = 30
RESTORED_RECOVERY_MAX_FILES = 20
RESTORED_RECOVERY_MAX_BYTES = 10 * 1024 * 1024


class SafetyError(RuntimeError):
    pass


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
    db = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=3)
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
        optional = [name for name in ("creator_user_id", "creator_account_id", "name") if name in available]
        selected = ["id", "title", "archived", "updated_at", "rollout_path", *optional]
        sql = "SELECT " + ",".join(f'"{name}"' for name in selected) + ' FROM threads ORDER BY updated_at DESC'
        return [dict(row) for row in db.execute(sql)]


def read_schema(db):
    required_catalog = {
        "host_id",
        "thread_id",
        "display_title",
        "source_updated_at",
        "source_kind",
    }
    require(
        required_catalog <= columns(db, "local_thread_catalog"),
        "Catalog is missing fields required for read-only inspection.",
    )
    require(
        {"host_id", "host_kind"} <= columns(db, "local_thread_catalog_hosts"),
        "Catalog host schema is missing required fields.",
    )


def write_schema(db):
    require(
        db.execute("PRAGMA user_version").fetchone()[0] == 34,
        "Unsupported catalog version. No changes were made.",
    )
    catalog_columns = columns(db, "local_thread_catalog")
    require(
        frozenset(catalog_columns) in VERIFIED_CATALOG_LAYOUTS,
        "Catalog columns do not match a write-verified layout.",
    )
    require(
        columns(db, "local_thread_catalog_hosts") == {"host_id", "host_kind"},
        "Catalog host schema does not match.",
    )
    require(
        columns(db, "local_thread_catalog_metadata") == {"id", "catalog_revision"},
        "Catalog revision schema does not match.",
    )
    require(
        columns(db, "local_thread_catalog_scan_entries") == {"host_id", "thread_id", "removed"},
        "Catalog scan schema does not match.",
    )
    require(
        columns(db, "local_thread_catalog_sync_state")
        == {
            "host_id",
            "watermark_updated_at",
            "initial_build_complete",
            "observation_sequence",
            "last_full_reconciled_at",
        },
        "Catalog sync-state schema does not match.",
    )
    require(
        columns(db, "local_thread_catalog_scan_checkpoints")
        == {"host_id", "checkpoint", "failed_at"},
        "Catalog scan-checkpoint schema does not match.",
    )
    require(
        db.execute("SELECT count(*) FROM local_thread_catalog_metadata WHERE id=1").fetchone()[0] == 1,
        "Catalog revision row is missing.",
    )
    require(
        db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name IN "
            "('local_thread_catalog','local_thread_catalog_metadata','local_thread_catalog_scan_entries')"
        ).fetchone()[0]
        == 0,
        "Unexpected catalog trigger detected.",
    )
    return catalog_columns


def catalog(profile: Profile):
    root = profile.root
    live_ids = {row["id"] for row in sessions(profile)}
    source_rows = catalog_rows(profile)
    rows = []
    for row in source_rows:
        if (
            row["host_kind"] == "chatgpt"
            and row["source_kind"] == "chatgpt"
            and row.get("missing_candidate") == 1
        ):
            row["classification"] = "missing-chatgpt-entry"
        elif (
            row["host_kind"] == "local"
            and row["source_kind"] != "chatgpt"
            and row["thread_id"] not in live_ids
        ):
            row["classification"] = "local-orphan"
        else:
            continue
        rows.append(row)
    return rows


def catalog_rows(profile: Profile):
    root = profile.root
    with database(root / "sqlite/codex-dev.db", root) as db:
        read_schema(db)
        return [
            dict(raw)
            for raw in db.execute(
            "SELECT c.*, h.host_kind FROM local_thread_catalog c "
            "LEFT JOIN local_thread_catalog_hosts h USING(host_id)"
            )
        ]


def chatgpt_catalog(profile: Profile):
    return [
        row
        for row in catalog_rows(profile)
        if row["host_kind"] == "chatgpt" and row["source_kind"] == "chatgpt"
    ]


def require_catalog_write(platform: PlatformAdapter):
    supported, reason = platform.catalog_write_gate()
    require(supported, reason)


def recovery_root(platform: PlatformAdapter):
    path = platform.recovery_root
    safe_path(path, platform.home, must_exist=False)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def purge_product_data(platform: PlatformAdapter) -> bool:
    root = safe_path(platform.data_root, platform.home, must_exist=False)
    if not root.exists():
        return False
    require(root.is_dir(), "Product data path is not a directory.")
    shutil.rmtree(root)
    return True


def retire_installed_copy(platform: PlatformAdapter) -> int:
    removed = 0
    for candidate in platform.legacy_program_paths:
        path = safe_path(candidate, platform.home, must_exist=False)
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        elif path.is_file():
            path.unlink()
        else:
            raise SafetyError("Legacy program path has an unsupported type.")
        removed += 1
    return removed


def utc_now():
    return datetime.now(timezone.utc)


def recovery_record(path: Path, directory: Path):
    path = safe_path(path, directory)
    require(path.is_file(), "Recovery record is not a regular file.")
    data = json.loads(path.read_text())
    version = data.get("version")
    require(version in (2, 3, 4), "Unsupported recovery record format.")
    state = data.get("state", "active")
    require(state in ("active", "restored"), "Unknown recovery record state.")
    created = data.get("created_at")
    if created:
        try:
            created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError as error:
            raise SafetyError("Recovery creation time is invalid.") from error
        require(created_at.tzinfo is not None, "Recovery creation time has no timezone.")
    else:
        created_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return {
        "path": path,
        "data": data,
        "state": state,
        "created_at": created_at,
        "size": path.stat().st_size,
    }


def _delete_restored_records(records, *, now):
    restored = sorted(
        (record for record in records if record["state"] == "restored"),
        key=lambda record: (record["created_at"], record["path"].name),
    )
    cutoff_seconds = RESTORED_RECOVERY_RETENTION_DAYS * 86400
    for record in list(restored):
        restored_at = record["data"].get("restored_at")
        reference = record["created_at"]
        if restored_at:
            try:
                reference = datetime.fromisoformat(restored_at.replace("Z", "+00:00"))
            except ValueError as error:
                raise SafetyError("Recovery completion time is invalid.") from error
            require(reference.tzinfo is not None, "Recovery completion time has no timezone.")
        if (now - reference.astimezone(timezone.utc)).total_seconds() >= cutoff_seconds:
            record["path"].unlink()
            restored.remove(record)
    while len(restored) > RESTORED_RECOVERY_MAX_FILES or sum(
        record["size"] for record in restored
    ) > RESTORED_RECOVERY_MAX_BYTES:
        restored.pop(0)["path"].unlink()


def maintain_recovery(directory: Path, *, now=None):
    now = (now or utc_now()).astimezone(timezone.utc)
    directory = safe_path(directory, directory)
    records = [recovery_record(path, directory) for path in sorted(directory.glob("*.json"))]
    _delete_restored_records(records, now=now)
    records = [recovery_record(path, directory) for path in sorted(directory.glob("*.json"))]
    active = [record for record in records if record["state"] == "active"]
    stale = [
        record
        for record in active
        if (now - record["created_at"].astimezone(timezone.utc)).total_seconds()
        >= ACTIVE_RECOVERY_MAX_AGE_DAYS * 86400
    ]
    return {
        "records": records,
        "active": active,
        "stale": stale,
        "active_bytes": sum(record["size"] for record in active),
    }


def require_recovery_capacity(directory: Path, additional_bytes: int, *, now=None):
    inventory = maintain_recovery(directory, now=now)
    require(not inventory["stale"], "Review recovery records older than 90 days before cleaning again.")
    require(
        len(inventory["active"]) + 1 <= ACTIVE_RECOVERY_MAX_FILES,
        "Active recovery record count reached the safety limit.",
    )
    require(
        inventory["active_bytes"] + additional_bytes <= ACTIVE_RECOVERY_MAX_BYTES,
        "Active recovery data reached the safety limit.",
    )
    return inventory


def _fsync_directory(path: Path):
    if os.name == "nt":
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def save_private(path: Path, data):
    with path.open("x", encoding="utf-8") as stream:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        json.dump(data, stream, ensure_ascii=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)


def replace_private(path: Path, data):
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    save_private(temp, data)
    try:
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


def evict_catalog(
    profile: Profile,
    selected: list[dict],
    *,
    recovery: Path,
    platform: PlatformAdapter,
):
    require(selected, "No catalog entries selected.")
    require_catalog_write(platform)
    try:
        platform.require_desktop_stopped()
    except PlatformError as error:
        raise SafetyError(str(error)) from error
    root = profile.root
    current = catalog_rows(profile)
    keys = {(row["host_id"], row["thread_id"]) for row in selected}
    require(len(keys) == len(selected), "Duplicate targets are not allowed.")
    fresh = [row for row in current if (row["host_id"], row["thread_id"]) in keys]
    sort_key = lambda row: (row["host_id"], row["thread_id"])
    comparable = lambda row: {
        key: value for key, value in row.items() if key != "classification"
    }
    require(
        len(fresh) == len(selected)
        and [comparable(row) for row in sorted(fresh, key=sort_key)]
        == [comparable(row) for row in sorted(selected, key=sort_key)],
        "Catalog changed after preview.",
    )
    operation_id = str(uuid.uuid4())
    backup = recovery / (operation_id + ".json")
    with database(root / "sqlite/codex-dev.db", root, write=True) as db:
        db.execute("BEGIN IMMEDIATE")
        catalog_columns = write_schema(db)
        try:
            platform.require_desktop_stopped()
        except PlatformError as error:
            db.rollback()
            raise SafetyError(str(error)) from error
        live_ids = {row["id"] for row in sessions(profile)}
        require(
            all(
                row.get("classification") != "local-orphan"
                or row["thread_id"] not in live_ids
                for row in selected
            ),
            "A Native conversation reappeared. Cleanup was stopped.",
        )
        saved = []
        for target in selected:
            key = (target["host_id"], target["thread_id"])
            actual = db.execute(
                "SELECT * FROM local_thread_catalog WHERE host_id=? AND thread_id=?", key
            ).fetchone()
            require(
                actual is not None
                and dict(actual) == {name: target.get(name) for name in catalog_columns},
                "Concurrent catalog change detected.",
            )
            scan = db.execute(
                "SELECT * FROM local_thread_catalog_scan_entries WHERE host_id=? AND thread_id=?",
                key,
            ).fetchone()
            checkpoint = db.execute(
                "SELECT 1 FROM local_thread_catalog_scan_checkpoints WHERE host_id=?",
                (key[0],),
            ).fetchone()
            saved.append(
                {
                    "row": dict(actual),
                    "scan": dict(scan) if scan else None,
                    "cleanup_tombstone": checkpoint is not None,
                }
            )
        payload = {
            "version": 4,
            "operation_id": operation_id,
            "created_at": utc_now().isoformat().replace("+00:00", "Z"),
            "state": "active",
            "profile_id": profile.id,
            "profile_provider": profile.provider,
            "profile_root": str(root),
            "platform": platform.key,
            "items": saved,
        }
        encoded = json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8")
        require_recovery_capacity(recovery, len(encoded))
        save_private(backup, payload)
        try:
            visible_removed = 0
            for host, thread in keys:
                sequence = db.execute(
                    "UPDATE local_thread_catalog_sync_state "
                    "SET observation_sequence=observation_sequence+1 "
                    "WHERE host_id=? RETURNING observation_sequence",
                    (host,),
                ).fetchone()
                require(
                    sequence is not None and isinstance(sequence[0], int),
                    "Catalog sync state is unavailable for this profile.",
                )
                checkpoint = db.execute(
                    "SELECT 1 FROM local_thread_catalog_scan_checkpoints WHERE host_id=?",
                    (host,),
                ).fetchone()
                if checkpoint is not None:
                    db.execute(
                        "INSERT INTO local_thread_catalog_scan_entries(host_id,thread_id,removed) "
                        "VALUES(?,?,1) ON CONFLICT(host_id,thread_id) "
                        "DO UPDATE SET removed=1",
                        (host, thread),
                    )
                deleted = db.execute(
                    "DELETE FROM local_thread_catalog WHERE host_id=? AND thread_id=? "
                    "RETURNING missing_candidate",
                    (host, thread),
                ).fetchone()
                require(deleted is not None, "Unexpected catalog deletion count.")
                if deleted[0] == 0:
                    visible_removed += 1
            if visible_removed:
                db.execute(
                    "UPDATE local_thread_catalog_metadata "
                    "SET catalog_revision=catalog_revision+? WHERE id=1",
                    (visible_removed,),
                )
            require(db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "Database verification failed.")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return backup


def restore_catalog(
    profile: Profile,
    backup: Path,
    *,
    platform: PlatformAdapter,
):
    require_catalog_write(platform)
    try:
        platform.require_desktop_stopped()
    except PlatformError as error:
        raise SafetyError(str(error)) from error
    root = profile.root
    data = json.loads(backup.read_text())
    require(data.get("version") in (2, 3, 4), "Unsupported recovery record.")
    require(data.get("profile_root") == str(root), "Recovery record belongs to another profile.")
    require(data.get("state") == "active", "Recovery record is already restored.")
    require(isinstance(data.get("items"), list) and data["items"], "Recovery record has no items.")
    with database(root / "sqlite/codex-dev.db", root, write=True) as db:
        db.execute("BEGIN IMMEDIATE")
        catalog_columns = write_schema(db)
        try:
            restored_visible = 0
            for item in data["items"]:
                row = item["row"]
                require(set(row) == catalog_columns, "Recovery columns do not match.")
                key = (row["host_id"], row["thread_id"])
                require(
                    not db.execute(
                        "SELECT 1 FROM local_thread_catalog WHERE host_id=? AND thread_id=?", key
                    ).fetchone(),
                    "An entry was recreated; it will not be overwritten.",
                )
                current_scan = db.execute(
                    "SELECT * FROM local_thread_catalog_scan_entries "
                    "WHERE host_id=? AND thread_id=?",
                    key,
                ).fetchone()
                saved_scan = item["scan"]
                tombstone_expected = bool(item.get("cleanup_tombstone", False))
                current_scan_dict = dict(current_scan) if current_scan else None
                cleanup_tombstone = (
                    tombstone_expected
                    and current_scan_dict is not None
                    and current_scan_dict.get("host_id") == key[0]
                    and current_scan_dict.get("thread_id") == key[1]
                    and current_scan_dict.get("removed") == 1
                )
                require(
                    current_scan_dict == saved_scan
                    or cleanup_tombstone,
                    "Catalog scan state changed. Restore was stopped.",
                )
                host = db.execute(
                    "SELECT host_kind FROM local_thread_catalog_hosts WHERE host_id=?", (key[0],)
                ).fetchone()
                require(host and host[0] in ("local", "chatgpt"), "Catalog host changed.")
                sequence = db.execute(
                    "UPDATE local_thread_catalog_sync_state "
                    "SET observation_sequence=observation_sequence+1 "
                    "WHERE host_id=? RETURNING observation_sequence",
                    (key[0],),
                ).fetchone()
                require(
                    sequence is not None and isinstance(sequence[0], int),
                    "Catalog sync state changed. Restore was stopped.",
                )
                row = dict(row)
                if "observation_sequence" in row:
                    row["observation_sequence"] = sequence[0]
                names = list(row)
                db.execute(
                    "INSERT INTO local_thread_catalog ("
                    + ",".join(names)
                    + ") VALUES ("
                    + ",".join("?" for _ in names)
                    + ")",
                    [row[name] for name in names],
                )
                if cleanup_tombstone:
                    db.execute(
                        "DELETE FROM local_thread_catalog_scan_entries "
                        "WHERE host_id=? AND thread_id=?",
                        key,
                    )
                if saved_scan:
                    require(
                        set(saved_scan) == {"host_id", "thread_id", "removed"}
                        and (saved_scan["host_id"], saved_scan["thread_id"]) == key,
                        "Recovery scan metadata does not match.",
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO local_thread_catalog_scan_entries VALUES (?,?,?)",
                        (key[0], key[1], saved_scan["removed"]),
                    )
                if row.get("missing_candidate") == 0:
                    restored_visible += 1
            if restored_visible:
                db.execute(
                    "UPDATE local_thread_catalog_metadata "
                    "SET catalog_revision=catalog_revision+? WHERE id=1",
                    (restored_visible,),
                )
            require(db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "Database verification failed.")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    data["state"] = "restored"
    data["restored_at"] = utc_now().isoformat().replace("+00:00", "Z")
    try:
        replace_private(backup, data)
        return True
    except OSError:
        return False
