"""Tests for reflex_ddns_auth.ai: finding the hub, the signed token, and reading a streamed
answer."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from reflex_ddns_auth import ai, tools

SECRET = "test-secret-long-enough-for-hs256-hmac-keys"


@pytest.fixture(autouse=True)
def platform(monkeypatch):
    monkeypatch.setenv("DDNS_AUTH_SECRET", SECRET)
    monkeypatch.setenv("SERVICE_SUBDOMAIN", "cellora")
    monkeypatch.delenv("DDNS_AI_HUB_URL", raising=False)
    monkeypatch.delenv("RE_DDNS_API_URL", raising=False)
    monkeypatch.setattr(ai, "_transport", None)
    ai._clear_cache()


def fake_hub(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def ndjson(*events) -> bytes:
    return "".join(json.dumps(e) + "\n" for e in events).encode()


# ------------------------------------------------------------ the token


def test_token_names_this_app_and_the_person() -> None:
    claims = ai.verify(f"Bearer {ai.token('milo@example.com')}")
    assert claims["app"] == "cellora" and claims["sub"] == "milo@example.com" and claims["aud"] == ai.AUDIENCE
    # Without a person, the app asks for itself.
    assert ai.verify(f"Bearer {ai.token()}")["sub"] == "cellora"


def test_other_tokens_are_refused() -> None:
    with pytest.raises(PermissionError):
        ai.verify("")
    with pytest.raises(PermissionError):
        ai.verify("Bearer not-a-token")
    # A tool-call token (another audience) doesn't open the hub.
    with pytest.raises(PermissionError):
        ai.verify(f"Bearer {tools.call_token('cellora', 'milo')}")


def test_without_the_platform_secret(monkeypatch) -> None:
    monkeypatch.delenv("DDNS_AUTH_SECRET")
    with pytest.raises(ai.HubUnavailable):
        ai.token("milo")
    with pytest.raises(PermissionError):
        ai.verify("Bearer x")


# ------------------------------------------------------------ finding the hub


def test_hub_url_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("DDNS_AI_HUB_URL", "http://localhost:8100/")
    assert asyncio.run(ai.hub_url()) == "http://localhost:8100"


def test_hub_url_from_the_service_list(monkeypatch) -> None:
    monkeypatch.setenv("RE_DDNS_API_URL", "http://re-ddns:8000")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert str(request.url) == "http://re-ddns:8000/api/service/list"
        return httpx.Response(200, json=[
            {"subdomain": "cellora", "upstream_host": "smart-app-cellora", "backend_port": 8000},
            {"subdomain": "ai", "upstream_host": "smart-app-ai", "backend_port": 8000},
        ])

    monkeypatch.setattr(ai, "_transport", fake_hub(handler))
    assert asyncio.run(ai.hub_url()) == "http://smart-app-ai:8000"
    assert asyncio.run(ai.hub_url()) == "http://smart-app-ai:8000"
    assert len(calls) == 1  # kept for a while


def test_no_hub_in_the_list(monkeypatch) -> None:
    monkeypatch.setenv("RE_DDNS_API_URL", "http://re-ddns:8000")
    monkeypatch.setattr(ai, "_transport", fake_hub(lambda request: httpx.Response(200, json=[{"subdomain": "cellora", "upstream_host": "x"}])))
    assert asyncio.run(ai.hub_url()) == ""
    assert asyncio.run(ai.status()) is None


def test_no_platform_at_all() -> None:
    assert asyncio.run(ai.hub_url()) == ""
    with pytest.raises(ai.HubUnavailable):
        asyncio.run(ai.complete("smart", "s", [{"role": "user", "content": "q"}]))


# ------------------------------------------------------------ asking


def test_stream_reads_the_answer(monkeypatch) -> None:
    monkeypatch.setenv("DDNS_AI_HUB_URL", "http://hub")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["claims"] = ai.verify(request.headers["authorization"])
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=ndjson({"model": "Ollama (qwen2.5-coder:7b)"}, {"text": "x = "}, {"text": "1"}, {"done": True, "stop": "end_turn"}))

    monkeypatch.setattr(ai, "_transport", fake_hub(handler))
    reply = ai.stream("coder", "Edit one cell.", [{"role": "user", "content": "write x"}], max_tokens=500, effort="low", user="milo")

    async def read():
        return [piece async for piece in reply]

    assert asyncio.run(read()) == ["x = ", "1"]
    assert reply.model == "Ollama (qwen2.5-coder:7b)" and reply.stop == "end_turn"
    assert seen["url"] == "http://hub/api/ai/stream"
    assert seen["claims"]["sub"] == "milo" and seen["claims"]["app"] == "cellora"
    assert seen["body"] == {
        "role": "coder",
        "system": "Edit one cell.",
        "messages": [{"role": "user", "content": "write x"}],
        "max_tokens": 500,
        "effort": "low",
    }


def test_complete_and_a_refusal(monkeypatch) -> None:
    monkeypatch.setenv("DDNS_AI_HUB_URL", "http://hub")
    answers = iter([
        ndjson({"model": "m"}, {"text": "fine"}, {"done": True, "stop": "end_turn"}),
        ndjson({"model": "m"}, {"done": True, "stop": "refusal"}),
    ])
    monkeypatch.setattr(ai, "_transport", fake_hub(lambda request: httpx.Response(200, content=next(answers))))
    assert asyncio.run(ai.complete("smart", "s", [{"role": "user", "content": "q"}])) == "fine"
    assert asyncio.run(ai.complete("smart", "s", [{"role": "user", "content": "q"}])) == ""


def test_errors_become_hub_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("DDNS_AI_HUB_URL", "http://hub")
    answers = iter([
        httpx.Response(403, json={"error": "this role is the owner's"}),
        httpx.Response(200, content=ndjson({"model": "m"}, {"text": "so far"}, {"error": "Ollama: model not found"})),
        httpx.Response(200, content=ndjson({"model": "m"}, {"text": "cut"})),
    ])
    monkeypatch.setattr(ai, "_transport", fake_hub(lambda request: next(answers)))

    async def read():
        out = []
        async for piece in ai.stream("smart", "s", [{"role": "user", "content": "q"}]):
            out.append(piece)
        return out

    with pytest.raises(ai.HubUnavailable, match="owner's"):
        asyncio.run(read())
    with pytest.raises(ai.HubUnavailable, match="model not found"):
        asyncio.run(read())
    with pytest.raises(ai.HubUnavailable, match="stopped before"):
        asyncio.run(read())


def test_status(monkeypatch) -> None:
    monkeypatch.setenv("DDNS_AI_HUB_URL", "http://hub")
    answers = iter([
        httpx.Response(200, json={"ok": True, "model": "Claude (claude-opus-5-5)", "provider": "anthropic", "local": False}),
        httpx.Response(404, json={"ok": False, "error": "no AI provider 'anthropic' is set up"}),
    ])

    subs = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["role"] == "coder"
        subs.append(ai.verify(request.headers["authorization"])["sub"])
        return next(answers)

    monkeypatch.setattr(ai, "_transport", fake_hub(handler))
    assert asyncio.run(ai.status("coder", user="milo@example.com"))["model"] == "Claude (claude-opus-5-5)"
    assert subs == ["milo@example.com"]
    assert asyncio.run(ai.status("coder")) == {"ok": False, "error": "no AI provider 'anthropic' is set up"}
