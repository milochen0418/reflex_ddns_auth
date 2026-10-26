"""A button that calls up the AI Hub's talking AI in any app, with one line.

    from reflex_ddns_auth.companion import ai_button, install_ai_button

    install_ai_button(app, room=MyState.room_id, lang=MyState.lang)   # every page
    ai_button(room=..., floating=False, class_name="...")               # or where you want it

The AI (the hub's ``ai.talk`` intent) opens as a keep-alive dialog over the page: a face that
listens, speaks and keeps its expression, and that knows what the page shows when the page says
so with ``window.__ddnsContext`` (a value or a function, maybe async: see
``reflex_ddns_auth.intent.protocol``). Opened again, the dialog comes back to the front with its
conversation. ⌥A / Alt+A calls it up from anywhere on the page (not from a text field), ending a
pointer lock first, so a 3D view's mouse comes back for the dialog.

The dialog gets ``in_app`` (this app's subdomain, so the AI has this app's tools and guide),
``room`` (what the app calls the place the person is in, for tools that take one) and ``lang``
(the page's language, e.g. ``zh-TW``, for the voice), plus any ``params`` given.
"""

from __future__ import annotations

import json
import os
from typing import Any

import reflex as rx
from reflex.event import EventSpec

from reflex_ddns_auth.intent.host import IntentState

ACTION = "ai.talk"
BUTTON_ID = "ddns-ai-button"
HOTKEY = "Alt+A"

_FLOATING = (
    "fixed bottom-5 right-5 z-[999] h-12 w-12 rounded-full flex items-center justify-center "
    "bg-slate-900 text-white shadow-xl border border-white/20 hover:bg-slate-700"
)

_HOTKEY_JS = """(() => {
  if (window.__ddnsAiHotkey) return;
  window.__ddnsAiHotkey = true;
  window.addEventListener("keydown", (e) => {
    if (!(e.altKey && !e.ctrlKey && !e.metaKey && e.code === "KeyA")) return;
    const t = e.target;
    if (t && t.closest && t.closest("input, textarea, select, [contenteditable=true]")) return;
    const b = document.getElementById(__BUTTON_ID__);
    if (!b) return;
    e.preventDefault();
    if (document.pointerLockElement && document.exitPointerLock) document.exitPointerLock();
    b.click();
  });
})()""".replace("__BUTTON_ID__", json.dumps(BUTTON_ID))


def own_app() -> str:
    """This app's subdomain on the platform ("" outside it)."""
    return (os.environ.get("SERVICE_SUBDOMAIN") or os.environ.get("DDNS_APP_ID") or "").strip().lower()


class CompanionState(rx.State):
    """Calls the AI up: a new dialog, or the open one back to the front."""

    @rx.event
    async def summon(self, room: str, lang: str, label: str, params: dict):
        host = await self.get_state(IntentState)
        if ACTION in host.open_actions:
            return IntentState.show_action(ACTION)
        query = {"in_app": own_app(), "room": room or "", "lang": lang or "", **(params or {})}
        return IntentState.open_intent("", ACTION, query, "", "", {}, {"keep_alive": True, "single": True, "label": label or "AI"})


def summon(room: Any = "", lang: Any = "", label: Any = "AI", **params: Any) -> EventSpec:
    """The event that calls the AI up (``ai_button`` does it on a click)."""
    return CompanionState.summon(room, lang, label, params)


def ai_button(
    room: Any = "",
    lang: Any = "",
    label: Any = "AI",
    title: str = f"Talk with the AI ({HOTKEY})",
    floating: bool = True,
    class_name: str = "",
    hotkey: bool = True,
    children: tuple[rx.Component, ...] = (),
    **params: Any,
) -> rx.Component:
    """The button (a face). ``floating`` puts it at the bottom right of the page; else give it
    ``class_name`` and ``children`` (its icon and words) for the place it goes in."""
    return rx.el.button(
        *(children or (rx.icon("bot", class_name="h-5 w-5"),)),
        on_click=summon(room, lang, label, **params),
        on_mount=rx.call_script(_HOTKEY_JS) if hotkey else None,
        id=BUTTON_ID,
        type="button",
        title=title,
        aria_label="AI",
        custom_attrs={"data-ddns-ai": ACTION},
        class_name=(_FLOATING if floating else "") + (" " + class_name if class_name else ""),
    )


@rx.memo
def ddns_ai_button(room: rx.Var[str], lang: rx.Var[str], label: rx.Var[str]) -> rx.Component:
    return ai_button(room=room, lang=lang, label=label)


def install_ai_button(app: rx.App, room: Any = "", lang: Any = "", label: Any = "AI") -> None:
    """The floating button on every page of the app (dialogs are hosted app-wide too)."""
    app.extra_app_wraps[(6, "DdnsAiButton")] = lambda stateful: rx.fragment(ddns_ai_button(room=room, lang=lang, label=label))
