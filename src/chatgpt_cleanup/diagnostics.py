"""Bounded, privacy-safe evidence for blocked Desktop cleanup."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path


def record_blocked_check(platform, *, operation: str, profiles, error: Exception) -> None:
    """Best-effort local log; never log command lines, profile paths or auth data."""
    try:
        owned_root = Path(platform.data_root)
        root = owned_root / "diagnostics"
        if owned_root.is_symlink() or root.is_symlink():
            return
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / "desktop-stop.jsonl"
        if path.is_symlink():
            return
        event = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": "desktop_stop_blocked",
            "operation": operation,
            "profiles": [p.id for p in profiles],
            "reason": "process-running" if getattr(error, "process_kind", None)
                      else "process-check-unavailable",
            "process_kind": getattr(error, "process_kind", None),
            "pid": getattr(error, "pid", None),
        }
        # Avoid unbounded data accumulation. Retain the latest 64 events.
        previous = []
        if path.is_file():
            previous = path.read_text(encoding="utf-8").splitlines()[-63:]
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join([*previous, json.dumps(event, separators=(",", ":"))]) + "\n")
    except (OSError, ValueError, TypeError, AttributeError):
        return
