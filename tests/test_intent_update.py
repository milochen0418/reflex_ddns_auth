"""News from a provider while its dialog stays open (``update``), the ⧉ button, and the view messages."""

import json

import pytest

pytest.importorskip("reflex")

import reflex as rx  # noqa: E402
from reflex.utils.format import format_event_handler  # noqa: E402

from reflex_ddns_auth.intent.host import Intent, IntentState  # noqa: E402
from reflex_ddns_auth.intent.protocol import provider_bridge_js  # noqa: E402
from reflex_ddns_auth.intent.provider import intent_update  # noqa: E402
from reflex_ddns_auth.intent.tray import tray_js  # noqa: E402

ORIGIN = "https://finder.reflex-ddns.com"


class UpdateCaller(rx.State):
    @rx.event
    def got_news(self, data: dict):
        pass


def _host(keep_alive: bool = True, update_key: str | None = None) -> IntentState:
    """A host page with one open dialog of the app ``finder``."""
    host = IntentState(_reflex_internal_init=True)
    host._launch(
        "finder",
        {
            "action": "audio.find",
            "params": {},
            "result_key": "",
            "update_key": format_event_handler(UpdateCaller.got_news) if update_key is None else update_key,
            "cancel_key": "",
            "private": {},
            "keep_alive": keep_alive,
            "label": "",
        },
    )
    return host


def _receive(host: IntentState, event: str, data, origin: str = ORIGIN):
    raw = json.dumps({"id": host.dialogs[0]["id"], "event": event, "data": data, "origin": origin})
    return IntentState.receive.fn(host, raw)


def test_start_carries_the_update_handler():
    spec = Intent.start("finder", "audio.find", on_update=UpdateCaller.got_news, keep_alive=True)
    options = str(spec.args[-1][1])
    assert "update_key" in options and "got_news" in options


def test_update_reaches_on_update_and_keeps_the_dialog():
    host = _host()
    dialog_id = host.dialogs[0]["id"]
    event = _receive(host, "update", {"ready": "audio:finder/yt-abc"})
    assert event is not None and event.handler.fn.__name__ == "got_news"
    assert "audio:finder/yt-abc" in str(event.args)
    assert [d["id"] for d in host.dialogs] == [dialog_id]
    assert host.active == dialog_id


def test_update_from_another_origin_is_ignored():
    assert _receive(_host(), "update", {"x": 1}, origin="https://evil.example") is None


def test_update_without_handler_is_harmless():
    host = _host(update_key="")
    assert _receive(host, "update", {"x": 1}) is None
    assert host.dialogs


def test_result_still_closes_the_dialog():
    host = _host()
    _receive(host, "result", {"x": 1})
    assert host.dialogs == []


def test_pip_shrinks_a_keep_alive_dialog_only():
    host = _host(keep_alive=True)
    dialog_id = host.dialogs[0]["id"]
    script = IntentState.pip.fn(host, dialog_id)
    assert host.active == "" and host.dialogs
    assert f'pip("{dialog_id}")' in str(script)

    plain = _host(keep_alive=False)
    assert IntentState.pip.fn(plain, plain.dialogs[0]["id"]) is None
    assert plain.active == plain.dialogs[0]["id"]


def test_scripts_carry_the_view_protocol():
    tray = tray_js("show")
    assert "api.pip" in tray and "api.resendView" in tray and 'event: "view"' in tray
    assert "__MESSAGE__" not in tray and "__VERSION__" not in tray
    bridge = provider_bridge_js("https://host.reflex-ddns.com", "abc", "root", "params")
    assert "data-intent-view" in bridge
    assert '"update"' in str(intent_update({"a": 1}))
