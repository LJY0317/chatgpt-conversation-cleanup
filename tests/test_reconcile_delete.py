from __future__ import annotations
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_cleanup.appserver import AppServerError
from chatgpt_cleanup.codex_delete import (
    execute_codex_delete_plan, preview_codex_delete, verified_rollout_path,
)
from chatgpt_cleanup.core import SafetyError, sessions
from chatgpt_cleanup.__main__ import delete_archived_cli, doctor_data, main
from chatgpt_cleanup.profiles import Profile

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
    def __init__(self, *, app_rows=None, delete_callback=None, fail_on=None, failures=None, calls=None):
        self.app_rows = list(app_rows or [])
        self.delete_callback = delete_callback
        self.fail_on = fail_on
        self.failures = failures or {}
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
        if thread_id in self.failures:
            raise self.failures[thread_id]
        if thread_id == self.fail_on:
            raise AppServerError("synthetic delete failure", code=-32600)
        if self.delete_callback is not None:
            self.delete_callback(thread_id)
        return {}




class CodexDeleteTests(unittest.TestCase):
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
            db.execute(
                "CREATE TABLE thread_spawn_edges("
                "parent_thread_id TEXT,child_thread_id TEXT,status TEXT)"
            )
        db.close()

    def add_thread(
        self, thread_id: str, *, archived: int,
        parent: str | None = None, spawn_parent: str | None = None,
    ):
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
            if spawn_parent is not None:
                db.execute(
                    "INSERT INTO thread_spawn_edges VALUES(?,?,?)",
                    (spawn_parent, thread_id, "open"),
                )
        db.close()

    def delete_db_row(self, thread_id: str):
        db = sqlite3.connect(self.root / "state_5.sqlite")
        pending = [thread_id]
        all_ids = set()
        while pending:
            target = pending.pop()
            if target in all_ids:
                continue
            all_ids.add(target)
            pending.extend(
                r[0] for r in db.execute(
                    "SELECT child_thread_id FROM thread_spawn_edges WHERE parent_thread_id=?",
                    (target,),
                )
            )
        with db:
            for target in all_ids:
                row = db.execute("SELECT rollout_path FROM threads WHERE id=?", (target,)).fetchone()
                if row:
                    Path(row[0]).unlink(missing_ok=True)
                    Path(row[0] + ".zst").unlink(missing_ok=True)
                db.execute("DELETE FROM threads WHERE id=?", (target,))
                db.execute(
                    "DELETE FROM thread_spawn_edges "
                    "WHERE parent_thread_id=? OR child_thread_id=?", (target, target),
                )
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
        self.add_thread("archived-child", archived=1, spawn_parent="archived-parent")
        compressed = self.compress_rollout("archived-child")
        before = compressed.read_bytes()
        original_path = self.root / "sessions" / "archived-child.jsonl"
        selected = [row for row in sessions(self.profile) if row["id"] == "archived-parent"]
        plan = preview_codex_delete(
            self.profile, selected, self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        self.assertEqual(plan.root_ids, ("archived-parent",))
        self.assertEqual(plan.affected_ids, {"archived-child", "archived-parent"})
        self.assertEqual(plan.dependent_ids, ("archived-child",))
        data = doctor_data(self.profile)
        self.assertEqual(data["archived_rollout_files_missing"], 0)
        self.assertEqual(compressed.read_bytes(), before)
        self.assertFalse(original_path.exists())

    def test_native_delete_is_not_reimplemented_by_parsing_compressed_headers(self):
        self.add_thread("thread-a", archived=1)
        self.add_thread("thread-b", archived=1)
        compressed = self.compress_rollout("thread-a")
        other = self.compress_rollout("thread-b")
        compressed.write_bytes(other.read_bytes())
        selected = [row for row in sessions(self.profile) if row["id"] == "thread-a"]
        plan = preview_codex_delete(
            self.profile, selected, self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        self.assertEqual(plan.root_ids, ("thread-a",))

    def test_corrupt_compressed_rollout_is_left_to_official_delete(self):
        self.add_thread("thread-a", archived=1)
        compressed = self.compress_rollout("thread-a")
        compressed.write_bytes(b"not a zstd frame")
        plan = preview_codex_delete(
            self.profile, sessions(self.profile), self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        self.assertEqual(plan.root_ids, ("thread-a",))
        server = FakeAppServer(fail_on="thread-a")
        with self.assertRaisesRegex(SafetyError, "synthetic delete failure"):
            execute_codex_delete_plan(
                self.profile, plan, self.platform,
                session_factory=self.factory(server),
            )
        self.assertEqual([row["id"] for row in sessions(self.profile)], ["thread-a"])

    def factory(self, server: FakeAppServer):
        return lambda *args, **kwargs: server

    def test_native_delete_one_parent_cascades_to_hidden_spawned_child(self):
        self.add_thread("parent", archived=1)
        self.add_thread("child", archived=1, spawn_parent="parent")
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
        self.assertEqual(plan.root_ids, ("parent",))
        self.assertEqual(plan.affected_ids, {"parent", "child"})

    def test_codex_delete_ignores_internal_paginated_history_base(self):
        self.add_thread("original", archived=1, parent="original")
        selected = sessions(self.profile)
        plan = preview_codex_delete(
            self.profile,
            selected,
            self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        self.assertEqual(plan.requested_ids, ("original",))
        self.assertEqual(plan.root_ids, ("original",))
        self.assertEqual(plan.dependent_ids, ())

    def test_codex_delete_blocks_active_dependent_child(self):
        self.add_thread("parent", archived=1)
        self.add_thread("child", archived=0, spawn_parent="parent")
        selected = [row for row in sessions(self.profile) if row["id"] == "parent"]
        plan = preview_codex_delete(
            self.profile, selected, self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        self.assertEqual(plan.root_ids, ())
        self.assertIn("parent", plan.protected)
        self.assertEqual(plan.affected_ids, frozenset())

    def test_codex_delete_rejects_nonarchived_requested_thread(self):
        self.add_thread("active", archived=0)
        with self.assertRaisesRegex(SafetyError, "selected Codex chat changed after review"):
            preview_codex_delete(
                self.profile,
                sessions(self.profile),
                self.platform,
                session_factory=self.factory(FakeAppServer()),
            )

    def test_codex_delete_protects_unknown_appserver_only_spawned_child(self):
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
        plan = preview_codex_delete(
            self.profile, selected, self.platform,
            session_factory=self.factory(server),
        )
        self.assertEqual(plan.root_ids, ())
        self.assertIn("parent", plan.protected)

    def test_codex_delete_delegates_spawned_cascade_to_one_native_request(self):
        self.add_thread("parent", archived=1)
        self.add_thread("child", archived=1, spawn_parent="parent")
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
        result = execute_codex_delete_plan(
            self.profile,
            preview,
            self.platform,
            session_factory=self.factory(server),
        )
        self.assertEqual(calls, ["parent"])
        self.assertEqual(set(result.deleted_ids), {"parent", "child"})
        self.assertEqual(result.native_calls, ("parent",))
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
        self.add_thread("new-active-child", archived=0, spawn_parent="parent")
        calls = []
        server = FakeAppServer(calls=calls, delete_callback=self.delete_db_row)
        with self.assertRaisesRegex(SafetyError, r"0 chat\(s\) were verified removed"):
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
        with self.assertRaisesRegex(SafetyError, r"0 chat\(s\) were verified removed"):
            execute_codex_delete_plan(
                self.profile,
                preview,
                self.platform,
                session_factory=self.factory(server),
            )
        self.assertEqual(calls, ["parent"])
        self.assertEqual(len(sessions(self.profile)), 1)

    def test_native_refusal_is_isolated_with_one_bounded_second_pass(self):
        self.add_thread("protected", archived=1)
        self.add_thread("free", archived=1)
        plan = preview_codex_delete(
            self.profile, [
                row for id in ("protected", "free")
                for row in sessions(self.profile) if row["id"] == id
            ], self.platform, session_factory=self.factory(FakeAppServer()),
        )
        calls = []
        server = FakeAppServer(
            calls=calls,
            failures={"protected": AppServerError(
                "cannot delete thread: forked history still references it", code=-32600,
            )},
            delete_callback=self.delete_db_row,
        )
        outcome = execute_codex_delete_plan(
            self.profile, plan, self.platform,
            session_factory=self.factory(server),
        )
        self.assertEqual(calls, ["protected", "free", "protected"])
        self.assertEqual(outcome.deleted_ids, ("free",))
        self.assertIn("protected", outcome.protected)
        self.assertEqual({r["id"] for r in sessions(self.profile)}, {"protected"})

    def test_reference_refusal_resolves_after_native_removes_archived_fork(self):
        self.add_thread("parent", archived=1)
        self.add_thread("fork", archived=1, parent="parent")
        plan = preview_codex_delete(
            self.profile, [
                row for id in ("parent", "fork")
                for row in sessions(self.profile) if row["id"] == id
            ], self.platform, session_factory=self.factory(FakeAppServer()),
        )
        test_case = self

        class NativeReferenceServer(FakeAppServer):
            def delete_thread(self, thread_id):
                if thread_id == "parent" and any(
                    r["id"] == "fork" for r in sessions(test_case.profile)
                ):
                    self.calls.append(thread_id)
                    raise AppServerError(
                        "cannot delete thread: forked history still references it",
                        code=-32600,
                    )
                super().delete_thread(thread_id)

        calls = []
        outcome = execute_codex_delete_plan(
            self.profile, plan, self.platform,
            session_factory=self.factory(
                NativeReferenceServer(calls=calls, delete_callback=self.delete_db_row)
            ),
        )
        self.assertEqual(calls, ["parent", "fork", "parent"])
        self.assertEqual(set(outcome.deleted_ids), {"parent", "fork"})
        self.assertEqual(outcome.protected, {})
        self.assertEqual(sessions(self.profile), [])

    def test_active_spawned_child_blocks_only_its_parent(self):
        self.add_thread("blocked", archived=1)
        self.add_thread("active-worker", archived=0, spawn_parent="blocked")
        self.add_thread("free", archived=1)
        chosen = [r for r in sessions(self.profile) if r["archived"] == 1]
        plan = preview_codex_delete(
            self.profile, chosen, self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        self.assertEqual(plan.root_ids, ("free",))
        self.assertIn("blocked", plan.protected)
        calls = []
        outcome = execute_codex_delete_plan(
            self.profile, plan, self.platform,
            session_factory=self.factory(
                FakeAppServer(calls=calls, delete_callback=self.delete_db_row)
            ),
        )
        self.assertEqual(calls, ["free"])
        self.assertEqual(outcome.deleted_ids, ("free",))
        self.assertEqual(
            {r["id"] for r in sessions(self.profile)}, {"blocked", "active-worker"},
        )

    def test_selected_spawned_child_uses_only_one_parent_native_call(self):
        self.add_thread("parent", archived=1)
        self.add_thread("child", archived=1, spawn_parent="parent")
        plan = preview_codex_delete(
            self.profile, [
                row for id in ("parent", "child")
                for row in sessions(self.profile) if row["id"] == id
            ], self.platform, session_factory=self.factory(FakeAppServer()),
        )
        self.assertEqual(plan.root_ids, ("parent",))
        calls = []
        outcome = execute_codex_delete_plan(
            self.profile, plan, self.platform,
            session_factory=self.factory(
                FakeAppServer(calls=calls, delete_callback=self.delete_db_row)
            ),
        )
        self.assertEqual(calls, ["parent"])
        self.assertEqual(set(outcome.deleted_ids), {"parent", "child"})

    def test_native_success_with_rollout_residue_is_not_claimed_clean(self):
        thread_id = "00000000-0000-0000-0000-000000000001"
        self.add_thread(thread_id, archived=1)
        old = self.root / "sessions" / (thread_id + ".jsonl")
        new = self.root / "archived_sessions" / (
            "rollout-2026-10-21T12-00-00-" + thread_id + ".jsonl"
        )
        new.parent.mkdir(exist_ok=True)
        old.rename(new)
        db = sqlite3.connect(self.root / "state_5.sqlite")
        with db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(new), thread_id))
        db.close()
        plan = preview_codex_delete(
            self.profile, sessions(self.profile), self.platform,
            session_factory=self.factory(FakeAppServer()),
        )

        def delete_only_row(target):
            db = sqlite3.connect(self.root / "state_5.sqlite")
            with db:
                db.execute("DELETE FROM threads WHERE id=?", (target,))
            db.close()

        with self.assertRaisesRegex(SafetyError, "left rollout/history records"):
            execute_codex_delete_plan(
                self.profile, plan, self.platform,
                session_factory=self.factory(
                    FakeAppServer(delete_callback=delete_only_row)
                ),
            )
        self.assertTrue(new.exists())

    def test_uncertain_native_timeout_is_never_retried(self):
        self.add_thread("target", archived=1)
        plan = preview_codex_delete(
            self.profile, sessions(self.profile), self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        calls = []
        with self.assertRaisesRegex(SafetyError, "timeout after request"):
            execute_codex_delete_plan(
                self.profile, plan, self.platform,
                session_factory=self.factory(
                    FakeAppServer(
                        calls=calls,
                        failures={"target": AppServerError("timeout after request")},
                    )
                ),
            )
        self.assertEqual(calls, ["target"])
        self.assertEqual(len(sessions(self.profile)), 1)

    def test_native_delete_succeeds_even_if_chatgpt_desktop_is_running(self):
        self.add_thread("archived", archived=1)
        plan = preview_codex_delete(
            self.profile, sessions(self.profile), self.platform,
            session_factory=self.factory(FakeAppServer()),
        )

        class LiveDesktopPlatform(DeletePlatform):
            def require_desktop_stopped(self):
                raise AssertionError("The UI should remain open during native thread/delete.")

            def require_profile_desktop_stopped(self, profile):
                raise AssertionError("A running profile must not be force-stopped.")

        calls = []
        outcome = execute_codex_delete_plan(
            self.profile, plan, LiveDesktopPlatform(self.home),
            session_factory=self.factory(
                FakeAppServer(calls=calls, delete_callback=self.delete_db_row)
            ),
        )
        self.assertEqual(outcome.deleted_ids, ("archived",))
        self.assertEqual(calls, ["archived"])

    def test_unrelated_live_thread_update_does_not_cancel_archived_delete(self):
        self.add_thread("archived", archived=1)
        self.add_thread("active", archived=0)
        plan = preview_codex_delete(
            self.profile,
            [r for r in sessions(self.profile) if r["archived"] == 1],
            self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        db = sqlite3.connect(self.root / "state_5.sqlite")
        with db:
            db.execute("UPDATE threads SET updated_at=22 WHERE id='active'")
        db.close()
        result = execute_codex_delete_plan(
            self.profile, plan, self.platform,
            session_factory=self.factory(FakeAppServer(delete_callback=self.delete_db_row)),
        )
        self.assertEqual(result.deleted_ids, ("archived",))
        self.assertEqual({r["id"] for r in sessions(self.profile)}, {"active"})

    def test_owned_native_writer_is_protected_without_retrying_or_stopping_other_chats(self):
        self.add_thread("writer-owned", archived=1)
        self.add_thread("free", archived=1)
        chosen = [
            r for name in ("writer-owned", "free")
            for r in sessions(self.profile) if r["id"] == name
        ]
        plan = preview_codex_delete(
            self.profile, chosen, self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        calls = []
        result = execute_codex_delete_plan(
            self.profile, plan, self.platform,
            session_factory=self.factory(
                FakeAppServer(
                    calls=calls,
                    failures={"writer-owned": AppServerError(
                        "thread writer-owned already has an active writer", code=-32600,
                    )},
                    delete_callback=self.delete_db_row,
                ),
            ),
        )
        self.assertEqual(calls, ["writer-owned", "free"])
        self.assertEqual(result.deleted_ids, ("free",))
        self.assertIn("writer-owned", result.protected)
        self.assertEqual(
            {r["id"] for r in sessions(self.profile)}, {"writer-owned"},
        )

    def test_native_history_projection_residue_is_reported_not_repaired(self):
        thread_id = "00000000-0000-0000-0000-000000000002"
        self.add_thread(thread_id, archived=1)
        old = self.root / "sessions" / (thread_id + ".jsonl")
        new = self.root / "archived_sessions" / (
            "rollout-2026-10-21T12-00-00-" + thread_id + ".jsonl"
        )
        new.parent.mkdir(exist_ok=True)
        old.rename(new)
        db = sqlite3.connect(self.root / "state_5.sqlite")
        with db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(new), thread_id))
        db.close()
        history = sqlite3.connect(self.root / "thread_history_1.sqlite")
        with history:
            history.execute("CREATE TABLE thread_history_projection_state(thread_id TEXT)")
            history.execute("CREATE TABLE thread_turns(thread_id TEXT)")
            history.execute("CREATE TABLE thread_items(thread_id TEXT)")
            history.execute(
                "INSERT INTO thread_history_projection_state VALUES (?)", (thread_id,),
            )
        history.close()
        plan = preview_codex_delete(
            self.profile, sessions(self.profile), self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        with self.assertRaisesRegex(SafetyError, "left rollout/history records"):
            execute_codex_delete_plan(
                self.profile, plan, self.platform,
                session_factory=self.factory(
                    FakeAppServer(delete_callback=self.delete_db_row),
                ),
            )
        history = sqlite3.connect(self.root / "thread_history_1.sqlite")
        self.assertEqual(
            history.execute("SELECT count(*) FROM thread_history_projection_state").fetchone()[0],
            1,
        )
        history.close()

    def test_unexpected_native_deletion_of_unselected_active_thread_stops(self):
        self.add_thread("archived", archived=1)
        self.add_thread("active", archived=0)
        plan = preview_codex_delete(
            self.profile,
            [r for r in sessions(self.profile) if r["archived"] == 1],
            self.platform,
            session_factory=self.factory(FakeAppServer()),
        )

        def wrong_native_delete(thread_id):
            self.delete_db_row(thread_id)
            self.delete_db_row("active")

        with self.assertRaisesRegex(SafetyError, "unexpected set of threads"):
            execute_codex_delete_plan(
                self.profile, plan, self.platform,
                session_factory=self.factory(
                    FakeAppServer(delete_callback=wrong_native_delete)
                ),
            )

    def test_native_refusal_with_unexpected_data_change_stops_batch(self):
        self.add_thread("protected", archived=1)
        self.add_thread("free", archived=1)
        plan = preview_codex_delete(
            self.profile, [
                r for id in ("protected", "free")
                for r in sessions(self.profile) if r["id"] == id
            ], self.platform,
            session_factory=self.factory(FakeAppServer()),
        )
        test_case = self

        class BrokenNativeRefusal(FakeAppServer):
            def delete_thread(self, thread_id):
                self.calls.append(thread_id)
                if thread_id == "protected":
                    test_case.delete_db_row(thread_id)
                    raise AppServerError(
                        "cannot delete thread: forked history still references it",
                        code=-32600,
                    )
                test_case.delete_db_row(thread_id)

        calls = []
        with self.assertRaisesRegex(SafetyError, "refused deletion but local state changed"):
            execute_codex_delete_plan(
                self.profile, plan, self.platform,
                session_factory=self.factory(BrokenNativeRefusal(calls=calls)),
            )
        self.assertEqual(calls, ["protected"])
        self.assertEqual({r["id"] for r in sessions(self.profile)}, {"free"})

    def test_agent_delete_cli_previews_then_requires_exact_confirmation(self):
        self.add_thread("archived", archived=1)
        calls = []
        fake = FakeAppServer(calls=calls, delete_callback=self.delete_db_row)

        def preview(profile, selected, platform):
            return preview_codex_delete(
                profile, selected, platform,
                session_factory=self.factory(FakeAppServer()),
            )

        def execute(profile, plan, platform):
            return execute_codex_delete_plan(
                profile, plan, platform, session_factory=self.factory(fake),
            )

        output = io.StringIO()
        with (
            patch("chatgpt_cleanup.__main__.preview_codex_delete", side_effect=preview),
            patch("chatgpt_cleanup.__main__.execute_codex_delete_plan", side_effect=execute),
            patch("chatgpt_cleanup.__main__.recovery_root", return_value=self.root),
            patch("chatgpt_cleanup.__main__.operation_lock", return_value=contextlib.nullcontext()),
            contextlib.redirect_stdout(output),
        ):
            delete_archived_cli(self.profile, self.platform, ids="archived")
            self.assertEqual(calls, [])
            self.assertEqual(len(sessions(self.profile)), 1)
            with self.assertRaisesRegex(SafetyError, "Confirmation does not match"):
                delete_archived_cli(
                    self.profile, self.platform, ids="archived", confirm="DELETE 9 CHATS",
                )
            self.assertEqual(calls, [])
            delete_archived_cli(
                self.profile, self.platform, ids="archived", confirm="DELETE 1 CHATS",
            )
        self.assertIn("Preview only", output.getvalue())
        self.assertEqual(calls, ["archived"])
        self.assertEqual(sessions(self.profile), [])

    def test_agent_delete_cli_rejects_unselected_and_duplicate_ids(self):
        self.add_thread("archived", archived=1)
        self.add_thread("active", archived=0)
        for ids in ("", "missing", "active", "archived,archived", "archived,active"):
            with self.subTest(ids=ids):
                with self.assertRaisesRegex(SafetyError, "Every --ids value"):
                    delete_archived_cli(self.profile, self.platform, ids=ids)
        self.assertEqual(len(sessions(self.profile)), 2)

    def test_agent_cli_can_parse_exact_profile_without_interactive_terminal(self):
        with (
            patch("sys.argv", [
                "run.py", "delete-archived-codex", "--profile", "default",
                "--ids", "archived",
            ]),
            patch("chatgpt_cleanup.__main__.current_platform", return_value=self.platform),
            patch("chatgpt_cleanup.__main__.discover_profiles", return_value=[self.profile]),
            patch("chatgpt_cleanup.__main__.delete_archived_cli") as delete,
        ):
            main()
            delete.assert_called_once_with(
                self.profile, self.platform, ids="archived", confirm=None,
            )



if __name__ == "__main__":
    unittest.main()
