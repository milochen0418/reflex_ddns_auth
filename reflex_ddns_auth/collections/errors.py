"""Exceptions raised by ``reflex_ddns_auth.collections``."""


class CollectionError(Exception):
    """Base class for collection errors."""


class InvalidRef(CollectionError, ValueError):
    """A ref, collection, owner or item id does not follow the naming rules."""


class ItemExists(CollectionError):
    """An item with this id is already published in the own contribution."""


class ItemNotFound(CollectionError):
    """No item answers to this ref, locally or on any known host."""


class NodeUnavailable(CollectionError):
    """The re-ddns node (``DDNS_COLLECTIONS_NODE_URL``) did not answer."""


class PublishError(CollectionError):
    """A publish cannot complete (empty item, unfinished ``.part`` file, ...)."""
