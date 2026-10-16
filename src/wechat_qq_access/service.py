"""One shared, fail-closed conversation policy. No model or server dependencies."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import math
import os
import stat
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_POLICY_LOCKS: dict[str, threading.RLock] = {}
_POLICY_LOCKS_GUARD = threading.Lock()


class AccessError(Exception):
    """Only these static messages may cross the JSON boundary."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def text_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
        raise AccessError("INVALID_REQUEST", "A nonempty identifier is required.")
    return value


def opaque_ref(platform: str, account: str, conversation_id: str) -> str:
    payload = json.dumps([platform, account, conversation_id], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Conversation:
    platform: str
    account: str
    conversation_id: str
    title: str
    kind: str = "private"
    last_active: float | None = None

    @property
    def ref(self) -> str:
        return opaque_ref(self.platform, self.account, self.conversation_id)

    def public(self) -> dict:
        # Metadata is optional and must remain safe for strict JSON consumers.
        stamp = self.last_active
        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or
            not 0 <= stamp <= 9999999999 or not math.isfinite(stamp)):
            stamp = None
        return {"ref": self.ref, "platform": self.platform, "account": self.account,
                "conversation_id": self.conversation_id, "title": self.title, "kind": self.kind,
                "last_active": stamp}


@dataclass
class Page:
    messages: list[dict]
    next_cursor: Any = None
    complete: bool = True
    warnings: list[str] = field(default_factory=list)


