from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import mmap
from pathlib import Path
import re
import time

from .platforms import MacOSAdapter, PlatformAdapter
from .profiles import Profile


# Structured server outcomes observed in current ChatGPT Desktop logs. These
# describe a conversation that the current profile could not retrieve. Keep
# them isolated here so future Desktop changes fail closed.
UNAVAILABLE_ERROR_CODES = frozenset(
    {
        "conversation_deleted",
        "conversation_inaccessible",
        "conversation_not_found",
        "history_disabled_conversation_not_found",
        "history_disabled_conversation_expired",
    }
)

# Only these server outcomes are strong enough to justify automatically
# offering a local catalog row for cleanup. conversation_inaccessible is
# deliberately excluded: a live Project conversation can produce that error
# in Desktop while remaining accessible on chatgpt.com.
SAFE_CLEANUP_ERROR_CODES = frozenset(
    {
        "conversation_deleted",
        "conversation_not_found",
        "history_disabled_conversation_not_found",
        "history_disabled_conversation_expired",
    }
)

MAX_LOG_AGE_DAYS = 90
MAX_LOG_FILE_BYTES = 32 * 1024 * 1024
MAX_FAILURE_LINE_BYTES = 64 * 1024
_ERROR_CODE_BYTES_RE = re.compile(rb"(?:^|\s)errorCode=([^\s]+)")
_SESSION_RE = re.compile(r"^codex-desktop-([0-9a-fA-F-]{36})-")
_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)"
)


@dataclass(frozen=True)
class DesktopFailureEvidence:
    conversation_id: str
    error_code: str
    status: int
    observed_at: datetime | None


def _log_root(platform: PlatformAdapter) -> Path | None:
    if isinstance(platform, MacOSAdapter):
        return platform.home / "Library/Logs/com.openai.codex"
    return None


def _profile_markers(profile: Profile) -> tuple[str, ...]:
    markers = [str(profile.root)]
    if profile.desktop_data_root is not None:
        markers.append(str(profile.desktop_data_root))
    return tuple(marker for marker in markers if marker)


_MARKER_BOUNDARY_BYTES = frozenset(b"/\\ \t\r\n\"'=:,;])}")


def _mmap_has_marker(data: mmap.mmap, marker: bytes) -> bool:
    start = 0
    while True:
        index = data.find(marker, start)
        if index < 0:
            return False
        end = index + len(marker)
        if end == len(data) or data[end] in _MARKER_BOUNDARY_BYTES:
            return True
        start = index + 1


