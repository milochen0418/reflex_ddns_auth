"""Refs: the location-independent identity of an item.

    <collection>:<owner>/<item-id>      e.g.  dicom:dicom-data-explorer/1.2.840.113...

A ref never carries a path.  Each host resolves it inside its own copy of
the collection tree, so the same ref is valid on every machine.  The three
segments are validated so that a ref can never escape that tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from reflex_ddns_auth.collections.errors import InvalidRef

_COLLECTION = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_OWNER = re.compile(r"^[a-z0-9_][a-z0-9_-]{0,62}$")
_ITEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


def valid_collection(name: str) -> bool:
    return bool(name) and bool(_COLLECTION.match(name))


def valid_owner(name: str) -> bool:
    return bool(name) and bool(_OWNER.match(name)) and name != "by-app"


def valid_item_id(name: str) -> bool:
    # The first character is alphanumeric, so ".", ".." and ".tmp" can never match.
    return bool(name) and bool(_ITEM.match(name))


def check_collection(name: str) -> str:
    if not valid_collection(name):
        raise InvalidRef(f"invalid collection name {name!r} (use a-z, 0-9, '-')")
    return name


def check_owner(name: str) -> str:
    if not valid_owner(name):
        raise InvalidRef(f"invalid owner {name!r} (use a-z, 0-9, '-', '_')")
    return name


def check_item_id(name: str) -> str:
    if not valid_item_id(name):
        raise InvalidRef(f"invalid item id {name!r} (start alphanumeric; a-z, 0-9, '.', '_', '-')")
    return name


@dataclass(frozen=True)
class Ref:
    collection: str
    owner: str
    id: str

    def __str__(self) -> str:
        return f"{self.collection}:{self.owner}/{self.id}"

    @property
    def parts(self) -> tuple[str, str, str]:
        """Path segments below the collection directory."""
        return ("by-app", self.owner, self.id)


def make_ref(collection: str, owner: str, item_id: str) -> Ref:
    return Ref(check_collection(collection), check_owner(owner), check_item_id(item_id))


def parse_ref(text: "str | Ref") -> Ref:
    if isinstance(text, Ref):
        return text
    if not isinstance(text, str):
        raise InvalidRef(f"ref must be a string, not {type(text).__name__}")
    collection, sep, rest = text.partition(":")
    owner, sep2, item_id = rest.partition("/")
    if not sep or not sep2:
        raise InvalidRef(f"ref {text!r} is not <collection>:<owner>/<item-id>")
    return make_ref(collection, owner, item_id)
