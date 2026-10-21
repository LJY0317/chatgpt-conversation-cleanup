"""Agent routing and native Desktop list-refresh, using synthetic profiles."""

import contextlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_cleanup import core
from chatgpt_cleanup.__main__ import (
    _refresh_preview, doctor_data, list_archived, main, refresh_cli,
)
from chatgpt_cleanup.core import SafetyError
from chatgpt_cleanup.desktop_refresh import _host_token
from chatgpt_cleanup.desktop_refresh import (
    DesktopCatalogRefreshRequest, DesktopCatalogRefreshResult,
)
from chatgpt_cleanup.platforms import MacOSAdapter
from chatgpt_cleanup.profiles import Profile, ProfileIdentity


class FakePlatform:
    def __init__(self, home):
        self.home = home
        self.recovery_root = home / "recovery"
        self.data_root = home / "tool-data"

    def catalog_write_gate(self):
        return True, "synthetic verified Desktop"

    def profile_catalog_refresh_gate(self, profile):
        return True, "synthetic verified refresh"

    def launch_profile_desktop(self, profile):
        return True, "synthetic original Desktop"


class CLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / ".codex"
        (self.root / "sqlite").mkdir(parents=True)
        self.platform = FakePlatform(self.home)
        self.profile = Profile("default", "ChatGPT", self.root, "official")
        self.hosts = (
            "chatgpt:account-one:user-one",
            "chatgpt:account-one:user-two",
        )
        self._dbs()

    def _dbs(self):
        db = sqlite3.connect(self.root / "state_5.sqlite")
        with db:
            db.execute(
                "CREATE TABLE threads("
                "id TEXT,title TEXT,archived INTEGER,updated_at INTEGER,rollout_path TEXT)"
            )
            db.execute(
                "INSERT INTO threads VALUES(?,?,?,?,?)",
                ("archived-id", "archived title", 1, 1, "unused"),
            )
        db.close()
        db = sqlite3.connect(self.root / "sqlite/codex-dev.db")
        with db:
            cols = []
            for name in sorted(core.CATALOG_COLUMNS | {"chatgpt_async_status"}):
                typ = "INTEGER" if name in (
                    "observation_sequence", "missing_candidate", "chatgpt_async_status",
                ) else "TEXT"
                cols.append(f'"{name}" {typ}')
            db.execute("CREATE TABLE local_thread_catalog(" + ",".join(cols) + ")")
            db.executescript(
                "CREATE TABLE local_thread_catalog_hosts(host_id TEXT,host_kind TEXT);"
                "CREATE TABLE local_thread_catalog_metadata(id INTEGER,catalog_revision INTEGER);"
                "CREATE TABLE local_thread_catalog_scan_entries("
                "host_id TEXT,thread_id TEXT,removed INTEGER);"
                "CREATE TABLE local_thread_catalog_scan_checkpoints("
                "host_id TEXT,checkpoint TEXT,failed_at INTEGER);"
                "CREATE TABLE local_thread_catalog_sync_state("
                "host_id TEXT,watermark_updated_at REAL,initial_build_complete INTEGER,"
                "observation_sequence INTEGER,last_full_reconciled_at INTEGER);"
                "PRAGMA user_version=34;"
            )
            db.execute("INSERT INTO local_thread_catalog_metadata VALUES(1,1)")
            for i, host in enumerate(self.hosts):
                db.execute(
                    "INSERT INTO local_thread_catalog_hosts VALUES(?,'chatgpt')", (host,),
                )
                db.execute(
                    "INSERT INTO local_thread_catalog_sync_state VALUES(?,1,1,3,100)",
                    (host,),
                )
                db.execute(
                    "INSERT INTO local_thread_catalog "
                    "(host_id,thread_id,source_kind,missing_candidate) "
                    "VALUES(?,?,'chatgpt',0)",
                    (host, f"chat-{i}"),
                )
        db.close()

    def test_doctor_and_archive_listing_are_read_only(self):
        baseline = (self.root / "state_5.sqlite").read_bytes()
        info = doctor_data(self.profile)
        self.assertEqual(info["archived_conversations"], 1)
        self.assertEqual(info["desktop_catalog_chats"], 2)
        self.assertEqual(list_archived(self.profile)[0]["id"], "archived-id")
        self.assertEqual(baseline, (self.root / "state_5.sqlite").read_bytes())

    def test_exact_natural_language_routes_have_distinct_cli_commands(self):
        with (
            patch("chatgpt_cleanup.__main__.current_platform", return_value=self.platform),
            patch("chatgpt_cleanup.__main__.discover_profiles", return_value=[self.profile]),
            patch("chatgpt_cleanup.__main__.refresh_cli") as refresh,
            patch("chatgpt_cleanup.__main__.delete_archived_cli") as delete,
        ):
            main(["repair-chat-list", "--profile", "default"])
            refresh.assert_called_once_with(
                self.profile, self.platform, confirm=None, host_token=None,
            )
            delete.assert_not_called()
            refresh.reset_mock()
            main([
                "delete-archived-codex", "--profile", "default",
                "--ids", "archived-id",
            ])
            delete.assert_called_once_with(
                self.profile, self.platform, ids="archived-id", confirm=None,
            )
            refresh.assert_not_called()

    def test_unspecified_command_never_guesses_a_deletion(self):
        with (
            patch("chatgpt_cleanup.__main__.current_platform", return_value=self.platform),
            patch("chatgpt_cleanup.__main__.discover_profiles", return_value=[self.profile]),
            patch("chatgpt_cleanup.__main__.refresh_cli") as refresh,
            patch("chatgpt_cleanup.__main__.delete_archived_cli") as delete,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            main([])
            refresh.assert_not_called()
            delete.assert_not_called()
            with self.assertRaisesRegex(SafetyError, "valid only for"):
                main([
                    "repair-chat-list", "--profile", "default",
                    "--ids", "archived-id",
                ])
            with self.assertRaisesRegex(SafetyError, "valid only for"):
                main([
                    "delete-archived-codex", "--profile", "default",
                    "--ids", "archived-id", "--host-token", "unknown",
                ])
            with self.assertRaisesRegex(SafetyError, "one explicit --profile"):
                main(["repair-chat-list"])

    def test_current_signed_build_refresh_gate_is_explicit(self):
        platform = MacOSAdapter(home=self.home)
        with patch.object(
            MacOSAdapter, "app_build", return_value=("26.1016.51333", "21161"),
        ):
            self.assertTrue(platform.catalog_refresh_gate()[0])
        with patch.object(
            MacOSAdapter, "app_build", return_value=("99.999", "999999"),
        ):
            self.assertFalse(platform.catalog_refresh_gate()[0])

    def test_ambiguous_hosts_are_never_guessed(self):
        identity = ProfileIdentity(workspace_account_id="account-one")
        out = io.StringIO()
        with (
            patch("chatgpt_cleanup.__main__.resolve_identity", return_value=identity),
            contextlib.redirect_stdout(out),
        ):
            selected = _refresh_preview(self.profile, self.platform)
        self.assertIsNone(selected)
        for host in self.hosts:
            self.assertIn(_host_token(host), out.getvalue())
        self.assertIn("do not guess", out.getvalue().lower())

    def test_explicit_opaque_host_selection_is_scoped(self):
        identity = ProfileIdentity(workspace_account_id="account-one")
        token = _host_token(self.hosts[1])
        with patch("chatgpt_cleanup.__main__.resolve_identity", return_value=identity):
            preview = _refresh_preview(self.profile, self.platform, host_token=token)
            self.assertEqual(preview[1], self.hosts[1])
            self.assertEqual(preview[2], {"chat-1"})
            with self.assertRaisesRegex(SafetyError, "exactly one"):
                _refresh_preview(self.profile, self.platform, host_token="wrong-token")

    def test_refresh_never_runs_without_exact_confirm_and_host(self):
        identity = ProfileIdentity(workspace_account_id="account-one")
        out = io.StringIO()
        with (
            patch("chatgpt_cleanup.__main__.resolve_identity", return_value=identity),
            patch("chatgpt_cleanup.__main__.capture_initial_desktops") as capture,
            patch("chatgpt_cleanup.__main__.prepare_desktop_catalog_refresh") as prepare,
            contextlib.redirect_stdout(out),
        ):
            refresh_cli(self.profile, self.platform, confirm=None)
            token = _host_token(self.hosts[0])
            refresh_cli(self.profile, self.platform, confirm=None, host_token=token)
            with self.assertRaisesRegex(SafetyError, "Confirmation does not match"):
                refresh_cli(
                    self.profile, self.platform, confirm="REFRESH default WRONG",
                    host_token=token,
                )
        self.assertIn("does NOT delete any live server-side chats", out.getvalue())
        capture.assert_not_called()
        prepare.assert_not_called()

    def test_approved_refresh_delegates_reconciliation_to_desktop(self):
        from dataclasses import replace

        token = _host_token(self.hosts[0])
        scoped = replace(
            self.profile,
            identity=ProfileIdentity(workspace_account_id="account-one"),
        )
        request = DesktopCatalogRefreshRequest(
            profile_id="default", host_id=self.hosts[0], requested_at_ms=100,
            starting_observation_sequence=2, previous_last_full_reconciled_at=90,
        )
        result = DesktopCatalogRefreshResult(
            host_id=self.hosts[0], completed_at_ms=101,
            observation_sequence=3,
            present_ids=frozenset(), missing_ids=frozenset(),
        )
        with (
            patch(
                "chatgpt_cleanup.__main__._refresh_preview",
                return_value=(scoped, self.hosts[0], {"already-server-deleted"}),
            ),
            patch(
                "chatgpt_cleanup.__main__.capture_initial_desktops",
                return_value={"default": "standalone"},
            ),
            patch(
                "chatgpt_cleanup.__main__.wait_for_desktop_to_close",
                return_value=True,
            ) as close,
            patch(
                "chatgpt_cleanup.__main__.prepare_desktop_catalog_refresh",
                return_value=request,
            ) as schedule,
            patch(
                "chatgpt_cleanup.__main__.desktop_catalog_refresh_result",
                return_value=result,
            ),
            patch("chatgpt_cleanup.__main__.chatgpt_catalog", return_value=[]),
            patch("chatgpt_cleanup.__main__.restore_initial_desktops") as restore,
            patch(
                "chatgpt_cleanup.__main__.operation_lock",
                return_value=contextlib.nullcontext(),
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            refresh_cli(
                self.profile, self.platform,
                confirm=f"REFRESH default {token}",
                host_token=token,
            )
        close.assert_called_once()
        schedule.assert_called_once_with(
            scoped, "account-one", self.platform, host_token=token,
        )
        restore.assert_called_once()
        self.assertIn("removed by Desktop", output.getvalue())
        self.assertEqual(len(core.chatgpt_catalog(self.profile)), 2)

    def test_catalog_database_never_accepts_unverified_schema(self):
        path = self.root / "sqlite/codex-dev.db"
        with core.database(path, self.root) as conn:
            self.assertIn("chatgpt_async_status", core.write_schema(conn))
        db = sqlite3.connect(path)
        with db:
            db.execute("ALTER TABLE local_thread_catalog ADD COLUMN unexpected TEXT")
        db.close()
        with core.database(path, self.root) as conn:
            with self.assertRaisesRegex(SafetyError, "verified Desktop layout"):
                core.write_schema(conn)

    def test_symlinked_original_database_is_not_followed(self):
        target = self.root / "state_5.sqlite"
        aliased = self.root / "data"
        try:
            aliased.symlink_to(target)
        except OSError:
            self.skipTest("This Windows environment does not allow symlink creation.")
        with self.assertRaisesRegex(SafetyError, "symlinked"):
            with core.database(aliased, self.root):
                pass


if __name__ == "__main__":
    unittest.main()