def _eligible_files(root: Path, *, now: float) -> list[Path]:
    if not root.is_dir():
        return []
    cutoff = now - MAX_LOG_AGE_DAYS * 86400
    files = []
    for path in root.rglob("*.log"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if (
            not path.is_file()
            or stat.st_mtime < cutoff
            or stat.st_size <= 0
            or stat.st_size > MAX_LOG_FILE_BYTES
        ):
            continue
        files.append(path)
    return files


def _observed_at(line: str) -> datetime | None:
    match = _TIMESTAMP_RE.match(line)
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None


def _observed_at_bytes(line: bytes) -> datetime | None:
    try:
        return _observed_at(line[:64].decode("ascii", "ignore"))
    except (UnicodeDecodeError, ValueError):
        return None


def desktop_failure_evidence_many(
    profiles: list[Profile],
    conversation_ids_by_profile: dict[str, set[str]],
    platform: PlatformAdapter,
    *,
    log_root: Path | None = None,
    now: float | None = None,
) -> dict[str, dict[str, DesktopFailureEvidence]]:
    """Index Desktop 404 evidence for several profiles in one bounded log pass."""

    result = {profile.id: {} for profile in profiles}
    if not profiles or not any(conversation_ids_by_profile.values()):
        return result
    root = Path(log_root) if log_root is not None else _log_root(platform)
    if root is None:
        return result

    files = _eligible_files(root, now=time.time() if now is None else now)
    markers_by_profile = {
        profile.id: tuple(
            marker.encode("utf-8", "surrogateescape")
            for marker in _profile_markers(profile)
        )
        for profile in profiles
    }
    session_profiles: dict[str, set[str]] = defaultdict(set)
    session_files: dict[str, list[Path]] = defaultdict(list)
    session_failures: dict[
        str, list[tuple[bytes, str, datetime | None]]
    ] = defaultdict(list)

    for path in files:
        match = _SESSION_RE.match(path.name)
        session_key = (
            match.group(1).lower() if match else "file:" + str(path)
        )
        session_files[session_key].append(path)
        try:
            with path.open("rb") as handle:
                data = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
                try:
                    for profile_id, markers in markers_by_profile.items():
                        if markers and any(
                            _mmap_has_marker(data, marker) for marker in markers
                        ):
                            session_profiles[session_key].add(profile_id)

                    position = 0
                    while True:
                        index = data.find(b"status=404", position)
                        if index < 0:
                            break
                        start = data.rfind(b"\n", 0, index) + 1
                        end = data.find(b"\n", index)
                        if end < 0:
                            end = len(data)
                        end = min(end, start + MAX_FAILURE_LINE_BYTES)
                        line = bytes(data[start:end])
                        error_match = _ERROR_CODE_BYTES_RE.search(line)
                        if error_match:
                            error_code = error_match.group(1).strip(b'"').decode(
                                "ascii", "ignore"
                            )
                            if error_code in UNAVAILABLE_ERROR_CODES:
                                session_failures[session_key].append(
                                    (line, error_code, _observed_at_bytes(line))
                                )
                        position = max(index + 1, end + 1)
                finally:
                    data.close()
        except (OSError, ValueError):
            continue

    encoded_ids = {
        profile_id: {
            conversation_id.encode("utf-8", "surrogateescape"): conversation_id
            for conversation_id in ids
        }
        for profile_id, ids in conversation_ids_by_profile.items()
    }
    for session_key, profile_ids in session_profiles.items():
        # A session that appears to belong to several profiles is ambiguous.
        if len(profile_ids) != 1:
            continue
        profile_id = next(iter(profile_ids))
        candidates = encoded_ids.get(profile_id, {})
        if not candidates:
            continue
        profile_result = result.setdefault(profile_id, {})
        for line, error_code, observed_at in session_failures.get(session_key, []):
            for encoded_id, conversation_id in candidates.items():
                if encoded_id not in line:
                    continue
                evidence = DesktopFailureEvidence(
                    conversation_id=conversation_id,
                    error_code=error_code,
                    status=404,
                    observed_at=observed_at,
                )
                previous = profile_result.get(conversation_id)
                if (
                    previous is None
                    or previous.observed_at is None
                    or (
                        evidence.observed_at is not None
                        and evidence.observed_at >= previous.observed_at
                    )
                ):
                    profile_result[conversation_id] = evidence

    # A historical deleted/not-found result is not enough if the same
    # conversation ID appears in a later Desktop log event. A later reference
    # can mean the conversation became usable again or the old failure was
    # transient. Fail closed by dropping that cleanup evidence.
    files_by_profile: dict[str, list[Path]] = defaultdict(list)
    for session_key, profile_ids in session_profiles.items():
        if len(profile_ids) == 1:
            files_by_profile[next(iter(profile_ids))].extend(
                session_files.get(session_key, [])
            )

    for profile_id, profile_result in result.items():
        safe = {
            conversation_id: evidence
            for conversation_id, evidence in profile_result.items()
            if evidence.error_code in SAFE_CLEANUP_ERROR_CODES
        }
        # Without a timestamp we cannot establish that the failure is the
        # latest observation, so it cannot be an automatic cleanup signal.
        for conversation_id, evidence in list(safe.items()):
            if evidence.observed_at is None:
                profile_result.pop(conversation_id, None)
                safe.pop(conversation_id, None)
        if not safe:
            continue
        encoded = {
            conversation_id.encode("utf-8", "surrogateescape"): conversation_id
            for conversation_id in safe
        }
        latest = {
            conversation_id: evidence.observed_at
            for conversation_id, evidence in safe.items()
        }
        for path in files_by_profile.get(profile_id, []):
            try:
                with path.open("rb") as handle:
                    data = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
                    try:
                        for encoded_id, conversation_id in encoded.items():
                            position = 0
                            while True:
                                index = data.find(encoded_id, position)
                                if index < 0:
                                    break
                                start = data.rfind(b"\n", 0, index) + 1
                                end = data.find(b"\n", index)
                                if end < 0:
                                    end = len(data)
                                observed_at = _observed_at_bytes(
                                    bytes(data[start : min(end, start + 256)])
                                )
                                if (
                                    observed_at is not None
                                    and observed_at > latest[conversation_id]
                                ):
                                    latest[conversation_id] = observed_at
                                position = index + len(encoded_id)
                    finally:
                        data.close()
            except (OSError, ValueError):
                continue
        for conversation_id, evidence in safe.items():
            if latest[conversation_id] > evidence.observed_at:
                profile_result.pop(conversation_id, None)
    return result


def desktop_failure_evidence(
    profile: Profile,
    conversation_ids: set[str],
    platform: PlatformAdapter,
    *,
    log_root: Path | None = None,
    now: float | None = None,
) -> dict[str, DesktopFailureEvidence]:
    """Return definite current-profile 404/unavailable observations.

    The parser intentionally ignores UI strings and error messages. It accepts
    only a structured HTTP 404 plus a known server error code, and only from a
    log session that contains a marker for the selected profile.
    """

    return desktop_failure_evidence_many(
        [profile],
        {profile.id: conversation_ids},
        platform,
        log_root=log_root,
        now=now,
    )[profile.id]
