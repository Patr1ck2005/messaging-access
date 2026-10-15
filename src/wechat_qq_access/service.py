"""One shared, fail-closed conversation policy. No model or server dependencies."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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

    @property
    def ref(self) -> str:
        return opaque_ref(self.platform, self.account, self.conversation_id)

    def public(self) -> dict:
        return {"ref": self.ref, "platform": self.platform, "account": self.account,
                "conversation_id": self.conversation_id, "title": self.title, "kind": self.kind}


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
            raise AccessError("POLICY_REQUIRED", "Choose conversation permissions in the local settings first.") from None
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
        if _linked(self.root) or _linked(self.path):
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

    @staticmethod
    def _request(request: Any) -> str:
        allowed = {"catalog": {"action"}, "policy.get": {"action"},
                   "policy.set": {"action", "accounts"}, "conversations": {"action"},
                   "authorize": {"action", "refs"}, "messages": {"action", "refs", "since", "until"},
                   "media": {"action", "ref", "message_id"}}
        if not isinstance(request, dict) or request.get("action") not in allowed:
            raise AccessError("INVALID_REQUEST", "Unknown access action.")
        action = request["action"]
        if set(request) != allowed[action]:
            raise AccessError("INVALID_REQUEST", "The request has missing or unsupported fields.")
        return action

    def execute(self, request: dict) -> dict:
        action = self._request(request)
        if action == "policy.get":
            return {"policy": self.policy.read(required=False)[0]}
        if action == "catalog":
            accounts, convs, warnings = self.catalog()
            return {"accounts": accounts, "conversations": [c.public() for c in convs], "warnings": warnings}
        if action == "policy.set":
            accounts = self.policy.validate_accounts(request["accounts"])
            known_accounts, _, _ = self.catalog()
            known = {(a["platform"], a["account"]) for a in known_accounts}
            try:
                existing, _ = self.policy.read(required=False)
            except AccessError:
                # Explicit local settings can replace a corrupt policy using currently
                # discovered accounts. Ordinary reads still fail closed.
                existing = None
            if existing:
                known.update((a["platform"], a["account"]) for a in existing["accounts"])
            if any((a["platform"], a["account"]) not in known for a in accounts):
                raise AccessError("ACCOUNT_UNKNOWN", "Configure only locally discovered accounts.")
            return {"policy": self.policy.write(accounts)}
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
