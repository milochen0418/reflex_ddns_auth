"""Owned Collections: shared storage between apps with three rules.

    write mine      an app writes only inside its own contribution, by-app/<app>/
    read all        every app with access reads the union of all contributions
    publish whole   an item appears only once it is complete (manifest.json last)

    from reflex_ddns_auth.collections import Collection

    video = Collection("video")                       # root + owner from the environment
    if not video.has(vid):
        with video.publish(vid, kind="video.file", title=t, source=url) as item:
            with item.open("clip.webm") as fh:
                download_into(fh)

    for it in video.items(kind="video.file"):         # local + other hosts (through the node)
        print(it.ref, it.owner, it.local, it.path)
    path = video.resolve(ref)                         # fetches from the home host when needed
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from reflex_ddns_auth.collections.errors import InvalidRef, ItemNotFound
from reflex_ddns_auth.collections.manifest import (
    BY_APP,
    MANIFEST_NAME,
    REPLICA_NAME,
    TMP_DIR,
    read_json,
    safe_relpath,
)
from reflex_ddns_auth.collections.node_client import NodeClient, Progress
from reflex_ddns_auth.collections.paths import resolve_app_id, resolve_node_url, resolve_root
from reflex_ddns_auth.collections.publish import Publish
from reflex_ddns_auth.collections.refs import (
    Ref,
    check_collection,
    check_item_id,
    make_ref,
    parse_ref,
    valid_item_id,
    valid_owner,
)


@dataclass
class Item:
    ref: str
    collection: str
    owner: str
    id: str
    kind: str
    title: str
    manifest: dict = field(repr=False)
    path: Path | None          # None until the item is local
    local: bool
    replica: bool              # a local copy fetched from another host
    home: str                  # node name of the host that owns it ("" if unknown)
    bytes: int

    @property
    def created_at(self) -> str:
        return str(self.manifest.get("created_at") or "")

    @property
    def meta(self) -> dict:
        m = self.manifest.get("meta")
        return m if isinstance(m, dict) else {}

    @property
    def files(self) -> list[dict]:
        f = self.manifest.get("files")
        return f if isinstance(f, list) else []

    @property
    def primary(self) -> Path | None:
        name = self.manifest.get("primary")
        if not name or self.path is None:
            return None
        return self.path / safe_relpath(str(name))

    def file(self, rel: str) -> Path:
        if self.path is None:
            raise ItemNotFound(f"{self.ref} is not local; call Collection.resolve() first")
        return self.path / safe_relpath(rel)


class Collection:
    def __init__(
        self,
        name: str,
        app_id: str | None = None,
        root: "str | os.PathLike | None" = None,
        node_url: str | None = None,
        http: Any = None,
    ):
        self.name = check_collection(name)
        self.root = resolve_root(root)
        self.path = self.root / self.name
        self.app_id = resolve_app_id(app_id)
        self.own_dir = self.path / BY_APP / self.app_id
        self.node_name = os.environ.get("DDNS_NODE_NAME", "")
        self.node = NodeClient(resolve_node_url(node_url), http=http)

    def __repr__(self) -> str:
        return f"Collection({self.name!r}, app_id={self.app_id!r}, root={str(self.root)!r})"

    # -- own contribution ---------------------------------------------------

    def ensure_own_dirs(self) -> None:
        (self.own_dir / TMP_DIR).mkdir(parents=True, exist_ok=True)

    def ref(self, item_id: str) -> str:
        return str(make_ref(self.name, self.app_id, item_id))

    def has(self, item_id: str) -> bool:
        return (self.own_dir / check_item_id(item_id) / MANIFEST_NAME).exists()

    def publish(
        self,
        item_id: str,
        kind: str,
        *,
        title: str = "",
        meta: dict | None = None,
        source: str = "",
        derived_from: Iterable[str] = (),
        primary: str | None = None,
        exist_ok: bool = False,
        resume: bool = False,
    ) -> Publish:
        return Publish(
            self, item_id, kind, title=title, meta=meta, source=source,
            derived_from=derived_from, primary=primary, exist_ok=exist_ok, resume=resume,
        )

    def remove(self, item_id: str) -> bool:
        """Delete one of our own items.  Other apps' items cannot be removed."""
        target = self.own_dir / check_item_id(item_id)
        if not (target / MANIFEST_NAME).exists():
            return False
        shutil.rmtree(target)
        return True

    def gc_tmp(self, max_age_hours: float = 36.0) -> int:
        """Remove our stale ``.tmp`` directories (crashed publishes nobody resumed)."""
        tmp_root = self.own_dir / TMP_DIR
        removed = 0
        cutoff = time.time() - max_age_hours * 3600
        try:
            entries = list(tmp_root.iterdir())
        except OSError:
            return 0
        for entry in entries:
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        return removed

    # -- reading ------------------------------------------------------------

    def local_items(self, kind: str | None = None) -> list[Item]:
        by_app = self.path / BY_APP
        items: list[Item] = []
        try:
            owners = list(os.scandir(by_app))
        except OSError:
            return items
        for owner in owners:
            if not owner.is_dir(follow_symlinks=False) or not valid_owner(owner.name):
                continue
            try:
                children = list(os.scandir(owner.path))
            except OSError:
                continue
            for child in children:
                if not child.is_dir(follow_symlinks=False) or not valid_item_id(child.name):
                    continue
                item = self._item_from_dir(owner.name, Path(child.path))
                if item is not None and (kind is None or item.kind == kind):
                    items.append(item)
        return items

    def items(self, kind: str | None = None, remote: bool = True) -> list[Item]:
        """Every published item: local ones, plus those on other hosts (via the node)."""
        items = self.local_items(kind)
        if remote and self.node.available:
            seen = {i.ref for i in items}
            for entry in self.node.remote_items(self.name, kind):
                item = self._item_from_remote(entry)
                if item is not None and item.ref not in seen and (kind is None or item.kind == kind):
                    items.append(item)
        items.sort(key=lambda i: (i.created_at, i.ref), reverse=True)
        return items

    def get(self, ref: "str | Ref") -> Item | None:
        r = self._own_ref(ref)
        item = self._item_from_dir(r.owner, self.path / BY_APP / r.owner / r.id)
        if item is not None:
            return item
        for entry in self.node.remote_items(self.name) if self.node.available else []:
            if entry.get("ref") == str(r):
                return self._item_from_remote(entry)
        return None

    def resolve(
        self,
        ref: "str | Ref",
        fetch: bool = True,
        timeout: float = 900.0,
        progress: Progress | None = None,
    ) -> Path:
        """The local directory of ``ref``; fetched from its home host first when needed."""
        r = self._own_ref(ref)
        target = self.path / BY_APP / r.owner / r.id
        if (target / MANIFEST_NAME).exists():
            return target
        if not fetch:
            raise ItemNotFound(f"{r} is not on this host")
        if not self.node.available:
            raise ItemNotFound(f"{r} is not on this host and no re-ddns node is configured")
        self.node.fetch(str(r), timeout=timeout, progress=progress)
        if (target / MANIFEST_NAME).exists():
            return target
        raise ItemNotFound(f"{r}: the node reported success but the item is not visible here")

    # -- helpers ------------------------------------------------------------

    def _own_ref(self, ref: "str | Ref") -> Ref:
        r = parse_ref(ref)
        if r.collection != self.name:
            raise InvalidRef(f"{r} belongs to collection {r.collection!r}, not {self.name!r}")
        return r

    def _item_from_dir(self, owner: str, item_dir: Path) -> Item | None:
        manifest = read_json(item_dir / MANIFEST_NAME)
        if manifest is None:
            return None
        replica = read_json(item_dir / REPLICA_NAME) if (item_dir / REPLICA_NAME).exists() else None
        producer = manifest.get("producer") if isinstance(manifest.get("producer"), dict) else {}
        home = str((replica or {}).get("home") or producer.get("host") or "")
        return Item(
            ref=str(make_ref(self.name, owner, item_dir.name)),
            collection=self.name,
            owner=owner,
            id=item_dir.name,
            kind=str(manifest.get("kind") or ""),
            title=str(manifest.get("title") or item_dir.name),
            manifest=manifest,
            path=item_dir,
            local=True,
            replica=replica is not None,
            home=home,
            bytes=int(manifest.get("bytes") or 0),
        )

    def _item_from_remote(self, entry: dict) -> Item | None:
        try:
            r = parse_ref(str(entry.get("ref", "")))
        except InvalidRef:
            return None
        if r.collection != self.name:
            return None
        manifest = entry.get("manifest") if isinstance(entry.get("manifest"), dict) else {}
        return Item(
            ref=str(r),
            collection=self.name,
            owner=r.owner,
            id=r.id,
            kind=str(entry.get("kind") or manifest.get("kind") or ""),
            title=str(entry.get("title") or manifest.get("title") or r.id),
            manifest=manifest,
            path=None,
            local=False,
            replica=False,
            home=str(entry.get("home") or ""),
            bytes=int(entry.get("bytes") or manifest.get("bytes") or 0),
        )
