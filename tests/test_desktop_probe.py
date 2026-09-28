import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from chatgpt_cleanup.desktop_probe import (
    CdpTarget,
    Presence,
    _debug_port_from_processes,
    _probe_expression,
    discover_cdp_target,
    probe_conversation_presence,
)
from chatgpt_cleanup.platforms import MacOSAdapter
from chatgpt_cleanup.profiles import Profile


class JsonResponse(io.StringIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class FakeClient:
    def __init__(self, target, payload):
        self.target = target
        self.payload = payload
        self.expression = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def evaluate(self, expression):
        self.expression = expression
        return json.dumps(self.payload)


class DesktopProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / ".codex-profile2"
        self.root.mkdir()
        self.data = self.home / "Library/Application Support/Codex-Profile2"
        self.data.mkdir(parents=True)
        self.profile = Profile(
            "profile2",
            "Profile 2",
            self.root,
            "plura-desktop",
            desktop_data_root=self.data,
        )
        self.platform = MacOSAdapter(home=self.home)

    def process_line(self, port=50747, *, data_root=None, address="127.0.0.1"):
        data_root = data_root or self.data
        return (
            "123 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT "
            f"--user-data-dir={data_root} "
            f"--remote-debugging-address={address} "
            f"--remote-debugging-port={port}"
        )

    def test_debug_port_requires_exact_profile_and_localhost(self):
        self.assertEqual(
            _debug_port_from_processes(self.profile, self.process_line()),
            50747,
        )
        other = self.home / "Library/Application Support/Codex-Profile3"
        self.assertIsNone(
            _debug_port_from_processes(
                self.profile,
                self.process_line(data_root=other),
            )
        )
        default_profile = Profile(
            "default",
            "ChatGPT",
            self.home / ".codex",
            "official",
            desktop_data_root=(
                self.home / "Library/Application Support/Codex"
            ),
        )
        self.assertIsNone(
            _debug_port_from_processes(
                default_profile,
                self.process_line(),
            )
        )
        self.assertIsNone(
            _debug_port_from_processes(
                self.profile,
                self.process_line(address="0.0.0.0"),
            )
        )
        self.assertIsNone(
            _debug_port_from_processes(
                self.profile,
                self.process_line(50747) + "\n" + self.process_line(50748),
            )
        )

    def test_target_discovery_validates_current_renderer(self):
        def run(*args, **kwargs):
            return SimpleNamespace(stdout=self.process_line())

        payload = [
            {
                "type": "page",
                "url": "app://-/index.html",
                "webSocketDebuggerUrl": (
                    "ws://127.0.0.1:50747/devtools/page/abc123"
                ),
            }
        ]

        def opener(url, timeout):
            self.assertEqual(url, "http://127.0.0.1:50747/json/list")
            self.assertEqual(timeout, 2)
            return JsonResponse(json.dumps(payload))

        target = discover_cdp_target(
            self.profile,
            self.platform,
            run=run,
            opener=opener,
        )
        self.assertEqual(
            target,
            CdpTarget(
                port=50747,
                websocket_url="ws://127.0.0.1:50747/devtools/page/abc123",
            ),
        )

        payload[0]["webSocketDebuggerUrl"] = (
            "ws://192.0.2.10:50747/devtools/page/abc123"
        )
        self.assertIsNone(
            discover_cdp_target(
                self.profile,
                self.platform,
                run=run,
                opener=opener,
            )
        )

    def test_probe_expression_avoids_hashed_or_minified_names(self):
        expression = _probe_expression(["conversation-a"])
        self.assertNotIn("app-shared-36eae88777f2", expression)
        self.assertNotIn("Wmt", expression)
        self.assertNotIn("uF", expression)
        self.assertIn("getRequestTarget", expression)
        self.assertNotIn("x-openai-attach-desktop-surface", expression.lower())
        self.assertNotIn("oai-did", expression.lower())
        self.assertIn("/conversations/batch", expression)
        self.assertIn("conversation_request_alias_unavailable", expression)
        self.assertIn("conversation_request_export_unavailable", expression)
        self.assertIn("rateLimited", expression)
        self.assertIn("status===429", expression)
        self.assertIn("slice(index,index+10)", expression)
        self.assertIn("setTimeout(resolve,750)", expression)
        self.assertIn("setTimeout(resolve,1250)", expression)
        self.assertIn("secondPresent.has(id)?'unverified':'missing'", expression)
        self.assertNotIn("Promise.all", expression)

    def test_presence_results_are_strictly_parsed(self):
        target = CdpTarget(50747, "ws://127.0.0.1:50747/devtools/page/test")
        payload = {
            "ok": True,
            "results": [
                ["conversation-a", "present"],
                ["conversation-b", "missing"],
                ["conversation-c", "unverified"],
            ],
        }
        client = FakeClient(target, payload)
        result = probe_conversation_presence(
            self.profile,
            {"conversation-a", "conversation-b", "conversation-c"},
            self.platform,
            target=target,
            client_factory=lambda _target: client,
        )
        self.assertEqual(
            result,
            {
                "conversation-a": Presence.PRESENT,
                "conversation-b": Presence.MISSING,
                "conversation-c": Presence.UNVERIFIED,
            },
        )
        self.assertIn("conversation-a", client.expression)

    def test_rate_limited_probe_discards_partial_results(self):
        target = CdpTarget(50747, "ws://127.0.0.1:50747/devtools/page/test")
        payload = {
            "ok": False,
            "rateLimited": True,
            "results": [["conversation-a", "missing"]],
        }
        result = probe_conversation_presence(
            self.profile,
            {"conversation-a", "conversation-b"},
            self.platform,
            target=target,
            client_factory=lambda _target: FakeClient(target, payload),
        )
        self.assertIsNone(result)

    def test_skipped_ids_are_unverified_without_entering_probe(self):
        target = CdpTarget(50747, "ws://127.0.0.1:50747/devtools/page/test")
        result = probe_conversation_presence(
            self.profile,
            {"conversation-a"},
            self.platform,
            skip_ids={"conversation-a"},
            target=target,
            client_factory=lambda _target: self.fail("client should not be opened"),
        )
        self.assertEqual(result, {"conversation-a": Presence.UNVERIFIED})

    def test_malformed_or_duplicate_results_fail_closed(self):
        target = CdpTarget(50747, "ws://127.0.0.1:50747/devtools/page/test")
        payload = {
            "ok": True,
            "results": [
                ["conversation-a", "missing"],
                ["conversation-a", "present"],
            ],
        }
        result = probe_conversation_presence(
            self.profile,
            {"conversation-a", "conversation-b"},
            self.platform,
            target=target,
            client_factory=lambda _target: FakeClient(target, payload),
        )
        self.assertEqual(
            result,
            {
                "conversation-a": Presence.UNVERIFIED,
                "conversation-b": Presence.UNVERIFIED,
            },
        )


if __name__ == "__main__":
    unittest.main()
