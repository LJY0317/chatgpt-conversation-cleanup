"""Consent-driven Desktop shutdown and bounded restoration.

The common workflow knows only a selected profile and an optional, verified
standalone-app lifecycle. It does not manage third-party runtimes or services.
The storage write guards remain authoritative even after the app exits.
"""

import subprocess
import time

from .diagnostics import record_blocked_check
from .platforms import PlatformError, require_profile_stopped


def wait_for_desktop_to_close(
    platform, *, purpose="cleanup", profile=None, profiles=None,
    approved_normal_quit=False, monotonic=time.monotonic, sleep=time.sleep,
):
    targets = [profile] if profile is not None else list(profiles or [])
    first = True
    attempted = set()
    while True:
        blocking_profile = None
        try:
            if targets:
                for target in targets:
                    blocking_profile = target
                    require_profile_stopped(platform, target)
            else:
                platform.require_desktop_stopped()
            return True
        except PlatformError as error:
            record_blocked_check(
                platform, operation=purpose, profiles=targets, error=error
            )
            if first:
                stage = (
                    "a fresh Desktop reconciliation can be queued"
                    if purpose == "refresh" else "local cleanup can proceed"
                )
                print(f"\nChatGPT must be closed before {stage}.")
                first = False

            normal_quit = (
                blocking_profile is not None
                and getattr(error, "process_kind", None) == "chatgpt"
                and callable(getattr(platform, "request_normal_desktop_quit", None))
            )
            receipt = (
                blocking_profile.id, getattr(error, "pid", None)
            ) if normal_quit else None
            if approved_normal_quit and normal_quit and receipt not in attempted:
                attempted.add(receipt)
                requested, reason = platform.request_normal_desktop_quit(
                    blocking_profile
                )
                print(f"  {reason}")
                if requested:
                    deadline = monotonic() + 8
                    while monotonic() < deadline:
                        try:
                            require_profile_stopped(platform, blocking_profile)
                            break
                        except PlatformError:
                            sleep(0.25)
                    # A save dialog or another runtime may still own the data.
                    # Never force quit or repeat an uncertain quit. Recheck
                    # every selected profile in order after a normal quit.
                    continue

            print(f"  Still blocked: {error}")
            if getattr(error, "process_kind", None) == "external-codex-app-server":
                prompt = (
                    "Close the other program through its own controls, then "
                    "press Enter to recheck, "
                )
            else:
                prompt = "When the affected app is closed, press Enter to recheck, "
            if normal_quit and receipt not in attempted:
                prompt += "Q to request a normal app quit, "
            answer = input(prompt + "or C to cancel: ").strip().lower()
            if answer in {"c", "cancel"}:
                return False
            if answer == "q" and normal_quit and receipt not in attempted:
                # Explicit consent on the manual path. The next iteration
                # retries the check and attempts the normal quit exactly once.
                approved_normal_quit = True


def capture_initial_desktops(platform, profiles):
    capture = getattr(platform, "desktop_restore_plan", None)
    if not callable(capture):
        return {}
    try:
        return capture(profiles)
    except (OSError, PlatformError, subprocess.SubprocessError):
        print(
            "Desktop startup ownership could not be verified; "
            "automatic reopening is disabled."
        )
        return {}


def restore_initial_desktops(platform, profiles, initially_open):
    """Reopen only standalone apps we observed at the start of this operation."""
    if not initially_open:
        return
    try:
        currently_open = platform.desktop_restore_plan(profiles)
    except (OSError, PlatformError, subprocess.SubprocessError):
        print("Desktop state could not be checked; reopen through the original app.")
        return
    for profile in profiles:
        previous = initially_open.get(profile.id)
        if previous is None or profile.id in currently_open:
            continue
        if previous != "standalone":
            print(
                f"{profile.display_name}: the previous Desktop session was "
                "managed by another app; restore it through that app."
            )
            continue
        try:
            require_profile_stopped(platform, profile)
        except PlatformError:
            print(
                f"{profile.display_name}: Desktop was not reopened because "
                "its data is still in use."
            )
            continue
        opened, reason = platform.launch_profile_desktop(profile)
        if opened:
            print(f"{profile.display_name}: the original Desktop app was reopened.")
        else:
            print(
                f"{profile.display_name}: could not reopen the original Desktop "
                f"app ({str(reason)[:120]})."
            )