def _linked(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except AttributeError:
        return path.is_symlink()
    except FileNotFoundError:
        return False


class PolicyStore:
    """Fixed local policy location. Request JSON never supplies a filesystem path."""
    def __init__(self, private_root: Path):
        self.root = Path(private_root).absolute()
        self.path = self.root / "access-policy.json"
        self.lock_path = self.root / ".access-policy.lock"

    @contextmanager
    def locked(self):
        """Serialize all writers across threads and processes on one persistent inode.

        Never truncate, unlink or replace the lock file. Refuse linked/replaced
        lock paths so a second writer cannot silently acquire a different lock.
        """
        key = os.path.normcase(str(self.lock_path))
        with _POLICY_LOCKS_GUARD:
            thread_lock = _POLICY_LOCKS.setdefault(key, threading.RLock())
        with thread_lock:
            fd, acquired = None, False
            try:
                if _linked(self.root) or _linked(self.path) or _linked(self.lock_path):
                    raise AccessError("POLICY_INVALID", "The conversation policy is unavailable or invalid.")
                self.root.mkdir(parents=True, exist_ok=True)
                flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOINHERIT", 0)
                fd = os.open(self.lock_path, flags, 0o600)
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise AccessError("POLICY_INVALID", "The conversation policy is unavailable or invalid.")
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX)
                acquired = True
                if (_linked(self.root) or _linked(self.path) or _linked(self.lock_path) or
                        os.fstat(fd).st_nlink != 1 or
                        not os.path.samestat(os.fstat(fd), self.lock_path.stat())):
                    raise AccessError("POLICY_INVALID", "The conversation policy is unavailable or invalid.")
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                    os.fsync(fd)
            except OSError:
                if fd is not None:
                    os.close(fd)
                    fd = None
                raise AccessError("POLICY_WRITE_FAILED", "The conversation policy could not be saved.") from None
            except AccessError:
                if fd is not None:
                    os.close(fd)
                    fd = None
                raise
            try:
                yield fd
            finally:
                if fd is not None:
                    try:
                        if acquired:
                            if os.name == "nt":
                                os.lseek(fd, 0, os.SEEK_SET)
                                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                            else:
                                fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)

    @staticmethod
    def validate_accounts(accounts: Any) -> list[dict]:
        if not isinstance(accounts, list) or len(accounts) > 1000:
            raise AccessError("POLICY_INVALID", "The conversation policy is invalid.")
        normalized, seen = [], set()
        for row in accounts:
            if not isinstance(row, dict) or set(row) != {"platform", "account", "mode", "conversations"}:
                raise AccessError("POLICY_INVALID", "The conversation policy is invalid.")
            platform, account = row["platform"], text_id(row["account"])
            if platform not in ("wechat", "qq") or row["mode"] not in ("allow", "deny"):
                raise AccessError("POLICY_INVALID", "The conversation policy is invalid.")
            if (platform, account) in seen or not isinstance(row["conversations"], list):
                raise AccessError("POLICY_INVALID", "The conversation policy is invalid.")
            ids = [text_id(item) for item in row["conversations"]]
            if len(ids) != len(set(ids)) or len(ids) > 100000:
                raise AccessError("POLICY_INVALID", "The conversation policy is invalid.")
            normalized.append({"platform": platform, "account": account, "mode": row["mode"],
                               "conversations": sorted(ids)})
            seen.add((platform, account))
        return sorted(normalized, key=lambda r: (r["platform"], r["account"]))

    def read(self, required: bool = True) -> tuple[dict | None, str]:
        if _linked(self.root) or _linked(self.path):
            raise AccessError("POLICY_INVALID", "The conversation policy is unavailable or invalid.")
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            if not required:
                return None, ""
            raise AccessError("POLICY_REQUIRED", "Authorize the requested conversations before reading messages.") from None
        except OSError:
            raise AccessError("POLICY_INVALID", "The conversation policy is unavailable or invalid.") from None
        try:
            policy = json.loads(raw.decode("utf-8-sig"))
            if set(policy) != {"schemaVersion", "revision", "accounts"} or policy["schemaVersion"] != 1:
                raise ValueError()
            text_id(policy["revision"])
            policy["accounts"] = self.validate_accounts(policy["accounts"])
        except (ValueError, TypeError, KeyError, AccessError):
            raise AccessError("POLICY_INVALID", "The conversation policy is unavailable or invalid.") from None
        return policy, hashlib.sha256(raw).hexdigest()

    def write(self, accounts: Any) -> dict:
        accounts = self.validate_accounts(accounts)
        with self.locked() as lock_fd:
            return self._write_locked(accounts, lock_fd)

    def _write_locked(self, accounts: list[dict], lock_fd: int) -> dict:
        """Caller holds locked() for the complete read/modify/write operation."""
        if (_linked(self.root) or _linked(self.path) or _linked(self.lock_path) or
                os.fstat(lock_fd).st_nlink != 1 or
                not os.path.samestat(os.fstat(lock_fd), self.lock_path.stat())):
            raise AccessError("POLICY_INVALID", "The conversation policy is unavailable or invalid.")
        self.root.mkdir(parents=True, exist_ok=True)
        policy = {"schemaVersion": 1, "revision": uuid.uuid4().hex, "accounts": accounts}
        # Only policy metadata is persisted. Atomic replacement also lets readers detect every edit.
        target = self.root / (".access-policy-" + uuid.uuid4().hex + ".tmp")
        try:
            with target.open("x", encoding="utf-8") as stream:
                json.dump(policy, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(target, self.path)
        except OSError:
            raise AccessError("POLICY_WRITE_FAILED", "The conversation policy could not be saved.") from None
        return policy

    @staticmethod
    def permits(policy: dict, conv: Conversation) -> bool:
        for account in policy["accounts"]:
            if (account["platform"], account["account"]) == (conv.platform, conv.account):
                listed = conv.conversation_id in account["conversations"]
                return listed if account["mode"] == "allow" else not listed
        return False  # A new account never inherits another account's deny-list default.

    def unchanged(self, digest: str) -> None:
        if self.read()[1] != digest:
            raise AccessError("POLICY_CHANGED", "Permissions changed during the request; no result was released.")


class AccessService:
    def __init__(self, policy: PolicyStore, backend):
        self.policy, self.backend = policy, backend

    def catalog(self) -> tuple[list[dict], list[Conversation], list[str]]:
        accounts, conversations, warnings = self.backend.catalog()
        refs = set()
        for conv in conversations:
            if conv.platform not in ("wechat", "qq") or not conv.account or not conv.conversation_id or conv.ref in refs:
                raise AccessError("SOURCE_INVALID", "Local conversation metadata is inconsistent.")
            refs.add(conv.ref)
        return accounts, conversations, warnings

    def _resolve(self, refs: Any, policy: dict) -> list[Conversation]:
        if not isinstance(refs, list) or len(refs) > 100000:
            raise AccessError("INVALID_REQUEST", "Conversation references must be a list.")
        requested = list(dict.fromkeys(text_id(ref) for ref in refs))
        _, catalog, _ = self.catalog()
        by_ref = {conv.ref: conv for conv in catalog}
        if any(ref not in by_ref or not self.policy.permits(policy, by_ref[ref]) for ref in requested):
            raise AccessError("ACCESS_DENIED", "One or more conversations are unavailable or not authorized.")
        return [by_ref[ref] for ref in requested]

    def _find(self, request: dict) -> dict:
        query = text_id(request["query"]).strip().casefold()
        if not query:
            raise AccessError("INVALID_REQUEST", "A nonempty conversation search is required.")
        if "platform" in request and request["platform"] not in ("wechat", "qq"):
            raise AccessError("INVALID_REQUEST", "Choose a supported messaging platform.")
        for field in ("account", "kind"):
            if field in request:
                text_id(request[field])
        limit = request.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise AccessError("INVALID_REQUEST", "The search limit must be between 1 and 100.")
        policy, digest = self.policy.read(required=False)
        _, convs, warnings = self.catalog()
        matches = []
        for conv in convs:
            if any(field in request and request[field] != getattr(conv, field)
                   for field in ("platform", "account", "kind")):
                continue
            values = (conv.title.casefold(), conv.conversation_id.casefold())
            if not any(query in value for value in values):
                continue
            exact = query in values
            matches.append({**conv.public(), "authorized": bool(policy and self.policy.permits(policy, conv)),
                            "match": "exact" if exact else "partial"})
        matches.sort(key=lambda row: (row["match"] != "exact", -(row["last_active"] or 0),
                                      row["platform"], row["account"], row["conversation_id"]))
        # Search metadata and authorization flags must describe the same snapshot.
        if self.policy.read(required=False)[1] != digest:
            raise AccessError("POLICY_CHANGED", "Permissions changed during the request; retry the conversation search.")
        return {"conversations": matches[:limit], "total": len(matches), "truncated": len(matches) > limit,
                "policy_revision": policy["revision"] if policy else None, "warnings": warnings}

    def _patch_policy(self, request: dict, grant: bool) -> dict:
        refs = request["refs"]
        if not isinstance(refs, list) or not refs or len(refs) > 100000:
            raise AccessError("INVALID_REQUEST", "Explicit nonempty conversation references are required.")
        requested = list(dict.fromkeys(text_id(ref) for ref in refs))
        expected = request["expected_revision"]
        if expected is not None:
            text_id(expected)
        with self.policy.locked() as lock_fd:
            policy, _ = self.policy.read(required=False)
            revision = policy["revision"] if policy else None
            if revision != expected:
                raise AccessError("POLICY_CHANGED", "Permissions changed; review current candidates before retrying.")
            _, catalog, _ = self.catalog()
            by_ref = {conv.ref: conv for conv in catalog}
            if any(ref not in by_ref for ref in requested):
                raise AccessError("ACCESS_DENIED", "One or more conversations are unavailable or not authorized.")
            convs = [by_ref[ref] for ref in requested]
            accounts = [{**row, "conversations": list(row["conversations"])}
                        for row in (policy["accounts"] if policy else [])]
            by_account = {(row["platform"], row["account"]): row for row in accounts}
            changed = False
            for conv in convs:
                key = conv.platform, conv.account
                row = by_account.get(key)
                if row is None:
                    if not grant:
                        continue  # Unconfigured accounts already deny every conversation.
                    row = {"platform": conv.platform, "account": conv.account,
                           "mode": "allow", "conversations": []}
                    accounts.append(row)
                    by_account[key] = row
                listed = conv.conversation_id in row["conversations"]
                should_list = grant == (row["mode"] == "allow")
                if listed != should_list:
                    changed = True
                    if should_list:
                        row["conversations"].append(conv.conversation_id)
                    else:
                        row["conversations"].remove(conv.conversation_id)
            if changed:
                policy = self.policy._write_locked(self.policy.validate_accounts(accounts), lock_fd)
            return {"changed": changed, "policy_revision": policy["revision"] if policy else None,
                    "conversations": [{**conv.public(),
                                       "authorized": bool(policy and self.policy.permits(policy, conv))}
                                      for conv in convs]}

    @staticmethod
    def _request(request: Any) -> str:
        allowed = {"catalog": {"action"}, "policy.get": {"action"},
                   "policy.set": {"action", "accounts"}, "conversations": {"action"},
                   "policy.grant": {"action", "refs", "expected_revision"},
                   "policy.revoke": {"action", "refs", "expected_revision"},
                   "conversations.find": {"action", "query"},
                   "authorize": {"action", "refs"}, "messages": {"action", "refs", "since", "until"},
                   "media": {"action", "ref", "message_id"}}
        if not isinstance(request, dict) or request.get("action") not in allowed:
            raise AccessError("INVALID_REQUEST", "Unknown access action.")
        action = request["action"]
        optional = ({"platform", "account", "kind", "limit"} if action == "conversations.find" else
                    {"expected_revision"} if action == "policy.set" else set())
        if not allowed[action] <= set(request) or set(request) - allowed[action] - optional:
            raise AccessError("INVALID_REQUEST", "The request has missing or unsupported fields.")
        return action

    def execute(self, request: dict) -> dict:
        action = self._request(request)
        if action == "policy.get":
            return {"policy": self.policy.read(required=False)[0]}
        if action == "catalog":
            accounts, convs, warnings = self.catalog()
            return {"accounts": accounts, "conversations": [c.public() for c in convs], "warnings": warnings}
        if action == "conversations.find":
            return self._find(request)
        if action in ("policy.grant", "policy.revoke"):
            return self._patch_policy(request, action == "policy.grant")
        if action == "policy.set":
            accounts = self.policy.validate_accounts(request["accounts"])
            compare_revision = "expected_revision" in request
            if compare_revision and request["expected_revision"] is not None:
                text_id(request["expected_revision"])
            known_accounts, _, _ = self.catalog()
            known = {(a["platform"], a["account"]) for a in known_accounts}
            with self.policy.locked() as lock_fd:
                try:
                    existing, _ = self.policy.read(required=False)
                except AccessError:
                    # Explicit settings replacement remains a separate recovery action.
                    # Incremental grant/revoke always reject a corrupt policy.
                    if compare_revision:
                        raise
                    existing = None
                if compare_revision and (existing["revision"] if existing else None) != request["expected_revision"]:
                    raise AccessError("POLICY_CHANGED", "Permissions changed; review current settings before saving.")
                if existing:
                    known.update((a["platform"], a["account"]) for a in existing["accounts"])
                if any((a["platform"], a["account"]) not in known for a in accounts):
                    raise AccessError("ACCOUNT_UNKNOWN", "Configure only locally discovered accounts.")
                return {"policy": self.policy._write_locked(accounts, lock_fd)}
        policy, digest = self.policy.read()
        if action == "conversations":
            _, convs, warnings = self.catalog()
            result = {"conversations": [c.public() for c in convs if self.policy.permits(policy, c)],
                      "warnings": warnings, "policy_revision": policy["revision"]}
        elif action == "authorize":
            convs = self._resolve(request["refs"], policy)
            result = {"revision": policy["revision"], "allowed": [c.ref for c in convs]}
        elif action == "messages":
            since, until = request["since"], request["until"]
            if (isinstance(since, bool) or isinstance(until, bool) or
                not isinstance(since, (int, float)) or not isinstance(until, (int, float)) or
                not math.isfinite(since) or not math.isfinite(until) or not 0 <= since <= until <= 9999999999):
                raise AccessError("INVALID_RANGE", "Use a fixed, valid Unix-seconds interval.")
            convs = self._resolve(request["refs"], policy)
            messages, coverage, warnings = [], [], []
            for conv in convs:
                cursor, seen, pages, complete, reason = None, set(), set(), True, ""
                try:
                    while True:
                        self.policy.unchanged(digest)
                        page = self.backend.page(conv, since, until, cursor)
                        if not isinstance(page, Page):
                            raise AccessError("SOURCE_INVALID", "The message source returned invalid pagination.")
                        warnings.extend(page.warnings)
                        if not page.complete:
                            complete, reason = False, "SOURCE_WARNING"
                        page_ids = []
                        for row in page.messages:
                            msg_id = text_id(row["id"])
                            timestamp = float(row["timestamp"])
                            if not math.isfinite(timestamp):
                                raise ValueError()
                            page_ids.append(msg_id)
                            if since <= timestamp <= until and msg_id not in seen:
                                # Explicit projection prevents adapters leaking raw paths, elements or credentials.
                                messages.append({"id": msg_id, "ref": conv.ref, "timestamp": timestamp,
                                                 "sender": str(row.get("sender", "")), "text": str(row.get("text", "")),
                                                 "type": str(row.get("type", "unknown")),
                                                 "source": {"platform": conv.platform, "id": msg_id}})
                                seen.add(msg_id)
                        if page.next_cursor is None:
                            break
                        signature = tuple(page_ids)
                        if not page_ids or signature in pages or page.next_cursor == cursor:
                            complete, reason = False, "PAGINATION_STALLED"
                            break
                        pages.add(signature)
                        cursor = page.next_cursor
                except AccessError as exc:
                    if exc.code in ("POLICY_CHANGED", "POLICY_INVALID", "POLICY_REQUIRED"):
                        raise
                    complete, reason = False, exc.code
                except Exception:
                    complete, reason = False, "SOURCE_READ_FAILED"
                coverage.append({"ref": conv.ref, "complete": complete, "reason": reason,
                                 "scope": "available_local_source_fixed_interval"})
            messages.sort(key=lambda m: (m["timestamp"], m["ref"], m["id"]))
            result = {"messages": messages, "coverage": coverage, "policy_revision": policy["revision"],
                      "warnings": sorted(set(warnings))}
        else:
            conv = self._resolve([request["ref"]], policy)[0]
            msg_id = text_id(request["message_id"])
            self.policy.unchanged(digest)
            data, mime = self.backend.media(conv, msg_id)
            if data is None:
                result = {"available": False, "reason": "LOCAL_MEDIA_UNAVAILABLE"}
            else:
                if not isinstance(data, bytes) or len(data) > 25 * 1024 * 1024:
                    raise AccessError("MEDIA_TOO_LARGE", "The attachment exceeds the inline media limit.")
                result = {"available": True, "mime": mime, "size": len(data),
                          "base64": base64.b64encode(data).decode("ascii")}
            result["policy_revision"] = policy["revision"]
        self.policy.unchanged(digest)
        return result


def safe_response(service: AccessService, request: dict) -> dict:
    try:
        return {"ok": True, "data": service.execute(request)}
    except AccessError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": exc.message}}
    except Exception:
        return {"ok": False, "error": {"code": "ACCESS_FAILED", "message": "The local access request failed."}}
