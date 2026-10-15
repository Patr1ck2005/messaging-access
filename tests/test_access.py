"""Offline permission and pagination tests. Only synthetic local data is used."""
from __future__ import annotations

import contextlib
import ast
import importlib.metadata
import json
import os
import sqlite3
import subprocess
import sys
import types
from access_fixtures import RecycleFixture
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wechat_qq_access import AccessError, AccessService, Conversation, Page, PolicyStore
from wechat_qq_access.backends import QQBackend, WeChatBackend, decode_id, local_bytes, media_output, private_path, source_id
from wechat_qq_access.service import safe_response


class FakeBackend:
    def __init__(self):
        self.convs = [Conversation("wechat", "account-a", "chat-a", "Same title"),
                      Conversation("wechat", "account-a", "chat-b", "Same title"),
                      Conversation("wechat", "account-b", "chat-a", "Other account"),
                      Conversation("qq", "account-a", "1:chat-a", "Other platform")]
        self.calls, self.media_calls = [], []
        self.after_page = None
        self.after_media = None
        self.pages = {None: Page([self.row("one", 10, "self"), self.row("two", 11)], "second"),
                      "second": Page([self.row("two", 11), self.row("three", 12)])}

    @staticmethod
    def row(msgid, stamp, sender="other"):
        return {"id": msgid, "timestamp": stamp, "sender": sender, "text": "synthetic " + msgid,
                "type": "text", "raw_path": "PRIVATE-CREDENTIAL-PATH", "accessToken": "NEVER-RELEASE"}

    def catalog(self):
        return list({(c.platform, c.account): {"platform": c.platform, "account": c.account}
                     for c in self.convs}.values()), self.convs, []

    def page(self, conv, since, until, cursor):
        self.calls.append((conv.ref, since, until, cursor))
        page = self.pages[cursor]
        if isinstance(page, Exception):
            raise page
        if self.after_page:
            self.after_page()
        return page

    def media(self, conv, message_id):
        self.media_calls.append((conv.ref, message_id))
        if self.after_media:
            self.after_media()
        return b"synthetic attachment", "text/plain"


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = RecycleFixture(prefix="wechat-qq-access-synthetic-")
        self.root = Path(self.temp.name)
        self.policy = PolicyStore(self.root)
        self.backend = FakeBackend()
        self.service = AccessService(self.policy, self.backend)
        self.a, self.b, self.other_account, self.other_platform = self.backend.convs

    def tearDown(self):
        self.temp.cleanup()

    def save(self, mode="allow", ids=None):
        return self.policy.write([{"platform": "wechat", "account": "account-a", "mode": mode,
                                   "conversations": ids if ids is not None else ["chat-a"]}])

    def request(self, **changes):
        return {"action": "messages", "refs": [self.a.ref], "since": 0, "until": 20, **changes}

    def assert_error(self, code, request):
        response = safe_response(self.service, request)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], code)
        return response

    def test_missing_policy_fails_before_message_read(self):
        self.assertEqual(self.service.execute({"action": "policy.get"}), {"policy": None})
        self.assert_error("POLICY_REQUIRED", self.request())
        self.assertEqual(self.backend.calls, [])

    def test_corrupt_policy_fails_closed(self):
        self.policy.path.write_text("{invalid}")
        for request in (self.request(), {"action": "conversations"},
                        {"action": "authorize", "refs": [self.a.ref]}):
            self.assert_error("POLICY_INVALID", request)
        self.assertEqual(self.backend.calls, [])

    def test_allow_listing_uses_stable_identity_not_title(self):
        self.save()
        result = self.service.execute({"action": "conversations"})
        self.assertEqual([c["ref"] for c in result["conversations"]], [self.a.ref])
        self.assert_error("ACCESS_DENIED", self.request(refs=[self.b.ref]))
        self.assertEqual(self.backend.calls, [])

    def test_deny_mode_new_conversation_allowed_but_new_account_denied(self):
        self.save("deny", ["chat-b"])
        new = Conversation("wechat", "account-a", "new-chat", "new")
        self.backend.convs.append(new)
        result = self.service.execute({"action": "conversations"})
        self.assertEqual({c["ref"] for c in result["conversations"]}, {self.a.ref, new.ref})
        self.assert_error("ACCESS_DENIED", self.request(refs=[self.other_account.ref]))
        self.assert_error("ACCESS_DENIED", self.request(refs=[self.other_platform.ref]))

    def test_new_conversation_in_allow_mode_is_denied(self):
        self.save()
        new = Conversation("wechat", "account-a", "new", "new")
        self.backend.convs.append(new)
        self.assert_error("ACCESS_DENIED", self.request(refs=[new.ref]))

    def test_one_denied_reference_blocks_entire_request(self):
        self.save()
        self.assert_error("ACCESS_DENIED", self.request(refs=[self.a.ref, self.b.ref]))
        self.assertEqual(self.backend.calls, [])

    def test_unknown_reference_and_wildcard_denied(self):
        self.save()
        self.assert_error("ACCESS_DENIED", self.request(refs=["*"]))
        self.assert_error("ACCESS_DENIED", self.request(refs=["unknown"]))

    def test_json_cannot_override_config_or_policy_path(self):
        self.save()
        for field in ("policy_file", "config", "privateRoot", "path", "cursor", "peer"):
            self.assert_error("INVALID_REQUEST", self.request(**{field: "../../secret"}))
        self.assertEqual(self.backend.calls, [])

    def test_pagination_dedup_own_messages_and_explicit_projection(self):
        self.save()
        result = self.service.execute(self.request())
        self.assertEqual([m["id"] for m in result["messages"]], ["one", "two", "three"])
        self.assertEqual(result["messages"][0]["sender"], "self")
        self.assertTrue(result["coverage"][0]["complete"])
        self.assertEqual([c[3] for c in self.backend.calls], [None, "second"])
        text = json.dumps(result)
        self.assertNotIn("PRIVATE-CREDENTIAL-PATH", text)
        self.assertNotIn("NEVER-RELEASE", text)

    def test_time_filter_is_fixed_inclusive_and_applied_locally(self):
        self.save()
        result = self.service.execute(self.request(since=11, until=11))
        self.assertEqual([m["id"] for m in result["messages"]], ["two"])
        self.assertTrue(all(call[1:3] == (11, 11) for call in self.backend.calls))

    def test_invalid_time_ranges_do_not_read(self):
        self.save()
        for since, until in ((20, 10), (-1, 20), (True, 20), (0, float("nan")), (0, 1700000000000)):
            self.assert_error("INVALID_RANGE", self.request(since=since, until=until))
        self.assertEqual(self.backend.calls, [])

    def test_page_failure_is_incomplete_and_raw_error_hidden(self):
        self.save()
        self.backend.pages["second"] = RuntimeError("SECRET=credential private path")
        result = self.service.execute(self.request())
        self.assertFalse(result["coverage"][0]["complete"])
        self.assertEqual(result["coverage"][0]["reason"], "SOURCE_READ_FAILED")
        self.assertNotIn("SECRET", json.dumps(result))
        self.backend.pages["second"] = Page([self.backend.row("three", 12)])
        self.assertTrue(self.service.execute(self.request())["coverage"][0]["complete"])

    def test_duplicate_page_stalls_instead_of_claiming_complete(self):
        self.save()
        rows = [self.backend.row("one", 10)]
        self.backend.pages = {None: Page(rows, 1), 1: Page(rows, 2)}
        result = self.service.execute(self.request())
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["coverage"][0]["reason"], "PAGINATION_STALLED")
        self.assertFalse(result["coverage"][0]["complete"])

    def test_upstream_warning_is_not_complete(self):
        self.save()
        self.backend.pages = {None: Page([], complete=False, warnings=["SOURCE_WARNING"])}
        result = self.service.execute(self.request())
        self.assertFalse(result["coverage"][0]["complete"])
        self.assertEqual(result["warnings"], ["SOURCE_WARNING"])

    def test_revocation_between_pages_discards_all_results(self):
        self.save()
        self.backend.after_page = lambda: self.save(ids=[])
        response = self.assert_error("POLICY_CHANGED", self.request())
        self.assertNotIn("data", response)
        self.assertEqual(len(self.backend.calls), 1)

    def test_same_public_revision_cannot_hide_policy_file_change(self):
        self.save()
        def change():
            value = json.loads(self.policy.path.read_text())
            value["accounts"][0]["conversations"] = []
            self.policy.path.write_text(json.dumps(value))
        self.backend.after_page = change
        self.assert_error("POLICY_CHANGED", self.request())

    def test_corruption_during_read_discards_all_results(self):
        self.save()
        self.backend.after_page = lambda: self.policy.path.write_text("invalid")
        self.assert_error("POLICY_INVALID", self.request())

    def test_authorize_revision_and_deduplicated_refs(self):
        policy = self.save()
        result = self.service.execute({"action": "authorize", "refs": [self.a.ref, self.a.ref]})
        self.assertEqual(result, {"revision": policy["revision"], "allowed": [self.a.ref]})

    def test_media_authorization_precedes_lookup(self):
        self.save()
        self.assert_error("ACCESS_DENIED", {"action": "media", "ref": self.b.ref, "message_id": "any"})
        self.assertEqual(self.backend.media_calls, [])

    def test_media_rejects_request_path_and_revocation_discards_bytes(self):
        self.save()
        self.assert_error("INVALID_REQUEST", {"action": "media", "ref": self.a.ref,
                                              "message_id": "one", "path": "C:/secret"})
        self.backend.after_media = lambda: self.save(ids=[])
        response = self.assert_error("POLICY_CHANGED", {"action": "media", "ref": self.a.ref, "message_id": "one"})
        self.assertNotIn("base64", json.dumps(response))

    def test_media_only_returns_bytes_never_paths(self):
        self.save()
        result = self.service.execute({"action": "media", "ref": self.a.ref, "message_id": "one"})
        self.assertTrue(result["available"])
        self.assertEqual(result["size"], len(b"synthetic attachment"))
        self.assertNotIn("path", result)

    def test_catalog_is_explicit_local_setup_metadata_without_reads(self):
        result = self.service.execute({"action": "catalog"})
        self.assertEqual(len(result["conversations"]), 4)
        self.assertEqual(self.backend.calls, [])
        self.assertNotIn("messages", result)

    def test_policy_set_known_accounts_only_and_recovers_corruption(self):
        self.policy.path.write_text("invalid")
        result = self.service.execute({"action": "policy.set", "accounts": [
            {"platform": "wechat", "account": "account-a", "mode": "allow", "conversations": []}]})
        self.assertEqual(result["policy"]["schemaVersion"], 1)
        self.assert_error("ACCOUNT_UNKNOWN", {"action": "policy.set", "accounts": [
            {"platform": "wechat", "account": "not-discovered", "mode": "deny", "conversations": []}]})

    def test_invalid_duplicate_account_or_mode_rejected(self):
        row = {"platform": "wechat", "account": "account-a", "mode": "allow", "conversations": []}
        for accounts in ([row, row], [{**row, "mode": "all"}], [{**row, "conversations": ["a", "a"]}],
                         [{**row, "policyPath": "override"}]):
            self.assert_error("POLICY_INVALID", {"action": "policy.set", "accounts": accounts})

    def test_local_media_root_and_junction_escape_guard(self):
        media = self.root / "media"
        media.mkdir()
        good, secret = media / "good.txt", self.root / "secret.txt"
        good.write_bytes(b"good")
        secret.write_bytes(b"secret")
        self.assertEqual(local_bytes(good, [media])[0], b"good")
        self.assertIsNone(local_bytes(secret, [media])[0])
        self.assertIsNone(local_bytes(media / ".." / "secret.txt", [media])[0])
        with patch("wechat_qq_access.backends._linked", side_effect=lambda p: p == good):
            self.assertIsNone(local_bytes(good, [media])[0])

    def test_media_source_ids_are_bound_to_conversation_and_shard(self):
        wx = object.__new__(WeChatBackend)
        other_id = source_id(["wechat", self.b.ref, "message__message_0.db", 1])
        path_id = source_id(["wechat", self.a.ref, "../../keys.json", 1])
        for msgid in (other_id, path_id, "../../keys.json"):
            with self.assertRaises(AccessError):
                wx.media(self.a, msgid)
        qq = object.__new__(QQBackend)
        with self.assertRaises(AccessError):
            qq.media(self.other_platform, source_id(["qq", self.a.ref, "message", 10]))

    def test_media_writes_are_hashed_inside_root_and_separate_shards(self):
        root = self.root / "media"
        first = media_output(root, source_id(["wechat", self.a.ref, "message__message_0.db", 1]))
        second = media_output(root, source_id(["wechat", self.a.ref, "message__message_1.db", 1]))
        path1 = Path(first("C:/outside", "../../secret.txt"))
        path2 = Path(second(None, "../../secret.txt"))
        self.assertEqual(path1.parent, root)
        self.assertNotEqual(path1, path2)
        self.assertNotIn("secret", path1.name)
        self.assertEqual(path1, Path(first(None, "../../secret.txt")))

    def test_private_file_namespace_alias_requires_same_parent_identity(self):
        private = self.root / "private"
        work = private / "wechat"
        work.mkdir(parents=True)
        keys = work / "keys.json"
        keys.write_text("synthetic")
        alias = self.root / "namespace-alias" / "wechat" / "keys.json"
        real_resolve, real_samefile = Path.resolve, Path.samefile
        def resolve(path, *args, **kwargs):
            return alias if path == keys else real_resolve(path, *args, **kwargs)
        def samefile(path, other):
            if path == alias.parent and other == work:
                return True
            return real_samefile(path, other)
        with patch.object(Path, "resolve", resolve), patch.object(Path, "samefile", samefile):
            self.assertTrue(private_path(keys, private))
            self.assertEqual(local_bytes(keys, [private])[0], b"synthetic")
            self.assertFalse(private_path(self.root / "outside.json", private))
            with patch("wechat_qq_access.backends._linked", side_effect=lambda p: p == keys):
                self.assertFalse(private_path(keys, private))
        with patch.object(Path, "resolve", resolve), patch.object(Path, "samefile", return_value=False):
            self.assertFalse(private_path(keys, private))

    def test_wechat_media_routes_pinned_raw_local_type_and_exact_shard(self):
        wx = object.__new__(WeChatBackend)
        wx.private = self.root
        wx.quiet = contextlib.nullcontext
        selected = []
        raw_row = {"local_type": 3, "local_id": 7, "shard": "message__message_0.db", "server_id": 0}
        class DB:
            def get_message_row(db, user, local_id, *, shard):
                self.assertEqual((user, local_id, shard), (self.a.conversation_id, 7, raw_row["shard"]))
                return raw_row
        class Media:
            def __init__(media, db, *, save_dir):
                pass
            def download_image(media, user, local_id, *, shard, tier):
                selected.append(("image", shard, tier))
                path = Path(media._out(None, "synthetic.jpg"))
                path.write_bytes(b"synthetic image")
                return str(path)
        wx._db = lambda account: DB()
        module = types.ModuleType("wechatauto.media")
        module.MediaDownloader = Media
        parent = types.ModuleType("wechatauto")
        parent.__path__ = []
        msg_id = source_id(["wechat", self.a.ref, raw_row["shard"], 7])
        with patch.dict(sys.modules, {"wechatauto": parent, "wechatauto.media": module}):
            data, mime = wx.media(self.a, msg_id)
        self.assertEqual(data, b"synthetic image")
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(selected, [("image", "message__message_0.db", "best")])

    def test_installed_pinned_wechat_source_protocol_matches_adapter(self):
        # Inspect source and metadata only; importing the package would initialize
        # its Windows modules. No account database or key values are opened.
        try:
            distribution = importlib.metadata.distribution("wechatauto-replica")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("pinned upstream source is not installed in this test interpreter")
        self.assertEqual(distribution.version, "1.2.6.1")
        db_tree = ast.parse(Path(distribution.locate_file("wechatauto/db.py")).read_text(encoding="utf-8"))
        media_tree = ast.parse(Path(distribution.locate_file("wechatauto/media.py")).read_text(encoding="utf-8"))
        db_class = next(node for node in db_tree.body if isinstance(node, ast.ClassDef) and node.name == "WeChatDB")
        methods = {node.name: node for node in db_class.body if isinstance(node, ast.FunctionDef)}
        required = {"_run_msg_query", "_shard_maps", "_shard_label", "_msg_row_to_dict", "_message_dbs", "_db_path"}
        self.assertTrue(required.issubset(methods))
        def names(method):
            return {arg.arg for arg in method.args.args + method.args.kwonlyargs}
        self.assertTrue({"db_dir", "workdir", "account"}.issubset(names(methods["__init__"])))
        self.assertTrue({"user", "local_id", "shard"}.issubset(names(methods["get_message_row"])))
        raw_keys = {key.value for node in ast.walk(methods["get_message_row"]) if isinstance(node, ast.Dict)
                    for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
        self.assertTrue({"local_type", "local_id", "shard", "server_id"}.issubset(raw_keys))
        self.assertNotIn("type", raw_keys)
        projected = {key.value for node in ast.walk(methods["_msg_row_to_dict"]) if isinstance(node, ast.Dict)
                     for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
        self.assertTrue({"local_id", "create_time", "sender_username", "content", "type", "shard"}.issubset(projected))
        sql = " ".join(node.value for node in ast.walk(methods["_shard_rows"])
                       if isinstance(node, ast.Constant) and isinstance(node.value, str))
        for column in ("local_id", "local_type", "real_sender_id", "create_time", "message_content", "source",
                       "packed_info_data", "compress_content", "server_id", "sort_seq"):
            self.assertIn(column, sql)
        media_class = next(node for node in media_tree.body if isinstance(node, ast.ClassDef) and node.name == "MediaDownloader")
        media_methods = {node.name: node for node in media_class.body if isinstance(node, ast.FunctionDef)}
        for method in ("download_image", "download_voice", "download_video", "download_file"):
            self.assertTrue({"user", "local_id", "shard"}.issubset(names(media_methods[method])))
        self.assertIn("tier", names(media_methods["download_image"]))
        self.assertTrue({"save_dir", "name"}.issubset(names(media_methods["_out"])))

    def test_qq_normalizes_self_text_and_attachments_without_raw_elements(self):
        row = {"msgId": "m", "msgTime": "12", "senderUid": "self", "elements": [
            {"textElement": {"content": "hello"}}, {"picElement": {"filePath": "C:/SECRET"}}]}
        result = QQBackend.normalize(row, self.other_platform.ref)
        self.assertEqual(result["sender"], "self")
        self.assertEqual(result["text"], "hello[pic]")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(decode_id(result["id"]), ["qq", self.other_platform.ref, "m", 12.0])

    def test_qq_fixed_window_refresh_and_identity_each_page(self):
        qq = object.__new__(QQBackend)
        calls, verifies = [], []
        qq.verify = lambda account: verifies.append(account)
        def request(path, body):
            calls.append((path, body))
            return {"messages": [], "hasNext": False}
        qq.request = request
        qq.page(self.other_platform, 10, 20, None)
        qq.page(self.other_platform, 10, 20, 2)
        self.assertEqual(len(verifies), 4)
        self.assertTrue(calls[0][1]["forceRefresh"])
        self.assertFalse(calls[1][1]["forceRefresh"])
        self.assertEqual(calls[0][1]["filter"], {"startTime": 10000, "endTime": 20000})
        self.assertEqual(calls[1][1]["filter"], calls[0][1]["filter"])

    def test_cli_policy_get_is_single_json_and_never_imports_reader(self):
        config = self.root / "deployment.json"
        config.write_text(json.dumps({"schemaVersion": 1, "privateRoot": str(self.root)}))
        env = {**os.environ, "MESSAGING_ACCESS_CONFIG": str(config)}
        process = subprocess.run([sys.executable, str(ROOT / "scripts" / "access.py")],
                                 input='{"action":"policy.get"}', text=True, capture_output=True, env=env)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        self.assertEqual(json.loads(process.stdout), {"ok": True, "data": {"policy": None}})
        self.assertEqual(len(process.stdout.splitlines()), 1)

    def test_wechat_fixed_range_sql_reads_both_shards_and_retains_identity(self):
        # Minimal pinned-reader protocol backed only by synthetic SQLite databases.
        conns = []
        table = "Msg_" + "a" * 32
        for number in range(2):
            path = self.root / ("message__message_" + str(number) + ".db")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE " + table + " (local_id, local_type, real_sender_id, create_time, "
                         "message_content, source, packed_info_data, compress_content, server_id, sort_seq)")
            for local_id, stamp in ((1, 9), (2, 10), (3, 12), (4, 21)):
                conn.execute("INSERT INTO " + table + " VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (local_id, 1, "self", stamp, "synthetic", "", "", None, 0, stamp))
            conn.commit()
            conns.append(conn)
        class DB:
            def _run_msg_query(db, user, build):
                return build([(conn, table) for conn in conns])
            def _shard_maps(db, tables):
                return {}
            def _shard_label(db, conn):
                return "message__message_" + str(conns.index(conn)) + ".db"
            def _msg_row_to_dict(db, row, maps):
                return {"local_id": row["local_id"], "shard": row["shard"], "create_time": row["create_time"],
                        "sender_username": "self", "content": row["message_content"], "type": "text"}
        wx = object.__new__(WeChatBackend)
        wx.PAGE_SIZE = 1
        wx.rows = {}
        wx.sink = type("Sink", (), {"warning_count": 0})()
        wx.quiet = contextlib.nullcontext
        wx._db = lambda account: DB()
        wx._fingerprint = lambda db: "same"
        rows, cursor = [], None
        while True:
            page = wx.page(self.a, 10, 20, cursor)
            rows.extend(page.messages)
            self.assertTrue(page.complete)
            cursor = page.next_cursor
            if cursor is None:
                break
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({row["id"] for row in rows}), 4)
        self.assertEqual({decode_id(row["id"])[2] for row in rows},
                         {"message__message_0.db", "message__message_1.db"})
        for conn in conns:
            conn.close()


if __name__ == "__main__":
    unittest.main()
