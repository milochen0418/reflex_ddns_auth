"""Revising an item in place: the owner keeps editing one item (e.g. a workbook saved as people type)."""

import json
import os

import httpx
import pytest

from reflex_ddns_auth.collections import Collection, ItemNotFound, PublishError, QuotaExceeded
from reflex_ddns_auth.collections.manifest import MANIFEST_NAME, META_DIR, REPLICA_NAME, TMP_DIR


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "collections"
    monkeypatch.setenv("DDNS_COLLECTIONS_ROOT", str(r))
    for name in ("DDNS_APP_ID", "DDNS_COLLECTIONS_NODE_URL", "RE_DDNS_API_URL"):
        monkeypatch.delenv(name, raising=False)
    return r


def _book(c: Collection, item_id="b1", data=b"v1") -> None:
    with c.publish(item_id, kind="spreadsheet.workbook", title="Plan", meta={"sheets": ["Sheet1"]}) as item:
        with item.open("book.xlsx") as fh:
            fh.write(data)


def _manifest(c: Collection, item_id="b1") -> dict:
    return json.loads((c.own_dir / item_id / META_DIR / MANIFEST_NAME).read_text())


def test_revise_replaces_the_file_and_the_record(root):
    c = Collection("spreadsheets", app_id="grid")
    _book(c)
    [before] = c.items()
    assert before.revision == 1 and before.updated_at == before.created_at
    with c.revise("b1", title="Plan v2", meta={"sheets": ["Sheet1", "Data"]}) as rev:
        with rev.open("book.xlsx") as fh:
            fh.write(b"version two")
        # nothing changes until the block ends
        assert (c.own_dir / "b1" / "book.xlsx").read_bytes() == b"v1"
        assert rev.path.parent.name == TMP_DIR
    assert (c.own_dir / "b1" / "book.xlsx").read_bytes() == b"version two"
    m = _manifest(c)
    assert m["files"] == [{"path": "book.xlsx", "bytes": 11}] and m["bytes"] == 11
    assert m["title"] == "Plan v2" and m["meta"] == {"sheets": ["Sheet1", "Data"]}
    assert m["revision"] == 2 and m["updated_at"] and m["created_at"] == before.created_at
    assert m["primary"] == "book.xlsx" and m["kind"] == "spreadsheet.workbook" and m["owner"] == "grid"
    [after] = c.items()
    assert after.revision == 2 and after.title == "Plan v2" and after.bytes == 11
    assert list((c.own_dir / TMP_DIR).iterdir()) == [], "the staging is gone"
    assert sorted(p.name for p in (c.own_dir / "b1").iterdir()) == [META_DIR, "book.xlsx"]


def test_revise_keeps_what_it_is_not_given(root):
    c = Collection("spreadsheets", app_id="grid")
    _book(c)
    with c.revise("b1"):
        pass                                        # a revision of the record only
    m = _manifest(c)
    assert m["title"] == "Plan" and m["meta"] == {"sheets": ["Sheet1"]} and m["revision"] == 2
    assert (c.own_dir / "b1" / "book.xlsx").read_bytes() == b"v1"


def test_revise_swaps_one_file_for_another(root):
    """A workbook that gains macros becomes book.xlsm: the new file in, the old one out."""
    c = Collection("spreadsheets", app_id="grid")
    _book(c)
    with c.revise("b1", primary="book.xlsm") as rev:
        with rev.open("book.xlsm") as fh:
            fh.write(b"macros")
        rev.remove("book.xlsx")
        rev.remove("never-there.txt")               # not an error
    assert sorted(p.name for p in (c.own_dir / "b1").iterdir()) == [META_DIR, "book.xlsm"]
    m = _manifest(c)
    assert m["primary"] == "book.xlsm" and m["files"] == [{"path": "book.xlsm", "bytes": 6}]
    # when the primary goes and one file is left, that file is the primary
    with c.revise("b1") as rev:
        with rev.open("book.xlsx") as fh:
            fh.write(b"plain")
        rev.remove("book.xlsm")
    assert _manifest(c)["primary"] == "book.xlsx"


