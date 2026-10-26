"""Wire protocol shared by intent hosts and providers.

A host page opens a provider page in an iframe:

    https://<app>.<zone>/intent/<action>?<params>&_intent_id=<id>&_intent_origin=<host origin>

The provider talks back with ``window.parent.postMessage``:

    {"type": "ddns-intent", "v": 1, "id": <id>, "event": <event>, "data": <any>}

where ``event`` is one of ``ready``, ``resize`` (data = height in px),
``update`` (data = JSON object; news for the caller while the dialog stays
open, e.g. "this file is ready"), ``result`` (data = JSON object) or
``cancel`` (data = optional details; ``{"via": "escape"}`` when the user
pressed Escape). A host page may hold several dialogs at once: each iframe
carries its id in ``data-intent-id``, and a message counts only when it comes
from the iframe with its ``id``.

Parameters too sensitive for the URL (logs, history) travel the other way:
with ``_intent_wait=1`` in the URL, the provider holds ``on_open`` until the
host answers its ``ready`` with

    {"type": "ddns-intent", "v": 1, "id": <id>, "event": "params", "data": {...}}

posted to the provider's origin only.

The host also tells the provider how its dialog is shown, on ``ready`` and
whenever that changes, with ``event`` ``view`` and data ``{"mode": ...}``:
``front`` (the dialog in front), ``pip`` (a small picture-in-picture window,
scaled down and not clickable) or ``tray`` (minimized, hidden). The provider
page gets it as ``<html data-intent-view="...">``, so it can show a compact,
large-print layout in the picture-in-picture window with CSS alone.

A dialog may ask what the host page shows right now (an AI in it wants to talk
about it): it posts ``want`` with ``{"what": "context"}``, and the host answers
``context`` with whatever the page's ``window.__ddnsContext`` gives (a value, or
a function, maybe async; ``null`` when the page has none). The host may also
send ``context`` unasked (``Intent.context(action, data)``, or
``window.__ddnsIntentHostApi.context(action, data)``). In the dialog,
``window.__ddnsIntentPage.ask()`` returns a promise of the next answer, the
latest one is kept in ``window.__ddnsIntentPage.context``, every answer fires a
``ddns-intent-context`` window event, and an action declared with
``on_context`` gets it in Python too.
"""

import json
import os
from urllib.parse import urlparse

MESSAGE_TYPE = "ddns-intent"
PROTOCOL_VERSION = 1

ID_PARAM = "_intent_id"
ORIGIN_PARAM = "_intent_origin"
WAIT_PARAM = "_intent_wait"

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


def host_bridge_js(inbox_button_id: str, close_button_id: str) -> str:
    """Installs (once) the host-side listener for all dialogs of the page.

    Resize messages are applied to the sending iframe directly; everything
    else is queued in ``window.__ddnsIntentInbox`` and delivered to Python by
    clicking a hidden button.
    """
    return f"""(() => {{
  if (window.__ddnsIntentHost) return;
  window.__ddnsIntentHost = true;
  window.__ddnsIntentInbox = [];
  window.__ddnsIntentPending = window.__ddnsIntentPending || {{}};
  const trusted = {_js_trust_check()};
  const frameOf = (source) => {{
    for (const f of document.querySelectorAll("iframe[data-intent-id]")) {{
      if (f.contentWindow === source) return f;
    }}
    return null;
  }};
  // What this page shows now, for a dialog that asks (an AI in it): window.__ddnsContext, a
  // value or a function (maybe async) the page sets; null without one.
  const contextOf = async () => {{
    const c = window.__ddnsContext;
    try {{ return (typeof c === "function" ? await c() : c) ?? null; }} catch (e) {{ return null; }}
  }};
  const tell = (frame, data) => {{
    let origin;
    try {{ origin = new URL(frame.src, location.href).origin; }} catch (e) {{ return; }}
    if (!frame.contentWindow) return;
    frame.contentWindow.postMessage(
      {{type: {json.dumps(MESSAGE_TYPE)}, v: {PROTOCOL_VERSION}, id: frame.dataset.intentId, event: "context", data: data ?? null}}, origin);
  }};
  window.__ddnsIntentHostApi = {{
    // Sends data to the open dialogs of an action (to all of them without one), unasked.
    context: (action, data) => {{
      for (const f of document.querySelectorAll("iframe[data-intent-id]")) {{
        if (!action || f.dataset.intentAction === action) tell(f, data);
      }}
    }},
  }};
  window.addEventListener("message", (ev) => {{
    const m = ev.data;
    if (!m || m.type !== {json.dumps(MESSAGE_TYPE)} || !trusted(ev.origin)) return;
    // Only the dialog's own iframe may speak for its id.
    const frame = frameOf(ev.source);
    if (!frame || frame.dataset.intentId !== m.id) return;
    if (m.event === "resize") {{
      const h = Math.max(120, Math.min(Number(m.data) || 0, window.innerHeight * 0.9));
      frame.style.height = h + "px";
      return;
    }}
    if (m.event === "want") {{
      contextOf().then((data) => tell(frame, data));
      return;
    }}
    if (m.event === "ready") {{
      // Hand over the private params of this dialog, to its origin only.
      const p = window.__ddnsIntentPending[m.id];
      if (p && p.origin === ev.origin) {{
        delete window.__ddnsIntentPending[m.id];
        frame.contentWindow.postMessage(
          {{type: {json.dumps(MESSAGE_TYPE)}, v: {PROTOCOL_VERSION}, id: m.id, event: "params", data: p.data}}, p.origin);
      }}
      // A (re)loaded page does not know yet how its dialog is shown.
      if (window.__ddnsIntentTray && window.__ddnsIntentTray.resendView) window.__ddnsIntentTray.resendView(m.id);
      return;
    }}
    window.__ddnsIntentInbox.push({{id: m.id, event: m.event, data: m.data ?? null, origin: ev.origin}});
    const btn = document.getElementById({json.dumps(inbox_button_id)});
    if (btn) btn.click();
  }});
  window.addEventListener("keydown", (ev) => {{
    if (ev.key !== "Escape") return;
    const btn = document.getElementById({json.dumps(close_button_id)});
    if (btn) btn.click();
  }});
  // A same-origin intent iframe shares this tab's sessionStorage, where Reflex
  // keeps its client token. The backend hands the iframe a fresh token (it looks
  // like a duplicated tab) and the iframe stores it, so after a reload this page
  // would resume the iframe's session. Storage events only reach *other*
  // documents, so put our token back whenever someone else changes it.
  window.addEventListener("storage", (ev) => {{
    if (ev.storageArea !== window.sessionStorage || ev.key !== "token" || !ev.oldValue) return;
    if (ev.newValue !== ev.oldValue) window.sessionStorage.setItem("token", ev.oldValue);
  }});
}})()"""


