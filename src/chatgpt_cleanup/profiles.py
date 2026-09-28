from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import plistlib
import re

from .platforms import MacOSAdapter, PlatformAdapter, WindowsAdapter


class ProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProfileIdentity:
    email: str | None = None
    workspace_name: str | None = None
    workspace_kind: str | None = None
    workspace_account_id: str | None = None
    plan: str | None = None


@dataclass(frozen=True)
class Profile:
    id: str
    display_name: str
    root: Path
    provider: str
    identity: ProfileIdentity | None = None
    desktop_data_root: Path | None = None

    @property
    def label(self) -> str:
        if self.identity:
            detail = self.identity.workspace_name or self.identity.email
            if detail:
                return f"{self.display_name} — {detail}"
        return self.display_name


def _safe_root(path: Path, home: Path) -> Path:
    path = path.absolute()
    home = home.absolute()
    if not path.is_relative_to(home):
        raise ProfileError("Profile path is outside the user home.")
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ProfileError(f"Refusing symlinked profile path: {part.name}")
        if part == home:
            break
    if not path.is_dir():
        raise ProfileError("Profile directory is missing.")
    return path


def _default_profile(platform: PlatformAdapter) -> Profile | None:
    root = platform.default_codex_home
    if not root.is_dir():
        return None
    return Profile(
        id="default",
        display_name="ChatGPT",
        root=_safe_root(root, platform.home),
        provider="official",
        desktop_data_root=(
            platform.home / "Library/Application Support/Codex"
            if isinstance(platform, MacOSAdapter)
            else None
        ),
    )


def _legacy_macos_profiles(platform: MacOSAdapter) -> list[Profile]:
    profiles: list[Profile] = []
    support = platform.home / "Library/Application Support/CodexMultiProfileLauncher"
    applications = platform.home / "Applications"
    for root in sorted(platform.home.glob(".codex-profile*")):
        match = re.fullmatch(r"\.codex-profile([2-9]|[1-9][0-9])", root.name)
        if not match:
            continue
        index = int(match.group(1))
        manifest = support / f"profile-{index}-install-manifest.json"
        selector = applications / f"ChatGPT Profile {index}.app/Contents/Info.plist"
        try:
            data = json.loads(manifest.read_text())
            info = plistlib.loads(selector.read_bytes())
        except (OSError, ValueError, TypeError):
            continue
        expected = f"local.codex-multi-profile-launcher.profile{index}"
        if not (
            data.get("ready") is True
            and data.get("profile_index") == index
            and data.get("id") == expected
            and info.get("CFBundleIdentifier") == expected
        ):
            continue
        profiles.append(
            Profile(
                id=f"profile{index}",
                display_name=f"Profile {index}",
                root=_safe_root(root, platform.home),
                provider="codex-multi-profile-launcher",
                desktop_data_root=(
                    platform.home
                    / f"Library/Application Support/Codex-Profile{index}"
                ),
            )
        )
    return profiles


def _plura_metadata_root(platform: PlatformAdapter) -> Path | None:
    if isinstance(platform, MacOSAdapter):
        return platform.home / "Library/Application Support/PluraDesktop"
    if isinstance(platform, WindowsAdapter):
        base = platform.home
        local = Path(
            os.environ.get("LOCALAPPDATA", str(base / "AppData/Local"))
        )
        return local / "PluraDesktop"
    return None


def _plura_user_data_root(platform: PlatformAdapter, index: int) -> Path | None:
    if isinstance(platform, MacOSAdapter):
        return platform.home / f"Library/Application Support/Codex-Profile{index}"
    if isinstance(platform, WindowsAdapter):
        local = Path(
            os.environ.get(
                "LOCALAPPDATA",
                str(platform.home / "AppData/Local"),
            )
        )
        return local / f"Codex-Profile{index}"
    return None


def _plura_profiles(platform: PlatformAdapter) -> list[Profile]:
    metadata = _plura_metadata_root(platform)
    if metadata is None or not metadata.is_dir() or metadata.is_symlink():
        return []
    profiles: list[Profile] = []
    for manifest in sorted(metadata.glob("profile-*-install-manifest.json")):
        if manifest.is_symlink() or not manifest.is_file():
            continue
        match = re.fullmatch(r"profile-([2-9]|[1-9][0-9])-install-manifest\.json", manifest.name)
        if not match:
            continue
        index = int(match.group(1))
        try:
            data = json.loads(manifest.read_text())
        except (OSError, ValueError, TypeError):
            continue
        expected = f"local.plura-desktop.profile{index}"
        if not (
            data.get("ready") is True
            and data.get("profile_index") == index
            and data.get("id") == expected
        ):
            continue
        root = platform.home / f".codex-profile{index}"
        user_data = _plura_user_data_root(platform, index)
        if user_data is None:
            continue
        if isinstance(platform, MacOSAdapter):
            selector = platform.home / f"Applications/ChatGPT Profile {index}.app/Contents/Info.plist"
            try:
                info = plistlib.loads(selector.read_bytes())
            except (OSError, ValueError, TypeError):
                continue
            if info.get("CFBundleIdentifier") != expected:
                continue
        app_executable = data.get("app_executable")
        if not isinstance(app_executable, str) or not Path(app_executable).is_file():
            continue
        try:
            safe_root = _safe_root(root, platform.home)
        except ProfileError:
            continue
        profiles.append(
            Profile(
                id=f"profile{index}",
                display_name=f"Profile {index}",
                root=safe_root,
                provider="plura-desktop",
                desktop_data_root=user_data,
            )
        )
    return profiles


def discover_profiles(platform: PlatformAdapter) -> list[Profile]:
    result: list[Profile] = []
    default = _default_profile(platform)
    if default:
        result.append(default)
    result.extend(_plura_profiles(platform))
    if isinstance(platform, MacOSAdapter):
        result.extend(_legacy_macos_profiles(platform))
    seen: set[Path] = set()
    unique: list[Profile] = []
    for item in result:
        canonical = item.root.resolve()
        if canonical in seen:
            continue
        seen.add(canonical)
        unique.append(item)
    return unique
