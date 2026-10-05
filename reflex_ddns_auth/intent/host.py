"""Host side: open other apps' intents in dialogs and receive their results.

    from reflex_ddns_auth.intent import Intent, intent_host

    rx.button("Profile", on_click=Intent.start("relack", "profile.view", user=email))
    rx.button("Pick", on_click=Intent.start("relack", "people.pick", on_result=MyState.on_pick))
    rx.button("Call", on_click=Intent.start(None, "call.join", on_cancel=MyState.on_left,
                                            private={"room": rid}, keep_alive=True))

    intent_host()  # once per page...
    install_intent_host(app)  # ...or once for the app: dialogs survive page changes

``app=None`` opens whichever installed app provides the action (see
``registry``); when several do, the dialog first asks which one to use.
``private`` params skip the URL and reach the provider by postMessage.

When several people must end up in the same app (e.g. everyone joining one
call), let the first one choose it, then start it by name for everyone:

    rx.button("Call", on_click=Intent.choose_app("call.join", on_result=MyState.app_chosen))
    # MyState.app_chosen gets {"app": ..., "action": ...}: keep the app with the call
    # and open it with Intent.start(app, "call.join", ...) for the caller and the callees

One dialog is in front at a time. Opening another one closes it, unless it
was started with ``keep_alive=True``: then it is minimized to a tray at the
bottom left and keeps running (e.g. a call goes on while a picker is open),
until the user brings it back or closes it. Clicking outside or Escape also
minimize a keep-alive dialog; its × closes it. ``single=True`` allows one
dialog of the action at a time (opening it again closes the previous one).

``on_result`` runs when the provider finishes with data. ``on_cancel`` runs when
the dialog closes without a result, with ``{"reason": ..., "action": ...}``:

- ``closed``: the × button (or, for other dialogs, a click outside or Escape)
- ``cancel``: the provider cancelled (plus its details, e.g. ``error``)
- ``replaced``: another intent opened in its place (``by`` names its action)
- ``unavailable``: no installed app provides the action
"""

import json
import secrets
from typing import Any
from urllib.parse import urlencode

import reflex as rx
from reflex.event import EventHandler, EventSpec
from reflex.utils.format import format_event_handler

from reflex_ddns_auth.intent.protocol import (
    ID_PARAM,
    ORIGIN_PARAM,
    WAIT_PARAM,
    app_url,
    host_bridge_js,
    host_pending_params_js,
    intent_route,
    origin_of,
)
from reflex_ddns_auth.intent.registry import resolve_providers

# id of the iframe of the dialog in front; the others get FRAME_ID-<dialog id>.
FRAME_ID = "ddns-intent-frame"
INBOX_BUTTON_ID = "ddns-intent-inbox"
CLOSE_BUTTON_ID = "ddns-intent-close"
# Features the dialog iframe may use (Permissions Policy). A cross-origin iframe
# cannot even prompt for the microphone or autoplay sound unless delegated.
DEFAULT_ALLOW = "autoplay; microphone; camera; clipboard-write"


def _resolve_handler(key: str) -> EventHandler | None:
    """Find an event handler from its ``format_event_handler`` name.

    Looked up at runtime because page components (where ``Intent.start`` runs)
    are not necessarily built in the backend process.
    """
    state_path, _, name = key.rpartition(".")
    parts = state_path.split(".")
    if not name or parts[0] != rx.State.get_name():
        return None
    try:
        state_cls = rx.State.get_class_substate(tuple(parts[1:])) if len(parts) > 1 else rx.State
    except ValueError:
        return None
    return state_cls.event_handlers.get(name)


def _bridge() -> EventSpec:
    return rx.call_script(host_bridge_js(INBOX_BUTTON_ID, CLOSE_BUTTON_ID))


