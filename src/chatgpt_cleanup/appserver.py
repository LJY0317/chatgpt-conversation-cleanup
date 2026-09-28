from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid

from . import __version__
from .platforms import PlatformAdapter, PlatformError
from .profiles import Profile


class AppServerError(RuntimeError):
    def __init__(self, message: str, *, code=None):
        super().__init__(message)
        self.code = code


def _reader(stream, output):
    try:
        for line in stream:
            output.put(line)
    finally:
        output.put(None)


class AppServerSession:
    """Small fail-closed JSONL client for the bundled official app-server."""

    def __init__(
        self,
        profile: Profile,
        platform: PlatformAdapter,
        *,
        timeout: float = 5,
        popen=subprocess.Popen,
    ):
        self.profile = profile
        self.platform = platform
        self.timeout = timeout
        self.popen = popen
        self.proc = None
        self.output = None
        self.reader = None

    def __enter__(self):
        try:
            codex = self.platform.codex_cli()
        except PlatformError as error:
            raise AppServerError(str(error)) from error
        if not codex.is_file():
            raise AppServerError("Official Codex CLI is unavailable.")
        env = dict(os.environ, CODEX_HOME=str(self.profile.root))
        env.pop("CODEX_PROFILE", None)
        self.proc = self.popen(
            [str(codex), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        if self.proc.stdin is None or self.proc.stdout is None:
            self.close()
            raise AppServerError("Could not open the official Codex app-server transport.")
        self.output = queue.Queue()
        self.reader = threading.Thread(
            target=_reader,
            args=(self.proc.stdout, self.output),
            daemon=True,
        )
        self.reader.start()
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "chatgpt-conversation-cleanup",
                        "title": "ChatGPT Conversation Cleanup",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": False},
                },
                request_id="cleanup-init-" + uuid.uuid4().hex,
            )
            self._send({"method": "initialized"})
        except BaseException:
            self.close()
            raise
        return self

    def _send(self, message):
        if self.proc is None or self.proc.stdin is None:
            raise AppServerError("Codex app-server is not connected.")
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def _wait(self, request_id, timeout):
        if self.output is None:
            raise AppServerError("Codex app-server is not connected.")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError("Timed out waiting for the official Codex app-server.")
            try:
                line = self.output.get(timeout=remaining)
            except queue.Empty as error:
                raise AppServerError(
                    "Timed out waiting for the official Codex app-server."
                ) from error
            if line is None:
                raise AppServerError(
                    "Codex app-server closed before returning a response."
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("method") and "id" in message:
                # The cleanup utility never guesses answers to approval or
                # elicitation requests. If a future delete/list protocol needs
                # an interactive server callback, fail closed until supported.
                raise AppServerError(
                    "Unexpected server request during a Codex app-server operation."
                )
            if message.get("id") != request_id:
                continue
            error = message.get("error")
            if error is not None:
                code = error.get("code") if isinstance(error, dict) else None
                detail = error.get("message") if isinstance(error, dict) else None
                detail = detail if isinstance(detail, str) and detail.strip() else None
                raise AppServerError(
                    detail
                    or (
                        "Official app-server request failed"
                        + (f" (code {code})." if code is not None else ".")
                    ),
                    code=code,
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise AppServerError(
                    "Official app-server returned an unexpected response."
                )
            return result

    def request(self, method, params, *, request_id=None, timeout=None):
        request_id = request_id or "cleanup-" + uuid.uuid4().hex
        self._send({"id": request_id, "method": method, "params": params})
        return self._wait(request_id, self.timeout if timeout is None else timeout)

    def account(self):
        return self.request("account/read", {"refreshToken": False})

    def list_threads(
        self,
        *,
        archived: bool,
        cursor: str | None = None,
        limit: int = 100,
        include_derived: bool = True,
        use_state_db_only: bool = False,
    ):
        source_kinds = ["cli", "vscode", "appServer"]
        if include_derived:
            source_kinds = [
                "cli",
                "vscode",
                "exec",
                "appServer",
                "subAgent",
                "subAgentReview",
                "subAgentCompact",
                "subAgentThreadSpawn",
                "subAgentOther",
                "unknown",
            ]
        return self.request(
            "thread/list",
            {
                "cursor": cursor,
                "limit": limit,
                "archived": archived,
                "searchTerm": None,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "sourceKinds": source_kinds,
                "useStateDbOnly": use_state_db_only,
            },
        )

    def delete_thread(self, thread_id: str):
        return self.request("thread/delete", {"threadId": thread_id}, timeout=90)

    def close(self):
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Do not signal an official app-server process. Closing its stdio is
            # the ownership boundary; current bundled builds exit cleanly on EOF.
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if self.reader is not None:
            self.reader.join(timeout=0.5)
        self.reader = None
        self.output = None

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
