"""Intent actions that open collection items: ``accepts`` in the manifest, and ``resolve_openers``."""

import asyncio

import httpx
import pytest

pytest.importorskip("reflex")

from reflex_ddns_auth.collections import InvalidKind  # noqa: E402
from reflex_ddns_auth.intent import provider, registry  # noqa: E402
from reflex_ddns_auth.intent.protocol import intent_route  # noqa: E402
from reflex_ddns_auth.intent.provider import intent, manifest  # noqa: E402


class _Api:
    def __init__(self):
        self.routes = []

    def add_route(self, path, endpoint, methods=None):
        self.routes.append(type("R", (), {"path": path})())


class _App:
    """Just enough of rx.App for ``intent``: pages and the backend's routes."""

    def __init__(self):
        self._api = _Api()
        self.pages = []

    def add_page(self, component, route, title, on_load):
        self.pages.append(route)


@pytest.fixture(autouse=True)
def clean_actions():
    saved = dict(provider._actions)
    provider._actions.clear()
    registry._openers_cache.clear()
    yield
    provider._actions.clear()
    provider._actions.update(saved)


def _page():
    return None


def test_accepts_declares_the_ref_param_and_shows_in_the_manifest():
    app = _App()
    intent(app, action="dicom.open-series", title="Open series", accepts=("dicom.series",))(_page)
    action = manifest()["actions"]["dicom.open-series"]
    assert action["accepts"] == ["dicom.series"]
    assert action["params"] == {"ref": {"type": "str", "required": True}}
    assert app.pages == [intent_route("dicom.open-series")]
    assert provider._check_params("dicom.open-series", {}) == ({}, "Missing required parameter: ref")
    assert provider._check_params("dicom.open-series", {"ref": "dicom:x/y"})[0] == {"ref": "dicom:x/y"}


def test_accepts_with_own_params_requires_ref():
    app = _App()
    intent(app, action="video.trim", params={"ref": str, "start": float}, accepts=["video.file"])(_page)
    params = manifest()["actions"]["video.trim"]["params"]
    assert params == {"ref": {"type": "str", "required": True}, "start": {"type": "float", "required": False}}
    with pytest.raises(ValueError):
        intent(_App(), action="video.cut", params={"start": float}, accepts=["video.file"])
    with pytest.raises(ValueError):
        intent(_App(), action="video.cut", params={"ref": int}, accepts=["video.file"])
    with pytest.raises(InvalidKind):
        intent(_App(), action="video.cut", accepts=["Video"])


def test_actions_without_accepts_keep_their_manifest_shape():
    intent(_App(), action="call.join", params={"room": str}, required=("room",))(_page)
    assert manifest()["actions"]["call.join"] == {
        "title": "call.join", "route": intent_route("call.join"),
        "params": {"room": {"type": "str", "required": True}},
    }


def test_resolve_openers(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"kind": "dicom.series", "openers": [
            {"app": "dicom-viewer", "action": "dicom.open-series", "title": "Open series", "route": "/intent/x"},
            {"app": "", "action": "broken"},
        ]})

    real = httpx.AsyncClient
    monkeypatch.setattr(registry.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("RE_DDNS_API_URL", "http://re-ddns:8000")
    monkeypatch.delenv("DDNS_INTENT_REGISTRY_URL", raising=False)
    got = asyncio.run(registry.resolve_openers("dicom.series"))
    assert got == [{"app": "dicom-viewer", "action": "dicom.open-series", "title": "Open series"}]
    assert seen == ["http://re-ddns:8000/api/intent/openers?kind=dicom.series"]
    assert asyncio.run(registry.resolve_openers("dicom.series")) == got and len(seen) == 1   # cached
    monkeypatch.delenv("RE_DDNS_API_URL")
    registry._openers_cache.clear()
    assert asyncio.run(registry.resolve_openers("dicom.series")) == []                     # no registry
