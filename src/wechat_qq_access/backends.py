"""Adapters for pinned upstream readers; only authorized conversations reach these methods.

The upstream WeChat library has no public fixed-time pagination. Its pinned SQL
reader is reused with parameterized time bounds and fetchmany, without exporting
a second history database. QQ uses the existing Framework's fixed-window API.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.metadata
import io
import json
import logging
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .service import AccessError, Conversation, Page, _linked


def source_id(parts: list) -> str:
    return base64.urlsafe_b64encode(json.dumps(parts, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_id(value: str) -> list:
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,8192}", value):
            raise ValueError()
        result = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if not isinstance(result, list):
            raise ValueError()
        return result
    except Exception:
        raise AccessError("MEDIA_NOT_FOUND", "The attachment message could not be located.") from None


def private_path(path: Path, root: Path) -> bool:
    """Reject escapes and reparse points, while accepting verified Windows file aliases.

    GetFinalPathNameByHandle can report a different namespace alias for an
    existing file than for its parent directory. Accept that spelling only when
    the original path is inside root, has no link components, and both parent
    spellings identify the exact same directory. This never grants another root.
    """
    try:
        root = Path(root).resolve()
        candidate = Path(os.path.abspath(path))
        if not candidate.is_relative_to(root):
            return False
        current = candidate
        while True:
            if _linked(current):
                return False
            if current == root:
                break
            if current.parent == current:
                return False
            current = current.parent
        resolved = candidate.resolve()
        if resolved.is_relative_to(root):
            return True
        return (candidate.is_file() and resolved.name == candidate.name and
                resolved.parent.samefile(candidate.parent))
    except (OSError, ValueError):
        return False


class QuietSink(io.TextIOBase):
    """Discard raw diagnostics rather than retaining private account/key strings."""
    def __init__(self):
        self.warning_count = 0

    def write(self, value):
        self.warning_count += sum(value.upper().count(word) for word in ("WARNING", "ERROR", "CRITICAL"))
        return len(value)

    def flush(self):
        pass


def local_bytes(path: Path, roots: list[Path]) -> tuple[bytes | None, str]:
    """Only known media directories; never expose a raw path or follow a link."""
    try:
        lexical = Path(os.path.abspath(path))
        allowed = next((root for root in roots if private_path(lexical, root)), None)
        if allowed is None or not lexical.is_file():
            return None, "application/octet-stream"
        # Check the lexical components too, as resolve() alone hides junctions.
        current = lexical
        while True:
            if _linked(current):
                return None, "application/octet-stream"
            if current == current.parent:
                break
            current = current.parent
        size = lexical.stat().st_size
        if size > 25 * 1024 * 1024:
            raise AccessError("MEDIA_TOO_LARGE", "The attachment exceeds the inline media limit.")
        with lexical.open("rb") as stream:
            data = stream.read(25 * 1024 * 1024 + 1)
        if len(data) > 25 * 1024 * 1024:
            raise AccessError("MEDIA_TOO_LARGE", "The attachment exceeds the inline media limit.")
        return data, mimetypes.guess_type(lexical.name)[0] or "application/octet-stream"
    except (OSError, ValueError):
        return None, "application/octet-stream"


def media_output(root: Path, message_id: str):
    """Constrain upstream media writes before they happen, including shard collisions."""
    def output(save_dir, name):
        root.mkdir(parents=True, exist_ok=True)
        if _linked(root):
            raise AccessError("PRIVATE_PATH_INVALID", "The private media directory is invalid.")
        suffix = Path(str(name)).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            suffix = ".bin"
        # Never use a chat title, attachment filename or source path as an output path.
        filename = hashlib.sha256((message_id + ":" + str(name)).encode()).hexdigest() + suffix
        target = root / filename
        if not private_path(target, root):
            raise AccessError("PRIVATE_PATH_INVALID", "The private media destination is invalid.")
        return str(target)
    return output


class WeChatBackend:
    PAGE_SIZE = 500

    def __init__(self, config: dict):
        self.config = config
        self.private = Path(os.path.expandvars(config["privateRoot"])).resolve()
        self.sink = QuietSink()
        self.module = None
        self.accounts = {}
        self.dbs = {}
        self.rows = {}

    @contextlib.contextmanager
    def quiet(self):
        work, temp, keys = self.private / "wechat", self.private / "tmp", self.private / "wechat-keys"
        for directory in (work, temp, keys):
            directory.mkdir(parents=True, exist_ok=True)
            if _linked(directory) or not directory.resolve().is_relative_to(self.private):
                raise AccessError("PRIVATE_PATH_INVALID", "The private reader paths are invalid.")
        previous_cwd = Path.cwd()
        previous = {key: os.environ.get(key) for key in ("TEMP", "TMP", "WECHATAUTO_KEYS_DIR")}
        previous_temp = tempfile.tempdir
        try:
            os.environ.update(TEMP=str(temp), TMP=str(temp), WECHATAUTO_KEYS_DIR=str(keys))
            tempfile.tempdir = None
            os.chdir(work)
            with contextlib.redirect_stdout(self.sink), contextlib.redirect_stderr(self.sink):
                if self.module is None:
                    if importlib.metadata.version("wechatauto-replica") != "1.2.6.1":
                        raise AccessError("UPSTREAM_VERSION", "Install the pinned WeChat reader version.")
                    from wechatauto.param import WxParam
                    WxParam.ENABLE_FILE_LOGGER = False
                    from wechatauto import db as db_module
                    from wechatauto.logger import wxlog
                    handler = wxlog.file_handler
                    if handler is not None:
                        logging.getLogger().removeHandler(handler)
                        handler.close()
                        wxlog.file_handler = None
                    wxlog.console_handler.setStream(self.sink)
                    self.module = db_module
                yield
        finally:
            os.chdir(previous_cwd)
            tempfile.tempdir = previous_temp
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _db(self, account: str):
        if account not in self.accounts:
            raise AccessError("ACCOUNT_CHANGED", "The selected local account is no longer available.")
        if account not in self.dbs:
            entry = self.accounts[account]
            # Reuse the existing single-account cache; separate workdirs for additional accounts.
            work = self.private / "wechat"
            if len(self.accounts) > 1:
                # Match the upstream fallback layout already recognized by cache.ps1.
                work = self.private / "tmp" / "wechatauto_db" / entry["account"]
            db = self.module.WeChatDB(db_dir=str(Path(entry["path"]).parent), account=entry["account"], workdir=str(work))
            if db.account != entry["account"] or db.wxid != account:
                raise AccessError("ACCOUNT_CHANGED", "The upstream reader selected a different account.")
            if not all(private_path(Path(p), self.private) for p in (db.workdir, db.keys_file)):
                raise AccessError("PRIVATE_PATH_INVALID", "The private reader paths are invalid.")
            self.dbs[account] = db
        db = self.dbs[account]
        if db.account != self.accounts[account]["account"] or db.wxid != account:
            raise AccessError("ACCOUNT_CHANGED", "The upstream reader selected a different account.")
        return db

    def catalog(self):
        conversations, accounts, warnings = [], [], []
        with self.quiet():
            for row in self.module.list_accounts():
                account = row.get("wxid")
                if account and account not in self.accounts:
                    self.accounts[account] = row
            for account in self.accounts:
                accounts.append({"platform": "wechat", "account": account})
                before = self.sink.warning_count
                try:
                    db = self._db(account)
                    rows = db.list_message_chats()
                    self._db(account)  # The upstream may switch accounts while resolving keys.
                    for row in rows:
                        cid = str(row["username"])
                        conversations.append(Conversation("wechat", account, cid, str(row.get("name") or cid),
                                                          "group" if cid.endswith("@chatroom") else "private"))
                    if self.sink.warning_count != before:
                        warnings.append("WECHAT_METADATA_WARNING")
                except Exception:
                    warnings.append("WECHAT_ACCOUNT_UNAVAILABLE")
        warnings.append("WECHAT_SCOPE_LOCAL_ORDINARY_MESSAGE_DATABASES")
        return accounts, conversations, sorted(set(warnings))

    def _fingerprint(self, db):
        result = []
        for rel in db._message_dbs():
            original = Path(db._db_path(rel))
            for path in (original, Path(str(original) + "-wal")):
                try:
                    st = path.stat()
                    result.append((str(path), st.st_size, st.st_mtime_ns))
                except FileNotFoundError:
                    result.append((str(path), None, None))
        return result

    def page(self, conv: Conversation, since: float, until: float, cursor):
        key = (conv.ref, since, until)
        with self.quiet():
            db = self._db(conv.account)
            if cursor is None:
                before, warning_before = self._fingerprint(db), self.sink.warning_count
                def read(tables):
                    result = []
                    maps = db._shard_maps(tables)
                    # Establish read transactions on all cache shards before fetching rows.
                    for connection, table in tables:
                        if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                            raise AccessError("SOURCE_INVALID", "The message table name is invalid.")
                        connection.execute("BEGIN")
                        connection.execute("SELECT count(*) FROM " + table).fetchone()
                    for connection, table in tables:
                        shard = db._shard_label(connection)
                        query = connection.execute(
                            "SELECT local_id, local_type, real_sender_id, create_time, message_content, "
                            "source, packed_info_data, compress_content, server_id, sort_seq, ? AS shard "
                            "FROM " + table + " WHERE create_time >= ? AND create_time <= ? "
                            "ORDER BY create_time, local_id", (shard, since, until))
                        while True:
                            batch = query.fetchmany(self.PAGE_SIZE)
                            if not batch:
                                break
                            for raw in batch:
                                row = db._msg_row_to_dict(raw, maps)
                                result.append({"id": source_id(["wechat", conv.ref, shard, row["local_id"]]),
                                               "timestamp": row["create_time"],
                                               "sender": row.get("sender_username") or str(row.get("sender_id") or ""),
                                               "text": row.get("content", ""), "type": row.get("type", "unknown")})
                    return result
                rows = db._run_msg_query(conv.conversation_id, read)
                if rows is None:
                    raise AccessError("SOURCE_CHANGED", "The conversation is no longer present in the local source.")
                self._db(conv.account)
                complete = before == self._fingerprint(db) and warning_before == self.sink.warning_count
                rows.sort(key=lambda row: (float(row["timestamp"]), row["id"]))
                self.rows[key] = (rows, complete)
            rows, complete = self.rows[key]
            offset = 0 if cursor is None else int(cursor)
            selected = rows[offset:offset + self.PAGE_SIZE]
            next_cursor = offset + self.PAGE_SIZE if offset + self.PAGE_SIZE < len(rows) else None
            if next_cursor is None:
                self.rows.pop(key, None)
            return Page(selected, next_cursor, complete,
                        [] if complete else ["WECHAT_SOURCE_CHANGED_OR_WARNED_DURING_READ"])

    def media(self, conv: Conversation, message_id: str):
        parts = decode_id(message_id)
        if (len(parts) != 4 or parts[0] != "wechat" or parts[1] != conv.ref or not isinstance(parts[2], str) or
            not re.fullmatch(r"message__message(?:_\d+)?\.db", parts[2]) or
            isinstance(parts[3], bool) or not isinstance(parts[3], int) or parts[3] < 0):
            raise AccessError("MEDIA_NOT_FOUND", "The attachment message could not be located.")
        with self.quiet():
            db = self._db(conv.account)
            row = db.get_message_row(conv.conversation_id, parts[3], shard=parts[2])
            self._db(conv.account)
            if not row or row.get("shard") != parts[2]:
                return None, "application/octet-stream"
            from wechatauto.media import MediaDownloader
            # One bounded, preview-cleanable media category across accounts and fallback workdirs.
            root = self.private / "wechat" / "media"
            media = MediaDownloader(db, save_dir=str(root))
            media._out = media_output(root, message_id)
            # get_message_row is the raw pinned API: it returns local_type, not
            # the display `type` field produced by get_messages/_msg_row_to_dict.
            method = {3: "download_image", 34: "download_voice", 43: "download_video",
                      49: "download_file"}.get(row.get("local_type"))
            if method is None:
                return None, "application/octet-stream"
            kwargs = {"shard": parts[2]}
            if method == "download_image":
                kwargs["tier"] = "best"
            path = getattr(media, method)(conv.conversation_id, parts[3], **kwargs)
            self._db(conv.account)
            return local_bytes(Path(path), [root]) if path else (None, "application/octet-stream")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise AccessError("SOURCE_REDIRECT", "The local QQ endpoint attempted a redirect.")


class QQBackend:
    PAGE_SIZE = 500

    def __init__(self, config: dict):
        self.config = config
        port = config.get("qqPort")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise AccessError("CONFIG_INVALID", "The QQ endpoint configuration is invalid.")
        self.base = "http://127.0.0.1:" + str(port)
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.account = None

    def request(self, path: str, body=None):
        try:
            # Reload per request: QCE may renew its token without restarting the panel.
            security = json.loads(Path(os.path.expandvars(self.config["qqSecurityFile"])).read_text(encoding="utf-8-sig"))
            token = security["accessToken"]
            if not isinstance(token, str) or not token:
                raise ValueError()
            request = Request(self.base + path,
                              data=json.dumps(body).encode() if body is not None else None,
                              headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            with self.opener.open(request, timeout=120) as response:
                raw = response.read(64 * 1024 * 1024 + 1)
            if len(raw) > 64 * 1024 * 1024:
                raise AccessError("SOURCE_RESPONSE_TOO_LARGE", "The QQ response exceeds the safe read limit.")
            data = json.loads(raw)
            if data.get("success") is not True or not isinstance(data.get("data"), dict):
                raise AccessError("QQ_API_FAILED", "The local QQ API did not complete this request.")
            return data["data"]
        except AccessError:
            raise
        except HTTPError as exc:
            code = "QQ_AUTH_FAILED" if exc.code in (401, 403) else "QQ_API_FAILED"
            raise AccessError(code, "The local QQ API rejected this request.") from None
        except (URLError, OSError, ValueError, KeyError, TypeError):
            raise AccessError("QQ_UNAVAILABLE", "The configured QQ reader is unavailable.") from None

    def identity(self) -> str:
        data = self.request("/api/system/info")
        napcat = data.get("napcat", {})
        account = napcat.get("selfInfo", {}).get("uid")
        if data.get("mode") != "plugin" or napcat.get("online") is not True or not isinstance(account, str) or not account:
            raise AccessError("QQ_OFFLINE", "QQ must be logged in through the configured Framework.")
        return account

    def verify(self, expected):
        if self.identity() != expected:
            raise AccessError("ACCOUNT_CHANGED", "The QQ account changed during the request.")

    def catalog(self):
        account = self.identity()
        self.account = account
        conversations = {}
        for endpoint, field, kind, chat_type in (("friends", "friends", "private", 1), ("groups", "groups", "group", 2)):
            page, signatures = 1, set()
            while True:
                data = self.request("/api/" + endpoint + "?" + urlencode({"page": page, "limit": 2000,
                                                                        "forceRefresh": "true" if page == 1 else "false"}))
                rows = data.get(field)
                if not isinstance(rows, list) or not isinstance(data.get("hasNext"), bool):
                    raise AccessError("SOURCE_INVALID", "The local QQ contact response is invalid.")
                signature = []
                for row in rows:
                    uid = row.get("uid") if chat_type == 1 else row.get("groupCode")
                    if uid is None or not str(uid):
                        continue
                    cid = str(chat_type) + ":" + str(uid)
                    signature.append(cid)
                    title = row.get("remark") or row.get("nick") or row.get("nickname") or row.get("groupName") or cid
                    conversations[cid] = Conversation("qq", account, cid, str(title), kind)
                if not data["hasNext"]:
                    break
                if not signature or tuple(signature) in signatures:
                    raise AccessError("PAGINATION_STALLED", "QQ contact pagination did not advance.")
                signatures.add(tuple(signature))
                page += 1
        recent = self.request("/api/recent-contacts?limit=2000&includeAll=true")
        rows = recent.get("contacts")
        if not isinstance(rows, list):
            raise AccessError("SOURCE_INVALID", "The local QQ contact response is invalid.")
        for row in rows:
            uid, chat_type = row.get("peerUid"), row.get("chatType")
            if uid is None or isinstance(chat_type, bool) or not str(chat_type).isdigit():
                continue
            cid = str(int(chat_type)) + ":" + str(uid)
            conversations.setdefault(cid, Conversation("qq", account, cid,
                str(row.get("name") or row.get("peerName") or row.get("nick") or cid),
                "group" if int(chat_type) == 2 else "private" if int(chat_type) == 1 else "device"))
        self.verify(account)
        warnings = ["QQ_SCOPE_AVAILABLE_LOCAL_CONTACTS_AND_HISTORY"]
        if int(recent.get("totalCount", len(rows))) > len(rows):
            warnings.append("QQ_RECENT_CONTACTS_LIMIT_REACHED")
        return [{"platform": "qq", "account": account}], list(conversations.values()), warnings

    def raw_page(self, conv, since, until, page):
        self.verify(conv.account)
        chat_type, uid = conv.conversation_id.split(":", 1)
        data = self.request("/api/messages/fetch", {"peer": {"chatType": int(chat_type), "peerUid": uid},
                            "filter": {"startTime": int(since * 1000), "endTime": int(until * 1000)},
                            "page": page, "limit": self.PAGE_SIZE, "forceRefresh": page == 1})
        self.verify(conv.account)
        if not isinstance(data.get("messages"), list) or not isinstance(data.get("hasNext"), bool):
            raise AccessError("SOURCE_INVALID", "The local QQ message response is invalid.")
        return data

    @staticmethod
    def normalize(row, ref):
        msgid = row.get("msgId")
        stamp = row.get("msgTime")
        if not msgid or stamp is None:
            raise AccessError("SOURCE_INVALID", "A QQ message has no stable identity or timestamp.")
        timestamp = float(stamp)
        if timestamp >= 10000000000:
            timestamp /= 1000
        text, kinds = [], []
        for element in row.get("elements", []):
            part = element.get("textElement")
            if isinstance(part, dict) and isinstance(part.get("content"), str):
                text.append(part["content"])
                kinds.append("text")
            else:
                kind = next((k.removesuffix("Element") for k, v in element.items()
                             if k.endswith("Element") and v is not None), "attachment")
                text.append("[" + kind + "]")
                kinds.append(kind)
        return {"id": source_id(["qq", ref, str(msgid), timestamp]), "timestamp": timestamp,
                "sender": str(row.get("sendMemberName") or row.get("sendNickName") or row.get("senderUid") or row.get("senderUin") or ""),
                "text": "".join(text), "type": "+".join(dict.fromkeys(kinds)) or "unknown"}

    def page(self, conv, since, until, cursor):
        page = 1 if cursor is None else int(cursor)
        data = self.raw_page(conv, since, until, page)
        return Page([self.normalize(row, conv.ref) for row in data["messages"]], page + 1 if data["hasNext"] else None,
                    warnings=["QQ_SERVER_COMPLETENESS_NOT_PROVEN"])

    def media(self, conv, message_id):
        parts = decode_id(message_id)
        if len(parts) != 4 or parts[0] != "qq" or parts[1] != conv.ref or not isinstance(parts[2], str) or not isinstance(parts[3], (int, float)):
            raise AccessError("MEDIA_NOT_FOUND", "The attachment message could not be located.")
        stamp = float(parts[3])
        if not 0 <= stamp <= 9999999999:
            raise AccessError("MEDIA_NOT_FOUND", "The attachment message could not be located.")
        page, seen = 1, set()
        while True:
            data = self.raw_page(conv, stamp, stamp, page)
            signature = tuple(str(row.get("msgId")) for row in data["messages"])
            if data["hasNext"] and (not signature or signature in seen):
                raise AccessError("PAGINATION_STALLED", "QQ attachment lookup did not advance.")
            seen.add(signature)
            for row in data["messages"]:
                if str(row.get("msgId")) != parts[2] or self.normalize(row, conv.ref)["id"] != message_id:
                    continue
                roots = [Path.home() / ".qq-chat-exporter" / "resources",
                         Path(os.path.expandvars(self.config["qqRoot"])) / "resources"]
                for element in row.get("elements", []):
                    for key in ("picElement", "videoElement", "pttElement", "fileElement"):
                        detail = element.get(key)
                        if isinstance(detail, dict):
                            for field in ("filePath", "sourcePath", "thumbPath"):
                                path = detail.get(field)
                                if isinstance(path, str):
                                    data_bytes, mime = local_bytes(Path(path), roots)
                                    if data_bytes is not None:
                                        self.verify(conv.account)
                                        return data_bytes, mime
                return None, "application/octet-stream"
            if not data["hasNext"]:
                return None, "application/octet-stream"
            page += 1


class LocalBackend:
    def __init__(self, config: dict):
        self.wechat = WeChatBackend(config)
        self.qq = QQBackend(config) if config.get("qqSecurityFile") and config.get("qqRoot") else None

    def catalog(self):
        accounts, conversations, warnings = [], [], []
        for backend, label in ((self.wechat, "WECHAT"), (self.qq, "QQ")):
            if backend is None:
                continue
            try:
                found_accounts, found_conversations, found_warnings = backend.catalog()
                accounts.extend(found_accounts)
                conversations.extend(found_conversations)
                warnings.extend(found_warnings)
            except Exception:
                warnings.append(label + "_UNAVAILABLE")
        return accounts, conversations, sorted(set(warnings))

    def page(self, conv, since, until, cursor):
        backend = self.wechat if conv.platform == "wechat" else self.qq
        return backend.page(conv, since, until, cursor)

    def media(self, conv, message_id):
        backend = self.wechat if conv.platform == "wechat" else self.qq
        return backend.media(conv, message_id)
