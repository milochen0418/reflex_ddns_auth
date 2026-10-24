"""Tests for reflex_ddns_auth.tools: declaring tools, their manifest, and signed calls."""

from __future__ import annotations

import time
from typing import Literal

import jwt
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from reflex_ddns_auth import tools


class FakeApp:
    def __init__(self) -> None:
        self._api = Starlette()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DDNS_AUTH_SECRET", "test-secret-long-enough-for-hs256-hmac-keys")
    monkeypatch.setenv("SERVICE_SUBDOMAIN", "cad")
    tools._tools.clear()
    app = FakeApp()

    @tools.tool(app, "cad.set_code", kind="write", params={"room": "The room.", "code": "The code."})
    async def set_code(ctx, room: str, code: str, typing_speed: float = 0) -> dict:
        """Puts code in the room's editor."""
        return {"room": room, "length": len(code), "user": ctx.user, "speed": typing_speed}

    @tools.tool(app, "cad.snapshot")
    async def snapshot(ctx, room: str, view: Literal["iso", "top"] = "iso") -> dict:
        return tools.image(b"\x89PNG fake", "image/png")

    @tools.tool(app, "cad.fails", kind="danger")
    async def fails(ctx, items: list[int] | None = None) -> None:
        raise RuntimeError("boom")

    yield TestClient(app._api)
    tools._tools.clear()


def auth(app: str = "cad", user: str = "milo@example.com") -> dict:
    return {"Authorization": f"Bearer {tools.call_token(app, user)}"}


def test_manifest_describes_the_tools(client) -> None:
    m = client.get(tools.MANIFEST_PATH).json()

    assert m["v"] == 1 and m["app"] == "cad"
    by_name = {t["name"]: t for t in m["tools"]}
    set_code = by_name["cad.set_code"]
    assert set_code["kind"] == "write"
    assert set_code["description"] == "Puts code in the room's editor."
    schema = set_code["input_schema"]
    assert schema["required"] == ["room", "code"]
    assert schema["properties"]["room"] == {"type": "string", "description": "The room."}
    assert schema["properties"]["typing_speed"] == {"type": "number", "default": 0}
    view = by_name["cad.snapshot"]["input_schema"]["properties"]["view"]
    assert view == {"enum": ["iso", "top"], "type": "string", "default": "iso"}
    items = by_name["cad.fails"]["input_schema"]["properties"]["items"]
    assert items == {"type": "array", "items": {"type": "integer"}, "default": None}


def test_a_signed_call_runs_the_tool(client) -> None:
    r = client.post("/_ddns_tool/call/cad.set_code", json={"args": {"room": "r1", "code": "abc"}}, headers=auth())

    assert r.status_code == 200
    assert r.json() == {"ok": True, "result": {"room": "r1", "length": 3, "user": "milo@example.com", "speed": 0}}


def test_images_come_back_marked(client) -> None:
    r = client.post("/_ddns_tool/call/cad.snapshot", json={"args": {"room": "r1"}}, headers=auth())

    result = r.json()["result"]
    assert result[tools.IMAGE_KEY] is True and result["mime"] == "image/png"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer nonsense"},
        {"Authorization": "Basic abc"},
    ],
)
def test_calls_without_a_valid_token_are_refused(client, headers) -> None:
    r = client.post("/_ddns_tool/call/cad.set_code", json={"args": {"room": "r", "code": "c"}}, headers=headers)
    assert r.status_code == 401 and r.json()["ok"] is False


def test_a_token_for_another_app_is_refused(client) -> None:
    r = client.post("/_ddns_tool/call/cad.set_code", json={"args": {"room": "r", "code": "c"}}, headers=auth("grid"))
    assert r.status_code == 401


def test_an_expired_token_is_refused(client) -> None:
    old = jwt.encode(
        {"aud": tools.AUDIENCE, "app": "cad", "sub": "x", "exp": int(time.time()) - 10},
        "test-secret-long-enough-for-hs256-hmac-keys",
        algorithm="HS256",
    )
    r = client.post(
        "/_ddns_tool/call/cad.set_code",
        json={"args": {"room": "r", "code": "c"}},
        headers={"Authorization": f"Bearer {old}"},
    )
    assert r.status_code == 401


@pytest.mark.parametrize(
    "args",
    [
        {"room": "r"},  # missing code
        {"room": "r", "code": 3},  # wrong type
        {"room": "r", "code": "c", "color": "red"},  # unknown
        {"room": "r", "code": "c", "typing_speed": True},  # bool is not a number
    ],
)
def test_bad_arguments_are_refused(client, args) -> None:
    r = client.post("/_ddns_tool/call/cad.set_code", json={"args": args}, headers=auth())
    assert r.status_code == 400 and r.json()["ok"] is False


def test_a_failing_tool_reports_its_error(client) -> None:
    r = client.post("/_ddns_tool/call/cad.fails", json={"args": {"items": [1, 2]}}, headers=auth())
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "RuntimeError: boom"}


def test_an_unknown_tool_is_404(client) -> None:
    r = client.post("/_ddns_tool/call/cad.nope", json={"args": {}}, headers=auth())
    assert r.status_code == 404


def test_without_the_platform_secret_nothing_runs(client, monkeypatch) -> None:
    headers = auth()
    monkeypatch.delenv("DDNS_AUTH_SECRET")
    r = client.post("/_ddns_tool/call/cad.set_code", json={"args": {"room": "r", "code": "c"}}, headers=headers)
    assert r.status_code == 401


def test_declaring_bad_tools_fails_early() -> None:
    app = FakeApp()
    with pytest.raises(TypeError):

        @tools.tool(app, "x.sync")
        def not_async(ctx, a: str) -> None: ...

    with pytest.raises(ValueError):

        @tools.tool(app, "x.kind", kind="maybe")
        async def bad_kind(ctx) -> None: ...

    with pytest.raises(ValueError):

        @tools.tool(app, "x.params", params={"nope": "?"})
        async def bad_params(ctx, a: str) -> None: ...

    with pytest.raises(TypeError):

        @tools.tool(app, "x.type")
        async def bad_type(ctx, a: set) -> None: ...
    tools._tools.clear()
