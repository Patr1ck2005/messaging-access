"""Deployment selection shared by local Python entry points.

Selection never retries a different installation after a selected file fails to
parse. Keeping new runtime files outside AppData avoids MSIX host virtualization.
"""
from __future__ import annotations

import os
from pathlib import Path

from .service import AccessError


def default_private_root(environ=None) -> Path:
    env = os.environ if environ is None else environ
    profile = env.get("USERPROFILE") or str(Path.home())
    return Path(profile) / ".local" / "share" / "wechat-qq-access"


def resolve_config_path(explicit=None, environ=None) -> Path:
    env = os.environ if environ is None else environ
    selected = explicit if explicit is not None else env.get("MESSAGING_ACCESS_CONFIG")
    if selected is not None:
        if not isinstance(selected, (str, os.PathLike)) or not str(selected).strip():
            raise AccessError("CONFIG_INVALID", "The deployment configuration path is invalid.")
        return Path(os.path.abspath(os.path.expandvars(str(selected))))
    fresh = default_private_root(env) / "deployment.json"
    if fresh.exists():
        return fresh
    profile = env.get("USERPROFILE") or str(Path.home())
    legacy_root = env.get("LOCALAPPDATA") or str(Path(profile) / "AppData" / "Local")
    legacy = Path(legacy_root) / "MessagingAccess" / "deployment.json"
    return legacy if legacy.exists() else fresh