class IntentState(rx.State):
    """The intent dialogs open on this page: one in front, keep-alive ones minimized."""

    # Open dialogs in the order they opened, each
    # {"id", "kind" ("frame" | "chooser"), "src", "action", "label", "keep_alive" ("1" | "")}.
    # Rendered with their id as key: an iframe that moved in the page would reload.
    dialogs: list[dict[str, str]] = []
    # The dialog in front ("" when none).
    active: str = ""
    # Apps to pick from in the chooser dialog ({"app", "url"}).
    choices: list[dict[str, str]] = []
    # id -> {"origin", "result_key", "cancel_key", "private"} or, for a chooser, its
    # "cancel_key" and either the "request" waiting for the app (Intent.start) or
    # the "result_key" to report the app to (Intent.choose_app).
    _meta: dict[str, dict[str, Any]] = {}

    @rx.var
    def is_open(self) -> bool:
        """Whether a dialog is in front."""
        return self.active != ""

    @rx.var
    def action(self) -> str:
        """Action of the dialog in front."""
        return next((d["action"] for d in self.dialogs if d["id"] == self.active), "")

    @rx.var
    def src(self) -> str:
        """URL of the dialog in front."""
        return next((d["src"] for d in self.dialogs if d["id"] == self.active), "")

    @rx.var
    def open_actions(self) -> list[str]:
        """Actions of all open dialogs, in front or minimized."""
        return [d["action"] for d in self.dialogs if d["kind"] == "frame"]

    @rx.var
    def minimized(self) -> list[dict[str, str]]:
        """Dialogs running in the background, shown in the tray."""
        return [d for d in self.dialogs if d["kind"] == "frame" and d["id"] != self.active]

    def _find(self, dialog_id: str) -> dict[str, str] | None:
        return next((d for d in self.dialogs if d["id"] == dialog_id), None)

    def _remove(self, dialog_id: str):
        removed = self._find(dialog_id)
        self.dialogs = [d for d in self.dialogs if d["id"] != dialog_id]
        self._meta = {k: v for k, v in self._meta.items() if k != dialog_id}
        if self.active == dialog_id:
            self.active = ""
        if removed is not None and removed["kind"] == "chooser":
            self.choices = []

    def _dismiss(self, dialog_id: str, reason: str, **details: Any) -> EventSpec | None:
        """Close a dialog without a result; returns the caller's ``on_cancel`` event, if any."""
        dialog = self._find(dialog_id)
        if dialog is None:
            return None
        handler = _resolve_handler(self._meta.get(dialog_id, {}).get("cancel_key", ""))
        data = {**details, "reason": reason, "action": dialog["action"]}
        self._remove(dialog_id)
        return handler(data) if handler is not None else None

    def _vacate_front(self, by: str) -> list[EventSpec]:
        """Make room in front: a keep-alive dialog there is minimized, any other closes."""
        front = self._find(self.active)
        if front is None:
            return []
        if front["keep_alive"]:
            self.active = ""
            return []
        dismissed = self._dismiss(front["id"], "replaced", by=by)
        return [dismissed] if dismissed is not None else []

    def _show(self, dialog_id: str) -> list[EventSpec]:
        dialog = self._find(dialog_id)
        if dialog is None or self.active == dialog_id:
            return []
        events = self._vacate_front(dialog["action"])
        self.active = dialog_id
        return events

    def _launch(self, app: str, request: dict[str, Any]) -> EventSpec:
        base = app_url(app)
        dialog_id = secrets.token_hex(8)
        private = request["private"]
        query = {k: "" if v is None else str(v) for k, v in request["params"].items()}
        query[ID_PARAM] = dialog_id
        query[ORIGIN_PARAM] = self.router.url.origin or ""
        if private:
            query[WAIT_PARAM] = "1"

        origin = origin_of(base)
        self.dialogs = [
            *self.dialogs,
            {
                "id": dialog_id,
                "kind": "frame",
                "src": f"{base}{intent_route(request['action'])}?{urlencode(query)}",
                "action": request["action"],
                "label": request["label"] or request["action"],
                "keep_alive": "1" if request["keep_alive"] else "",
            },
        ]
        self._meta = {
            **self._meta,
            dialog_id: {
                "origin": origin,
                "result_key": request["result_key"],
                "cancel_key": request["cancel_key"],
                # Kept to hand over again if the page reloads under the dialog.
                "private": private,
            },
        }
        self.active = dialog_id
        script = host_bridge_js(INBOX_BUTTON_ID, CLOSE_BUTTON_ID)
        if private:
            script += ";" + host_pending_params_js(dialog_id, origin, private)
        return rx.call_script(script)

    def _open_chooser(self, action: str, label: str, providers: list[str], meta: dict[str, Any]) -> EventSpec:
        """Ask which of ``providers`` should handle ``action``; ``choose`` goes on from there."""
        dialog_id = secrets.token_hex(8)
        self.dialogs = [
            *self.dialogs,
            {
                "id": dialog_id,
                "kind": "chooser",
                "src": "",
                "action": action,
                "label": label or action,
                "keep_alive": "",
            },
        ]
        self._meta = {**self._meta, dialog_id: meta}
        self.choices = [{"app": p, "url": app_url(p)} for p in providers]
        self.active = dialog_id
        return _bridge()

    @staticmethod
    def _unavailable(action: str, cancel_key: str) -> list[EventSpec]:
        """No installed app provides ``action``: say so, and tell the caller's ``on_cancel``."""
        events = [rx.toast.error(f"No installed app can open {action}.")]
        handler = _resolve_handler(cancel_key)
        if handler is not None:
            events.append(handler({"reason": "unavailable", "action": action}))
        return events

    @rx.event
    async def open_intent(
        self,
        app: str,
        action: str,
        params: dict,
        result_key: str = "",
        cancel_key: str = "",
        private: dict | None = None,
        options: dict | None = None,
    ):
        options = options or {}
        request = {
            "action": action,
            "params": params,
            "result_key": result_key,
            "cancel_key": cancel_key,
            "private": private or {},
            "keep_alive": bool(options.get("keep_alive")),
            "label": str(options.get("label") or ""),
        }
        events: list[EventSpec | None] = []
        if options.get("single"):
            for dialog in list(self.dialogs):
                if dialog["action"] == action:
                    events.append(self._dismiss(dialog["id"], "replaced", by=action))
        events.extend(self._vacate_front(action))

        if not app:
            providers = await resolve_providers(action)
            if not providers:
                events.extend(self._unavailable(action, cancel_key))
                return [e for e in events if e is not None]
            if len(providers) > 1:
                meta = {"cancel_key": cancel_key, "request": request}
                events.append(self._open_chooser(action, request["label"], providers, meta))
                return [e for e in events if e is not None]
            app = providers[0]

        events.append(self._launch(app, request))
        return [e for e in events if e is not None]

    @rx.event
    async def choose_app(self, action: str, result_key: str, cancel_key: str = ""):
        """Find the app for ``action`` without opening it, asking when several provide it."""
        providers = await resolve_providers(action)
        if not providers:
            return self._unavailable(action, cancel_key)
        if len(providers) == 1:
            handler = _resolve_handler(result_key)
            return handler({"app": providers[0], "action": action}) if handler is not None else None
        meta = {"cancel_key": cancel_key, "result_key": result_key}
        return [*self._vacate_front(action), self._open_chooser(action, "", providers, meta)]

    @rx.event
    def rearm(self):
        """On (re)mount, e.g. after a reload: the dialogs are still open and their
        pages load again, so listen to them and resend their private params."""
        if not self.dialogs:
            return
        script = host_bridge_js(INBOX_BUTTON_ID, CLOSE_BUTTON_ID)
        for dialog in self.dialogs:
            meta = self._meta.get(dialog["id"], {})
            if dialog["kind"] == "frame" and meta.get("private"):
                script += ";" + host_pending_params_js(dialog["id"], meta["origin"], meta["private"])
        return rx.call_script(script)

    @rx.event
    def choose(self, app: str):
        """The user picked ``app`` in the chooser: open the intent waiting there, or report the app."""
        chooser = self._find(self.active)
        if chooser is None or chooser["kind"] != "chooser":
            return
        if app not in [c["app"] for c in self.choices]:
            return
        meta = self._meta.get(chooser["id"], {})
        self._remove(chooser["id"])
        if meta.get("request") is not None:
            return self._launch(app, meta["request"])
        handler = _resolve_handler(meta.get("result_key", ""))
        if handler is not None:
            return handler({"app": app, "action": chooser["action"]})

    @rx.event
    def close(self):
        """Click outside or Escape: close the dialog in front, or minimize a keep-alive one."""
        front = self._find(self.active)
        if front is None:
            return
        if front["keep_alive"]:
            self.active = ""
            return
        return self._dismiss(front["id"], "closed")

    @rx.event
    def close_dialog(self, dialog_id: str):
        """The × of a dialog (in front or in the tray): close it."""
        return self._dismiss(dialog_id, "closed")

    @rx.event
    def minimize(self, dialog_id: str):
        """Send a keep-alive dialog to the tray; it keeps running."""
        dialog = self._find(dialog_id)
        if dialog is not None and dialog["keep_alive"] and self.active == dialog_id:
            self.active = ""

    @rx.event
    def show(self, dialog_id: str):
        """Bring a minimized dialog back to the front."""
        return self._show(dialog_id)

    @rx.event
    def show_action(self, action: str):
        """Bring the latest open dialog of ``action`` back to the front."""
        for dialog in reversed(self.dialogs):
            if dialog["action"] == action and dialog["kind"] == "frame":
                return self._show(dialog["id"])

    @rx.event
    def receive(self, raw: str):
        """Deliver one queued postMessage from a dialog's iframe."""
        try:
            message = json.loads(raw or "null")
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        dialog_id = message.get("id")
        dialog = self._find(dialog_id) if isinstance(dialog_id, str) else None
        meta = self._meta.get(dialog_id, {}) if dialog is not None else {}
        if dialog is None or dialog["kind"] != "frame" or message.get("origin") != meta.get("origin"):
            return

        event = message.get("event")
        data = message.get("data")
        if event == "cancel":
            details = data if isinstance(data, dict) else {}
            if dialog["keep_alive"] and details.get("via") == "escape":
                # Escape inside a keep-alive dialog minimizes it, like on the page.
                if self.active == dialog_id:
                    self.active = ""
                return
            return self._dismiss(dialog_id, "cancel", **details)
        elif event == "result":
            handler = _resolve_handler(meta.get("result_key", ""))
            self._remove(dialog_id)
            if handler is not None:
                return handler(data if isinstance(data, dict) else {"value": data})


