"""Normal Desktop lifecycle safety, entirely synthetic."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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



if __name__ == "__main__":
    unittest.main()
