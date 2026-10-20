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

When the re-ddns node is reachable, a publish also respects the app's quota
in the collection (set on the App Store's storage page): it does not start
when the quota is used up, and it does not finish when the item would go
over it (``QuotaExceeded``; the ``.tmp`` directory stays for a resume once
there is room).  Without a node there is no quota.
"""

from __future__ import annotations

import errno
import os
import secrets
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from reflex_ddns_auth.collections.errors import ItemExists, PublishError, QuotaExceeded
from reflex_ddns_auth.collections.kinds import check_kind
from reflex_ddns_auth.collections.manifest import (
    PART_SUFFIX,
    REVISE_INFIX,
    SCHEMA_VERSION,
    THUMBNAIL_MAX_BYTES,
    THUMBNAIL_STEM,
    THUMBNAIL_TYPES,
    TMP_DIR,
    image_type,
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


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


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
        kind = str(kind or "").strip()
        if not kind:
            raise PublishError("kind is required (e.g. 'dicom.series', 'video.file')")
        self.kind = check_kind(kind)
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
        if manifest_path(self.final).exists():
            if not self.exist_ok:
                raise ItemExists(f"{self.ref} is already published")
        else:
            self._check_quota(0)
        try:
            self.collection.ensure_own_dirs()
        except OSError as exc:
            raise PublishError(f"cannot write to {self.collection.own_dir}: {exc}") from exc
        tmp_root = self.collection.own_dir / TMP_DIR
        if self.resume:
            candidates = sorted(
                (p for p in tmp_root.glob(f"{self.id}.*")
                 if p.is_dir() and ".replica." not in p.name and REVISE_INFIX not in p.name),
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

    def set_thumbnail(self, image: "bytes | str | os.PathLike") -> Path:
        """Attach a preview image: PNG, JPEG, WebP or GIF bytes (or a file), 512 KB at most.

        It is kept in ``.item/`` with the bookkeeping, so it is not part of the
        payload; the App Store's storage page shows it next to the item.
        """
        data = bytes(image) if isinstance(image, (bytes, bytearray)) else Path(image).read_bytes()
        if len(data) > THUMBNAIL_MAX_BYTES:
            raise PublishError(f"thumbnail is {len(data)} bytes; at most {THUMBNAIL_MAX_BYTES} are kept")
        ext = image_type(data[:16])
        if ext is None:
            raise PublishError("thumbnail must be a PNG, JPEG, WebP or GIF image")
        meta = meta_dir(self.path)
        meta.mkdir(exist_ok=True)
        for old in THUMBNAIL_TYPES:
            (meta / f"{THUMBNAIL_STEM}.{old}").unlink(missing_ok=True)
        dest = meta / f"{THUMBNAIL_STEM}.{ext}"
        part = dest.with_name(dest.name + PART_SUFFIX)
        part.write_bytes(data)
        os.replace(part, dest)
        return dest

    def discard(self) -> None:
        """Throw the staging directory away (nothing was published)."""
        if self.tmp is not None and self.tmp.exists():
            shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp = None

    # -- finishing ---------------------------------------------------------

    def _check_quota(self, adding: int) -> None:
        """Refuse when the node says this app has no room left here for ``adding`` more bytes."""
        quota = self.collection.node.quota(self.collection.name, self.collection.app_id)
        if not quota:
            return
        limit, used = int(quota.get("limit") or 0), int(quota.get("used") or 0)
        # Starting (adding == 0) needs at least one free byte.
        if limit > 0 and used + max(adding, 1) > limit:
            needs = f"; this item needs {_mb(adding)}" if adding else ""
            raise QuotaExceeded(
                f"{self.ref}: {self.collection.app_id} may keep {_mb(limit)} in "
                f"{self.collection.name!r} on this host and has {_mb(used)}{needs}"
            )

    def _thumbnail_name(self) -> str:
        meta = meta_dir(self.path)
        for ext in THUMBNAIL_TYPES:
            if (meta / f"{THUMBNAIL_STEM}.{ext}").is_file():
                return f"{THUMBNAIL_STEM}.{ext}"
        return ""

    def _finish(self) -> None:
        assert self.tmp is not None
        files, unfinished = scan_files(self.tmp)
        if unfinished:
            raise PublishError(f"unfinished files in {self.ref}: {', '.join(unfinished)}")
        if not files:
            raise PublishError(f"{self.ref} has no files")
        total = sum(int(f["bytes"]) for f in files)
        if not manifest_path(self.final).exists():
            self._check_quota(total)
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
            "bytes": total,
            "meta": self.meta,
        }
        thumbnail = self._thumbnail_name()
        if thumbnail:
            manifest["thumbnail"] = thumbnail
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
