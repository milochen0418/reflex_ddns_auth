"""Where the collection tree is and who we are.

Resolution order (first match wins), the same in Docker and on a laptop:

    collection root   DDNS_COLLECTIONS_ROOT  →  ~/.re-ddns/config.json ("collections_root")
                      →  /Users/Shared/re-ddns/collections on macOS,
                         $XDG_DATA_HOME/re-ddns/collections elsewhere
    owner (app id)    DDNS_APP_ID  →  Collection(app_id=...)  →  the Reflex app_name
                      (underscores become hyphens)  →  "_local"
    node URL          DDNS_COLLECTIONS_NODE_URL  →  RE_DDNS_API_URL  →  none

Inside an App Store container re-ddns sets all three.  On a developer's
machine nothing is set, the platform default is used, and the Docker side
mounts that same host folder, so a local app and a containerised app share
one tree.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from reflex_ddns_auth.collections.refs import check_owner, valid_owner

ENV_ROOT = "DDNS_COLLECTIONS_ROOT"
ENV_APP_ID = "DDNS_APP_ID"
ENV_NODE_URL = "DDNS_COLLECTIONS_NODE_URL"
ENV_FALLBACK_NODE_URL = "RE_DDNS_API_URL"
CONFIG_PATH = Path.home() / ".re-ddns" / "config.json"
LOCAL_OWNER = "_local"
USER_OWNER = "_user"


def default_root() -> Path:
    if sys.platform == "darwin":
        return Path("/Users/Shared/re-ddns/collections")
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "re-ddns" / "collections"


def _config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_root(explicit: "str | os.PathLike | None" = None) -> Path:
    for candidate in (explicit, os.environ.get(ENV_ROOT), _config().get("collections_root")):
        if candidate:
            return Path(str(candidate)).expanduser()
    return default_root()


def app_id_from_name(name: str) -> str:
    """``dicom_data_explorer`` → ``dicom-data-explorer``; "" when nothing usable is left."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower().replace("_", "-"))
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug if valid_owner(slug) else ""


def _reflex_app_name() -> str:
    # Only look when Reflex is already loaded by the app; never import it here.
    rx = sys.modules.get("reflex")
    if rx is None:
        return ""
    try:
        return str(rx.config.get_config().app_name or "")
    except Exception:  # noqa: BLE001 - no rxconfig, or not a Reflex process
        return ""


def resolve_app_id(explicit: str | None = None) -> str:
    for candidate in (os.environ.get(ENV_APP_ID), explicit):
        if candidate:
            return check_owner(str(candidate))
    return app_id_from_name(_reflex_app_name()) or LOCAL_OWNER


def resolve_node_url(explicit: str | None = None) -> str:
    for candidate in (explicit, os.environ.get(ENV_NODE_URL), os.environ.get(ENV_FALLBACK_NODE_URL)):
        if candidate:
            return str(candidate).rstrip("/")
    return ""
