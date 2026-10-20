"""Item kinds: what an item holds, named ``<domain>.<type>``.

    dicom.series    video.file    audio.file    file.generic

A kind is two or more dot-separated segments of lowercase letters and digits,
with single hyphens inside a segment (``image.slide-deck``), at most 64
characters.  The domain comes first so that related kinds sort together and
an unknown kind still says what it is about.

The kinds in use, and the payload each one promises (which files, which
``meta`` keys), are listed in re_ddns ``docs/collections.md`` under "Kinds".
Add a kind there before an app publishes it, so that two apps never name the
same thing twice (``video.mp4`` next to ``media.video``) and a reader knows
what it gets.  ``file.generic`` promises nothing: any files, e.g. a user's
own import.
"""

from __future__ import annotations

import re

from reflex_ddns_auth.collections.errors import InvalidKind

KIND_MAX = 64
GENERIC_KIND = "file.generic"

_SEGMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_KIND = re.compile(rf"^{_SEGMENT}(?:\.{_SEGMENT})+$")


def valid_kind(kind: str) -> bool:
    return isinstance(kind, str) and len(kind) <= KIND_MAX and bool(_KIND.match(kind))


def check_kind(kind: str) -> str:
    if not valid_kind(kind):
        raise InvalidKind(
            f"invalid kind {kind!r}: use <domain>.<type> in lowercase, e.g. 'dicom.series' "
            f"or 'video.file' (a-z, 0-9, inner '-', at most {KIND_MAX} characters)"
        )
    return kind
