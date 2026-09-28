from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .profiles import Profile


class PlatformError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlatformAdapter:
    home: Path
    key: str
    display_name: str

    @property
    def default_codex_home(self) -> Path:
        return self.home / ".codex"

    @property
    def data_root(self) -> Path:
        raise NotImplementedError

    @property
    def recovery_root(self) -> Path:
        return self.data_root / "recovery"

    @property
    def legacy_program_paths(self) -> tuple[Path, ...]:
        return ()

    def codex_cli(self) -> Path:
        override = os.environ.get("CHATGPT_CLEANUP_CODEX")
        if override:
            return Path(override).expanduser()
        found = shutil.which("codex")
        if found:
            return Path(found)
        raise PlatformError("Could not find the official Codex CLI.")

    def require_desktop_stopped(self) -> None:
        raise NotImplementedError

    def require_profile_desktop_stopped(self, profile: "Profile") -> None:
        # Platforms without a verified per-profile process contract keep the
        # conservative all-Desktop guard.
        self.require_desktop_stopped()

    def catalog_write_gate(self) -> tuple[bool, str]:
        return False, f"Direct catalog cleanup is not verified on {self.display_name}."

    def catalog_refresh_gate(self) -> tuple[bool, str]:
        return False, f"Desktop catalog refresh is not verified on {self.display_name}."

    def profile_catalog_refresh_gate(self, profile: "Profile") -> tuple[bool, str]:
        return self.catalog_refresh_gate()

    def launch_profile_desktop(self, profile: "Profile") -> tuple[bool, str]:
        return False, f"Automatic profile launch is not supported on {self.display_name}."