class Intent:
    @staticmethod
    def start(
        app: str | None,
        action: str,
        on_result: EventHandler | None = None,
        on_cancel: EventHandler | None = None,
        private: dict[str, Any] | None = None,
        keep_alive: bool = False,
        single: bool = False,
        label: Any = "",
        **params: Any,
    ) -> EventSpec:
        """Event that opens ``action`` of ``app`` in an intent dialog.

        ``app=None`` uses whichever installed app provides ``action``.
        ``params`` (plain values or state Vars) go in the iframe URL;
        ``private`` ones are posted to the provider instead. ``on_result`` is
        an event handler taking one ``dict`` argument, called when the intent
        finishes; ``on_cancel`` (same signature) when it closes without a result.

        ``keep_alive`` keeps the dialog running, minimized, while other dialogs
        are in front; ``single`` closes any open dialog of the same action
        first; ``label`` names the dialog in the tray (default: the action).
        """
        result_key = format_event_handler(on_result) if on_result is not None else ""
        cancel_key = format_event_handler(on_cancel) if on_cancel is not None else ""
        options = {"keep_alive": keep_alive, "single": single, "label": label}
        return IntentState.open_intent(
            app or "", action, params, result_key, cancel_key, private or {}, options
        )

    @staticmethod
    def choose_app(
        action: str,
        on_result: EventHandler,
        on_cancel: EventHandler | None = None,
    ) -> EventSpec:
        """Event that finds the app to open ``action`` with, without opening it.

        When several installed apps provide ``action``, the chooser asks which
        one; ``on_result`` then gets ``{"app": <app>, "action": action}`` (right
        away when only one does). ``on_cancel`` gets the ``reason`` as for
        ``start``: ``closed`` or ``replaced`` (the chooser), or ``unavailable``.

        Start that app by name for everyone who must end up in the same app,
        e.g. the people joining one call: with ``Intent.start(None, ...)`` each
        of them would choose for themselves.
        """
        cancel_key = format_event_handler(on_cancel) if on_cancel is not None else ""
        return IntentState.choose_app(action, format_event_handler(on_result), cancel_key)

    @staticmethod
    def show(action: str) -> EventSpec:
        """Event that brings the open dialog of ``action`` back to the front."""
        return IntentState.show_action(action)


