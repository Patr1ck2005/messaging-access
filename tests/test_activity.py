"""Conversation ordering metadata: synthetic catalogs only, no account access."""
from __future__ import annotations

import contextlib
import ast
import importlib.metadata
import json
import sqlite3
import sys
import types
import unittest
from pathlib import Path

from access_fixtures import RecycleFixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wechat_qq_access import AccessError, AccessService, Conversation, PolicyStore
from wechat_qq_access.backends import QQBackend, WeChatBackend, last_active_seconds


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = RecycleFixture(prefix="wechat-qq-activity-synthetic-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_source_seconds_milliseconds_and_zoned_iso(self):
        for value in (1700000000, 1700000000.0, "1700000000", 1700000000000, "1700000000000",
                      "2023-11-14T22:13:20.000Z", "2023-11-15T06:13:20+08:00"):
            with self.subTest(value=value):
                self.assertEqual(last_active_seconds(value), 1700000000)
        self.assertEqual(last_active_seconds("2023-11-14T22:13:20.125+00:00"), 1700000000.125)
        self.assertEqual(last_active_seconds(0), 0)

    def test_invalid_and_timezone_unknown_sources_stay_unknown(self):
        for value in (None, True, False, -1, "-1", float("nan"), float("inf"), "nan", "inf", "",
                      {}, [], "not a date", "2023-11-14T22:13:20", "2023-11-14",
                      "1969-12-31T23:59:59Z", 10 ** 1000, 10 ** 15):
            with self.subTest(value=str(value)[:60]):
                self.assertIsNone(last_active_seconds(value))

    def test_public_projection_is_optional_and_strict_json_safe(self):
        original = Conversation("qq", "synthetic-account", "2:group", "Synthetic group", "group")
        self.assertIsNone(original.public()["last_active"])
        for value in (float("nan"), float("inf"), -1, True, "1700000000", 10 ** 1000):
            conv = Conversation("qq", original.account, original.conversation_id, original.title, "group", value)
            self.assertEqual(conv.ref, original.ref)
            self.assertIsNone(conv.public()["last_active"])
            json.dumps(conv.public(), allow_nan=False)

    def test_catalog_and_authorized_projection_do_not_bypass_policy(self):
        first = Conversation("qq", "synthetic-account", "1:a", "Same title", "private", 1700000000)
        denied = Conversation("qq", first.account, "2:b", "Same title", "group", 1700000001)
        other = Conversation("qq", "another-account", "1:a", "Same title", "private", 1700000002)
        conversations = [first, denied, other]
        backend = types.SimpleNamespace(catalog=lambda: ([{"platform": "qq", "account": first.account},
                                                           {"platform": "qq", "account": other.account}],
                                                          conversations, []))
        policy = PolicyStore(self.root)
        service = AccessService(policy, backend)
        catalog = service.execute({"action": "catalog"})["conversations"]
        self.assertEqual([row["last_active"] for row in catalog], [1700000000, 1700000001, 1700000002])
        policy.write([{"platform": "qq", "account": first.account, "mode": "allow", "conversations": ["1:a"]}])
        allowed = service.execute({"action": "conversations"})["conversations"]
        self.assertEqual(allowed, [first.public()])
        with self.assertRaises(AccessError):
            service.execute({"action": "authorize", "refs": [denied.ref]})

    def test_wechat_session_projection_reads_only_identity_and_time(self):
        path = self.root / "session.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE SessionTable (username, last_timestamp)")
        connection.executemany("INSERT INTO SessionTable VALUES (?,?)", [
            ("chat-a", 1700000000), ("chat-b@chatroom", 1700000001000),
            ("chat-c", "invalid"), ("chat-a", 1699999999)])
        connection.commit()
        connection.close()
        opened = []
        def open_session(rel):
            opened.append(rel)
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            return conn
        db = types.SimpleNamespace(_db_files=[("contact", "contact.db", None), ("session", "session.db", None)],
                                   _open=open_session)
        self.assertEqual(WeChatBackend._activity(db), {"chat-a": 1700000000, "chat-b@chatroom": 1700000001})
        self.assertEqual(opened, ["session"])
        self.assertEqual(WeChatBackend._activity(types.SimpleNamespace(_db_files=[])), {})

    def test_pinned_wechat_session_schema_matches_metadata_projection(self):
        # Source inspection only: importing upstream initializes Windows modules.
        try:
            distribution = importlib.metadata.distribution("wechatauto-replica")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("pinned upstream source is not installed in this interpreter")
        self.assertEqual(distribution.version, "1.2.6.1")
        tree = ast.parse(Path(distribution.locate_file("wechatauto/db.py")).read_text(encoding="utf-8"))
        db_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WeChatDB")
        sessions = next(node for node in db_class.body if isinstance(node, ast.FunctionDef) and node.name == "get_sessions")
        literals = " ".join(node.value for node in ast.walk(sessions)
                            if isinstance(node, ast.Constant) and isinstance(node.value, str))
        for field in ("session.db", "SessionTable", "username", "last_timestamp"):
            self.assertIn(field, literals)

    def test_wechat_missing_activity_keeps_catalog_order_and_conversations(self):
        wx = object.__new__(WeChatBackend)
        wx.quiet = contextlib.nullcontext
        wx.sink = types.SimpleNamespace(warning_count=0)
        wx.accounts = {}
        wx.module = types.SimpleNamespace(list_accounts=lambda: [{"wxid": "synthetic-account"}])
        rows = [{"username": "chat-a", "name": "Same name"}, {"username": "chat-b@chatroom", "name": "Same name"}]
        db = types.SimpleNamespace(list_message_chats=lambda: rows)
        wx._db = lambda account: db
        _, conversations, warnings = wx.catalog()
        self.assertEqual([conv.conversation_id for conv in conversations], ["chat-a", "chat-b@chatroom"])
        self.assertEqual([conv.last_active for conv in conversations], [None, None])
        self.assertIn("WECHAT_ACTIVITY_UNAVAILABLE", warnings)
        wx._activity = lambda db: {"chat-b@chatroom": 1700000001}
        _, conversations, warnings = wx.catalog()
        self.assertEqual([conv.last_active for conv in conversations], [None, 1700000001])
        self.assertEqual([conv.kind for conv in conversations], ["private", "group"])
        self.assertNotIn("WECHAT_ACTIVITY_UNAVAILABLE", warnings)

    def test_qq_recent_time_enriches_canonical_contacts_and_keeps_unknowns(self):
        qq = object.__new__(QQBackend)
        qq.identity = lambda: "synthetic-account"
        verified, calls = [], []
        qq.verify = lambda account: verified.append(account)
        def request(path):
            calls.append(path)
            if path.startswith("/api/friends?"):
                return {"friends": [{"uid": "friend-a", "remark": "Canonical private"},
                                    {"uid": "never-recent", "nick": "Unknown activity"}], "hasNext": False}
            if path.startswith("/api/groups?"):
                return {"groups": [{"groupCode": "group-a", "groupName": "Canonical group"}], "hasNext": False}
            if path.startswith("/api/recent-contacts?"):
                return {"contacts": [
                    {"chatType": 1, "peerUid": "friend-a", "name": "Recent alias", "lastMsgTime": "2023-11-14T22:13:20Z"},
                    {"chatType": 2, "peerUid": "group-a", "lastMsgTime": 1700000001000},
                    {"chatType": 8, "peerUid": "device-a", "name": "Synthetic device", "lastMsgTime": "invalid"},
                    {"chatType": 1, "peerUid": "friend-a", "lastMsgTime": "2023-11-14T22:13:19Z"},
                    {"chatType": 1, "peerUid": "other-a", "msgTime": 1700000002000}], "totalCount": 5}
            self.fail("Activity must not fetch message bodies")
        qq.request = request
        _, conversations, _ = qq.catalog()
        self.assertEqual([conv.conversation_id for conv in conversations],
                         ["1:friend-a", "1:never-recent", "2:group-a", "8:device-a", "1:other-a"])
        self.assertEqual([conv.last_active for conv in conversations], [1700000000, None, 1700000001, None, None])
        self.assertEqual(conversations[0].title, "Canonical private")
        self.assertEqual(conversations[2].title, "Canonical group")
        self.assertEqual(verified, ["synthetic-account"])
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
