"""Host side: open another app's intent in a dialog and receive its result.

    from reflex_ddns_auth.intent import Intent, intent_host

    rx.button("Profile", on_click=Intent.start("relack", "profile.view", user=email))
    rx.button("Pick", on_click=Intent.start("relack", "user.pick", on_result=MyState.on_pick))
    rx.button("Call", on_click=Intent.start("livekit", "call.join", on_cancel=MyState.on_left, room=rid))

    intent_host()  # once per page

``on_result`` runs when the provider finishes with data; ``on_cancel`` runs when
the dialog closes without a result (provider cancel, the × button, a click
outside, Escape, or another intent replacing it).
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
    app_url,
    host_bridge_js,
    intent_route,
    origin_of,
)

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


class IntentState(rx.State):
    """The intent dialog currently open on this page (at most one)."""

    is_open: bool = False
    src: str = ""
    _call_id: str = ""
    _expected_origin: str = ""
    _result_key: str = ""
    _cancel_key: str = ""

    def _close(self):
        self.is_open = False
        self.src = ""
        self._call_id = ""
        self._expected_origin = ""
        self._result_key = ""
        self._cancel_key = ""

    def _dismiss(self):
        """Close without a result; returns the caller's ``on_cancel`` event, if any."""
        handler = _resolve_handler(self._cancel_key) if self.is_open else None
        self._close()
        return handler({}) if handler is not None else None

    @rx.event
    def open_intent(
        self, app: str, action: str, params: dict, result_key: str = "", cancel_key: str = ""
    ):
        # Only one dialog at a time: a replaced dialog counts as cancelled.
        dismissed = self._dismiss()
        base = app_url(app)
        call_id = secrets.token_hex(8)
        query = {k: "" if v is None else str(v) for k, v in params.items()}
        query[ID_PARAM] = call_id
        query[ORIGIN_PARAM] = self.router.url.origin or ""

        self.src = f"{base}{intent_route(action)}?{urlencode(query)}"
        self._call_id = call_id
        self._expected_origin = origin_of(base)
        self._result_key = result_key
        self._cancel_key = cancel_key
        self.is_open = True
        bridge = rx.call_script(host_bridge_js(INBOX_BUTTON_ID, CLOSE_BUTTON_ID, FRAME_ID))
        return [dismissed, bridge] if dismissed is not None else bridge

    @rx.event
    def close(self):
        return self._dismiss()

    @rx.event
    def receive(self, raw: str):
        """Deliver one queued postMessage from the iframe."""
        try:
            message = json.loads(raw or "null")
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        if message.get("id") != self._call_id or message.get("origin") != self._expected_origin:
            return

        event = message.get("event")
        if event == "cancel":
            return self._dismiss()
        elif event == "result":
            handler = _resolve_handler(self._result_key)
            self._close()
            if handler is not None:
                data = message.get("data")
                return handler(data if isinstance(data, dict) else {"value": data})


class Intent:
    @staticmethod
    def start(
        app: str,
        action: str,
        on_result: EventHandler | None = None,
        on_cancel: EventHandler | None = None,
        **params: Any,
    ) -> EventSpec:
        """Event that opens ``action`` of ``app`` in the intent dialog.

        ``params`` may be plain values or state Vars. ``on_result`` is an event
        handler taking one ``dict`` argument, called when the intent finishes;
        ``on_cancel`` (same signature, called with ``{}``) when it closes without
        a result.
        """
        result_key = format_event_handler(on_result) if on_result is not None else ""
        cancel_key = format_event_handler(on_cancel) if on_cancel is not None else ""
        return IntentState.open_intent(app, action, params, result_key, cancel_key)


def _close_button() -> rx.Component:
    return rx.el.button(
        "×",
        on_click=IntentState.close,
        aria_label="Close",
        style={
            "position": "absolute",
            "top": "8px",
            "right": "10px",
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
        },
    )


def intent_host(allow: str = DEFAULT_ALLOW) -> rx.Component:
    """Dialog container for intents. Place once on every page that starts intents.

    ``allow`` is the iframe's Permissions Policy (features the provider may use).
    """
    return rx.fragment(
        # Hidden buttons the JS bridge clicks to hand events to Python.
        rx.el.button(
            id=INBOX_BUTTON_ID,
            on_click=IntentState.receive(
                rx.Var("JSON.stringify(window.__ddnsIntentInbox?.shift() ?? null)", _var_type=str)
            ),
            style={"display": "none"},
        ),
        rx.el.button(id=CLOSE_BUTTON_ID, on_click=IntentState.close, style={"display": "none"}),
        rx.cond(
            IntentState.is_open,
            rx.el.div(
                rx.el.div(
                    _close_button(),
                    rx.el.iframe(
                        id=FRAME_ID,
                        src=IntentState.src,
                        title="Dialog",
                        allow=allow,
                        style={
                            "display": "block",
                            "width": "100%",
                            "height": "480px",
                            "border": "none",
                            "transition": "height 120ms ease",
                        },
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
                on_click=IntentState.close,
                style={
                    "position": "fixed",
                    "inset": "0",
                    "zIndex": "1000",
                    "display": "flex",
                    "alignItems": "center",
                    "justifyContent": "center",
                    "background": "rgba(15, 23, 42, 0.45)",
                },
            ),
        ),
    )
