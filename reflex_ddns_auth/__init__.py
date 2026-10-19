"""reflex_ddns_auth: shared pieces for Reflex apps on *.reflex-ddns.com.

- ``AuthState``            SSO state (``reflex_ddns_auth.state``)
- ``reflex_ddns_auth.intent``       open other apps' pages as dialogs
- ``reflex_ddns_auth.collections``  shared storage between apps (Owned Collections)

``AuthState`` is imported lazily so that ``reflex_ddns_auth.collections`` (which
has no Reflex dependency) can be used from plain scripts and tests.
"""

__all__ = ["AuthState"]


def __getattr__(name: str):
    if name == "AuthState":
        from reflex_ddns_auth.state import AuthState

        return AuthState
    raise AttributeError(f"module 'reflex_ddns_auth' has no attribute {name!r}")