def host_pending_params_js(call_id: str, origin: str, data: dict) -> str:
    """Remembers the private params the host bridge sends on the dialog's ``ready``."""
    pending = json.dumps({"origin": origin, "data": data})
    return f"(window.__ddnsIntentPending = window.__ddnsIntentPending || {{}})[{json.dumps(call_id)}] = {pending};"


def provider_bridge_js(
    parent_origin: str,
    call_id: str,
    root_id: str,
    params_button_id: str,
    wait: bool = False,
    python_context: bool = False,
) -> str:
    """Installs (once) the provider-side bridge inside the iframe.

    Exposes ``window.__ddnsIntentPage.post(event, data)``, reports the content
    height whenever it changes, turns Escape into ``cancel`` (``via: "escape"``,
    which a host may take as "minimize" for a keep-alive dialog), and delivers the
    host's ``params`` message to Python by clicking a hidden button. With
    ``wait`` (private params expected), ``ready`` is repeated until they come:
    the host's listener may start after this page, e.g. when the host reloads.
    The host's ``view`` message sets ``<html data-intent-view>`` and fires a
    ``ddns-intent-view`` window event (``detail`` = the mode).

    ``window.__ddnsIntentPage.ask(timeout_ms)`` asks the host what its page shows
    now (see the module docstring) and resolves with the answer (``null`` without
    one in time); ``window.__ddnsIntentPage.context`` keeps the latest answer.
    With ``python_context`` (the action has an ``on_context`` handler), each
    answer also goes to Python, as the params do.
    """
    return f"""(() => {{
  if (window.__ddnsIntentPage || window.parent === window) return;
  const origin = {json.dumps(parent_origin)};
  const id = {json.dumps(call_id)};
  const post = (event, data) => window.parent.postMessage(
    {{type: {json.dumps(MESSAGE_TYPE)}, v: {PROTOCOL_VERSION}, id, event, data: data ?? null}}, origin);
  let askers = [];
  const ask = (timeout) => new Promise((resolve) => {{
    const entry = (data) => {{ clearTimeout(timer); resolve(data); }};
    const timer = setTimeout(() => {{ askers = askers.filter((a) => a !== entry); resolve(null); }}, timeout || 1500);
    askers.push(entry);
    post("want", {{what: "context"}});
  }});
  window.__ddnsIntentPage = {{post, ask, context: null}};
  let last = 0;
  let ready = false;
  let gotParams = false;
  const watch = () => {{
    const root = document.getElementById({json.dumps(root_id)});
    if (!root) {{ requestAnimationFrame(watch); return; }}
    new ResizeObserver(() => {{
      const h = Math.ceil(root.getBoundingClientRect().height);
      if (h !== last) {{ last = h; post("resize", h); }}
      if (!ready) {{
        ready = true;
        post("ready");
        if ({json.dumps(wait)}) {{
          let tries = 0;
          const again = setInterval(() => {{
            if (gotParams || ++tries > 60) {{ clearInterval(again); return; }}
            post("ready");
          }}, 1000);
        }}
      }}
    }}).observe(root);
  }};
  watch();
  window.addEventListener("keydown", (ev) => {{ if (ev.key === "Escape") post("cancel", {{via: "escape"}}); }});
  window.__ddnsIntentPageInbox = [];
  window.addEventListener("message", (ev) => {{
    const m = ev.data;
    if (ev.source !== window.parent || ev.origin !== origin) return;
    if (!m || m.type !== {json.dumps(MESSAGE_TYPE)} || m.id !== id) return;
    if (m.event === "view") {{
      const mode = String((m.data && m.data.mode) || "front");
      document.documentElement.setAttribute("data-intent-view", mode);
      window.dispatchEvent(new CustomEvent("ddns-intent-view", {{detail: mode}}));
      return;
    }}
    const toPython = () => {{
      window.__ddnsIntentPageInbox.push({{event: m.event, data: m.data ?? null}});
      const btn = document.getElementById({json.dumps(params_button_id)});
      if (btn) btn.click();
    }};
    if (m.event === "context") {{
      window.__ddnsIntentPage.context = m.data ?? null;
      const waiting = askers;
      askers = [];
      for (const a of waiting) a(m.data ?? null);
      window.dispatchEvent(new CustomEvent("ddns-intent-context", {{detail: m.data ?? null}}));
      if ({json.dumps(python_context)}) toPython();
      return;
    }}
    if (m.event !== "params") return;
    gotParams = true;
    toPython();
  }});
}})()"""


def provider_post_js(event: str, data=None) -> str:
    return (
        "window.__ddnsIntentPage && "
        f"window.__ddnsIntentPage.post({json.dumps(event)}, {json.dumps(data)})"
    )