_ROUND_BUTTON = {
    "position": "absolute",
    "top": "8px",
    "width": "32px",
    "height": "32px",
    "border": "none",
    "borderRadius": "999px",
    "background": "rgba(255,255,255,0.85)",
    "color": "#374151",
    "fontSize": "22px",
    "lineHeight": "32px",
    "cursor": "pointer",
    "zIndex": "1",
}


def _dialog_controls(dialog: rx.Var) -> rx.Component:
    return rx.fragment(
        rx.cond(
            dialog["keep_alive"] != "",
            rx.el.button(
                "–",
                on_click=IntentState.minimize(dialog["id"]),
                aria_label="Minimize",
                title="Minimize",
                style={**_ROUND_BUTTON, "right": "46px"},
            ),
        ),
        rx.el.button(
            "×",
            on_click=IntentState.close_dialog(dialog["id"]),
            aria_label="Close",
            title="Close",
            style={**_ROUND_BUTTON, "right": "10px"},
        ),
    )


def _chooser() -> rx.Component:
    """Asks which app should open the action when several provide it."""
    return rx.el.div(
        rx.el.p("Open with", style={"fontWeight": "600", "margin": "0 0 4px"}),
        rx.el.p(IntentState.action, style={"margin": "0 0 16px", "color": "#6b7280", "fontSize": "14px"}),
        rx.foreach(
            IntentState.choices,
            lambda choice: rx.el.button(
                rx.el.span(choice["app"], style={"fontWeight": "600"}),
                rx.el.span(choice["url"], style={"color": "#6b7280", "fontSize": "13px"}),
                on_click=IntentState.choose(choice["app"]),
                style={
                    "display": "flex",
                    "flexDirection": "column",
                    "alignItems": "flex-start",
                    "width": "100%",
                    "padding": "10px 14px",
                    "marginBottom": "8px",
                    "border": "1px solid #e5e7eb",
                    "borderRadius": "10px",
                    "background": "#ffffff",
                    "cursor": "pointer",
                    "textAlign": "left",
                },
            ),
        ),
        style={"padding": "24px 56px 16px 24px", "fontFamily": "system-ui, sans-serif"},
    )


