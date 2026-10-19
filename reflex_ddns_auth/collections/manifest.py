"""On-disk conventions shared by the library and the re-ddns node.

    <root>/<collection>/by-app/<owner>/<item-id>/manifest.json   published item
    <root>/<collection>/by-app/<owner>/<item-id>/.replica        this copy came from another host
    <root>/<collection>/by-app/<owner>/.tmp/<item-id>.<token>/   publish (or replica fetch) in progress

``manifest.json`` is written last and its presence is the only signal that an
item is complete.  Readers ignore everything else.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
REPLICA_NAME = ".replica"
TMP_DIR = ".tmp"
BY_APP = "by-app"
PART_SUFFIX = ".part"


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
    """``(files, unfinished)``: regular files below ``item_dir`` and leftover ``.part`` names.

    Symlinks are skipped (never followed), so an item can only describe its
    own bytes.  Paths are POSIX-style and relative to ``item_dir``.
    """
    files: list[dict] = []
    unfinished: list[str] = []
    root = str(item_dir)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel in (MANIFEST_NAME, REPLICA_NAME):
                continue
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
    if parts[-1] in (MANIFEST_NAME, REPLICA_NAME) and len(parts) == 1:
        raise ValueError(f"{rel!r} is reserved")
    return "/".join(parts)
