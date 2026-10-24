"""Revising an item: its owner changes it in place, one whole file at a time.

    with collection.revise("book-1", title="Q3 plan", meta={...}) as rev:
        with rev.open("book.xlsx") as fh:          # staged in .tmp until the block ends
            fh.write(data)
        rev.remove("book.xlsm")                    # left out of the item from now on
        rev.set_thumbnail(png)                     # a new preview, as for a publish
    # here: the files swapped into the item, .item/manifest.json rewritten last

Most items never change once published (a downloaded DICOM series).  Some
are a document an app keeps editing, e.g. a workbook ReflexGrid saves while
people type: publishing a new item for every save would bury the collection
in copies.  Such an app publishes the item once and revises it from then on.

What readers can rely on:

* nothing changes until the ``with`` block ends without an exception; the
  new files are staged in the owner's ``.tmp/`` meanwhile;
* each file is replaced with one rename, so a reader opens either the old or
  the new file, never half of one;
* the manifest is rewritten last, with ``revision`` one higher (an item never
  revised has none: read it as 1) and ``updated_at`` set; between the renames
  and the manifest a reader may briefly see new files under the old manifest;
* only the owner revises, and only on the host that holds the original: a
  copy fetched from another host (``.item/replica.json``) is refused.  Copies
  already fetched elsewhere are not refreshed: they keep the revision they
  were fetched at.

A revision that makes the item bigger is checked against the app's quota,
like a publish (``QuotaExceeded``, and nothing changes).  The owner's
directory is touched at the end, so the node's index sees the new manifest
at once.
"""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from reflex_ddns_auth.collections.errors import ItemNotFound, PublishError, QuotaExceeded
from reflex_ddns_auth.collections.manifest import (
    PART_SUFFIX,
    REVISE_INFIX,
    TMP_DIR,
    manifest_path,
    meta_dir,
    read_json,
    replica_path,
    safe_relpath,
    scan_files,
    utcnow_iso,
    write_json_atomic,
)
from reflex_ddns_auth.collections.publish import _mb, _PartFile, thumbnail_image, write_thumbnail
from reflex_ddns_auth.collections.refs import check_item_id

if TYPE_CHECKING:  # pragma: no cover
    from reflex_ddns_auth.collections.collection import Collection


