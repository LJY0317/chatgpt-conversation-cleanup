from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_cleanup.appserver import AppServerError
from chatgpt_cleanup.codex_delete import (
    execute_codex_delete_plan,
    preview_codex_delete,
    verified_rollout_path,
)
from chatgpt_cleanup.core import SafetyError, sessions
from chatgpt_cleanup.desktop_probe import Presence
from chatgpt_cleanup.evidence import DesktopFailureEvidence
from chatgpt_cleanup.__main__ import choose_cleanup_assessments, doctor_data
from chatgpt_cleanup.__main__ import _parse_number_selection
from chatgpt_cleanup.profiles import Profile
from chatgpt_cleanup.reconcile import ReconcileStatus, assess_catalog_rows


class DeletePlatform:
    key = "test"

    def __init__(self, home: Path):
        self.home = home

    def require_desktop_stopped(self):
        return None

    def catalog_write_gate(self):
        return False, "synthetic read-only test"

    def profile_catalog_refresh_gate(self, profile):
        return False, "synthetic read-only test"


class FakeAppServer:
    def __init__(self, *, app_rows=None, delete_callback=None, fail_on=None, calls=None):
        self.app_rows = list(app_rows or [])
        self.delete_callback = delete_callback
        self.fail_on = fail_on
        self.calls = calls if calls is not None else []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def list_threads(self, *, archived, cursor=None, **kwargs):
        rows = [row for row in self.app_rows if bool(row.get("archived")) == archived]
        return {"data": rows, "nextCursor": None}

    def delete_thread(self, thread_id):
        self.calls.append(thread_id)
        if thread_id == self.fail_on:
            raise AppServerError("synthetic delete failure", code=-32600)
        if self.delete_callback is not None:
            self.delete_callback(thread_id)
        return {}


class ReconcileAndDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / ".codex"
        self.root.mkdir()
        self.profile = Profile("default", "ChatGPT", self.root, "official")
        self.platform = DeletePlatform(self.home)
        db = sqlite3.connect(self.root / "state_5.sqlite")
        with db:
            db.execute(
                "CREATE TABLE threads("
                "id TEXT PRIMARY KEY,title TEXT,archived INTEGER,updated_at INTEGER,"
                "rollout_path TEXT)"
            )
        db.close()

    def add_thread(self, thread_id: str, *, archived: int, parent: str | None = None):
        rollout = self.root / "sessions" / f"{thread_id}.jsonl"
        rollout.parent.mkdir(exist_ok=True)
        payload = {"id": thread_id}
        if parent:
            payload["history_base"] = {"thread_id": parent}
        rollout.write_text(
            json.dumps({"type": "session_meta", "payload": payload}) + "\n",
            encoding="utf-8",
        )
        db = sqlite3.connect(self.root / "state_5.sqlite")
        with db:
            db.execute(
                "INSERT INTO threads VALUES(?,?,?,?,?)",
                (thread_id, thread_id, archived, 1, str(rollout)),
            )
        db.close()

    def delete_db_row(self, thread_id: str):
        db = sqlite3.connect(self.root / "state_5.sqlite")
        with db:
            db.execute("DELETE FROM threads WHERE id=?", (thread_id,))
        db.close()

    def compress_rollout(self, thread_id: str) -> Path:
        rollout = self.root / "sessions" / f"{thread_id}.jsonl"
        try:
            from compression import zstd
            compressed = zstd.compress(rollout.read_bytes())
        except ImportError:
            try:
                import zstandard
            except ImportError:
                self.skipTest("Zstandard is unavailable on this interpreter")
            compressed = zstandard.ZstdCompressor().compress(rollout.read_bytes())
        dest = Path(str(rollout) + ".zst")
        dest.write_bytes(compressed)
        rollout.unlink()
        return dest

    def test_compressed_archive_header_and_local_inventory_are_read_only(self):
        self.add_thread("archived-parent", archived=1)
        self.add_thread("archived-child", archived=1, parent="archived-parent")
        compressed = self.compress_rollout("archived-child")
        before = compressed.read_bytes()
        original_path = self.root / "sessions" / "archived-child.jsonl"
        selected = [row for row in sessions(self.profile) if row["id"] == "archived-parent"]
        plan = preview_codex_delete(
            self.profile, selected, self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        self.assertEqual(plan.ordered_ids, ("archived-child", "archived-parent"))
        data = doctor_data(self.profile, self.platform)
        self.assertEqual(data["archived_rollout_files_found"], 2)
        self.assertEqual(data["archived_compressed_rollouts"], 1)
        self.assertEqual(data["archived_rollout_files_missing"], 0)
        self.assertEqual(compressed.read_bytes(), before)
        self.assertFalse(original_path.exists())

    def test_compressed_rollout_rejects_wrong_session_identity(self):
        self.add_thread("thread-a", archived=1)
        self.add_thread("thread-b", archived=1)
        compressed = self.compress_rollout("thread-a")
        other = self.compress_rollout("thread-b")
        compressed.write_bytes(other.read_bytes())
        selected = [row for row in sessions(self.profile) if row["id"] == "thread-a"]
        with self.assertRaisesRegex(SafetyError, "does not belong to the selected thread"):
            preview_codex_delete(
                self.profile, selected, self.platform,
                session_factory=self.factory(FakeAppServer()),
            )

    def test_compressed_rollout_never_guesses_uuid_matches_elsewhere(self):
        self.add_thread("thread-a", archived=1)
        original = self.root / "sessions" / "thread-a.jsonl"
        compressed = self.compress_rollout("thread-a")
        compressed.rename(self.root / "sessions" / "different-name.jsonl.zst")
        row = sessions(self.profile)[0]
        with self.assertRaisesRegex(SafetyError, "rollout path could not be verified"):
            verified_rollout_path(self.profile, row)

    def test_corrupt_compressed_rollout_stops_planning(self):
        self.add_thread("thread-a", archived=1)
        compressed = self.compress_rollout("thread-a")
        compressed.write_bytes(b"not a zstd frame")
        with self.assertRaisesRegex(SafetyError, "header could not be read"):
            preview_codex_delete(
                self.profile, sessions(self.profile), self.platform,
                session_factory=self.factory(FakeAppServer()),
            )

    def factory(self, server: FakeAppServer):
        return lambda *args, **kwargs: server

    def test_reconcile_keeps_unknown_rows_visible_without_live_provider(self):
        rows = [
            {"thread_id": "confirmed", "display_title": "Confirmed", "missing_candidate": 0},
            {"thread_id": "suspected", "display_title": "Suspected", "missing_candidate": 1},
            {"thread_id": "unknown", "display_title": "Unknown", "missing_candidate": 0},
            {"thread_id": "protected", "display_title": "Protected", "missing_candidate": 0},
        ]
        observed = datetime(2026, 9, 28, tzinfo=timezone.utc)
        failures = {
            "confirmed": DesktopFailureEvidence(
                "confirmed", "conversation_deleted", 404, observed
            ),
            "protected": DesktopFailureEvidence(
                "protected", "conversation_inaccessible", 404, observed
            ),
        }
        assessed = assess_catalog_rows(rows, failures, None)
        self.assertEqual(
            [item.status for item in assessed],
            [
                ReconcileStatus.CONFIRMED,
                ReconcileStatus.SUSPECTED,
                ReconcileStatus.UNKNOWN,
                ReconcileStatus.PROTECTED,
            ],
        )

        output = io.StringIO()
        with (
            patch("builtins.input", side_effect=["r", "2-4", "2-3"]),
            contextlib.redirect_stdout(output),
        ):
            selected = choose_cleanup_assessments(assessed)
        self.assertEqual(
            [row["thread_id"] for row in selected],
            ["suspected", "unknown"],
        )
        self.assertIn("Unknown", output.getvalue())
        self.assertIn("Protected", output.getvalue())
        self.assertIn("Choose only unprotected entry numbers/ranges", output.getvalue())

    def test_manual_review_number_ranges_are_explicit_and_bounded(self):
        self.assertEqual(_parse_number_selection("1-3,5", 6), [0, 1, 2, 4])
        self.assertEqual(_parse_number_selection("2", 3), [1])
        self.assertIsNone(_parse_number_selection("0-2", 5))
        self.assertIsNone(_parse_number_selection("3-1", 5))
        self.assertIsNone(_parse_number_selection("1-3,3", 5))
        self.assertIsNone(_parse_number_selection("1-9", 5))

    def test_possible_leftover_titles_preview_without_auto_selection(self):
        rows = [
            {"thread_id": "possible", "display_title": "의심되는 대화", "missing_candidate": 1},
            {"thread_id": "unknown", "display_title": "확인되지 않은 대화", "missing_candidate": 0},
        ]
        assessed = assess_catalog_rows(rows, {}, None)
        output = io.StringIO()
        with patch("builtins.input", return_value="c"), contextlib.redirect_stdout(output):
            self.assertEqual(choose_cleanup_assessments(assessed), [])
        self.assertIn("의심되는 대화", output.getvalue())
        self.assertIn("none selected", output.getvalue())
        self.assertNotIn("확인되지 않은 대화", output.getvalue())
        with patch("builtins.input", side_effect=["r", "1"]), contextlib.redirect_stdout(io.StringIO()):
            selected = choose_cleanup_assessments(assessed)
        self.assertEqual([row["thread_id"] for row in selected], ["possible"])

    def test_reconcile_live_present_and_inaccessible_are_protected(self):
        rows = [
            {"thread_id": "present", "display_title": "Present", "missing_candidate": 1},
            {"thread_id": "access", "display_title": "Access", "missing_candidate": 1},
        ]
        observed = datetime(2026, 9, 28, tzinfo=timezone.utc)
        failures = {
            "access": DesktopFailureEvidence(
                "access", "conversation_inaccessible", 404, observed
            )
        }
        assessed = assess_catalog_rows(
            rows,
            failures,
            {"present": Presence.PRESENT, "access": Presence.MISSING},
        )
        self.assertEqual(
            [item.status for item in assessed],
            [ReconcileStatus.PRESENT, ReconcileStatus.PROTECTED],
        )
        self.assertTrue(all(not item.selectable for item in assessed))

    def test_codex_delete_plan_includes_hidden_archived_fork_child_first(self):
        self.add_thread("parent", archived=1)
        self.add_thread("child", archived=1, parent="parent")
        selected = [row for row in sessions(self.profile) if row["id"] == "parent"]
        server = FakeAppServer(app_rows=[])
        plan = preview_codex_delete(
            self.profile,
            selected,
            self.platform,
            session_factory=self.factory(server),
        )
        self.assertEqual(plan.requested_ids, ("parent",))
        self.assertEqual(plan.dependent_ids, ("child",))
        self.assertEqual(plan.ordered_ids, ("child", "parent"))

    def test_codex_delete_blocks_active_dependent_child(self):
        self.add_thread("parent", archived=1)
        self.add_thread("child", archived=0, parent="parent")
        selected = [row for row in sessions(self.profile) if row["id"] == "parent"]
        with self.assertRaisesRegex(SafetyError, "active Codex child/fork"):
            preview_codex_delete(
                self.profile,
                selected,
                self.platform,
                session_factory=self.factory(FakeAppServer()),
            )

    def test_codex_delete_rejects_nonarchived_requested_thread(self):
        self.add_thread("active", archived=0)
        with self.assertRaisesRegex(SafetyError, "Only archived Codex chats"):
            preview_codex_delete(
                self.profile,
                sessions(self.profile),
                self.platform,
                session_factory=self.factory(FakeAppServer()),
            )

    def test_codex_delete_uses_appserver_parent_metadata_when_rollout_has_none(self):
        self.add_thread("parent", archived=1)
        self.add_thread("child", archived=1)
        selected = [row for row in sessions(self.profile) if row["id"] == "parent"]
        server = FakeAppServer(
            app_rows=[
                {"id": "parent", "archived": True},
                {
                    "id": "child",
                    "archived": True,
                    "parentThreadId": "parent",
                    "forkedFromId": None,
                },
            ]
        )
        plan = preview_codex_delete(
            self.profile,
            selected,
            self.platform,
            session_factory=self.factory(server),
        )
        self.assertEqual(plan.ordered_ids, ("child", "parent"))

    def test_codex_delete_blocks_appserver_only_dependent_child(self):
        self.add_thread("parent", archived=1)
        selected = sessions(self.profile)
        server = FakeAppServer(
            app_rows=[
                {"id": "parent", "archived": True},
                {
                    "id": "hidden-child",
                    "archived": True,
                    "parentThreadId": "parent",
                },
            ]
        )
        with self.assertRaisesRegex(SafetyError, "missing from the local database snapshot"):
            preview_codex_delete(
                self.profile,
                selected,
                self.platform,
                session_factory=self.factory(server),
            )

    def test_codex_delete_blocks_when_rollout_lineage_cannot_be_read(self):
        self.add_thread("parent", archived=1)
        self.add_thread("other", archived=1)
        db = sqlite3.connect(self.root / "state_5.sqlite")
        with db:
            db.execute(
                "UPDATE threads SET rollout_path=? WHERE id='other'",
                (str(self.root / "sessions" / "missing.jsonl"),),
            )
        db.close()
        selected = [row for row in sessions(self.profile) if row["id"] == "parent"]
        with self.assertRaisesRegex(SafetyError, "rollout path could not be verified"):
            preview_codex_delete(
                self.profile,
                selected,
                self.platform,
                session_factory=self.factory(FakeAppServer()),
            )

    def test_codex_delete_appserver_active_child_overrides_stale_archived_db_flag(self):
        self.add_thread("parent", archived=1)
        self.add_thread("child", archived=1, parent="parent")
        selected = [row for row in sessions(self.profile) if row["id"] == "parent"]
        server = FakeAppServer(
            app_rows=[
                {"id": "parent", "archived": True},
                {"id": "child", "archived": False, "parentThreadId": "parent"},
            ]
        )
        with self.assertRaisesRegex(SafetyError, "active Codex child/fork"):
            preview_codex_delete(
                self.profile,
                selected,
                self.platform,
                session_factory=self.factory(server),
            )

    def test_codex_delete_executes_child_first_and_verifies_each_row(self):
        self.add_thread("parent", archived=1)
        self.add_thread("child", archived=1, parent="parent")
        selected = [row for row in sessions(self.profile) if row["id"] == "parent"]
        preview = preview_codex_delete(
            self.profile,
            selected,
            self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        calls = []
        server = FakeAppServer(
            calls=calls,
            delete_callback=self.delete_db_row,
        )
        execute_codex_delete_plan(
            self.profile,
            preview,
            self.platform,
            session_factory=self.factory(server),
        )
        self.assertEqual(calls, ["child", "parent"])
        self.assertEqual(sessions(self.profile), [])

    def test_codex_delete_revalidates_dependencies_before_first_delete(self):
        self.add_thread("parent", archived=1)
        selected = sessions(self.profile)
        preview = preview_codex_delete(
            self.profile,
            selected,
            self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        self.add_thread("new-active-child", archived=0, parent="parent")
        calls = []
        server = FakeAppServer(calls=calls, delete_callback=self.delete_db_row)
        with self.assertRaisesRegex(SafetyError, "0 of 1 planned chat"):
            execute_codex_delete_plan(
                self.profile,
                preview,
                self.platform,
                session_factory=self.factory(server),
            )
        self.assertEqual(calls, [])
        self.assertEqual({row["id"] for row in sessions(self.profile)}, {"parent", "new-active-child"})

    def test_codex_delete_failure_is_not_retried(self):
        self.add_thread("parent", archived=1)
        selected = sessions(self.profile)
        preview = preview_codex_delete(
            self.profile,
            selected,
            self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        calls = []
        server = FakeAppServer(calls=calls, fail_on="parent")
        with self.assertRaisesRegex(SafetyError, "0 of 1 planned chat"):
            execute_codex_delete_plan(
                self.profile,
                preview,
                self.platform,
                session_factory=self.factory(server),
            )
        self.assertEqual(calls, ["parent"])
        self.assertEqual(len(sessions(self.profile)), 1)


if __name__ == "__main__":
    unittest.main()
