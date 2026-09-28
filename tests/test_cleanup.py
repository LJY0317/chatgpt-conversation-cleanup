import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from chatgpt_cleanup import core as c
from chatgpt_cleanup.__main__ import (
    _cleanup_candidates,
    _cleanup_candidates_batch,
    _collect_work,
    _wait_for_desktop_to_close,
    choose,
    display,
    doctor_data,
    interactive,
    select_profiles,
)
from chatgpt_cleanup.desktop_probe import Presence
from chatgpt_cleanup.evidence import DesktopFailureEvidence
from chatgpt_cleanup.identity import _identity_from_account_response
from chatgpt_cleanup.locking import operation_lock
from chatgpt_cleanup.platforms import MacOSAdapter, WindowsAdapter
from chatgpt_cleanup.profiles import Profile, ProfileIdentity, discover_profiles


@contextlib.contextmanager
def fixture_db(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


class FakePlatform:
    key = "test"
    display_name = "Test"

    def __init__(self, home, codex):
        self.home = home
        self._codex = codex
        self.recovery_root = home / "recovery"

    def codex_cli(self):
        return self._codex

    def require_desktop_stopped(self):
        return None

    def catalog_write_gate(self):
        return True, "test fixture"


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / ".codex"
        self.root.mkdir()
        self.rec = self.home / "recovery"
        self.rec.mkdir()
        self.codex = self.home / "codex"
        self.codex.write_text("#!/bin/sh\n")
        self.platform = FakePlatform(self.home, self.codex)
        self.profile = Profile("default", "ChatGPT", self.root, "official")
        self.native_id = str(uuid.uuid4())
        with fixture_db(self.root / "state_5.sqlite") as db:
            db.execute(
                "CREATE TABLE threads("
                "id TEXT PRIMARY KEY,title TEXT,archived INTEGER,updated_at INTEGER,"
                "rollout_path TEXT,creator_user_id TEXT,creator_account_id TEXT)"
            )
            db.execute(
                "INSERT INTO threads VALUES (?,?,?,?,?,?,?)",
                (self.native_id, "Keep native", 1, 1, "unused", "user-test", "account-test"),
            )
        (self.root / "sqlite").mkdir()
        with fixture_db(self.root / "sqlite/codex-dev.db") as db:
            cols = ",".join(
                f'"{name}" '
                + (
                    "INTEGER"
                    if name in ("observation_sequence", "missing_candidate", "pending_observed_title")
                    else "TEXT"
                )
                for name in sorted(c.CATALOG_COLUMNS)
            )
            db.execute(
                "CREATE TABLE local_thread_catalog("
                + cols
                + ",PRIMARY KEY(host_id,thread_id))"
            )
            db.execute(
                "CREATE TABLE local_thread_catalog_hosts(host_id TEXT PRIMARY KEY,host_kind TEXT)"
            )
            db.execute(
                "CREATE TABLE local_thread_catalog_metadata(id INTEGER PRIMARY KEY,catalog_revision INTEGER)"
            )
            db.execute("INSERT INTO local_thread_catalog_metadata VALUES(1,3)")
            db.execute(
                "CREATE TABLE local_thread_catalog_scan_entries("
                "host_id TEXT,thread_id TEXT,removed INTEGER,PRIMARY KEY(host_id,thread_id))"
            )
            db.execute(
                "CREATE TABLE local_thread_catalog_sync_state("
                "host_id TEXT PRIMARY KEY,watermark_updated_at REAL,"
                "initial_build_complete INTEGER NOT NULL DEFAULT 0,"
                "observation_sequence INTEGER NOT NULL DEFAULT 0,"
                "last_full_reconciled_at INTEGER)"
            )
            db.execute(
                "CREATE TABLE local_thread_catalog_scan_checkpoints("
                "host_id TEXT PRIMARY KEY,checkpoint TEXT NOT NULL,failed_at INTEGER)"
            )
            db.executemany(
                "INSERT INTO local_thread_catalog_hosts VALUES (?,?)",
                [("local", "local"), ("chat", "chatgpt"), ("remote", "ssh")],
            )
            db.executemany(
                "INSERT INTO local_thread_catalog_sync_state(host_id,observation_sequence) "
                "VALUES (?,?)",
                [("local", 10), ("chat", 20), ("remote", 30)],
            )
            db.execute("PRAGMA user_version=34")
        self.add_row("local", self.native_id, "vscode")
        self.add_row("local", "missing", "vscode")
        self.add_row("chat", "server-chat", "chatgpt", missing_candidate=1)
        self.add_row("remote", "remote-thread", "vscode")

    def add_row(self, host, thread, kind, *, missing_candidate=0):
        row = {key: None for key in c.CATALOG_COLUMNS}
        row.update(
            host_id=host,
            thread_id=thread,
            source_kind=kind,
            display_title="Fixture",
            source_created_at="2026-01-01T00:00:00Z",
            source_updated_at="2026-01-01T00:00:00Z",
            observation_sequence=1,
            missing_candidate=missing_candidate,
        )
        with fixture_db(self.root / "sqlite/codex-dev.db") as db:
            db.execute(
                "INSERT INTO local_thread_catalog("
                + ",".join(row)
                + ") VALUES("
                + ",".join("?" for _ in row)
                + ")",
                list(row.values()),
            )
            db.execute(
                "INSERT INTO local_thread_catalog_scan_entries VALUES(?,?,0)", (host, thread)
            )

    def mutate(self, sql, args=()):
        with fixture_db(self.root / "sqlite/codex-dev.db") as db:
            db.execute(sql, args)

    def evict(self, rows):
        return c.evict_catalog(
            self.profile,
            rows,
            recovery=self.rec,
            platform=self.platform,
        )

    def restore(self, backup):
        return c.restore_catalog(self.profile, backup, platform=self.platform)

    def test_single_profile_is_default_and_selection_is_skipped(self):
        adapter = MacOSAdapter(home=self.home)
        found = discover_profiles(adapter)
        self.assertEqual([item.id for item in found], ["default"])
        self.assertEqual(select_profiles(found), found)

    def test_multiple_profiles_are_selected_by_default(self):
        profiles = [
            self.profile,
            Profile("profile2", "Profile 2", self.home / ".codex-profile2", "test"),
        ]
        with patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(select_profiles(profiles), profiles)
        with patch("builtins.input", return_value="2"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(select_profiles(profiles), [profiles[1]])
        with patch("builtins.input", return_value="c"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(select_profiles(profiles), [])

    def test_managed_profiles_are_optional_and_verified(self):
        root = self.home / ".codex-profile2"
        root.mkdir()
        folder = self.home / "Library/Application Support/CodexMultiProfileLauncher"
        folder.mkdir(parents=True)
        selector = self.home / "Applications/ChatGPT Profile 2.app/Contents"
        selector.mkdir(parents=True)
        ident = "local.codex-multi-profile-launcher.profile2"
        (folder / "profile-2-install-manifest.json").write_text(
            json.dumps({"id": ident, "ready": True, "profile_index": 2})
        )
        (selector / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": ident})
        )
        found = discover_profiles(MacOSAdapter(home=self.home))
        self.assertEqual([item.id for item in found], ["default", "profile2"])
        (selector / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "wrong"})
        )
        found = discover_profiles(MacOSAdapter(home=self.home))
        self.assertEqual([item.id for item in found], ["default"])

    def test_plura_profile_is_discovered_from_verified_manifest(self):
        root = self.home / ".codex-profile2"
        root.mkdir()
        metadata = self.home / "Library/Application Support/PluraDesktop"
        metadata.mkdir(parents=True)
        selector = self.home / "Applications/ChatGPT Profile 2.app/Contents"
        selector.mkdir(parents=True)
        executable = selector / "MacOS/ChatGPT"
        executable.parent.mkdir()
        executable.write_text("fixture")
        ident = "local.plura-desktop.profile2"
        (metadata / "profile-2-install-manifest.json").write_text(
            json.dumps(
                {
                    "id": ident,
                    "ready": True,
                    "profile_index": 2,
                    "app_executable": str(executable),
                }
            )
        )
        (selector / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": ident})
        )
        found = discover_profiles(MacOSAdapter(home=self.home))
        self.assertEqual([item.id for item in found], ["default", "profile2"])
        self.assertEqual(found[1].provider, "plura-desktop")
        self.assertEqual(
            found[1].desktop_data_root,
            self.home / "Library/Application Support/Codex-Profile2",
        )
        (selector / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "wrong"})
        )
        found = discover_profiles(MacOSAdapter(home=self.home))
        self.assertEqual([item.id for item in found], ["default"])

    def test_windows_catalog_write_starts_fail_closed(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.home)}):
            adapter = WindowsAdapter(home=self.home)
            self.assertFalse(adapter.catalog_write_gate()[0])

    def test_sessions_expose_identity_metadata_when_available(self):
        row = c.sessions(self.profile)[0]
        self.assertEqual(row["creator_user_id"], "user-test")
        self.assertEqual(row["creator_account_id"], "account-test")

    def test_doctor_data_is_privacy_safe(self):
        data = doctor_data(self.profile, self.platform)
        self.assertEqual(data["native_conversations"], 1)
        self.assertEqual(data["missing_chatgpt_entries"], 1)
        serialized = json.dumps(data)
        self.assertNotIn("Keep native", serialized)
        self.assertNotIn("person@example.com", serialized)

    def test_official_account_response_maps_to_display_identity(self):
        identity = _identity_from_account_response(
            {
                "account": {
                    "type": "chatgpt",
                    "email": "person@example.com",
                    "planType": "team",
                },
                "workspaceRouting": {"chatgptAccountId": "workspace-12345678"},
            }
        )
        self.assertEqual(identity.email, "person@example.com")
        self.assertEqual(identity.plan, "team")
        self.assertEqual(identity.workspace_account_id, "workspace-12345678")

    def test_symlink_and_sidecar_rejected(self):
        (self.root / "state_5.sqlite-wal").symlink_to(self.home / "external")
        with self.assertRaises(c.SafetyError):
            c.sessions(self.profile)

    def test_catalog_only_safe_categories(self):
        rows = c.catalog(self.profile)
        self.assertEqual({row["thread_id"] for row in rows}, {"missing", "server-chat"})
        self.assertEqual(
            {row["classification"] for row in rows},
            {"local-orphan", "missing-chatgpt-entry"},
        )

    def test_chatgpt_entries_must_be_marked_missing_to_be_cleanup_candidates(self):
        self.add_row("chat", "still-present", "chatgpt", missing_candidate=0)
        rows = c.catalog(self.profile)
        self.assertNotIn("still-present", {row["thread_id"] for row in rows})

    def test_local_orphans_are_diagnostic_only_for_cleanup_flow(self):
        candidates = [
            row
            for row in c.catalog(self.profile)
            if row["classification"] == "missing-chatgpt-entry"
        ]
        self.assertEqual(
            {row["thread_id"] for row in candidates},
            {"server-chat"},
        )

    def test_cleanup_candidates_require_definite_deleted_or_not_found_evidence(self):
        self.add_row("chat", "server-present", "chatgpt", missing_candidate=0)
        self.add_row("chat", "project-live", "chatgpt", missing_candidate=0)

        def evidence(profile, ids, platform):
            return {
                "server-present": DesktopFailureEvidence(
                    conversation_id="server-present",
                    error_code="conversation_deleted",
                    status=404,
                    observed_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
                ),
                "project-live": DesktopFailureEvidence(
                    conversation_id="project-live",
                    error_code="conversation_inaccessible",
                    status=404,
                    observed_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
                )
            }

        with contextlib.redirect_stdout(io.StringIO()):
            candidates = _cleanup_candidates(
                self.profile,
                self.platform,
                evidence_reader=evidence,
            )
        self.assertEqual(
            {row["thread_id"] for row in candidates},
            {"server-present"},
        )
        self.assertNotIn("missing", {row["thread_id"] for row in candidates})
        self.assertNotIn("server-chat", {row["thread_id"] for row in candidates})
        self.assertNotIn("project-live", {row["thread_id"] for row in candidates})
        self.assertEqual(candidates[0]["classification"], "confirmed-missing")

    def test_live_present_overrides_historical_deleted_evidence(self):
        self.add_row("chat", "server-present", "chatgpt", missing_candidate=0)

        def evidence(profile, ids, platform):
            return {
                "server-present": DesktopFailureEvidence(
                    conversation_id="server-present",
                    error_code="conversation_deleted",
                    status=404,
                    observed_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
                )
            }

        def presence(profile, ids, platform, **kwargs):
            return {thread_id: Presence.PRESENT for thread_id in ids}

        candidates = _cleanup_candidates(
            self.profile,
            self.platform,
            evidence_reader=evidence,
            presence_probe=presence,
            announce=False,
        )
        self.assertEqual(candidates, [])

    def test_live_missing_is_candidate_without_historical_log(self):
        self.add_row("chat", "server-present", "chatgpt", missing_candidate=0)

        def presence(profile, ids, platform, **kwargs):
            return {
                thread_id: (
                    Presence.MISSING
                    if thread_id == "server-present"
                    else Presence.PRESENT
                )
                for thread_id in ids
            }

        candidates = _cleanup_candidates(
            self.profile,
            self.platform,
            evidence_reader=lambda *args: {},
            presence_probe=presence,
            announce=False,
        )
        self.assertEqual(
            {row["thread_id"] for row in candidates},
            {"server-present"},
        )
        self.assertEqual(candidates[0]["classification"], "confirmed-missing")

    def test_live_unverified_falls_back_only_to_safe_log(self):
        self.add_row("chat", "deleted", "chatgpt", missing_candidate=0)
        self.add_row("chat", "inaccessible", "chatgpt", missing_candidate=0)

        def evidence(profile, ids, platform):
            observed = datetime(2026, 9, 28, tzinfo=timezone.utc)
            return {
                "deleted": DesktopFailureEvidence(
                    "deleted", "conversation_deleted", 404, observed
                ),
                "inaccessible": DesktopFailureEvidence(
                    "inaccessible", "conversation_inaccessible", 404, observed
                ),
            }

        candidates = _cleanup_candidates(
            self.profile,
            self.platform,
            evidence_reader=evidence,
            presence_probe=lambda profile, ids, platform, **kwargs: {
                thread_id: Presence.UNVERIFIED for thread_id in ids
            },
            announce=False,
        )
        self.assertEqual(
            {row["thread_id"] for row in candidates},
            {"deleted"},
        )

    def test_live_missing_does_not_override_inaccessible_veto(self):
        self.add_row("chat", "project-live", "chatgpt", missing_candidate=0)

        def evidence(profile, ids, platform):
            return {
                "project-live": DesktopFailureEvidence(
                    "project-live",
                    "conversation_inaccessible",
                    404,
                    datetime(2026, 9, 28, tzinfo=timezone.utc),
                )
            }

        candidates = _cleanup_candidates(
            self.profile,
            self.platform,
            evidence_reader=evidence,
            presence_probe=lambda profile, ids, platform, **kwargs: {
                thread_id: Presence.MISSING for thread_id in ids
            },
            announce=False,
        )
        self.assertNotIn(
            "project-live",
            {row["thread_id"] for row in candidates},
        )

    def test_live_probe_policy_skips_inaccessible(self):
        self.add_row("chat", "deleted", "chatgpt", missing_candidate=0)
        self.add_row("chat", "inaccessible", "chatgpt", missing_candidate=0)
        seen = {}

        def evidence(profile, ids, platform):
            observed = datetime(2026, 9, 28, tzinfo=timezone.utc)
            return {
                "deleted": DesktopFailureEvidence(
                    "deleted", "conversation_deleted", 404, observed
                ),
                "inaccessible": DesktopFailureEvidence(
                    "inaccessible", "conversation_inaccessible", 404, observed
                ),
            }

        def presence(profile, ids, platform, **kwargs):
            seen.update(kwargs)
            return {thread_id: Presence.UNVERIFIED for thread_id in ids}

        _cleanup_candidates(
            self.profile,
            self.platform,
            evidence_reader=evidence,
            presence_probe=presence,
            announce=False,
        )
        self.assertEqual(seen["skip_ids"], {"inaccessible"})

    def test_retire_installed_copy_preserves_recovery_and_is_symlink_safe(self):
        platform = MacOSAdapter(home=self.home)
        recovery = platform.recovery_root
        recovery.mkdir(parents=True)
        (recovery / "keep.json").write_text("{}")
        tool = platform.data_root / "tool"
        tool.mkdir()
        (tool / "run.py").write_text("old")
        launcher = self.home / "Applications/ChatGPT Conversation Cleanup.command"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("old")

        self.assertEqual(c.retire_installed_copy(platform), 2)
        self.assertFalse(tool.exists())
        self.assertFalse(launcher.exists())
        self.assertTrue((recovery / "keep.json").exists())

        outside = self.home / "outside-old-tool"
        outside.mkdir()
        tool.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(c.SafetyError):
            c.retire_installed_copy(platform)
        self.assertTrue(outside.exists())

    def test_product_data_purge_is_scoped_and_symlink_safe(self):
        platform = MacOSAdapter(home=self.home)
        data_root = platform.data_root
        (data_root / "recovery").mkdir(parents=True)
        (data_root / "recovery" / "one.json").write_text("{}")
        self.assertTrue(c.purge_product_data(platform))
        self.assertFalse(data_root.exists())
        self.assertFalse(c.purge_product_data(platform))

        outside = self.home / "outside"
        outside.mkdir()
        data_root.parent.mkdir(parents=True, exist_ok=True)
        data_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(c.SafetyError):
            c.purge_product_data(platform)
        self.assertTrue(outside.exists())

    def test_confirmed_missing_rows_can_use_recoverable_catalog_cleanup(self):
        row = dict(
            c.chatgpt_catalog(self.profile)[0],
            classification="confirmed-missing",
        )
        backup = self.evict([row])
        self.assertTrue(backup.exists())
        self.assertTrue(self.restore(backup))

    def test_default_all_selection_and_subset_override(self):
        rows = [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]
        with patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(choose(rows, kind="archived", default_all=True), rows)
        with patch("builtins.input", return_value="A"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(choose(rows, kind="archived", default_all=True), rows)
        with patch("builtins.input", return_value="2"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                choose(rows, kind="archived", default_all=True),
                [rows[1]],
            )
        with patch("builtins.input", return_value="C"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(choose(rows, kind="archived", default_all=True), [])

    def test_default_all_selection_reprompts_after_arrow_keys(self):
        rows = [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]
        output = io.StringIO()
        with (
            patch("builtins.input", side_effect=["\x1b[A\x1b[B", ""]),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(choose(rows, kind="archived", default_all=True), rows)
        self.assertIn("Arrow keys are not used here", output.getvalue())

    def test_interactive_returns_to_main_after_an_operation(self):
        output = io.StringIO()
        with (
            patch(
                "chatgpt_cleanup.__main__.enrich_identities",
                return_value=[self.profile],
            ),
            patch(
                "chatgpt_cleanup.__main__.select_profiles",
                return_value=[self.profile],
            ),
            patch("chatgpt_cleanup.__main__.apply_batch") as apply_batch,
            patch("builtins.input", side_effect=["1", "", ""]),
            contextlib.redirect_stdout(output),
        ):
            interactive(self.platform, [self.profile])
        apply_batch.assert_called_once_with("catalog", [self.profile], self.platform)
        self.assertEqual(output.getvalue().count("ChatGPT Conversation Cleanup"), 2)

    def test_interactive_returns_to_main_after_safe_stop(self):
        output = io.StringIO()
        with (
            patch(
                "chatgpt_cleanup.__main__.enrich_identities",
                return_value=[self.profile],
            ),
            patch(
                "chatgpt_cleanup.__main__.select_profiles",
                return_value=[self.profile],
            ),
            patch(
                "chatgpt_cleanup.__main__.apply_batch",
                side_effect=c.SafetyError("The selected chats changed. Try again."),
            ),
            patch("builtins.input", side_effect=["1", "", ""]),
            contextlib.redirect_stdout(output),
        ):
            interactive(self.platform, [self.profile])
        text = output.getvalue()
        self.assertIn("Couldn’t continue: The selected chats changed. Try again.", text)
        self.assertEqual(text.count("ChatGPT Conversation Cleanup"), 2)

    def test_option_one_has_no_extra_read_only_confirmation(self):
        def evidence_many(profiles, ids_by_profile, platform):
            return {
                profile.id: {
                    "server-chat": DesktopFailureEvidence(
                        conversation_id="server-chat",
                        error_code="conversation_deleted",
                        status=404,
                        observed_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
                    )
                }
                for profile in profiles
            }

        with (
            patch("builtins.input", return_value="") as prompt,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            work = _collect_work(
                "catalog",
                [self.profile],
                self.platform,
                evidence_many_reader=evidence_many,
            )
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(
            {row["thread_id"] for row in work[0][1]},
            {"server-chat"},
        )

    def test_live_probe_is_scoped_to_current_workspace_host(self):
        current_host = "chatgpt:workspace-current:user-test"
        old_host = "chatgpt:workspace-old:user-test"
        with fixture_db(self.root / "sqlite/codex-dev.db") as db:
            db.execute(
                "UPDATE local_thread_catalog SET host_id=? WHERE host_id='chat'",
                (current_host,),
            )
            db.execute(
                "UPDATE local_thread_catalog_hosts SET host_id=? WHERE host_id='chat'",
                (current_host,),
            )
            db.execute(
                "INSERT INTO local_thread_catalog_hosts VALUES (?, 'chatgpt')",
                (old_host,),
            )
        self.add_row(old_host, "old-workspace", "chatgpt", missing_candidate=0)
        profile = Profile(
            self.profile.id,
            self.profile.display_name,
            self.profile.root,
            self.profile.provider,
            identity=ProfileIdentity(workspace_account_id="workspace-current"),
        )
        probed = []

        def presence(profile, ids, platform, **kwargs):
            probed.append(set(ids))
            return {thread_id: Presence.PRESENT for thread_id in ids}

        with contextlib.redirect_stdout(io.StringIO()):
            result = _cleanup_candidates_batch(
                [profile],
                self.platform,
                evidence_many_reader=lambda profiles, ids, platform: {
                    profile.id: {}
                },
                presence_probe=presence,
            )
        self.assertEqual(probed, [{"server-chat"}])
        self.assertEqual(result[profile.id], [])

    def test_cleanup_waits_for_user_to_close_desktop(self):
        calls = []

        class Platform:
            def require_desktop_stopped(self):
                calls.append("check")
                if len(calls) < 2:
                    raise c.PlatformError("running")

        with (
            patch("builtins.input", return_value="") as prompt,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(_wait_for_desktop_to_close(Platform()))
        self.assertEqual(calls, ["check", "check"])
        self.assertEqual(prompt.call_count, 1)

    def test_cleanup_close_prompt_can_cancel(self):
        class Platform:
            def require_desktop_stopped(self):
                raise c.PlatformError("running")

        with (
            patch("builtins.input", return_value="C"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertFalse(_wait_for_desktop_to_close(Platform()))

    def test_read_tolerates_version_drift_but_writes_reject_it(self):
        self.mutate("PRAGMA user_version=35")
        self.assertEqual(len(c.catalog(self.profile)), 2)
        with self.assertRaises(c.SafetyError):
            self.evict(c.catalog(self.profile))
        self.mutate("PRAGMA user_version=34")
        self.mutate(
            "CREATE TRIGGER unexpected AFTER DELETE ON local_thread_catalog "
            "BEGIN DELETE FROM local_thread_catalog_hosts; END"
        )
        self.assertEqual(len(c.catalog(self.profile)), 2)
        with self.assertRaises(c.SafetyError):
            self.evict(c.catalog(self.profile))

    def test_evict_restore_and_preserve_other_rows(self):
        rows = c.catalog(self.profile)
        backup = self.evict(rows)
        manifest = json.loads(backup.read_text())
        self.assertEqual(manifest["version"], 4)
        self.assertEqual(manifest["profile_id"], "default")
        self.assertEqual(manifest["platform"], "test")
        self.assertEqual(c.catalog(self.profile), [])
        self.assertEqual(len(c.sessions(self.profile)), 1)
        with c.database(self.root / "sqlite/codex-dev.db", self.root) as db:
            self.assertEqual(
                db.execute("select count(*) from local_thread_catalog").fetchone()[0],
                2,
            )
        self.assertTrue(self.restore(backup))
        self.assertEqual(json.loads(backup.read_text())["state"], "restored")
        restored = c.catalog(self.profile)
        strip_sequence = lambda row: {
            key: value for key, value in row.items() if key != "observation_sequence"
        }
        self.assertEqual(
            [strip_sequence(row) for row in restored],
            [strip_sequence(row) for row in rows],
        )
        self.assertTrue(
            all(row["observation_sequence"] > 1 for row in restored)
        )
        with self.assertRaises(c.SafetyError):
            self.restore(backup)

    def test_current_catalog_layout_with_trial_column_round_trips(self):
        self.mutate(
            "ALTER TABLE local_thread_catalog ADD COLUMN trial_conversation_type TEXT"
        )
        self.mutate(
            "UPDATE local_thread_catalog SET trial_conversation_type='standard' "
            "WHERE thread_id='server-chat'"
        )
        rows = c.catalog(self.profile)
        backup = self.evict(rows)
        self.assertEqual(c.catalog(self.profile), [])
        self.assertTrue(self.restore(backup))
        restored = {row["thread_id"]: row for row in c.catalog(self.profile)}
        self.assertEqual(restored["server-chat"]["trial_conversation_type"], "standard")

    def test_preview_change_refused_before_backup(self):
        rows = c.catalog(self.profile)
        self.mutate(
            "UPDATE local_thread_catalog SET display_title='changed' WHERE thread_id='missing'"
        )
        with self.assertRaises(c.SafetyError):
            self.evict(rows)
        self.assertEqual(list(self.rec.iterdir()), [])

    def test_backup_failure_leaves_rows_intact(self):
        rows = c.catalog(self.profile)
        with patch.object(c, "save_private", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.evict(rows)
        self.assertEqual(c.catalog(self.profile), rows)

    def test_restore_wrong_profile_and_scan_change(self):
        rows = c.catalog(self.profile)
        backup = self.evict(rows)
        data = json.loads(backup.read_text())
        data["profile_root"] = "/other"
        backup.write_text(json.dumps(data))
        with self.assertRaises(c.SafetyError):
            self.restore(backup)
        data["profile_root"] = str(self.root)
        backup.write_text(json.dumps(data))
        self.mutate(
            "UPDATE local_thread_catalog_scan_entries SET removed=1 "
            "WHERE host_id='local' AND thread_id='missing'"
        )
        with self.assertRaises(c.SafetyError):
            self.restore(backup)
        self.assertEqual(c.catalog(self.profile), [])

    def test_authoritative_removal_bookkeeping_matches_desktop_semantics(self):
        row = next(
            item for item in c.chatgpt_catalog(self.profile)
            if item["thread_id"] == "server-chat"
        )
        self.mutate(
            "INSERT INTO local_thread_catalog_scan_checkpoints(host_id,checkpoint) "
            "VALUES('chat','synthetic')"
        )
        with c.database(self.root / "sqlite/codex-dev.db", self.root) as db:
            before_sequence = db.execute(
                "SELECT observation_sequence FROM local_thread_catalog_sync_state "
                "WHERE host_id='chat'"
            ).fetchone()[0]
            before_revision = db.execute(
                "SELECT catalog_revision FROM local_thread_catalog_metadata WHERE id=1"
            ).fetchone()[0]
        backup = self.evict([row])
        with c.database(self.root / "sqlite/codex-dev.db", self.root) as db:
            self.assertEqual(
                db.execute(
                    "SELECT observation_sequence FROM local_thread_catalog_sync_state "
                    "WHERE host_id='chat'"
                ).fetchone()[0],
                before_sequence + 1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT removed FROM local_thread_catalog_scan_entries "
                    "WHERE host_id='chat' AND thread_id='server-chat'"
                ).fetchone()[0],
                1,
            )
            # missing_candidate=1 is already not a visible sidebar row, so the
            # Desktop implementation does not bump the catalog revision here.
            self.assertEqual(
                db.execute(
                    "SELECT catalog_revision FROM local_thread_catalog_metadata WHERE id=1"
                ).fetchone()[0],
                before_revision,
            )
        manifest = json.loads(backup.read_text())
        self.assertTrue(manifest["items"][0]["cleanup_tombstone"])
        self.assertTrue(self.restore(backup))
        with c.database(self.root / "sqlite/codex-dev.db", self.root) as db:
            self.assertEqual(
                db.execute(
                    "SELECT removed FROM local_thread_catalog_scan_entries "
                    "WHERE host_id='chat' AND thread_id='server-chat'"
                ).fetchone()[0],
                0,
            )

    def test_manual_visible_chatgpt_row_uses_authoritative_bookkeeping(self):
        self.add_row("chat", "manual-visible", "chatgpt", missing_candidate=0)
        row = next(
            item for item in c.chatgpt_catalog(self.profile)
            if item["thread_id"] == "manual-visible"
        )
        row["classification"] = "manual-local-cleanup"
        with c.database(self.root / "sqlite/codex-dev.db", self.root) as db:
            before_sequence = db.execute(
                "SELECT observation_sequence FROM local_thread_catalog_sync_state "
                "WHERE host_id='chat'"
            ).fetchone()[0]
            before_revision = db.execute(
                "SELECT catalog_revision FROM local_thread_catalog_metadata WHERE id=1"
            ).fetchone()[0]
        backup = self.evict([row])
        with c.database(self.root / "sqlite/codex-dev.db", self.root) as db:
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM local_thread_catalog "
                    "WHERE host_id='chat' AND thread_id='manual-visible'"
                ).fetchone()
            )
            self.assertEqual(
                db.execute(
                    "SELECT observation_sequence FROM local_thread_catalog_sync_state "
                    "WHERE host_id='chat'"
                ).fetchone()[0],
                before_sequence + 1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT catalog_revision FROM local_thread_catalog_metadata WHERE id=1"
                ).fetchone()[0],
                before_revision + 1,
            )
        self.assertTrue(self.restore(backup))

    def test_lock_conflict_and_terminal_escape(self):
        with operation_lock(self.rec):
            with self.assertRaises(c.SafetyError):
                with operation_lock(self.rec):
                    pass
        self.assertNotIn("\x1b", display("hello\x1b[2J"))

    def test_stale_active_recovery_blocks_new_catalog_mutation(self):
        stale = self.rec / "stale.json"
        stale.write_text(
            json.dumps(
                {
                    "version": 3,
                    "operation_id": str(uuid.uuid4()),
                    "created_at": (
                        datetime.now(timezone.utc) - timedelta(days=91)
                    ).isoformat().replace("+00:00", "Z"),
                    "state": "active",
                    "profile_id": "default",
                    "profile_root": str(self.root),
                    "platform": "test",
                    "items": [],
                }
            )
        )
        with self.assertRaises(c.SafetyError):
            c.require_recovery_capacity(self.rec, 1)

    def test_restored_recovery_is_pruned_but_active_is_preserved(self):
        now = datetime.now(timezone.utc)
        active = self.rec / "active.json"
        restored = self.rec / "restored.json"
        active.write_text(
            json.dumps(
                {
                    "version": 3,
                    "operation_id": str(uuid.uuid4()),
                    "created_at": now.isoformat().replace("+00:00", "Z"),
                    "state": "active",
                    "profile_root": str(self.root),
                    "items": [],
                }
            )
        )
        restored.write_text(
            json.dumps(
                {
                    "version": 3,
                    "operation_id": str(uuid.uuid4()),
                    "created_at": (now - timedelta(days=40)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "restored_at": (now - timedelta(days=31)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "state": "restored",
                    "profile_root": str(self.root),
                    "items": [],
                }
            )
        )
        inventory = c.maintain_recovery(self.rec, now=now)
        self.assertTrue(active.exists())
        self.assertFalse(restored.exists())
        self.assertEqual([record["path"] for record in inventory["active"]], [active])

    def test_recovery_count_and_size_budget_fail_closed(self):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        template = {
            "version": 3,
            "created_at": now,
            "state": "active",
            "profile_root": str(self.root),
            "items": [],
        }
        with patch.object(c, "ACTIVE_RECOVERY_MAX_FILES", 1):
            first = dict(template, operation_id=str(uuid.uuid4()))
            (self.rec / "one.json").write_text(json.dumps(first))
            with self.assertRaises(c.SafetyError):
                c.require_recovery_capacity(self.rec, 1)
        for path in self.rec.glob("*.json"):
            path.unlink()
        with patch.object(c, "ACTIVE_RECOVERY_MAX_BYTES", 10):
            with self.assertRaises(c.SafetyError):
                c.require_recovery_capacity(self.rec, 11)


if __name__ == "__main__":
    unittest.main()
