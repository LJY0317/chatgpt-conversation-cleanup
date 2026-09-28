from __future__ import annotations

import contextlib
import os
from pathlib import Path

from .core import SafetyError, safe_path


@contextlib.contextmanager
def operation_lock(directory: Path):
    path = safe_path(directory / "operation.lock", directory, must_exist=False)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise SafetyError("Another cleanup operation is already running.") from error
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SafetyError("Another cleanup operation is already running.") from error
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
