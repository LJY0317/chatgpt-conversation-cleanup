from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys


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

    def catalog_write_gate(self) -> tuple[bool, str]:
        return False, f"Direct catalog cleanup is not verified on {self.display_name}."


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


def current_platform(*, home: Path | None = None) -> PlatformAdapter:
    home = Path(home) if home is not None else Path.home()
    if sys.platform == "darwin":
        return MacOSAdapter(home=home)
    if sys.platform == "win32":
        return WindowsAdapter(home=home)
    raise PlatformError(
        f"Unsupported platform: {sys.platform}. ChatGPT Desktop is currently targeted on macOS and Windows."
    )