def test_a_failed_revision_changes_nothing(root):
    c = Collection("spreadsheets", app_id="grid")
    _book(c)
    with pytest.raises(RuntimeError):
        with c.revise("b1", title="never") as rev:
            with rev.open("book.xlsx") as fh:
                fh.write(b"half")
            raise RuntimeError("boom")
    assert (c.own_dir / "b1" / "book.xlsx").read_bytes() == b"v1"
    assert _manifest(c)["title"] == "Plan" and "revision" not in _manifest(c)
    assert list((c.own_dir / TMP_DIR).iterdir()) == []
    with pytest.raises(PublishError):
        with c.revise("b1", primary="missing.xlsx"):
            pass
    with pytest.raises(PublishError):
        with c.revise("b1") as rev:
            rev.remove("book.xlsx")                 # an item keeps at least one file
    assert (c.own_dir / "b1" / "book.xlsx").read_bytes() == b"v1"


def test_only_own_published_items_can_be_revised(root):
    grid, other = Collection("spreadsheets", app_id="grid"), Collection("spreadsheets", app_id="other")
    _book(grid)
    with pytest.raises(ItemNotFound):
        other.revise("b1").__enter__()              # other has no item b1 of its own
    with pytest.raises(ItemNotFound):
        grid.revise("nope").__enter__()
    (grid.own_dir / "b1" / META_DIR / REPLICA_NAME).write_text(json.dumps({"home": "mac-b"}))
    with pytest.raises(PublishError, match="copy from mac-b"):
        grid.revise("b1").__enter__()


def test_an_item_deleted_meanwhile_is_not_brought_back(root):
    """The storage page deletes the item while the app is saving: the save fails, nothing reappears."""
    c = Collection("spreadsheets", app_id="grid")
    _book(c)
    with pytest.raises(ItemNotFound):
        with c.revise("b1") as rev:
            with rev.open("book.xlsx") as fh:
                fh.write(b"late")
            c.remove("b1")
    assert not (c.own_dir / "b1").exists() and not c.has("b1")


def test_revision_touches_the_owner_directory(root):
    """The node's index re-reads an owner when the owner directory's mtime moves."""
    c = Collection("spreadsheets", app_id="grid")
    _book(c)
    os.utime(c.own_dir, (1_000_000, 1_000_000))
    with c.revise("b1") as rev:
        with rev.open("book.xlsx") as fh:
            fh.write(b"v2")
    assert c.own_dir.stat().st_mtime > 1_000_000


def _quota_node(limit: int, used: int, calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"collection": "spreadsheets", "owner": "grid", "limit": limit, "used": used})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node")


def test_growing_revisions_respect_the_quota(root):
    calls: list[str] = []
    c = Collection("spreadsheets", app_id="grid", node_url="http://node", http=_quota_node(100, 0, calls))
    _book(c, data=b"x" * 10)
    calls.clear()
    c2 = Collection("spreadsheets", app_id="grid", node_url="http://node", http=_quota_node(100, 10, calls))
    with c2.revise("b1") as rev:                    # 10 -> 5 bytes: smaller, no question asked
        with rev.open("book.xlsx") as fh:
            fh.write(b"x" * 5)
    assert calls == []
    c3 = Collection("spreadsheets", app_id="grid", node_url="http://node", http=_quota_node(100, 5, calls))
    with c3.revise("b1") as rev:                    # 5 + 95 == 100: fits exactly
        with rev.open("book.xlsx") as fh:
            fh.write(b"x" * 100)
    assert calls == ["/api/collections/spreadsheets/quota/grid"]
    c4 = Collection("spreadsheets", app_id="grid", node_url="http://node", http=_quota_node(100, 100, calls))
    with pytest.raises(QuotaExceeded, match="this revision adds"):
        with c4.revise("b1") as rev:
            with rev.open("book.xlsx") as fh:
                fh.write(b"x" * 101)
    assert (c.own_dir / "b1" / "book.xlsx").read_bytes() == b"x" * 100
    assert list((c.own_dir / TMP_DIR).iterdir()) == []


def test_resume_never_adopts_a_revision_staging(root):
    c = Collection("spreadsheets", app_id="grid")
    c.ensure_own_dirs()
    (c.own_dir / TMP_DIR / "b2.revise.1.abcd").mkdir()
    with c.publish("b2", kind="spreadsheet.workbook", resume=True) as item:
        assert ".revise." not in item.path.name
        with item.open("book.xlsx") as fh:
            fh.write(b"new")
    assert c.has("b2")


def test_get_without_remote_stays_local(root):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"items": []})

    c = Collection("spreadsheets", app_id="grid", node_url="http://node",
                   http=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://node"))
    assert c.get("spreadsheets:grid/absent", remote=False) is None and calls == []
    assert c.get("spreadsheets:grid/absent") is None and calls
