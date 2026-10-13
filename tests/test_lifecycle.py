"""Lifecycle invariants tested without launching Desktop or deleting chats."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_cleanup.__main__ import _apply_batch_selected
from chatgpt_cleanup.diagnostics import record_blocked_check
from chatgpt_cleanup.lifecycle import (
    restore_initial_desktops,
    wait_for_desktop_to_close,
)
from chatgpt_cleanup.platforms import PlatformError
from chatgpt_cleanup.profiles import Profile


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        self.profile = Profile("default", "ChatGPT", self.home / ".codex", "official")
        self.other = Profile("profile2", "Profile 2", self.home / ".codex-profile2", "plura-desktop")

    def test_approval_requests_one_normal_quit_without_an_extra_prompt(self):
        class Platform:
            data_root = self.home / "app-data"
            running = True
            requests = 0

            def require_profile_desktop_stopped(self, profile):
                if self.running:
                    raise PlatformError("app open", process_kind="chatgpt", pid=104)

            def request_normal_desktop_quit(self, profile):
                self.requests += 1
                self.running = False
                return True, "Normal quit requested"

        platform = Platform()
        with (
            patch("builtins.input", side_effect=AssertionError("Unexpected extra prompt")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(wait_for_desktop_to_close(
                platform, profiles=[self.profile], approved_normal_quit=True
            ))
        self.assertEqual(platform.requests, 1)

    def test_one_quit_then_blocks_on_different_managed_profile(self):
        class Platform:
            data_root = self.home / "app-data"
            requests = 0
            official_running = True

            def require_profile_desktop_stopped(self, profile):
                if profile.id == "default" and self.official_running:
                    raise PlatformError("app open", process_kind="chatgpt", pid=104)
                if profile.id == "profile2":
                    raise PlatformError(
                        "managed profile open", process_kind="external-codex-app-server", pid=105
                    )

            def request_normal_desktop_quit(self, profile):
                self.requests += 1
                self.official_running = False
                return True, "Normal quit requested"

        platform = Platform()
        with (
            patch("builtins.input", return_value="c") as prompt,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertFalse(wait_for_desktop_to_close(
                platform, profiles=[self.profile, self.other],
                approved_normal_quit=True,
            ))
        self.assertEqual(platform.requests, 1)
        self.assertIn("other program", prompt.call_args.args[0])

    def test_failure_does_not_repeat_uncertain_quit(self):
        class Platform:
            data_root = self.home / "app-data"
            requests = 0

            def require_profile_desktop_stopped(self, profile):
                raise PlatformError("app open", process_kind="chatgpt", pid=104)

            def request_normal_desktop_quit(self, profile):
                self.requests += 1
                return False, "Normal quit declined"

        platform = Platform()
        with (
            patch("builtins.input", side_effect=["", "c"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertFalse(wait_for_desktop_to_close(
                platform, profiles=[self.profile], approved_normal_quit=True
            ))
        self.assertEqual(platform.requests, 1)

    def test_quit_timeout_never_forces_process_termination(self):
        class Platform:
            data_root = self.home / "app-data"
            requests = 0

            def require_profile_desktop_stopped(self, profile):
                raise PlatformError("app still open", process_kind="chatgpt", pid=104)

            def request_normal_desktop_quit(self, profile):
                self.requests += 1
                return True, "Normal quit requested"

        platform = Platform()
        clock = [0.0]
        with (
            patch("builtins.input", return_value="c"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertFalse(wait_for_desktop_to_close(
                platform, profiles=[self.profile], approved_normal_quit=True,
                monotonic=lambda: clock[0],
                sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            ))
        self.assertEqual(platform.requests, 1)
        self.assertGreaterEqual(clock[0], 8)

    def test_cancelled_confirmation_does_not_close_any_app(self):
        selected = [(self.profile, [{"thread_id": "synthetic"}])]
        order = []

        def summary(*args, **kwargs):
            order.append("confirm")
            return False

        with (
            patch("chatgpt_cleanup.__main__._collect_work", return_value=selected),
            patch("chatgpt_cleanup.__main__._batch_summary", side_effect=summary),
            patch("chatgpt_cleanup.__main__._wait_for_desktop_to_close") as wait,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            _apply_batch_selected("catalog", [self.profile], object())
        self.assertEqual(order, ["confirm"])
        wait.assert_not_called()

    def test_approved_selection_reaches_close_gate_only_after_confirmation(self):
        selected = [(self.profile, [{"thread_id": "synthetic"}])]
        order = []

        def summary(*args, **kwargs):
            order.append("confirm")
            return True

        def stop_gate(*args, **kwargs):
            order.append("stop")
            self.assertTrue(kwargs["approved_normal_quit"])
            return False

        with (
            patch("chatgpt_cleanup.__main__._collect_work", return_value=selected),
            patch("chatgpt_cleanup.__main__._batch_summary", side_effect=summary),
            patch("chatgpt_cleanup.__main__._wait_for_desktop_to_close", side_effect=stop_gate),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            _apply_batch_selected("catalog", [self.profile], object())
        self.assertEqual(order, ["confirm", "stop"])

    def test_previously_closed_profile_is_not_launched(self):
        class Platform:
            def desktop_restore_plan(self, profiles):
                return {}

            def launch_profile_desktop(self, profile):
                raise AssertionError("Closed profile must stay closed")

        restore_initial_desktops(Platform(), [self.profile], {})

    def test_managed_profile_is_not_reopened_as_official_desktop(self):
        class Platform:
            def desktop_restore_plan(self, profiles):
                return {}

            def launch_profile_desktop(self, profile):
                raise AssertionError("Managed profile must use its own owner")

        with contextlib.redirect_stdout(io.StringIO()) as output:
            restore_initial_desktops(
                Platform(), [self.other], {"profile2": "managed"}
            )
        self.assertIn("through that app", output.getvalue())

    def test_no_reopen_if_standalone_data_becomes_owned_again(self):
        class Platform:
            def desktop_restore_plan(self, profiles):
                return {}

            def require_profile_desktop_stopped(self, profile):
                raise PlatformError("still shared", process_kind="codex-app-server")

            def launch_profile_desktop(self, profile):
                raise AssertionError("Unsafe to reopen")

        with contextlib.redirect_stdout(io.StringIO()) as output:
            restore_initial_desktops(
                Platform(), [self.profile], {"default": "standalone"}
            )
        self.assertIn("data is still in use", output.getvalue())

    def test_blocked_check_log_refuses_symlink_targets(self):
        class Platform:
            data_root = self.home / "data"

        diagnostics = Platform.data_root / "diagnostics"
        diagnostics.mkdir(parents=True)
        private_target = self.home / "user-document"
        private_target.write_text("KEEP THIS TEXT")
        (diagnostics / "desktop-stop.jsonl").symlink_to(private_target)
        record_blocked_check(
            Platform(), operation="cleanup", profiles=[self.profile],
            error=PlatformError("private detail", process_kind="chatgpt", pid=123),
        )
        self.assertEqual(private_target.read_text(), "KEEP THIS TEXT")


if __name__ == "__main__":
    unittest.main()
