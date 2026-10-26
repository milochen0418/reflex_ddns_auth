"""What the host page shows now, for a dialog: the ``want``/``context`` messages, ``Intent.context``,
``on_context`` and the companion button."""

import json

import pytest

pytest.importorskip("reflex")

import reflex as rx  # noqa: E402

from reflex_ddns_auth import companion  # noqa: E402
from reflex_ddns_auth.intent.host import Intent  # noqa: E402
from reflex_ddns_auth.intent.protocol import host_bridge_js, provider_bridge_js  # noqa: E402
from reflex_ddns_auth.intent import provider  # noqa: E402
from reflex_ddns_auth.intent.provider import IntentPage  # noqa: E402

ORIGIN = "https://cad-roam.reflex-ddns.com"


class Talker(rx.State):
    @rx.event
    def got_context(self, data):
        pass


def _page(action: str = "ai.talk", call_id: str = "abc") -> IntentPage:
    page = IntentPage(_reflex_internal_init=True)
    page.action = action
    page.call_id = call_id
    return page


def test_host_bridge_answers_want_and_pushes_context():
    js = host_bridge_js("inbox", "close")
    assert 'm.event === "want"' in js and "window.__ddnsContext" in js
    assert "window.__ddnsIntentHostApi" in js and 'event: "context"' in js
    # Answers go to the dialog's own origin only.
    assert "new URL(frame.src, location.href).origin" in js


def test_provider_bridge_asks_and_keeps_the_answer():
    js = provider_bridge_js(ORIGIN, "abc", "root", "params")
    assert 'post("want", {what: "context"})' in js
    assert "window.__ddnsIntentPage = {post, ask, context: null}" in js
    assert "ddns-intent-context" in js
    # Without on_context, answers stay in the browser; with it they go to Python too.
    assert "if (false) toPython();" in js
    assert "if (true) toPython();" in provider_bridge_js(ORIGIN, "abc", "root", "params", python_context=True)


def test_intent_context_event_names_the_action_and_data():
    spec = Intent.context("ai.talk", {"text": "at the door"})
    script = str(spec.args)
    assert "__ddnsIntentHostApi.context" in script and '"ai.talk"' in script and "at the door" in script


def test_on_context_gets_the_answer(monkeypatch):
    monkeypatch.setitem(provider._actions, "ai.talk", {"on_open": None, "params": None, "on_context": Talker.got_context})
    page = _page()
    events = IntentPage.deliver.fn(page, json.dumps({"event": "context", "data": {"text": "inside the chapel"}}))
    assert events and events[0].handler.fn.__name__ == "got_context"
    assert "inside the chapel" in str(events[0].args)


def test_context_without_handler_or_caller_is_ignored(monkeypatch):
    monkeypatch.setitem(provider._actions, "ai.talk", {"on_open": None, "params": None, "on_context": None})
    assert IntentPage.deliver.fn(_page(), json.dumps({"event": "context", "data": 1})) is None
    monkeypatch.setitem(provider._actions, "ai.talk", {"on_open": None, "params": None, "on_context": Talker.got_context})
    assert IntentPage.deliver.fn(_page(call_id=""), json.dumps({"event": "context", "data": 1})) is None


def test_params_still_open_the_page(monkeypatch):
    opened = []
    monkeypatch.setitem(provider._actions, "ai.talk", {"on_open": lambda p: opened.append(p) or Talker.got_context(p), "params": None, "on_context": None})
    page = _page()
    page._waiting = True
    IntentPage.deliver.fn(page, json.dumps({"event": "params", "data": {"room": "r1"}}))
    assert opened == [{"room": "r1"}] and page._waiting is False
    # The old bridge's bare dict still works.
    page._waiting = True
    IntentPage.deliver.fn(page, json.dumps({"room": "r2"}))
    assert opened[-1] == {"room": "r2"}


def test_ai_button_summons_the_talk_intent():
    button = companion.ai_button(room="sagrada", lang="zh-TW", floating=False, class_name="x")
    rendered = str(button.render())
    assert companion.BUTTON_ID in rendered and "ai.talk" in rendered
    assert "x" in rendered and "fixed" not in rendered
    assert "fixed" in str(companion.ai_button().render())


def test_hotkey_script_ends_a_pointer_lock_and_skips_text_fields():
    assert "exitPointerLock" in companion._HOTKEY_JS
    assert "textarea" in companion._HOTKEY_JS and 'e.code === "KeyA"' in companion._HOTKEY_JS
