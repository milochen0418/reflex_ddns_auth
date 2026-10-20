"""On-disk conventions shared by the library and the re-ddns node.

    <root>/<collection>/by-app/<owner>/<item-id>/                     the item: its payload files
    <root>/<collection>/by-app/<owner>/<item-id>/.item/manifest.json  written last; marks the item complete
    <root>/<collection>/by-app/<owner>/<item-id>/.item/replica.json   this copy came from another host
    <root>/<collection>/by-app/<owner>/<item-id>/.item/thumbnail.png  optional preview (png/jpg/webp/gif)
    <root>/<collection>/by-app/<owner>/.tmp/<item-id>.<token>/        publish (or replica fetch) in progress
    <root>/<collection>/by-app/<owner>/.tmp/<item-id>.revise.<token>/ a revision's files, staged (revise.py)

All bookkeeping lives in the hidden ``.item/`` directory (like ``.git/`` in a
working tree), so an app that treats the item directory as "a folder of
files" sees nothing but the payload.  The manifest is written last and its
presence is the only signal that an item is complete.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
META_DIR = ".item"
MANIFEST_NAME = "manifest.json"
REPLICA_NAME = "replica.json"
TMP_DIR = ".tmp"
BY_APP = "by-app"
PART_SUFFIX = ".part"
REVISE_INFIX = ".revise."

# An item's preview image: ``.item/thumbnail.<ext>``, written while publishing
# (before the manifest, which names it).  Small, and one of these types only.
THUMBNAIL_STEM = "thumbnail"
THUMBNAIL_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
THUMBNAIL_MAX_BYTES = 512 * 1024


def image_type(head: bytes) -> str | None:
    """The thumbnail extension ``head`` (the first bytes of a file) really is, or ``None``."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def meta_dir(item_dir: Path) -> Path:
    return item_dir / META_DIR


def manifest_path(item_dir: Path) -> Path:
    return item_dir / META_DIR / MANIFEST_NAME


def replica_path(item_dir: Path) -> Path:
    return item_dir / META_DIR / REPLICA_NAME


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path: Path, data: dict) -> None:
    """Write ``path`` so that it is either complete or absent (``.part`` + rename)."""
    part = path.with_name(path.name + PART_SUFFIX)
    with open(part, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(part, path)


def scan_files(item_dir: Path) -> tuple[list[dict], list[str]]:
    """``(files, unfinished)``: payload files below ``item_dir`` and leftover ``.part`` names.

    The ``.item/`` bookkeeping directory is not part of the payload.  Symlinks
    are skipped (never followed), so an item can only describe its own bytes.
    Paths are POSIX-style and relative to ``item_dir``.
    """
    files: list[dict] = []
    unfinished: list[str] = []
    root = str(item_dir)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames
            if not os.path.islink(os.path.join(dirpath, d)) and not (dirpath == root and d == META_DIR)
        )
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if name.endswith(PART_SUFFIX):
                unfinished.append(rel)
                continue
            files.append({"path": rel, "bytes": os.lstat(full).st_size})
    files.sort(key=lambda f: f["path"])
    return files, unfinished


def safe_relpath(rel: str) -> str:
    """A relative POSIX path that stays inside the item, or ``ValueError``."""
    rel = str(rel).replace("\\", "/").strip()
    if not rel or rel.startswith("/") or rel.endswith("/"):
        raise ValueError(f"bad file path {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"bad file path {rel!r}")
    if parts[0] == META_DIR:
        raise ValueError(f"{META_DIR}/ is reserved for the item's bookkeeping")
    return "/".join(parts)
