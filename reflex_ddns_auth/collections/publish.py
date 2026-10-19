"""The publish protocol: write in ``.tmp``, finish with one rename.

    with collection.publish("1.2.840...", kind="dicom.series", meta={...}) as item:
        with item.open("slice-001.dcm") as fh:      # writes slice-001.dcm.part, renamed on close
            fh.write(...)
    # here: manifest.json written, directory renamed into place, item visible to everyone

Two layers of atomicity (the Maildir idea): each file is written under a
``.part`` name and renamed when closed; the whole item lives under
``.tmp/<id>.<token>/`` and is renamed into place after ``.item/manifest.json``
was written last.  Readers never see a half item.  An exception inside the
``with`` block leaves the ``.tmp`` directory for ``resume=True`` to pick up.
"""

from __future__ import annotations

import errno
import os
import secrets
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from reflex_ddns_auth.collections.errors import ItemExists, PublishError
from reflex_ddns_auth.collections.manifest import (
    PART_SUFFIX,
    SCHEMA_VERSION,
    TMP_DIR,
    manifest_path,
    meta_dir,
    safe_relpath,
    scan_files,
    utcnow_iso,
    write_json_atomic,
)
from reflex_ddns_auth.collections.refs import check_item_id

if TYPE_CHECKING:  # pragma: no cover
    from reflex_ddns_auth.collections.collection import Collection


class _PartFile:
    """A file written as ``<name>.part`` and renamed to ``<name>`` on close."""

    def __init__(self, final: Path, mode: str):
        self.final = final
        self.part = final.with_name(final.name + PART_SUFFIX)
        self.final.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.part, mode)  # noqa: SIM115 - closed in close()
        self.closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fh, name)

    def __enter__(self) -> "_PartFile":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()

    def close(self) -> None:
        if self.closed:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        os.replace(self.part, self.final)
        self.closed = True

    def abort(self) -> None:
        """Drop the partial file (the item itself stays resumable)."""
        if self.closed:
            return
        self._fh.close()
        self.closed = True
        try:
            os.unlink(self.part)
        except FileNotFoundError:
            pass


class Publish:
    """Context manager returned by :meth:`Collection.publish`."""

    def __init__(
        self,
        collection: "Collection",
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
    ):
        self.collection = collection
        self.id = check_item_id(item_id)
        self.kind = str(kind or "").strip()
        if not self.kind:
            raise PublishError("kind is required (e.g. 'dicom.series', 'video.file')")
        self.title = title
        self.meta = dict(meta or {})
        self.source = source
        self.derived_from = [str(r) for r in derived_from]
        self.primary = primary
        self.exist_ok = exist_ok
        self.resume = resume
        self.final = collection.own_dir / self.id
        self.tmp: Path | None = None
        self.existing = False      # True when another publish of this id won
        self.ref = collection.ref(self.id)
        self.manifest: dict | None = None

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "Publish":
        if manifest_path(self.final).exists() and not self.exist_ok:
            raise ItemExists(f"{self.ref} is already published")
        try:
            self.collection.ensure_own_dirs()
        except OSError as exc:
            raise PublishError(f"cannot write to {self.collection.own_dir}: {exc}") from exc
        tmp_root = self.collection.own_dir / TMP_DIR
        if self.resume:
            candidates = sorted(
                (p for p in tmp_root.glob(f"{self.id}.*") if p.is_dir() and ".replica." not in p.name),
                key=lambda p: p.stat().st_mtime,
            )
            if candidates:
                self.tmp = candidates[-1]
        if self.tmp is None:
            self.tmp = tmp_root / f"{self.id}.{os.getpid()}.{secrets.token_hex(4)}"
            self.tmp.mkdir()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            return False  # keep .tmp for resume=True
        self._finish()
        return False

    # -- writing -----------------------------------------------------------

    @property
    def path(self) -> Path:
        """The staging directory; write files here (or use :meth:`open`)."""
        if self.tmp is None:
            raise PublishError("use Publish inside a 'with' block")
        return self.tmp

    def file(self, rel: str) -> Path:
        try:
            return self.path / safe_relpath(rel)
        except ValueError as exc:
            raise PublishError(str(exc)) from exc

    def open(self, rel: str, mode: str = "wb") -> _PartFile:
        if "r" in mode or "+" in mode:
            raise PublishError("open() only writes; read the file back after publishing")
        return _PartFile(self.file(rel), mode)

    def add_file(self, src: "str | os.PathLike", rel: str | None = None, move: bool = False) -> Path:
        """Copy (or move) an existing file into the item."""
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

    def discard(self) -> None:
        """Throw the staging directory away (nothing was published)."""
        if self.tmp is not None and self.tmp.exists():
            shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp = None

    # -- finishing ---------------------------------------------------------

    def _finish(self) -> None:
        assert self.tmp is not None
        files, unfinished = scan_files(self.tmp)
        if unfinished:
            raise PublishError(f"unfinished files in {self.ref}: {', '.join(unfinished)}")
        if not files:
            raise PublishError(f"{self.ref} has no files")
        primary = self.primary
        if primary is None and len(files) == 1:
            primary = files[0]["path"]
        if primary is not None:
            primary = safe_relpath(primary)
            if primary not in {f["path"] for f in files}:
                raise PublishError(f"primary file {primary!r} is not in the item")
        manifest = {
            "schema": SCHEMA_VERSION,
            "collection": self.collection.name,
            "owner": self.collection.app_id,
            "id": self.id,
            "kind": self.kind,
            "title": self.title or self.id,
            "created_at": utcnow_iso(),
            "producer": {"app": self.collection.app_id, "host": self.collection.node_name},
            "source": self.source,
            "derived_from": self.derived_from,
            "primary": primary or "",
            "files": files,
            "bytes": sum(int(f["bytes"]) for f in files),
            "meta": self.meta,
        }
        meta_dir(self.tmp).mkdir(exist_ok=True)
        write_json_atomic(manifest_path(self.tmp), manifest)
        try:
            os.rename(self.tmp, self.final)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY, errno.EISDIR) or manifest_path(self.final).exists():
                shutil.rmtree(self.tmp, ignore_errors=True)
                self.tmp = None
                if manifest_path(self.final).exists():
                    self.existing = True
                    if self.exist_ok:
                        self.manifest = None
                        return
                    raise ItemExists(f"{self.ref} was published concurrently") from exc
            raise
        self.manifest = manifest
        self.tmp = None