def _dialog(dialog: rx.Var, allow: str) -> rx.Component:
    in_front = IntentState.active == dialog["id"]
    return rx.el.div(
        rx.el.div(
            _dialog_controls(dialog),
            rx.cond(
                dialog["kind"] == "chooser",
                _chooser(),
                rx.el.iframe(
                    id=rx.cond(in_front, FRAME_ID, FRAME_ID + "-" + dialog["id"]),
                    src=dialog["src"],
                    title=dialog["label"],
                    allow=allow,
                    custom_attrs={"data-intent-id": dialog["id"], "data-intent-action": dialog["action"]},
                    style={
                        "display": "block",
                        "width": "100%",
                        "height": "480px",
                        "border": "none",
                        "transition": "height 120ms ease",
                    },
                ),
            ),
            on_click=rx.stop_propagation,
            style={
                "position": "relative",
                "width": "min(800px, 94vw)",
                "maxHeight": "90vh",
                "overflow": "auto",
                "background": "#ffffff",
                "borderRadius": "16px",
                "boxShadow": "0 24px 64px rgba(15, 23, 42, 0.35)",
            },
        ),
        key=dialog["id"],
        on_click=IntentState.close,
        custom_attrs={"data-intent-dialog": dialog["id"]},
        style={
            "position": "fixed",
            "inset": "0",
            "zIndex": "1000",
            "display": "flex",
            "alignItems": "center",
            "justifyContent": "center",
            "background": "rgba(15, 23, 42, 0.45)",
            # Minimized: hidden but still laid out, so its page keeps running.
            "visibility": rx.cond(in_front, "visible", "hidden"),
            "pointerEvents": rx.cond(in_front, "auto", "none"),
        },
    )


