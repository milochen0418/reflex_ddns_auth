"""Wire protocol shared by intent hosts and providers.

A host page opens a provider page in an iframe:

    https://<app>.<zone>/intent/<action>?<params>&_intent_id=<id>&_intent_origin=<host origin>

The provider talks back with ``window.parent.postMessage``:

    {"type": "ddns-intent", "v": 1, "id": <id>, "event": <event>, "data": <any>}

where ``event`` is one of ``ready``, ``resize`` (data = height in px),
``result`` (data = JSON object) or ``cancel``.
"""

import json
import os
from urllib.parse import urlparse

MESSAGE_TYPE = "ddns-intent"
PROTOCOL_VERSION = 1

ID_PARAM = "_intent_id"
ORIGIN_PARAM = "_intent_origin"

ZONE = os.environ.get("DDNS_INTENT_ZONE", "reflex-ddns.com")
TRUST_LOCALHOST = os.environ.get("DDNS_INTENT_TRUST_LOCALHOST", "") == "1"
_URL_ENV_PREFIX = "DDNS_INTENT_URL_"
_LOCAL_HOSTS = ("localhost", "127.0.0.1")


def intent_route(action: str) -> str:
    """Page route for an action, e.g. ``profile.view`` -> ``/intent/profile-view``.

    Dots are avoided in the path because dev servers treat them as file
    extensions.
    """
    return "/intent/" + action.replace(".", "-")


def _override_urls() -> dict[str, str]:
    return {
        key[len(_URL_ENV_PREFIX):].lower().replace("_", "-"): value.rstrip("/")
        for key, value in os.environ.items()
        if key.startswith(_URL_ENV_PREFIX) and value
    }


def app_url(app: str) -> str:
    """Base URL of an app, overridable with ``DDNS_INTENT_URL_<APP>``.

    e.g. ``DDNS_INTENT_URL_RELACK=http://localhost:3000`` for local dev.
    """
    return _override_urls().get(app.lower(), f"https://{app}.{ZONE}")


def origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


def _trusted_origins() -> list[str]:
    return sorted({origin_of(url) for url in _override_urls().values()} - {""})


def is_local(origin: str) -> bool:
    return (urlparse(origin or "").hostname or "") in _LOCAL_HOSTS


def is_trusted_origin(origin: str, own_origin: str = "") -> bool:
    """Whether ``origin`` may talk to us over the intent protocol.

    Trusted: https apps on the zone, explicit ``DDNS_INTENT_URL_*`` overrides,
    and localhost when we run on localhost ourselves (or
    ``DDNS_INTENT_TRUST_LOCALHOST=1``).
    """
    parsed = urlparse(origin or "")
    host = parsed.hostname or ""
    if not host:
        return False
    if parsed.scheme == "https" and (host == ZONE or host.endswith("." + ZONE)):
        return True
    if host in _LOCAL_HOSTS and (TRUST_LOCALHOST or is_local(own_origin)):
        return True
    return origin_of(origin) in _trusted_origins()


def _js_trust_check() -> str:
    """JS expression ``(origin) => bool`` mirroring :func:`is_trusted_origin`."""
    config = json.dumps(
        {
            "zone": ZONE,
            "localhost": TRUST_LOCALHOST,
            "localHosts": list(_LOCAL_HOSTS),
            "origins": _trusted_origins(),
        }
    )
    return f"""((o) => {{
  const c = {config};
  try {{
    const u = new URL(o);
    const h = u.hostname;
    if (u.protocol === "https:" && (h === c.zone || h.endsWith("." + c.zone))) return true;
    if (c.localHosts.includes(h) && (c.localhost || c.localHosts.includes(location.hostname))) return true;
    return c.origins.includes(u.origin);
  }} catch (e) {{ return false; }}
}})"""


def host_bridge_js(inbox_button_id: str, close_button_id: str, frame_id: str) -> str:
    """Installs (once) the host-side listener.

    Resize messages are applied to the iframe directly; everything else is
    queued in ``window.__ddnsIntentInbox`` and delivered to Python by clicking
    a hidden button.
    """
    return f"""(() => {{
  if (window.__ddnsIntentHost) return;
  window.__ddnsIntentHost = true;
  window.__ddnsIntentInbox = [];
  const trusted = {_js_trust_check()};
  window.addEventListener("message", (ev) => {{
    const m = ev.data;
    if (!m || m.type !== {json.dumps(MESSAGE_TYPE)} || !trusted(ev.origin)) return;
    const frame = document.getElementById({json.dumps(frame_id)});
    if (!frame || ev.source !== frame.contentWindow) return;
    if (m.event === "resize") {{
      const h = Math.max(120, Math.min(Number(m.data) || 0, window.innerHeight * 0.9));
      frame.style.height = h + "px";
      return;
    }}
    if (m.event === "ready") return;
    window.__ddnsIntentInbox.push({{id: m.id, event: m.event, data: m.data ?? null, origin: ev.origin}});
    const btn = document.getElementById({json.dumps(inbox_button_id)});
    if (btn) btn.click();
  }});
  window.addEventListener("keydown", (ev) => {{
    if (ev.key !== "Escape") return;
    const btn = document.getElementById({json.dumps(close_button_id)});
    if (btn) btn.click();
  }});
}})()"""


def provider_bridge_js(parent_origin: str, call_id: str, root_id: str) -> str:
    """Installs (once) the provider-side bridge inside the iframe.

    Exposes ``window.__ddnsIntentPage.post(event, data)``, reports the content
    height whenever it changes, and turns Escape into ``cancel``.
    """
    return f"""(() => {{
  if (window.__ddnsIntentPage || window.parent === window) return;
  const origin = {json.dumps(parent_origin)};
  const id = {json.dumps(call_id)};
  const post = (event, data) => window.parent.postMessage(
    {{type: {json.dumps(MESSAGE_TYPE)}, v: {PROTOCOL_VERSION}, id, event, data: data ?? null}}, origin);
  window.__ddnsIntentPage = {{post}};
  let last = 0;
  let ready = false;
  const watch = () => {{
    const root = document.getElementById({json.dumps(root_id)});
    if (!root) {{ requestAnimationFrame(watch); return; }}
    new ResizeObserver(() => {{
      const h = Math.ceil(root.getBoundingClientRect().height);
      if (h !== last) {{ last = h; post("resize", h); }}
      if (!ready) {{ ready = true; post("ready"); }}
    }}).observe(root);
  }};
  watch();
  window.addEventListener("keydown", (ev) => {{ if (ev.key === "Escape") post("cancel"); }});
}})()"""


def provider_post_js(event: str, data=None) -> str:
    return (
        "window.__ddnsIntentPage && "
        f"window.__ddnsIntentPage.post({json.dumps(event)}, {json.dumps(data)})"
    )
