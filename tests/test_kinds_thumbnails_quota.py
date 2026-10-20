"""Kind naming, thumbnails and the node-side quota, as the publishing app sees them."""

import json

import httpx
import pytest

from reflex_ddns_auth.collections import (
    GENERIC_KIND,
    Collection,
    InvalidKind,
    PublishError,
    QuotaExceeded,
    check_kind,
    valid_kind,
)
from reflex_ddns_auth.collections.manifest import MANIFEST_NAME, META_DIR, TMP_DIR, image_type

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "collections"
    monkeypatch.setenv("DDNS_COLLECTIONS_ROOT", str(r))
    for name in ("DDNS_APP_ID", "DDNS_COLLECTIONS_NODE_URL", "RE_DDNS_API_URL"):
        monkeypatch.delenv(name, raising=False)
    return r


# -- kinds -----------------------------------------------------------------

@pytest.mark.parametrize("kind", ["dicom.series", "video.file", "image.slide-deck", "a.b.c", GENERIC_KIND, "x1.y2"])
def test_valid_kinds(kind):
    assert valid_kind(kind) and check_kind(kind) == kind


@pytest.mark.parametrize("kind", [
    "dicom", "Dicom.series", "dicom_series", "dicom.", ".series", "dicom..series", "dicom.-series",
    "dicom.series-", "dicom.se ries", "a." + "b" * 70, "", None, 3,
])
def test_invalid_kinds(kind):
    assert not valid_kind(kind)
    with pytest.raises(InvalidKind):
        check_kind(kind)


def test_publish_rejects_a_kind_outside_the_rule(root):
    c = Collection("video", app_id="a")
    with pytest.raises(InvalidKind):
        c.publish("x", kind="Video")
    with pytest.raises(PublishError):
        c.publish("x", kind="")


# -- thumbnails ------------------------------------------------------------

def test_image_type_by_signature():
    assert image_type(PNG) == "png" and image_type(JPEG) == "jpg"
    assert image_type(b"GIF89a....") == "gif" and image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert image_type(b"<svg xmlns=") is None and image_type(b"") is None


def test_thumbnail_is_bookkeeping_not_payload(root, tmp_path):
    c = Collection("video", app_id="a")
    with c.publish("clip", kind="video.file") as item:
        with item.open("clip.mp4") as fh:
            fh.write(b"video")
        item.set_thumbnail(JPEG)
        src = tmp_path / "t.png"
        src.write_bytes(PNG)
        item.set_thumbnail(src)            # replaces the JPEG
    final = c.own_dir / "clip"
    manifest = json.loads((final / META_DIR / MANIFEST_NAME).read_text())
    assert manifest["thumbnail"] == "thumbnail.png"
    assert manifest["files"] == [{"path": "clip.mp4", "bytes": 5}] and manifest["bytes"] == 5
    assert sorted(p.name for p in (final / META_DIR).iterdir()) == [MANIFEST_NAME, "thumbnail.png"]
    [it] = c.items()
    assert it.thumbnail == final / META_DIR / "thumbnail.png" and it.thumbnail.read_bytes() == PNG


def test_no_thumbnail_key_without_one(root):
    c = Collection("video", app_id="a")
    with c.publish("clip", kind="video.file") as item:
        with item.open("f") as fh:
            fh.write(b"1")
    [it] = c.items()
    assert "thumbnail" not in it.manifest and it.thumbnail is None


def test_bad_thumbnails_are_refused(root):
    c = Collection("video", app_id="a")
    with c.publish("clip", kind="video.file") as item:
        with item.open("f") as fh:
            fh.write(b"1")
        with pytest.raises(PublishError):
            item.set_thumbnail(b"<svg onload=alert(1)>")
        with pytest.raises(PublishError):
            item.set_thumbnail(PNG + b"\x00" * (600 * 1024))
    assert "thumbnail" not in c.items()[0].manifest


# -- quota -----------------------------------------------------------------

def _quota_node(limit: int, used: int, calls: list[str], status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/collections/video/quota/a" and status == 200:
            return httpx.Response(200, json={"collection": "video", "owner": "a", "limit": limit, "used": used})
        return httpx.Response(status if status != 200 else 404, json={"detail": "nope"})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node")


def _write(c: Collection, item_id: str, data: bytes) -> None:
    with c.publish(item_id, kind="video.file") as item:
        with item.open("f.bin") as fh:
            fh.write(data)


def test_quota_room_left(root):
    calls: list[str] = []
    c = Collection("video", app_id="a", node_url="http://node", http=_quota_node(100, 10, calls))
    _write(c, "ok", b"x" * 90)                     # 10 + 90 == 100: fits exactly
    assert c.has("ok") and calls == ["/api/collections/video/quota/a"] * 2   # at start and at the end


def test_quota_used_up_refuses_before_writing(root):
    c = Collection("video", app_id="a", node_url="http://node", http=_quota_node(100, 100, []))
    with pytest.raises(QuotaExceeded):
        c.publish("late", kind="video.file").__enter__()
    assert not (c.own_dir / TMP_DIR).exists() or list((c.own_dir / TMP_DIR).iterdir()) == []


def test_quota_too_big_keeps_staging_for_resume(root):
    c = Collection("video", app_id="a", node_url="http://node", http=_quota_node(100, 50, []))
    with pytest.raises(QuotaExceeded) as info:
        _write(c, "big", b"x" * 51)
    assert "this item needs" in str(info.value)
    assert not c.has("big")
    [staged] = list((c.own_dir / TMP_DIR).iterdir())
    assert staged.name.startswith("big.") and (staged / "f.bin").read_bytes() == b"x" * 51


def test_no_quota_when_node_is_old_down_or_unlimited(root):
    for http in (_quota_node(0, 999, []), _quota_node(1, 1, [], status=404), _quota_node(1, 1, [], status=500)):
        c = Collection("video", app_id="a", node_url="http://node", http=http)
        _write(c, f"x{id(http)}", b"data")
    down = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))),
                        base_url="http://node")
    c = Collection("video", app_id="a", node_url="http://node", http=down)
    _write(c, "offline", b"data")
    assert c.has("offline")