def _tray_item(dialog: rx.Var) -> rx.Component:
    return rx.el.div(
        rx.el.button(
            rx.el.span(
                style={
                    "width": "8px",
                    "height": "8px",
                    "borderRadius": "999px",
                    "background": "#16a34a",
                    "flexShrink": "0",
                },
            ),
            rx.el.span(dialog["label"]),
            on_click=IntentState.show(dialog["id"]),
            aria_label="Show " + dialog["label"],
            title="Show",
            style={
                "display": "flex",
                "alignItems": "center",
                "gap": "8px",
                "border": "none",
                "background": "none",
                "padding": "8px 4px 8px 14px",
                "fontSize": "14px",
                "fontWeight": "600",
                "color": "#111827",
                "cursor": "pointer",
            },
        ),
        rx.el.button(
            "×",
            on_click=IntentState.close_dialog(dialog["id"]),
            aria_label="Close " + dialog["label"],
            title="Close",
            style={
                "border": "none",
                "background": "none",
                "padding": "4px 12px 4px 6px",
                "fontSize": "20px",
                "color": "#6b7280",
                "cursor": "pointer",
            },
        ),
        key=dialog["id"],
        style={
            "display": "flex",
            "alignItems": "center",
            "background": "#ffffff",
            "border": "1px solid #e5e7eb",
            "borderRadius": "999px",
            "boxShadow": "0 8px 24px rgba(15, 23, 42, 0.18)",
            "fontFamily": "system-ui, sans-serif",
        },
    )


def _host(allow: str) -> rx.Component:
    return rx.fragment(
        # Hidden buttons the JS bridge clicks to hand events to Python.
        rx.el.button(
            id=INBOX_BUTTON_ID,
            on_click=IntentState.receive(
                rx.Var("JSON.stringify(window.__ddnsIntentInbox?.shift() ?? null)", _var_type=str)
            ),
            on_mount=IntentState.rearm,
            style={"display": "none"},
        ),
        rx.el.button(id=CLOSE_BUTTON_ID, on_click=IntentState.close, style={"display": "none"}),
        rx.foreach(IntentState.dialogs, lambda dialog: _dialog(dialog, allow)),
        # Minimized dialogs, above the dialog in front so they can be brought back.
        rx.el.div(
            rx.foreach(IntentState.minimized, _tray_item),
            aria_label="Running in background",
            style={
                "position": "fixed",
                "left": "16px",
                "bottom": "16px",
                "zIndex": "1001",
                "display": "flex",
                "flexDirection": "column",
                "alignItems": "flex-start",
                "gap": "8px",
            },
        ),
    )


# Set by install_intent_host(): the dialogs then live above every page.
_app_level = False
_app_allow = DEFAULT_ALLOW


def intent_host(allow: str = DEFAULT_ALLOW) -> rx.Component:
    """Dialog container for intents. Place once on every page that starts intents.

    ``allow`` is the iframe's Permissions Policy (features the provider may use).
    Renders nothing once ``install_intent_host(app)`` hosts the dialogs app-wide.
    """
    if _app_level:
        return rx.fragment()
    return _host(allow)


@rx.memo
def ddns_intent_host() -> rx.Component:
    return _host(_app_allow)


def install_intent_host(app: rx.App, allow: str = DEFAULT_ALLOW) -> None:
    """Host the intent dialogs once for the whole app, around every page.

    A dialog placed on a page dies with it: leaving the page (even to another
    page of the app) closes its iframe, so a call in the tray would drop. Hosted
    app-wide, dialogs keep running across page changes. Pages may keep their
    ``intent_host()``: it renders nothing afterwards. Links between pages must
    navigate in the browser (e.g. ``rx.link``), not reload the document.
    """
    global _app_level, _app_allow
    _app_level = True
    _app_allow = allow
    app.extra_app_wraps[(7, "DdnsIntentHost")] = lambda stateful: rx.fragment(ddns_intent_host())
