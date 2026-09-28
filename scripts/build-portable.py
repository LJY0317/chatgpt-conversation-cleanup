#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def _version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from chatgpt_cleanup import __version__

    return __version__


def _pyinstaller(name: str) -> Path:
    try:
        import PyInstaller  # noqa: F401
    except ImportError as error:
        raise SystemExit(
            "PyInstaller is required only to build a portable release: "
            "python -m pip install pyinstaller"
        ) from error

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--console",
        "--clean",
        "--noconfirm",
        "--paths",
        str(ROOT / "src"),
        "--name",
        name,
        str(ROOT / "run.py"),
    ]
    identity = os.environ.get("CCC_CODESIGN_IDENTITY")
    if sys.platform == "darwin" and identity:
        command[3:3] = ["--codesign-identity", identity]
    subprocess.run(command, cwd=ROOT, check=True)

    suffix = ".exe" if sys.platform == "win32" else ""
    artifact = DIST / (name + suffix)
    if not artifact.is_file():
        raise SystemExit(f"Expected portable artifact was not created: {artifact.name}")
    return artifact


def _checksum(path: Path) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    output = path.with_name(path.name + ".sha256")
    output.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return output


def _build_macos() -> tuple[Path, Path]:
    cli = _pyinstaller("chatgpt-cleanup")
    subprocess.run([str(cli), "--version"], check=True)

    app = DIST / "ChatGPT Conversation Cleanup.app"
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents/MacOS"
    resources = app / "Contents/Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    embedded = resources / "chatgpt-cleanup"
    shutil.move(str(cli), embedded)

    launcher = macos / "launcher"
    subprocess.run(
        [
            "/usr/bin/clang",
            "-Os",
            "-mmacosx-version-min=14.0",
            str(ROOT / "scripts/macos-launcher.c"),
            "-o",
            str(launcher),
        ],
        check=True,
    )

    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": "ChatGPT Conversation Cleanup",
        "CFBundleExecutable": "launcher",
        "CFBundleIdentifier": "io.github.ljy0317.chatgpt-conversation-cleanup",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "ChatGPT Conversation Cleanup",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": _version(),
        "CFBundleVersion": _version(),
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
    }
    with (app / "Contents/Info.plist").open("wb") as stream:
        plistlib.dump(info, stream, sort_keys=True)

    identity = os.environ.get("CCC_CODESIGN_IDENTITY")
    if identity:
        subprocess.run(
            [
                "/usr/bin/codesign",
                "--force",
                "--deep",
                "--options",
                "runtime",
                "--sign",
                identity,
                str(app),
            ],
            check=True,
        )

    archive = DIST / "ChatGPT Conversation Cleanup.app.zip"
    archive.unlink(missing_ok=True)
    subprocess.run(
        [
            "/usr/bin/ditto",
            "-c",
            "-k",
            "--norsrc",
            "--noextattr",
            "--keepParent",
            str(app),
            str(archive),
        ],
        check=True,
    )
    return archive, _checksum(archive)


def _build_windows() -> tuple[Path, Path]:
    artifact = _pyinstaller("ChatGPT Conversation Cleanup")
    subprocess.run([str(artifact), "--version"], check=True)
    return artifact, _checksum(artifact)


def main() -> int:
    if sys.platform == "darwin":
        artifact, checksum = _build_macos()
    elif sys.platform == "win32":
        artifact, checksum = _build_windows()
    else:
        raise SystemExit(
            "Portable release builds are currently verified only for macOS and Windows."
        )
    print(artifact)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
