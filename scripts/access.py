"""One stdin JSON request and one stdout JSON response. No server or model calls."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_qq_access.backends import LocalBackend
from wechat_qq_access.service import AccessError, AccessService, PolicyStore, safe_response


def main():
    try:
        if len(sys.argv) != 1:
            raise AccessError("INVALID_REQUEST", "Provide one JSON request on standard input.")
        raw_request = sys.stdin.read(4 * 1024 * 1024 + 1)
        if len(raw_request) > 4 * 1024 * 1024:
            raise AccessError("INVALID_REQUEST", "The JSON request exceeds the size limit.")
        request = json.loads(raw_request)
        config_path = Path(os.environ.get("MESSAGING_ACCESS_CONFIG") or
                           str(Path(os.environ["LOCALAPPDATA"]) / "MessagingAccess" / "deployment.json"))
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if config.get("schemaVersion") != 1 or not isinstance(config.get("privateRoot"), str):
            raise AccessError("CONFIG_INVALID", "The private deployment configuration is invalid.")
        private = Path(os.path.expandvars(config["privateRoot"]))
        if not private.is_absolute():
            raise AccessError("CONFIG_INVALID", "The private deployment configuration is invalid.")
        service = AccessService(PolicyStore(private), LocalBackend(config))
        result = safe_response(service, request)
    except AccessError as exc:
        result = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
    except (json.JSONDecodeError, UnicodeError):
        result = {"ok": False, "error": {"code": "INVALID_JSON", "message": "The request or private configuration is invalid JSON."}}
    except Exception:
        result = {"ok": False, "error": {"code": "ACCESS_FAILED", "message": "The local access request failed."}}
    # Upstream imports occur only inside redirection scopes; keep stdout machine-readable.
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