class Revise:
    """Context manager returned by :meth:`Collection.revise`."""

    def __init__(
        self,
        collection: "Collection",
        item_id: str,
        *,
        title: str | None = None,
        meta: dict | None = None,
        primary: str | None = None,
    ):
        self.collection = collection
        self.id = check_item_id(item_id)
        self.title = title                                  # None: keep
        self.meta = None if meta is None else dict(meta)    # None: keep, else replaces it
        self.primary = primary                              # None: keep while the file stays
        self.final = collection.own_dir / self.id
        self.ref = collection.ref(self.id)
        self.tmp: Path | None = None
        self.removed: set[str] = set()
        self.thumbnail: tuple[str, bytes] | None = None     # (extension, image): replaces the old one
        self.current: dict = {}
        self.manifest: dict | None = None                   # the new manifest, once revised

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "Revise":
        current = read_json(manifest_path(self.final))
        if current is None:
            raise ItemNotFound(f"{self.ref} is not published here")
        if replica_path(self.final).exists():
            home = (read_json(replica_path(self.final)) or {}).get("home") or "another host"
            raise PublishError(f"{self.ref} is a copy from {home}; only its home host can revise it")
        self.current = current
        try:
            self.collection.ensure_own_dirs()
            self.tmp = (self.collection.own_dir / TMP_DIR
                        / f"{self.id}{REVISE_INFIX}{os.getpid()}.{secrets.token_hex(4)}")
            self.tmp.mkdir()
        except OSError as exc:
            raise PublishError(f"cannot write to {self.collection.own_dir}: {exc}") from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._finish()
        finally:
            self.discard()
        return False

    # -- writing -----------------------------------------------------------

    @property
    def path(self) -> Path:
        """The staging directory; write files here (or use :meth:`open`)."""
        if self.tmp is None:
            raise PublishError("use Revise inside a 'with' block")
        return self.tmp

    def file(self, rel: str) -> Path:
        try:
            return self.path / safe_relpath(rel)
        except ValueError as exc:
            raise PublishError(str(exc)) from exc

    def open(self, rel: str, mode: str = "wb") -> _PartFile:
        """A new version of ``rel`` (or a new file); it replaces the old one when the block ends."""
        if "r" in mode or "+" in mode:
            raise PublishError("open() only writes; read the item's own file to read")
        return _PartFile(self.file(rel), mode)

    def add_file(self, src: "str | os.PathLike", rel: str | None = None, move: bool = False) -> Path:
        """Copy (or move) an existing file in as ``rel``."""
        src_path = Path(src)
        dest = self.file(rel or src_path.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + PART_SUFFIX)
        if move:
            shutil.move(str(src_path), str(part))
        else:
            shutil.copy2(str(src_path), str(part))
        os.replace(part, dest)
        return dest

    def remove(self, rel: str) -> None:
        """Leave ``rel`` out of the item (no error when it isn't there; a file written in the
        same block stays)."""
        try:
            self.removed.add(safe_relpath(rel))
        except ValueError as exc:
            raise PublishError(str(exc)) from exc

    def set_thumbnail(self, image: "bytes | str | os.PathLike") -> None:
        """A new preview image, as for a publish (PNG, JPEG, WebP or GIF, 512 KB at most); it
        replaces the old one when the block ends."""
        self.thumbnail = thumbnail_image(image)

    def discard(self) -> None:
        """Throw the staged files away (nothing changes)."""
        if self.tmp is not None and self.tmp.exists():
            shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp = None

    # -- finishing ---------------------------------------------------------

    def _check_quota(self, adding: int) -> None:
        quota = self.collection.node.quota(self.collection.name, self.collection.app_id)
        if not quota:
            return
        limit, used = int(quota.get("limit") or 0), int(quota.get("used") or 0)
        if limit > 0 and used + adding > limit:
            raise QuotaExceeded(
                f"{self.ref}: {self.collection.app_id} may keep {_mb(limit)} in "
                f"{self.collection.name!r} on this host and has {_mb(used)}; this revision adds {_mb(adding)}"
            )

    def _finish(self) -> None:
        assert self.tmp is not None
        staged, unfinished = scan_files(self.tmp)
        if unfinished:
            raise PublishError(f"unfinished files in {self.ref}: {', '.join(unfinished)}")
        old = {str(f["path"]): int(f.get("bytes") or 0)
               for f in self.current.get("files", []) if isinstance(f, dict) and f.get("path")}
        written = {f["path"]: int(f["bytes"]) for f in staged}
        files = {p: n for p, n in old.items() if p not in self.removed}
        files.update(written)
        if not files:
            raise PublishError(f"{self.ref} would have no files")
        total = sum(files.values())
        growth = total - int(self.current.get("bytes") or sum(old.values()))
        if growth > 0:
            self._check_quota(growth)
        if self.primary is not None:
            primary = safe_relpath(self.primary)
            if primary not in files:
                raise PublishError(f"primary file {primary!r} is not in the item")
        else:
            primary = str(self.current.get("primary") or "")
            if primary not in files:
                primary = next(iter(files)) if len(files) == 1 else ""
        manifest = dict(self.current)
        manifest.update(
            files=[{"path": p, "bytes": files[p]} for p in sorted(files)],
            bytes=total,
            primary=primary,
            revision=_revision(self.current) + 1,
            updated_at=utcnow_iso(),
        )
        if self.title is not None:
            manifest["title"] = self.title or self.id
        if self.meta is not None:
            manifest["meta"] = self.meta
        try:
            if not manifest_path(self.final).exists():
                raise FileNotFoundError(self.final)
            for rel in written:
                dest = self.final / rel
                if dest.parent != self.final:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(self.tmp / rel, dest)
            for rel in self.removed - written.keys():
                (self.final / rel).unlink(missing_ok=True)
            if self.thumbnail is not None:
                manifest["thumbnail"] = write_thumbnail(meta_dir(self.final), *self.thumbnail).name
            write_json_atomic(manifest_path(self.final), manifest)
        except FileNotFoundError as exc:
            if not self.final.is_dir():
                raise ItemNotFound(f"{self.ref} was deleted meanwhile") from exc
            raise
        self.manifest = manifest
        try:
            os.utime(self.collection.own_dir)
        except OSError:
            pass


def _revision(manifest: dict) -> int:
    try:
        return max(1, int(manifest.get("revision") or 1))
    except (TypeError, ValueError):
        return 1
