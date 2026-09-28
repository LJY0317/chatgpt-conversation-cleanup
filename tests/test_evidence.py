from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import time
import unittest

from chatgpt_cleanup.evidence import (
    desktop_failure_evidence,
    desktop_failure_evidence_many,
)
from chatgpt_cleanup.platforms import MacOSAdapter
from chatgpt_cleanup.profiles import Profile


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.platform = MacOSAdapter(home=self.home)
        self.root = self.home / ".codex"
        self.root.mkdir()
        self.data_root = self.home / "Library/Application Support/Codex"
        self.data_root.mkdir(parents=True)
        self.profile = Profile(
            "default",
            "ChatGPT",
            self.root,
            "official",
            desktop_data_root=self.data_root,
        )
        self.logs = self.home / "logs"
        self.logs.mkdir()

    def write_log(self, session, text, *, suffix="t0-i1-000001-0"):
        path = self.logs / f"codex-desktop-{session}-12345-{suffix}.log"
        path.write_text(text)
        return path

    def test_accepts_structured_404_without_ui_string(self):
        session = "11111111-1111-4111-8111-111111111111"
        self.write_log(
            session,
            (
                f"startup codexHome={self.root}\n"
                "2026-09-28T00:00:00.000Z warning arbitrary_event "
                "status=404 errorCode=conversation_deleted "
                "conversation=conversation-a\n"
            ),
        )
        found = desktop_failure_evidence(
            self.profile,
            {"conversation-a", "conversation-b"},
            self.platform,
            log_root=self.logs,
        )
        self.assertEqual(set(found), {"conversation-a"})
        self.assertEqual(found["conversation-a"].error_code, "conversation_deleted")

    def test_inaccessible_is_supported_but_500_is_not(self):
        session = "22222222-2222-4222-8222-222222222222"
        self.write_log(
            session,
            (
                f"dataRoot={self.data_root}\n"
                "2026-09-28T00:00:00Z warning event status=404 "
                "errorCode=conversation_inaccessible id=conversation-a\n"
                "2026-09-28T00:00:01Z warning event status=500 "
                "errorCode=conversation_deleted id=conversation-b\n"
            ),
        )
        found = desktop_failure_evidence(
            self.profile,
            {"conversation-a", "conversation-b"},
            self.platform,
            log_root=self.logs,
        )
        self.assertEqual(set(found), {"conversation-a"})

    def test_wrong_profile_session_is_ignored(self):
        session = "33333333-3333-4333-8333-333333333333"
        other = self.home / ".codex-profile2"
        self.write_log(
            session,
            (
                f"codexHome={other}\n"
                "2026-09-28T00:00:00Z warning event status=404 "
                "errorCode=conversation_deleted id=conversation-a\n"
            ),
        )
        self.assertEqual(
            desktop_failure_evidence(
                self.profile,
                {"conversation-a"},
                self.platform,
                log_root=self.logs,
            ),
            {},
        )

    def test_split_session_uses_marker_from_sibling_log(self):
        session = "44444444-4444-4444-8444-444444444444"
        self.write_log(session, f"codexHome={self.root}\n", suffix="t0-i1-000001-0")
        self.write_log(
            session,
            (
                "2026-09-28T00:00:00Z warning event status=404 "
                "errorCode=conversation_not_found id=conversation-a\n"
            ),
            suffix="t1-i1-000002-0",
        )
        found = desktop_failure_evidence(
            self.profile,
            {"conversation-a"},
            self.platform,
            log_root=self.logs,
        )
        self.assertEqual(set(found), {"conversation-a"})

    def test_error_message_text_alone_is_not_evidence(self):
        session = "55555555-5555-4555-8555-555555555555"
        self.write_log(
            session,
            (
                f"codexHome={self.root}\n"
                "Could not load this ChatGPT conversation conversation-a\n"
            ),
        )
        self.assertEqual(
            desktop_failure_evidence(
                self.profile,
                {"conversation-a"},
                self.platform,
                log_root=self.logs,
            ),
            {},
        )

    def test_old_logs_are_ignored(self):
        session = "66666666-6666-4666-8666-666666666666"
        path = self.write_log(
            session,
            (
                f"codexHome={self.root}\n"
                "2026-01-01T00:00:00Z warning event status=404 "
                "errorCode=conversation_deleted id=conversation-a\n"
            ),
        )
        old = time.time() - 120 * 86400
        os.utime(path, (old, old))
        self.assertEqual(
            desktop_failure_evidence(
                self.profile,
                {"conversation-a"},
                self.platform,
                log_root=self.logs,
            ),
            {},
        )

    def test_latest_structured_observation_is_retained(self):
        session = "77777777-7777-4777-8777-777777777777"
        self.write_log(
            session,
            (
                f"codexHome={self.root}\n"
                "2026-09-27T00:00:00Z warning event status=404 "
                "errorCode=conversation_inaccessible id=conversation-a\n"
                "2026-09-28T00:00:00Z warning event status=404 "
                "errorCode=conversation_deleted id=conversation-a\n"
            ),
        )
        found = desktop_failure_evidence(
            self.profile,
            {"conversation-a"},
            self.platform,
            log_root=self.logs,
        )
        self.assertEqual(found["conversation-a"].error_code, "conversation_deleted")
        self.assertEqual(
            found["conversation-a"].observed_at,
            datetime(2026, 9, 28, tzinfo=timezone.utc),
        )

    def test_later_activity_invalidates_deleted_cleanup_evidence(self):
        session = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.write_log(
            session,
            (
                f"codexHome={self.root}\n"
                "2026-09-28T00:00:00Z warning event status=404 "
                "errorCode=conversation_deleted id=conversation-a\n"
                "2026-09-28T01:00:00Z info later_activity "
                "conversation=conversation-a\n"
            ),
        )
        self.assertEqual(
            desktop_failure_evidence(
                self.profile,
                {"conversation-a"},
                self.platform,
                log_root=self.logs,
            ),
            {},
        )

    def test_batch_scan_separates_profiles_and_rejects_ambiguous_sessions(self):
        second_root = self.home / ".codex-profile2"
        second_root.mkdir()
        second_data = self.home / "Library/Application Support/Codex-Profile2"
        second_data.mkdir(parents=True)
        second = Profile(
            "profile2",
            "Profile 2",
            second_root,
            "plura-desktop",
            desktop_data_root=second_data,
        )
        first_session = "88888888-8888-4888-8888-888888888888"
        second_session = "99999999-9999-4999-8999-999999999999"
        ambiguous = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.write_log(
            first_session,
            (
                f"codexHome={self.root}\n"
                "2026-09-28T00:00:00Z warning event status=404 "
                "errorCode=conversation_deleted id=conversation-a\n"
            ),
        )
        self.write_log(
            second_session,
            (
                f"codexHome={second_root}\n"
                "2026-09-28T00:00:00Z warning event status=404 "
                "errorCode=conversation_deleted id=conversation-b\n"
            ),
        )
        self.write_log(
            ambiguous,
            (
                f"default={self.root} profile2={second_root}\n"
                "2026-09-28T00:00:00Z warning event status=404 "
                "errorCode=conversation_deleted id=conversation-c\n"
            ),
        )
        found = desktop_failure_evidence_many(
            [self.profile, second],
            {
                "default": {"conversation-a", "conversation-c"},
                "profile2": {"conversation-b", "conversation-c"},
            },
            self.platform,
            log_root=self.logs,
        )
        self.assertEqual(set(found["default"]), {"conversation-a"})
        self.assertEqual(set(found["profile2"]), {"conversation-b"})


if __name__ == "__main__":
    unittest.main()
