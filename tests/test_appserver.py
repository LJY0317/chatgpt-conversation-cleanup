from __future__ import annotations

from pathlib import Path
import unittest

from chatgpt_cleanup.appserver import AppServerSession
from chatgpt_cleanup.profiles import Profile


class AppServerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile("default", "ChatGPT", Path("/tmp/synthetic-codex"), "test")
        self.session = AppServerSession(self.profile, object())
        self.calls = []

        def request(method, params, **kwargs):
            self.calls.append((method, params, kwargs))
            return {"data": [], "nextCursor": None}

        self.session.request = request

    def test_list_threads_uses_official_inventory_contract(self):
        self.session.list_threads(
            archived=True,
            cursor="cursor-1",
            limit=77,
            include_derived=True,
            use_state_db_only=False,
        )
        method, params, kwargs = self.calls[-1]
        self.assertEqual(method, "thread/list")
        self.assertEqual(params["cursor"], "cursor-1")
        self.assertEqual(params["limit"], 77)
        self.assertIs(params["archived"], True)
        self.assertIs(params["useStateDbOnly"], False)
        self.assertIn("subAgent", params["sourceKinds"])
        self.assertIn("unknown", params["sourceKinds"])
        self.assertEqual(kwargs, {})

    def test_delete_thread_uses_thread_delete_without_retry_wrapper(self):
        self.session.delete_thread("thread-id")
        method, params, kwargs = self.calls[-1]
        self.assertEqual(method, "thread/delete")
        self.assertEqual(params, {"threadId": "thread-id"})
        self.assertEqual(kwargs, {"timeout": 90})


if __name__ == "__main__":
    unittest.main()
