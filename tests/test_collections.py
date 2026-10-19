"""Tests for reflex_ddns_auth.collections (no Reflex, no network)."""

import json
import os
import time
from pathlib import Path

import httpx
import pytest

from reflex_ddns_auth.collections import (
    Collection,
    InvalidRef,
    ItemExists,
    ItemNotFound,
    PublishError,
    app_id_from_name,
    make_ref,
    parse_ref,
    resolve_app_id,
    resolve_root,
)
from reflex_ddns_auth.collections import paths
from reflex_ddns_auth.collections.manifest import MANIFEST_NAME, REPLICA_NAME, TMP_DIR


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "collections"
    monkeypatch.setenv("DDNS_COLLECTIONS_ROOT", str(r))
    monkeypatch.delenv("DDNS_APP_ID", raising=False)
    monkeypatch.delenv("DDNS_COLLECTIONS_NODE_URL", raising=False)
    monkeypatch.delenv("RE_DDNS_API_URL", raising=False)
    return r


# -- refs ------------------------------------------------------------------

def test_ref_roundtrip():
    r = parse_ref("dicom:dicom-data-explorer/1.2.840.113619")
    assert (r.collection, r.owner, r.id) == ("dicom", "dicom-data-explorer", "1.2.840.113619")
    assert str(r) == "dicom:dicom-data-explorer/1.2.840.113619"
    assert r.parts == ("by-app", "dicom-data-explorer", "1.2.840.113619")


@pytest.mark.parametrize("bad", [
    "dicom", "dicom:owner", "dicom/owner:id", "Dicom:owner/id", "dicom:owner/../etc",
    "dicom:owner/.tmp", "dicom:owner/.hidden", "dicom:by-app/x", "dicom:owner/a/b",
    "dicom:ow ner/id", "", "dicom:owner/" + "x" * 300,
])
def test_invalid_refs(bad):
    with pytest.raises(InvalidRef):
        parse_ref(bad)


def test_make_ref_allows_user_owner():
    assert str(make_ref("video", "_user", "clip-1")) == "video:_user/clip-1"


# -- resolution ------------------------------------------------------------

