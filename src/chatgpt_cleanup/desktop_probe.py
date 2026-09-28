from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
from urllib.parse import urlparse
from urllib.request import urlopen

from .platforms import MacOSAdapter, PlatformAdapter
from .profiles import Profile


class Presence(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    UNVERIFIED = "unverified"


class DesktopProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class CdpTarget:
    port: int
    websocket_url: str


_PORT_RE = re.compile(r"(?:^|\s)--remote-debugging-port=(\d{2,5})(?:\s|$)")


def _has_exact_process_argument(command: str, argument: str) -> bool:
    return re.search(
        rf"(?:^|\s){re.escape(argument)}(?=\s|$)",
        command,
    ) is not None


def _debug_port_from_processes(profile: Profile, process_text: str) -> int | None:
    data_root = profile.desktop_data_root
    if data_root is None:
        return None
    marker = f"--user-data-dir={data_root}"
    ports: set[int] = set()
    for line in process_text.splitlines():
        if (
            "/Contents/MacOS/ChatGPT" not in line
            or not _has_exact_process_argument(line, marker)
            or not _has_exact_process_argument(
                line, "--remote-debugging-address=127.0.0.1"
            )
        ):
            continue
        match = _PORT_RE.search(line)
        if not match:
            continue
        port = int(match.group(1))
        if 1024 <= port <= 65535:
            ports.add(port)
    if len(ports) != 1:
        return None
    return next(iter(ports))


def discover_cdp_target(
    profile: Profile,
    platform: PlatformAdapter,
    *,
    run=subprocess.run,
    opener=urlopen,
) -> CdpTarget | None:
    if not isinstance(platform, MacOSAdapter):
        return None
    try:
        process = run(
            ["/bin/ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    port = _debug_port_from_processes(profile, process.stdout)
    if port is None:
        return None
    try:
        with opener(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
            payload = json.load(response)
    except Exception:
        return None
    if not isinstance(payload, list):
        return None
    targets = [
        item
        for item in payload
        if isinstance(item, dict)
        and item.get("type") == "page"
        and item.get("url") == "app://-/index.html"
        and isinstance(item.get("webSocketDebuggerUrl"), str)
    ]
    if len(targets) != 1:
        return None
    websocket_url = targets[0]["webSocketDebuggerUrl"]
    parsed = urlparse(websocket_url)
    if (
        parsed.scheme != "ws"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != port
        or not parsed.path.startswith("/devtools/page/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    return CdpTarget(port=port, websocket_url=websocket_url)


class _CdpClient:
    def __init__(self, target: CdpTarget, *, timeout: float = 70):
        parsed = urlparse(target.websocket_url)
        self._socket = socket.create_connection(
            (parsed.hostname, parsed.port),
            timeout=min(timeout, 5),
        )
        self._socket.settimeout(timeout)
        self._buffer = b""
        self._next_id = 1
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._socket.sendall(request.encode("ascii"))
        response = self._read_http_headers()
        first_line = response.split(b"\r\n", 1)[0]
        expected_accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        headers = {}
        for line in response.split(b"\r\n")[1:]:
            if b":" not in line:
                continue
            name, value = line.split(b":", 1)
            headers[name.decode("ascii", "ignore").lower()] = value.decode(
                "ascii", "ignore"
            ).strip()
        if (
            b" 101 " not in first_line
            or headers.get("upgrade", "").lower() != "websocket"
            or headers.get("sec-websocket-accept") != expected_accept
        ):
            self.close()
            raise DesktopProbeError("Desktop debugging handshake failed.")

    def _read_http_headers(self) -> bytes:
        while b"\r\n\r\n" not in self._buffer:
            data = self._socket.recv(4096)
            if not data:
                raise DesktopProbeError("Desktop debugging connection closed.")
            self._buffer += data
        head, self._buffer = self._buffer.split(b"\r\n\r\n", 1)
        return head

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def _recv_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            data = self._socket.recv(max(4096, size - len(self._buffer)))
            if not data:
                raise DesktopProbeError("Desktop debugging connection closed.")
            self._buffer += data
        value, self._buffer = self._buffer[:size], self._buffer[size:]
        return value

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        head = self._recv_exact(2)
        final = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if head[1] & 0x80 else None
        payload = self._recv_exact(length)
        if mask is not None:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return final, opcode, payload

    def _recv_text(self) -> str:
        parts: list[bytes] = []
        collecting = False
        while True:
            final, opcode, payload = self._recv_frame()
            if opcode == 0x8:
                raise DesktopProbeError("Desktop debugging connection closed.")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x1:
                parts = [payload]
                collecting = True
            elif opcode == 0x0 and collecting:
                parts.append(payload)
            else:
                continue
            if final:
                return b"".join(parts).decode("utf-8")

    def evaluate(self, expression: str) -> str:
        request_id = self._next_id
        self._next_id += 1
        self._send_frame(
            0x1,
            json.dumps(
                {
                    "id": request_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        while True:
            try:
                message = json.loads(self._recv_text())
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise DesktopProbeError("Desktop debugging returned invalid data.") from error
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                raise DesktopProbeError("Desktop debugging evaluation failed.")
            result = message.get("result")
            if not isinstance(result, dict) or result.get("exceptionDetails") is not None:
                raise DesktopProbeError("Desktop native verification failed.")
            remote = result.get("result")
            if not isinstance(remote, dict) or not isinstance(remote.get("value"), str):
                raise DesktopProbeError("Desktop native verification returned an unexpected result.")
            return remote["value"]

    def close(self) -> None:
        sock = getattr(self, "_socket", None)
        if sock is None:
            return
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        self._socket = None
        try:
            sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def _probe_expression(conversation_ids: list[str]) -> str:
    encoded_ids = json.dumps(conversation_ids, separators=(",", ":"))
    # Discover the current request module by the stable /conversations/batch
    # capability used by ChatGPT Desktop itself. Desktop also groups IDs in
    # batches of 10. We keep the same batch size but run sequentially and pace
    # requests to avoid the burst that the old one-ID-per-request probe caused.
    # Asset hashes and minified symbol names are never part of the Python
    # contract.
    return f"""(async()=>{{try{{
const entry=[...document.scripts].map(s=>s.src).find(Boolean);
if(!entry)return JSON.stringify({{ok:false,reason:'no_entry'}});
const text=await fetch(entry).then(r=>r.text());
const specs=[...new Set([...text.matchAll(/[\"'](\\.\\/[^\"']+\\.js)[\"']/g)].map(m=>m[1]))].slice(0,32);
const sources=[];
for(const spec of specs){{try{{const url=new URL(spec,entry).href;const source=await fetch(url).then(r=>r.text());sources.push([url,source]);}}catch{{}}}}
const uses=[];
for(const [url,source] of sources){{for(const match of source.matchAll(/([A-Za-z_$][A-Za-z0-9_$]*)\\.safePost\\(\\s*[`\"']\\/conversations\\/batch[`\"']/g))uses.push([url,source,match[1]]);}}
const aliases=[...new Set(uses.map(item=>item[2]))];
if(aliases.length!==1)return JSON.stringify({{ok:false,reason:'conversation_request_alias_unavailable'}});
const alias=aliases[0],mappings=[];
for(const [url,source] of uses){{for(const match of source.matchAll(/import\\s*\\{{([^}}]*)\\}}\\s*from\\s*[`\"'](\\.\\/[^`\"']+\\.js)[`\"']/g)){{for(const raw of match[1].split(',')){{const binding=raw.trim().match(/^([A-Za-z_$][A-Za-z0-9_$]*)(?:\\s+as\\s+([A-Za-z_$][A-Za-z0-9_$]*))?$/);if(!binding)continue;const exported=binding[1],local=binding[2]||binding[1];if(local===alias)mappings.push([new URL(match[2],url).href,exported]);}}}}}}
const uniqueMappings=[...new Map(mappings.map(item=>[item.join('::'),item])).values()];
if(uniqueMappings.length!==1)return JSON.stringify({{ok:false,reason:'conversation_request_export_unavailable'}});
const [moduleUrl,exportName]=uniqueMappings[0],mod=await import(moduleUrl),api=mod[exportName];
if(!(api&&typeof api==='object'&&typeof api.safePost==='function'&&typeof api.getRequestTarget==='function'))return JSON.stringify({{ok:false,reason:'conversation_request_capability_unavailable'}});
const ids={encoded_ids};
async function checkChunk(chunk){{const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);try{{const out=await api.safePost('/conversations/batch',{{requestBody:{{conversation_ids:chunk}},signal:controller.signal}});if(!Array.isArray(out))return{{ok:false,rateLimited:false}};const returned=out.filter(item=>item&&typeof item.id==='string').map(item=>item.id),unique=new Set(returned);if(unique.size!==returned.length||returned.some(id=>!chunk.includes(id)))return{{ok:false,rateLimited:false}};return{{ok:true,present:[...unique]}};}}catch(error){{const status=Number(error?.status??error?.responseStatus??0);return{{ok:false,rateLimited:status===429}};}}finally{{clearTimeout(timer);}}}}
async function checkMany(targetIds){{const present=new Set;for(let index=0;index<targetIds.length;index+=10){{const chunk=targetIds.slice(index,index+10),checked=await checkChunk(chunk);if(!checked.ok)return checked;for(const id of checked.present)present.add(id);if(index+10<targetIds.length)await new Promise(resolve=>setTimeout(resolve,750));}}return{{ok:true,present:[...present]}};}}
const first=await checkMany(ids);if(!first.ok)return JSON.stringify({{ok:false,rateLimited:first.rateLimited===true,reason:'detail_check_failed'}});
const firstPresent=new Set(first.present),missingOnce=ids.filter(id=>!firstPresent.has(id));
if(missingOnce.length===0)return JSON.stringify({{ok:true,rateLimited:false,results:ids.map(id=>[id,'present'])}});
await new Promise(resolve=>setTimeout(resolve,1250));
const second=await checkMany(missingOnce);if(!second.ok)return JSON.stringify({{ok:false,rateLimited:second.rateLimited===true,reason:'detail_recheck_failed'}});
const secondPresent=new Set(second.present),results=ids.map(id=>[id,firstPresent.has(id)?'present':secondPresent.has(id)?'unverified':'missing']);
return JSON.stringify({{ok:true,rateLimited:false,results}});
}}catch{{return JSON.stringify({{ok:false,reason:'probe_failed'}});}}}})()"""


def probe_conversation_presence(
    profile: Profile,
    conversation_ids: set[str],
    platform: PlatformAdapter,
    *,
    skip_ids: set[str] | None = None,
    target: CdpTarget | None = None,
    client_factory=_CdpClient,
) -> dict[str, Presence] | None:
    if not conversation_ids:
        return {}
    target = target or discover_cdp_target(profile, platform)
    if target is None:
        return None
    all_ids = sorted(conversation_ids)
    skipped = conversation_ids & (skip_ids or set())
    ids = sorted(conversation_ids - skipped)
    if not ids:
        return {
            conversation_id: Presence.UNVERIFIED
            for conversation_id in all_ids
        }
    try:
        with client_factory(target) as client:
            raw = client.evaluate(
                _probe_expression(ids)
            )
        payload = json.loads(raw)
    except (DesktopProbeError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return {conversation_id: Presence.UNVERIFIED for conversation_id in all_ids}
    if isinstance(payload, dict) and payload.get("rateLimited") is True:
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return {conversation_id: Presence.UNVERIFIED for conversation_id in all_ids}
    results = payload.get("results")
    if not isinstance(results, list):
        return {conversation_id: Presence.UNVERIFIED for conversation_id in all_ids}
    expected = set(ids)
    states: dict[str, Presence] = {}
    for item in results:
        if not (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and item[0] in expected
            and item[0] not in states
        ):
            return {conversation_id: Presence.UNVERIFIED for conversation_id in all_ids}
        try:
            states[item[0]] = Presence(item[1])
        except (TypeError, ValueError):
            states[item[0]] = Presence.UNVERIFIED
    for conversation_id in ids:
        states.setdefault(conversation_id, Presence.UNVERIFIED)
    for conversation_id in skipped:
        states[conversation_id] = Presence.UNVERIFIED
    return states