@dataclass(frozen=True)
class MacOSAdapter(PlatformAdapter):
    key: str = "macos"
    display_name: str = "macOS"

    @property
    def app_path(self) -> Path:
        override = os.environ.get("CHATGPT_CLEANUP_APP")
        return Path(override).expanduser() if override else Path("/Applications/ChatGPT.app")

    @property
    def data_root(self) -> Path:
        return self.home / "Library/Application Support/ChatGPT Conversation Cleanup"

    @property
    def legacy_program_paths(self) -> tuple[Path, ...]:
        return (
            self.data_root / "tool",
            self.home / "Applications/ChatGPT Conversation Cleanup.command",
            self.home / "Library/Application Support/ChatGPT Repair Kit/tool",
            self.home / "Applications/ChatGPT Repair Kit.command",
        )

    def codex_cli(self) -> Path:
        override = os.environ.get("CHATGPT_CLEANUP_CODEX")
        if override:
            return Path(override).expanduser()
        bundled = self.app_path / "Contents/Resources/codex-cli/bin/codex"
        if bundled.is_file():
            return bundled
        legacy = self.app_path / "Contents/Resources/codex"
        if legacy.is_file():
            return legacy
        return super().codex_cli()

    def require_desktop_stopped(self) -> None:
        result = subprocess.run(
            ["/bin/ps", "-axo", "comm=,args="],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.splitlines():
            if "/Contents/MacOS/ChatGPT" in line or ("codex" in line and "app-server" in line):
                raise PlatformError(
                    "Quit ChatGPT and Codex app-server normally before applying changes."
                )

    def require_profile_desktop_stopped(self, profile: "Profile") -> None:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,args="],
            capture_output=True,
            text=True,
            check=True,
        )
        official_executable = str(self.app_path / "Contents/MacOS/ChatGPT")
        data_root = profile.desktop_data_root
        marker = f"--user-data-dir={data_root}" if data_root is not None else None
        for raw in result.stdout.splitlines():
            parts = raw.strip().split(maxsplit=1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            pid, line = parts
            if "codex" in line and "app-server" in line:
                # Alternate profile launchers can run an app-server from the
                # official app bundle while pointing CODEX_HOME at their own
                # isolated profile root. Inspect only that process's environment
                # so an unrelated profile does not block this profile's refresh.
                try:
                    detailed = subprocess.run(
                        ["/bin/ps", "eww", "-p", pid, "-o", "command="],
                        capture_output=True,
                        text=True,
                        check=True,
                    ).stdout
                except (OSError, subprocess.SubprocessError):
                    raise PlatformError(
                        "Could not verify which ChatGPT profile owns Codex app-server."
                    )
                codex_home = f"CODEX_HOME={profile.root}"
                home_tokens = [
                    token
                    for token in detailed.split()
                    if token.startswith("CODEX_HOME=")
                ]
                if home_tokens and codex_home not in home_tokens:
                    continue
                if not home_tokens and profile.root != self.home / ".codex":
                    # An app-server with no explicit CODEX_HOME uses the normal
                    # default ~/.codex. It cannot own an isolated alternate
                    # profile rooted elsewhere (for example ~/.codex-profile2).
                    continue
                raise PlatformError(
                    f"Quit {profile.display_name}'s Codex app-server normally before applying changes."
                )
            if "/Contents/MacOS/ChatGPT" not in line:
                continue
            if profile.provider == "official":
                # The official app can also be hosted by developer runtimes. If
                # it points at the same Desktop user-data root, it owns the same
                # catalog and must be stopped before scheduling refresh.
                if (
                    line == official_executable
                    or line.startswith(official_executable + " ")
                ) and (marker is None or marker in line or "--user-data-dir=" not in line):
                    raise PlatformError(
                        f"Quit {profile.display_name} normally before applying changes."
                    )
                continue
            if marker is not None and marker in line:
                raise PlatformError(
                    f"Quit {profile.display_name} normally before applying changes."
                )

    def app_build(self) -> tuple[str, str] | None:
        info_path = self.app_path / "Contents/Info.plist"
        if not info_path.is_file():
            return None
        info = plistlib.loads(info_path.read_bytes())
        short = info.get("CFBundleShortVersionString")
        build = info.get("CFBundleVersion")
        if isinstance(short, str) and isinstance(build, str):
            return short, build
        return None

    def catalog_write_gate(self) -> tuple[bool, str]:
        # Direct catalog writes stay build-gated even when the SQLite schema matches.
        verified = {
            ("26.915.31945", "9922"),
            ("26.924.22138", "11645"),
        }
        current = self.app_build()
        if current in verified:
            return True, f"verified ChatGPT build {current[0]} ({current[1]})"
        if current is None:
            return False, "ChatGPT app build could not be verified."
        return False, f"ChatGPT build {current[0]} ({current[1]}) is not write-verified."

    def catalog_refresh_gate(self) -> tuple[bool, str]:
        # The refresh contract depends on internal Desktop sync semantics, so it
        # stays narrower than the ordinary catalog-write gate.
        verified = {("26.924.22138", "11645")}
        current = self.app_build()
        if current in verified:
            return True, f"refresh-verified ChatGPT build {current[0]} ({current[1]})"
        if current is None:
            return False, "ChatGPT app build could not be verified for refresh."
        return False, (
            f"ChatGPT build {current[0]} ({current[1]}) is not refresh-verified."
        )

    def profile_catalog_refresh_gate(self, profile: "Profile") -> tuple[bool, str]:
        if profile.provider == "official":
            return self.catalog_refresh_gate()
        if profile.provider != "plura-desktop":
            return False, f"Fresh Desktop reconciliation is not verified for {profile.provider}."

        suffix = profile.id.removeprefix("profile")
        if not suffix.isdigit():
            return False, "Could not identify the Plura profile runtime."
        manifest = (
            self.home
            / "Library/Application Support/PluraDesktop"
            / f"profile-{int(suffix)}-install-manifest.json"
        )
        try:
            import json

            data = json.loads(manifest.read_text())
            executable = Path(data["app_executable"])
            info_path = executable.parents[2] / "Contents/Info.plist"
            info = plistlib.loads(info_path.read_bytes())
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            return False, "Could not verify the Plura ChatGPT runtime build."
        expected_id = f"local.plura-desktop.profile{int(suffix)}"
        expected_root = self.home / f".codex-profile{int(suffix)}"
        expected_data_root = (
            self.home / f"Library/Application Support/Codex-Profile{int(suffix)}"
        )
        if not (
            data.get("ready") is True
            and data.get("profile_index") == int(suffix)
            and data.get("id") == expected_id
            and profile.root == expected_root
            and profile.desktop_data_root == expected_data_root
            and executable.is_file()
        ):
            return False, "The Plura profile manifest is not trusted."
        short = info.get("CFBundleShortVersionString")
        build = info.get("CFBundleVersion")
        current = (short, build) if isinstance(short, str) and isinstance(build, str) else None
        verified = {("26.924.22138", "11645")}
        if current in verified:
            return True, f"refresh-verified Plura ChatGPT build {short} ({build})"
        if current is None:
            return False, "The Plura ChatGPT runtime build could not be read."
        return False, f"Plura ChatGPT build {short} ({build}) is not refresh-verified."

    def launch_profile_desktop(self, profile: "Profile") -> tuple[bool, str]:
        try:
            if profile.provider == "official":
                result = subprocess.run(
                    ["/usr/bin/open", "-a", str(self.app_path)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
                if result.returncode == 0:
                    return True, "opened ChatGPT"
                return False, result.stderr.strip() or "ChatGPT could not be opened."

            if profile.provider == "plura-desktop":
                suffix = profile.id.removeprefix("profile")
                if not suffix.isdigit():
                    return False, "Could not identify the Plura profile runtime."
                command = shutil.which("plura-desktop")
                if not command:
                    return False, "plura-desktop is unavailable."
                target = f"local.plura-desktop.profile{int(suffix)}"
                result = subprocess.run(
                    [
                        command,
                        "launch-target",
                        "--target",
                        target,
                        "--renderer-cdp",
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                if result.returncode == 0:
                    return True, f"opened {profile.display_name}"
                return False, result.stderr.strip() or result.stdout.strip() or (
                    f"{profile.display_name} could not be opened."
                )
        except (OSError, subprocess.SubprocessError) as error:
            return False, str(error)
        return False, f"Automatic launch is not verified for {profile.provider}."


@dataclass(frozen=True)
class WindowsAdapter(PlatformAdapter):
    key: str = "windows"
    display_name: str = "Windows"

    @property
    def data_root(self) -> Path:
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise PlatformError("LOCALAPPDATA is unavailable.")
        return Path(base) / "ChatGPT Conversation Cleanup"

    @property
    def legacy_program_paths(self) -> tuple[Path, ...]:
        return (
            self.data_root / "tool",
            self.data_root / "chatgpt-cleanup.cmd",
        )

    def require_desktop_stopped(self) -> None:
        # CommandLine is required to distinguish the Codex app-server from ordinary CLI use.
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -match '^(ChatGPT|codex)(\\.exe)?$' -or "
                "$_.CommandLine -match 'codex.*app-server' } | "
                "Select-Object -ExpandProperty CommandLine"
            ),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=10)
        if result.returncode != 0:
            raise PlatformError("Could not verify that ChatGPT processes are stopped.")
        if result.stdout.strip():
            raise PlatformError(
                "Quit ChatGPT and Codex app-server normally before applying changes."
            )

    def catalog_write_gate(self) -> tuple[bool, str]:
        return False, "Windows direct catalog cleanup is read-only until a build is verified."

    def catalog_refresh_gate(self) -> tuple[bool, str]:
        return False, "Windows Desktop catalog refresh is not yet build-verified."

    def profile_catalog_refresh_gate(self, profile: "Profile") -> tuple[bool, str]:
        return self.catalog_refresh_gate()

    def launch_profile_desktop(self, profile: "Profile") -> tuple[bool, str]:
        return False, "Automatic Desktop launch is not yet verified on Windows."


def current_platform(*, home: Path | None = None) -> PlatformAdapter:
    home = Path(home) if home is not None else Path.home()
    if sys.platform == "darwin":
        return MacOSAdapter(home=home)
    if sys.platform == "win32":
        return WindowsAdapter(home=home)
    raise PlatformError(
        f"Unsupported platform: {sys.platform}. ChatGPT Desktop is currently targeted on macOS and Windows."
    )