def test_root_resolution_order(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(paths, "CONFIG_PATH", cfg)
    monkeypatch.delenv("DDNS_COLLECTIONS_ROOT", raising=False)
    assert resolve_root() == paths.default_root()
    cfg.write_text(json.dumps({"collections_root": str(tmp_path / "from-config")}))
    assert resolve_root() == tmp_path / "from-config"
    monkeypatch.setenv("DDNS_COLLECTIONS_ROOT", str(tmp_path / "from-env"))
    assert resolve_root() == tmp_path / "from-env"
    assert resolve_root(tmp_path / "explicit") == tmp_path / "explicit"


def test_app_id_resolution(monkeypatch):
    monkeypatch.delenv("DDNS_APP_ID", raising=False)
    assert app_id_from_name("dicom_data_explorer") == "dicom-data-explorer"
    assert app_id_from_name("My App!!") == "my-app"
    assert app_id_from_name("") == ""
    assert resolve_app_id("explicit-app") == "explicit-app"
    assert resolve_app_id() == "_local"
    monkeypatch.setenv("DDNS_APP_ID", "from-env")
    assert resolve_app_id("explicit-app") == "from-env"   # the platform wins
    monkeypatch.setenv("DDNS_APP_ID", "Bad Name")
    with pytest.raises(InvalidRef):
        resolve_app_id()


# -- publish ---------------------------------------------------------------

def test_publish_single_file(root):
    video = Collection("video", app_id="youtube-downloader")
    assert video.own_dir == root / "video" / "by-app" / "youtube-downloader"
    assert not video.has("yt-abc")
    with video.publish("yt-abc", kind="video.file", title="A clip", source="https://x",
                       meta={"duration": 3}) as item:
        with item.open("clip.webm") as fh:
            fh.write(b"0123456789")
        # not visible while publishing
        assert video.items() == []
        assert item.path.parent.name == TMP_DIR
    assert video.has("yt-abc")
    assert item.ref == "video:youtube-downloader/yt-abc"
    final = video.own_dir / "yt-abc"
    manifest = json.loads((final / MANIFEST_NAME).read_text())
    assert manifest["kind"] == "video.file"
    assert manifest["owner"] == "youtube-downloader"
    assert manifest["primary"] == "clip.webm"
    assert manifest["files"] == [{"path": "clip.webm", "bytes": 10}]
    assert manifest["bytes"] == 10 and manifest["meta"] == {"duration": 3}
    assert list((video.own_dir / TMP_DIR).iterdir()) == []
    [it] = video.items()
    assert it.local and not it.replica and it.owner == "youtube-downloader"
    assert it.primary == final / "clip.webm" and it.primary.read_bytes() == b"0123456789"
    assert it.kind == "video.file" and it.title == "A clip" and it.bytes == 10


def test_part_file_is_renamed_on_close(root):
    c = Collection("video", app_id="a")
    with c.publish("x", kind="video.file") as item:
        fh = item.open("sub/dir/file.bin")
        fh.write(b"abc")
        assert (item.path / "sub" / "dir" / "file.bin.part").exists()
        fh.close()
        assert not (item.path / "sub" / "dir" / "file.bin.part").exists()
        assert (item.path / "sub" / "dir" / "file.bin").read_bytes() == b"abc"
    assert c.items()[0].files == [{"path": "sub/dir/file.bin", "bytes": 3}]


def test_publish_rejects_bad_paths_and_modes(root):
    c = Collection("video", app_id="a")
    with pytest.raises(PublishError):
        with c.publish("x", kind="video.file") as item:
            item.open("../escape.bin")
    with pytest.raises(PublishError):
        with c.publish("y", kind="video.file") as item:
            item.open("/abs.bin")
    with pytest.raises(PublishError):
        with c.publish("z", kind="video.file") as item:
            item.open("f.bin", "rb")
    with pytest.raises(PublishError):
        c.publish("k", kind="")


def test_publish_empty_item_fails(root):
    c = Collection("video", app_id="a")
    with pytest.raises(PublishError):
        with c.publish("empty", kind="video.file"):
            pass
    assert not c.has("empty")


def test_abort_keeps_tmp_then_resume(root):
    c = Collection("video", app_id="a")
    with pytest.raises(RuntimeError):
        with c.publish("big", kind="video.file") as item:
            with item.open("part1.bin") as fh:
                fh.write(b"1" * 5)
            raise RuntimeError("network died")
    assert not c.has("big")
    tmp_dirs = [p for p in (c.own_dir / TMP_DIR).iterdir()]
    assert len(tmp_dirs) == 1 and (tmp_dirs[0] / "part1.bin").exists()
    with c.publish("big", kind="video.file", resume=True) as item:
        assert item.path == tmp_dirs[0]
        assert (item.path / "part1.bin").exists()
        with item.open("part2.bin") as fh:
            fh.write(b"2" * 5)
    assert c.has("big")
    files = {f["path"] for f in c.items()[0].files}
    assert files == {"part1.bin", "part2.bin"}


def test_unfinished_part_blocks_publish(root):
    c = Collection("video", app_id="a")
    with pytest.raises(PublishError):
        with c.publish("p", kind="video.file") as item:
            (item.path / "stuck.bin.part").write_bytes(b"x")
    assert not c.has("p")


def test_duplicate_publish(root):
    c = Collection("video", app_id="a")
    with c.publish("dup", kind="video.file") as item:
        with item.open("f") as fh:
            fh.write(b"1")
    with pytest.raises(ItemExists):
        c.publish("dup", kind="video.file").__enter__()
    with c.publish("dup", kind="video.file", exist_ok=True) as item:
        with item.open("f") as fh:
            fh.write(b"2")
    assert (c.own_dir / "dup" / "f").read_bytes() == b"1"  # first publish wins
    assert len(c.items()) == 1


def test_concurrent_publish_same_id(root):
    c = Collection("video", app_id="a")
    first = c.publish("race", kind="video.file").__enter__()
    with first.open("f") as fh:
        fh.write(b"1")
    second = c.publish("race", kind="video.file").__enter__()  # both in .tmp, allowed
    with second.open("f") as fh:
        fh.write(b"2")
    first.__exit__(None, None, None)
    with pytest.raises(ItemExists):
        second.__exit__(None, None, None)
    assert (c.own_dir / "race" / "f").read_bytes() == b"1"
    assert list((c.own_dir / TMP_DIR).iterdir()) == []


def test_add_file_and_primary(root, tmp_path):
    src = tmp_path / "report.pdf"
    src.write_bytes(b"%PDF")
    c = Collection("documents", app_id="pdf-signature")
    with c.publish("r1", kind="pdf.signed", primary="report.pdf") as item:
        item.add_file(src)
        with item.open("thumb.png") as fh:
            fh.write(b"png")
    it = c.items()[0]
    assert it.primary.name == "report.pdf" and src.exists()
    with pytest.raises(PublishError):
        with c.publish("r2", kind="pdf.signed", primary="missing.pdf") as item:
            item.add_file(src, move=True)
    assert not src.exists()


def test_remove_own_item_only(root):
    a, b = Collection("video", app_id="a"), Collection("video", app_id="b")
    with a.publish("x", kind="video.file") as item:
        with item.open("f") as fh:
            fh.write(b"1")
    assert not b.remove("x")       # b has no item x of its own
    assert a.remove("x") and not a.has("x")


# -- reading ---------------------------------------------------------------

def _publish(c: Collection, item_id: str, kind="video.file", data=b"data"):
    with c.publish(item_id, kind=kind) as item:
        with item.open("f.bin") as fh:
            fh.write(data)


def test_union_view_ignores_noise(root):
    a, b = Collection("video", app_id="downloader"), Collection("video", app_id="converter")
    _publish(a, "one")
    time.sleep(1.1)  # created_at has second resolution; make ordering deterministic
    _publish(b, "two", kind="video.segment")
    by_app = root / "video" / "by-app"
    (by_app / "downloader" / "no-manifest").mkdir()                 # not an item
    (by_app / "downloader" / "no-manifest" / "f").write_bytes(b"x")
    (by_app / "Bad Owner").mkdir()                                   # invalid owner dir
    (by_app / "Bad Owner" / "it").mkdir()
    (by_app / "Bad Owner" / "it" / MANIFEST_NAME).write_text("{}")
    (by_app / "converter" / "link").symlink_to(by_app / "downloader" / "one")  # never followed
    (by_app / "converter" / "stray.txt").write_text("x")
    refs = [i.ref for i in a.items()]
    assert refs == ["video:converter/two", "video:downloader/one"]
    assert [i.ref for i in b.items()] == refs
    assert [i.ref for i in a.items(kind="video.segment")] == ["video:converter/two"]
    assert a.get("video:converter/two").owner == "converter"
    assert a.get("video:converter/nope") is None


def test_replica_marker_is_reported(root):
    c = Collection("video", app_id="a")
    _publish(c, "r")
    (c.own_dir / "r" / REPLICA_NAME).write_text(json.dumps({"home": "mac-b"}))
    it = c.items()[0]
    assert it.replica and it.home == "mac-b"


def test_resolve_local_and_missing(root):
    c = Collection("video", app_id="a")
    _publish(c, "x")
    assert c.resolve("video:a/x") == c.own_dir / "x"
    with pytest.raises(ItemNotFound):
        c.resolve("video:a/missing")
    with pytest.raises(InvalidRef):
        c.resolve("dicom:a/x")
    assert not c.node.available


def test_readonly_tree_lists_fine(root):
    assert Collection("video", app_id="a").items() == []
    assert not root.exists()


def test_gc_tmp(root):
    c = Collection("video", app_id="a")
    c.ensure_own_dirs()
    old = c.own_dir / TMP_DIR / "old.1.aaaa"
    old.mkdir()
    os.utime(old, (time.time() - 48 * 3600, time.time() - 48 * 3600))
    (c.own_dir / TMP_DIR / "new.1.bbbb").mkdir()
    assert c.gc_tmp(max_age_hours=36) == 1
    assert sorted(p.name for p in (c.own_dir / TMP_DIR).iterdir()) == ["new.1.bbbb"]


# -- node integration (mocked) --------------------------------------------

def _mock_node(root: Path, remote_entries: list[dict], fetched: list[str]):
    """A fake re-ddns node: lists remote items and 'fetches' one by creating it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/collections/video/items":
            assert request.url.params["scope"] == "remote"
            return httpx.Response(200, json={"items": remote_entries})
        if request.url.path == "/api/collections/video/fetch":
            ref = json.loads(request.content)["ref"]
            entry = next((e for e in remote_entries if e["ref"] == ref), None)
            if entry is None:
                return httpx.Response(404, json={"detail": "unknown on every host"})
            owner, item_id = ref.split(":", 1)[1].split("/", 1)
            d = root / "video" / "by-app" / owner / item_id
            d.mkdir(parents=True)
            (d / "f.bin").write_bytes(b"remote-bytes")
            (d / REPLICA_NAME).write_text(json.dumps({"home": entry["home"]}))
            (d / MANIFEST_NAME).write_text(json.dumps(entry["manifest"]))
            fetched.append(ref)
            return httpx.Response(200, json={"ok": True, "replica": True, "bytes": 12})
        if request.url.path == "/api/collections/video/fetch-status":
            return httpx.Response(200, json={"state": "fetching", "bytes_done": 6, "bytes_total": 12})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node")


def test_items_merge_remote_and_resolve_fetches(root):
    remote = [{
        "ref": "video:downloader/far", "kind": "video.file", "title": "Far away", "home": "mac-b",
        "local": False, "replica": False, "bytes": 12,
        "manifest": {"schema": 1, "kind": "video.file", "title": "Far away", "created_at": "2026-10-19T00:00:00+00:00",
                     "files": [{"path": "f.bin", "bytes": 12}], "bytes": 12, "primary": "f.bin"},
    }]
    fetched: list[str] = []
    http = _mock_node(root, remote, fetched)
    c = Collection("video", app_id="converter", node_url="http://node", http=http)
    assert c.node.available
    _publish(c, "near")
    items = {i.ref: i for i in c.items()}
    assert set(items) == {"video:converter/near", "video:downloader/far"}
    far = items["video:downloader/far"]
    assert not far.local and far.path is None and far.home == "mac-b"
    with pytest.raises(ItemNotFound):
        far.file("f.bin")
    assert c.get("video:downloader/far").title == "Far away"

    seen: list[tuple[int, int]] = []
    path = c.resolve("video:downloader/far", progress=lambda d, t: seen.append((d, t)))
    assert fetched == ["video:downloader/far"]
    assert path == root / "video" / "by-app" / "downloader" / "far"
    assert (path / "f.bin").read_bytes() == b"remote-bytes"
    assert seen[-1] == (12, 12)
    # now local: no second fetch, and the union no longer lists it as remote
    assert c.resolve("video:downloader/far") == path and fetched == ["video:downloader/far"]
    far = {i.ref: i for i in c.items()}["video:downloader/far"]
    assert far.local and far.replica and far.home == "mac-b"
    with pytest.raises(ItemNotFound):
        c.resolve("video:downloader/unknown")


def test_node_outage_degrades_to_local(root):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node")
    c = Collection("video", app_id="a", node_url="http://node", http=http)
    _publish(c, "x")
    assert [i.ref for i in c.items()] == ["video:a/x"]
    with pytest.raises(Exception):
        c.resolve("video:b/y")
