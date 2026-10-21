import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_cleanup import core
from chatgpt_cleanup.core import SafetyError
from chatgpt_cleanup.desktop_refresh import (
    DesktopCatalogRefreshProgress,
    DesktopCatalogRefreshRequest,
    DesktopCatalogRefreshResult,
    _host_token,
    _workspace_host,
    _workspace_hosts,
    desktop_catalog_refresh_progress,
    desktop_catalog_refresh_result,
    prepare_desktop_catalog_refresh,
)
from chatgpt_cleanup.platforms import PlatformError
from chatgpt_cleanup.profiles import Profile, ProfileIdentity

class RefreshPlatform:
    key = "test"
    display_name = "Test"

    def __init__(self, home: Path):
        self.home = home
        self.running = False

    def catalog_write_gate(self):
        return True, "test write gate"

    def catalog_refresh_gate(self):
        return True, "test refresh gate"

    def profile_catalog_refresh_gate(self, profile):
        return True, "test profile refresh gate"

    def require_desktop_stopped(self):
        if self.running:
            raise PlatformError("Desktop is running")




class DesktopRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / ".codex"
        (self.root / "sqlite").mkdir(parents=True)
        self.profile = Profile("default", "ChatGPT", self.root, "official")
        self.platform = RefreshPlatform(self.home)
        self.db_path = self.root / "sqlite/codex-dev.db"
        self.host = "chatgpt:account-one:user-one"
        self._create_db()

    def _create_db(self):
        db = sqlite3.connect(self.db_path)
        try:
            catalog_columns = ",".join(
                f'"{name}" '
                + (
                    "INTEGER"
                    if name
                    in ("observation_sequence", "missing_candidate", "pending_observed_title")
                    else "TEXT"
                )
                for name in sorted(core.CATALOG_COLUMNS)
            )
            db.executescript(
                "CREATE TABLE local_thread_catalog("
                + catalog_columns
                + ",PRIMARY KEY(host_id,thread_id));"
                "CREATE TABLE local_thread_catalog_hosts("
                "host_id TEXT PRIMARY KEY,host_kind TEXT);"
                "CREATE TABLE local_thread_catalog_metadata("
                "id INTEGER PRIMARY KEY,catalog_revision INTEGER);"
                "CREATE TABLE local_thread_catalog_scan_entries("
                "host_id TEXT,thread_id TEXT,removed INTEGER,"
                "PRIMARY KEY(host_id,thread_id));"
                "CREATE TABLE local_thread_catalog_sync_state("
                "host_id TEXT PRIMARY KEY,watermark_updated_at REAL,"
                "initial_build_complete INTEGER NOT NULL DEFAULT 0,"
                "observation_sequence INTEGER NOT NULL DEFAULT 0,"
                "last_full_reconciled_at INTEGER);"
                "CREATE TABLE local_thread_catalog_scan_checkpoints("
                "host_id TEXT PRIMARY KEY,checkpoint TEXT NOT NULL,failed_at INTEGER);"
                "PRAGMA user_version=34;"
            )
            db.execute("INSERT INTO local_thread_catalog_metadata VALUES(1,1)")
            db.execute(
                "INSERT INTO local_thread_catalog_hosts VALUES(?, 'chatgpt')",
                (self.host,),
            )
            db.execute(
                "INSERT INTO local_thread_catalog_sync_state "
                "VALUES(?,100.0,1,20,900)",
                (self.host,),
            )
            db.commit()
        finally:
            db.close()

    def add_row(self, thread_id, *, missing=0, sequence=1):
        values = {name: None for name in core.CATALOG_COLUMNS}
        values.update(
            host_id=self.host,
            thread_id=thread_id,
            display_title=thread_id,
            source_created_at="1",
            source_updated_at="1",
            source_kind="chatgpt",
            observation_sequence=sequence,
            missing_candidate=missing,
            pending_observed_title=0,
        )
        db = sqlite3.connect(self.db_path)
        try:
            db.execute(
                "INSERT INTO local_thread_catalog("
                + ",".join(values)
                + ") VALUES("
                + ",".join("?" for _ in values)
                + ")",
                list(values.values()),
            )
            db.commit()
        finally:
            db.close()

    def test_prepare_clears_marker_checkpoint_and_seen_entries(self):
        self.add_row("stale-seen")
        db = sqlite3.connect(self.db_path)
        try:
            db.execute(
                "INSERT INTO local_thread_catalog_scan_checkpoints VALUES(?,?,?)",
                (self.host, '{"attempt":{"mode":"incremental"}}', 123),
            )
            db.execute(
                "INSERT INTO local_thread_catalog_scan_entries VALUES(?,?,0)",
                (self.host, "stale-seen"),
            )
            db.commit()
        finally:
            db.close()

        with patch("chatgpt_cleanup.desktop_refresh.time.time", return_value=1.234):
            request = prepare_desktop_catalog_refresh(
                self.profile, "account-one", self.platform
            )

        self.assertEqual(request.host_id, self.host)
        self.assertEqual(request.requested_at_ms, 1234)
        self.assertEqual(request.starting_observation_sequence, 20)
        self.assertEqual(request.previous_last_full_reconciled_at, 900)
        db = sqlite3.connect(self.db_path)
        try:
            state = db.execute(
                "SELECT last_full_reconciled_at FROM local_thread_catalog_sync_state "
                "WHERE host_id=?",
                (self.host,),
            ).fetchone()[0]
            checkpoints = db.execute(
                "SELECT count(*) FROM local_thread_catalog_scan_checkpoints WHERE host_id=?",
                (self.host,),
            ).fetchone()[0]
            seen = db.execute(
                "SELECT count(*) FROM local_thread_catalog_scan_entries WHERE host_id=?",
                (self.host,),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT count(*) FROM local_thread_catalog WHERE host_id=?",
                (self.host,),
            ).fetchone()[0]
        finally:
            db.close()
        self.assertIsNone(state)
        self.assertEqual(checkpoints, 0)
        self.assertEqual(seen, 0)
        self.assertEqual(rows, 1)

    def test_prepare_is_strictly_host_scoped(self):
        other = "chatgpt:account-two:user-two"
        self.add_row("current-row")
        db = sqlite3.connect(self.db_path)
        try:
            db.execute("INSERT INTO local_thread_catalog_hosts VALUES(?, 'chatgpt')", (other,))
            db.execute(
                "INSERT INTO local_thread_catalog_sync_state "
                "VALUES(?,200.0,1,40,800)",
                (other,),
            )
            values = {name: None for name in core.CATALOG_COLUMNS}
            values.update(
                host_id=other,
                thread_id="other-row",
                display_title="other",
                source_created_at="1",
                source_updated_at="1",
                source_kind="chatgpt",
                observation_sequence=7,
                missing_candidate=0,
                pending_observed_title=0,
            )
            db.execute(
                "INSERT INTO local_thread_catalog("
                + ",".join(values)
                + ") VALUES("
                + ",".join("?" for _ in values)
                + ")",
                list(values.values()),
            )
            db.execute(
                "INSERT INTO local_thread_catalog_scan_checkpoints VALUES(?,?,NULL)",
                (other, '{"attempt":{"mode":"incremental"}}'),
            )
            db.execute(
                "INSERT INTO local_thread_catalog_scan_entries VALUES(?,?,0)",
                (other, "other-row"),
            )
            db.commit()
        finally:
            db.close()

        prepare_desktop_catalog_refresh(self.profile, "account-one", self.platform)

        db = sqlite3.connect(self.db_path)
        try:
            other_full = db.execute(
                "SELECT last_full_reconciled_at FROM local_thread_catalog_sync_state "
                "WHERE host_id=?",
                (other,),
            ).fetchone()[0]
            other_checkpoint = db.execute(
                "SELECT count(*) FROM local_thread_catalog_scan_checkpoints WHERE host_id=?",
                (other,),
            ).fetchone()[0]
            other_seen = db.execute(
                "SELECT count(*) FROM local_thread_catalog_scan_entries WHERE host_id=?",
                (other,),
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(other_full, 800)
        self.assertEqual(other_checkpoint, 1)
        self.assertEqual(other_seen, 1)

    def test_prepare_fails_closed_for_ambiguous_workspace_host(self):
        db = sqlite3.connect(self.db_path)
        try:
            other = "chatgpt:account-one:user-two"
            db.execute("INSERT INTO local_thread_catalog_hosts VALUES(?, 'chatgpt')", (other,))
            db.execute(
                "INSERT INTO local_thread_catalog_sync_state(host_id) VALUES(?)",
                (other,),
            )
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(SafetyError, "exactly one"):
            prepare_desktop_catalog_refresh(self.profile, "account-one", self.platform)

    def test_prepare_requires_desktop_stopped(self):
        self.platform.running = True
        with self.assertRaises(PlatformError):
            prepare_desktop_catalog_refresh(self.profile, "account-one", self.platform)

    def test_prepare_rolls_back_if_desktop_appears_before_commit(self):
        db = sqlite3.connect(self.db_path)
        try:
            db.execute(
                "INSERT INTO local_thread_catalog_scan_checkpoints VALUES(?,?,NULL)",
                (self.host, '{"attempt":{"mode":"incremental"}}'),
            )
            db.commit()
        finally:
            db.close()

        class RacingPlatform(RefreshPlatform):
            def __init__(self, home):
                super().__init__(home)
                self.checks = 0

            def require_desktop_stopped(self):
                self.checks += 1
                if self.checks == 3:
                    raise PlatformError("Desktop started during refresh preparation")

        with self.assertRaises(PlatformError):
            prepare_desktop_catalog_refresh(
                self.profile, "account-one", RacingPlatform(self.home)
            )

        db = sqlite3.connect(self.db_path)
        try:
            full = db.execute(
                "SELECT last_full_reconciled_at FROM local_thread_catalog_sync_state "
                "WHERE host_id=?",
                (self.host,),
            ).fetchone()[0]
            checkpoints = db.execute(
                "SELECT count(*) FROM local_thread_catalog_scan_checkpoints WHERE host_id=?",
                (self.host,),
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(full, 900)
        self.assertEqual(checkpoints, 1)

    def test_result_requires_newer_full_scan_and_only_promotes_fresh_missing_rows(self):
        self.add_row("present", missing=0, sequence=1)
        self.add_row("fresh-missing", missing=1, sequence=25)
        self.add_row("old-missing", missing=1, sequence=5)
        request = DesktopCatalogRefreshRequest(
            profile_id="default",
            host_id=self.host,
            requested_at_ms=1000,
            starting_observation_sequence=20,
            previous_last_full_reconciled_at=900,
        )
        self.assertIsNone(
            desktop_catalog_refresh_result(self.profile, request, self.platform)
        )

        db = sqlite3.connect(self.db_path)
        try:
            db.execute(
                "UPDATE local_thread_catalog_sync_state "
                "SET observation_sequence=25,last_full_reconciled_at=1100,"
                "initial_build_complete=1 WHERE host_id=?",
                (self.host,),
            )
            db.commit()
        finally:
            db.close()

        result = desktop_catalog_refresh_result(self.profile, request, self.platform)
        self.assertIsNotNone(result)
        self.assertEqual(result.present_ids, frozenset({"present"}))
        self.assertEqual(result.missing_ids, frozenset({"fresh-missing"}))
        self.assertEqual(result.completed_at_ms, 1100)
        self.assertEqual(result.observation_sequence, 25)

    def test_result_rejects_old_full_timestamp_even_if_sequence_advanced(self):
        request = DesktopCatalogRefreshRequest(
            profile_id="default",
            host_id=self.host,
            requested_at_ms=1000,
            starting_observation_sequence=20,
            previous_last_full_reconciled_at=900,
        )
        db = sqlite3.connect(self.db_path)
        try:
            db.execute(
                "UPDATE local_thread_catalog_sync_state "
                "SET observation_sequence=21,last_full_reconciled_at=950 "
                "WHERE host_id=?",
                (self.host,),
            )
            db.commit()
        finally:
            db.close()
        self.assertIsNone(
            desktop_catalog_refresh_result(self.profile, request, self.platform)
        )

    def test_progress_reports_waiting_full_and_complete_stages(self):
        request = DesktopCatalogRefreshRequest(
            profile_id="default",
            host_id=self.host,
            requested_at_ms=1000,
            starting_observation_sequence=20,
            previous_last_full_reconciled_at=900,
        )
        progress = desktop_catalog_refresh_progress(self.profile, request)
        self.assertEqual(progress.stage, "waiting")
        self.assertEqual(progress.catalog_rows, 0)

        db = sqlite3.connect(self.db_path)
        try:
            db.execute(
                "UPDATE local_thread_catalog_sync_state SET observation_sequence=21,"
                "last_full_reconciled_at=NULL WHERE host_id=?",
                (self.host,),
            )
            db.execute(
                "INSERT INTO local_thread_catalog_scan_checkpoints VALUES(?,?,NULL)",
                (
                    self.host,
                    '{"attempt":{"mode":"full"},"progress":{"phase":"head"}}',
                ),
            )
            db.commit()
        finally:
            db.close()
        progress = desktop_catalog_refresh_progress(self.profile, request)
        self.assertEqual(progress.stage, "full")
        self.assertEqual(progress.phase, "head")

        db = sqlite3.connect(self.db_path)
        try:
            db.execute(
                "UPDATE local_thread_catalog_sync_state SET observation_sequence=22,"
                "last_full_reconciled_at=1100 WHERE host_id=?",
                (self.host,),
            )
            db.commit()
        finally:
            db.close()
        progress = desktop_catalog_refresh_progress(self.profile, request)
        self.assertEqual(progress.stage, "complete")

    def test_same_account_host_token_scopes_refresh_and_preserves_catalog_rows(self):
        """One ChatGPT account may have two local Desktop hosts."""
        second = "chatgpt:account-one:another-user-host"
        self.add_row("original-chat")
        db = sqlite3.connect(self.db_path)
        with db:
            db.execute(
                "INSERT INTO local_thread_catalog_hosts VALUES(?,'chatgpt')",
                (second,),
            )
            db.execute(
                "INSERT INTO local_thread_catalog_sync_state VALUES(?,100,1,18,850)",
                (second,),
            )
            db.execute(
                "INSERT INTO local_thread_catalog "
                "(host_id,thread_id,source_kind,missing_candidate) "
                "VALUES(?,'second-chat','chatgpt',0)",
                (second,),
            )
        db.close()

        with self.assertRaisesRegex(SafetyError, "exactly one"):
            prepare_desktop_catalog_refresh(
                self.profile, "account-one", self.platform,
            )
        request = prepare_desktop_catalog_refresh(
            self.profile, "account-one", self.platform,
            host_token=_host_token(second),
        )
        self.assertEqual(request.host_id, second)
        db = sqlite3.connect(self.db_path)
        try:
            state = dict(db.execute(
                "SELECT host_id,last_full_reconciled_at FROM "
                "local_thread_catalog_sync_state",
            ))
            ids = {
                row[0] for row in db.execute(
                    "SELECT thread_id FROM local_thread_catalog",
                )
            }
        finally:
            db.close()
        self.assertEqual(state[self.host], 900)
        self.assertIsNone(state[second])
        self.assertEqual(ids, {"original-chat", "second-chat"})


if __name__ == "__main__":
    unittest.main()
