"""Owned Collections: shared storage between apps (write mine, read all, publish whole).

    from reflex_ddns_auth.collections import Collection

    dicom = Collection("dicom")
    with dicom.publish(series_uid, kind="dicom.series", meta=m) as item:
        with item.open("slice-001.dcm") as fh:
            fh.write(data)
    for it in dicom.items(kind="dicom.series"):
        ...
    path = dicom.resolve(ref)          # local path; fetched from its home host when needed

Layout on disk (the same on every host and in every container):

    <root>/<collection>/by-app/<owner>/<item-id>/<files...>           the payload, nothing else
    <root>/<collection>/by-app/<owner>/<item-id>/.item/manifest.json  bookkeeping, hidden like .git/
    <root>/<collection>/by-app/<owner>/.tmp/                          publishes in progress (ignored by readers)

See ``paths.py`` for how the root and the owner are resolved.
"""

from reflex_ddns_auth.collections.collection import Collection, Item
from reflex_ddns_auth.collections.errors import (
    CollectionError,
    InvalidRef,
    ItemExists,
    ItemNotFound,
    NodeUnavailable,
    PublishError,
)
from reflex_ddns_auth.collections.manifest import BY_APP, MANIFEST_NAME, META_DIR, REPLICA_NAME, TMP_DIR
from reflex_ddns_auth.collections.paths import (
    ENV_APP_ID,
    ENV_NODE_URL,
    ENV_ROOT,
    app_id_from_name,
    default_root,
    resolve_app_id,
    resolve_node_url,
    resolve_root,
)
from reflex_ddns_auth.collections.publish import Publish
from reflex_ddns_auth.collections.refs import Ref, make_ref, parse_ref

__all__ = [
    "Collection", "Item", "Publish", "Ref", "make_ref", "parse_ref",
    "CollectionError", "InvalidRef", "ItemExists", "ItemNotFound", "NodeUnavailable", "PublishError",
    "BY_APP", "MANIFEST_NAME", "META_DIR", "REPLICA_NAME", "TMP_DIR",
    "ENV_APP_ID", "ENV_NODE_URL", "ENV_ROOT",
    "app_id_from_name", "default_root", "resolve_app_id", "resolve_node_url", "resolve_root",
]
