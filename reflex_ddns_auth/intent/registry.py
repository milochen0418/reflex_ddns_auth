"""Which apps provide an action: lets callers start an intent without naming the app.

    Intent.start(None, "call.join", room=rid)

Providers publish their actions at ``MANIFEST_PATH`` (see ``provider.intent``);
re-ddns collects them from every installed app and answers
``GET <registry>/api/intent/providers?action=<action>``.

Lookup order:

1. ``DDNS_INTENT_PROVIDER_<ACTION>`` (comma-separated apps), e.g.
   ``DDNS_INTENT_PROVIDER_CALL_JOIN=livekit`` for local dev.
2. The registry at ``DDNS_INTENT_REGISTRY_URL``, else ``RE_DDNS_API_URL`` (set
   in every app container the App Store installs).
"""

import logging
import os
import re
import time

import httpx

logger = logging.getLogger(__name__)

MANIFEST_PATH = "/_ddns_intent/manifest"
_PROVIDER_ENV_PREFIX = "DDNS_INTENT_PROVIDER_"
_CACHE_SECONDS = 30
_TIMEOUT_SECONDS = 3

_cache: dict[str, tuple[float, list[str]]] = {}


def provider_env_key(action: str) -> str:
    """e.g. ``call.join`` -> ``DDNS_INTENT_PROVIDER_CALL_JOIN``."""
    return _PROVIDER_ENV_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", action).upper()


def _registry_url() -> str:
    url = os.environ.get("DDNS_INTENT_REGISTRY_URL") or os.environ.get("RE_DDNS_API_URL") or ""
    return url.rstrip("/")


async def resolve_providers(action: str) -> list[str]:
    """Apps that provide ``action``, best effort (empty when none is known)."""
    configured = os.environ.get(provider_env_key(action), "")
    if configured:
        return [app.strip() for app in configured.split(",") if app.strip()]

    registry = _registry_url()
    if not registry:
        return []
    cached = _cache.get(action)
    if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"{registry}/api/intent/providers", params={"action": action})
            resp.raise_for_status()
            apps = [p["app"] for p in resp.json().get("providers", []) if p.get("app")]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        logger.warning("Intent registry lookup for %r failed: %s", action, e)
        return []
    _cache[action] = (time.monotonic(), apps)
    return apps
