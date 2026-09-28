"""Shared SSO AuthState for Reflex apps on *.reflex-ddns.com.

Usage in any Reflex app:

    from reflex_ddns_auth import AuthState

    def index():
        return rx.vstack(
            rx.cond(
                AuthState.is_logged_in,
                rx.text("Hi ", AuthState.user_name),
                rx.link("Login", href=AuthState.login_url),
            ),
            on_mount=AuthState.load_auth,
        )
"""

import os
import logging
from http.cookies import SimpleCookie

import reflex as rx

try:
    import jwt as _jwt
except ImportError:  # pragma: no cover
    _jwt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DDNS_AUTH_SECRET = os.environ.get("DDNS_AUTH_SECRET", "")
DDNS_AUTH_DEV_USER = os.environ.get("DDNS_AUTH_DEV_USER", "")
DDNS_AUTH_COOKIE = os.environ.get("DDNS_AUTH_COOKIE", "ddns_auth")
RELACK_URL = os.environ.get("RELACK_URL", "https://relack.reflex-ddns.com")


def _extract_cookie(cookie_header: str, name: str) -> str:
    if not cookie_header:
        return ""
    try:
        cookies = SimpleCookie(cookie_header)
        morsel = cookies.get(name)
        return morsel.value if morsel else ""
    except Exception:
        return ""


class AuthState(rx.State):
    """Cross-app SSO state backed by a JWT cookie from Relack.

    Provides:
        user_email, user_name, user_avatar  – identity from Google login
        is_approved  – admin-approved flag from Relack
        is_logged_in – whether a valid JWT was found
        login_url    – link to Relack login (with redirect back)
        logout_url   – link to Relack logout (with redirect back)
    """

    user_email: str = ""
    user_name: str = ""
    user_avatar: str = ""
    auth_status: str = "anonymous"
    _auth_loaded: bool = False
    _last_token: str = ""
    _is_approved: bool = False

    @rx.var
    def is_logged_in(self) -> bool:
        return self.auth_status == "authenticated"

    @rx.var
    def is_approved(self) -> bool:
        return self._is_approved and self.is_logged_in

    @rx.var
    def login_url(self) -> str:
        current = self._current_origin()
        return f"{RELACK_URL}/auth/google/login?redirect={current}"

    @rx.var
    def logout_url(self) -> str:
        current = self._current_origin()
        return f"{RELACK_URL}/auth/sso/logout?redirect={current}"

    def _current_origin(self) -> str:
        """Return the full origin URL (with protocol) for redirect after login/logout."""
        origin = self.router.url.origin or ""
        if origin and origin != "://":
            return origin
        return "http://localhost:3000"

    @rx.event
    async def load_auth(self):
        # Re-read on every page load: login/logout on Relack changes the cookie
        # while this tab's state persists.
        if DDNS_AUTH_DEV_USER:
            self._auth_loaded = True
            self._load_dev_user()
            return

        cookie_header = self.router.headers.cookie
        token = _extract_cookie(cookie_header, DDNS_AUTH_COOKIE)
        if self._auth_loaded and token == self._last_token:
            return
        self._auth_loaded = True
        self._last_token = token

        if not token:
            self._set_anonymous()
            return

        if _jwt is None:
            logger.warning("PyJWT not installed — cannot verify ddns_auth cookie")
            self._set_anonymous()
            return

        if not DDNS_AUTH_SECRET:
            logger.warning("DDNS_AUTH_SECRET not set — cannot verify ddns_auth cookie")
            self._set_anonymous()
            return

        try:
            payload = _jwt.decode(token, DDNS_AUTH_SECRET, algorithms=["HS256"])
            self.user_email = payload.get("email", "")
            self.user_name = payload.get("name", "")
            self.user_avatar = payload.get("picture", "")
            self._is_approved = payload.get("approved", False)
            self.auth_status = "authenticated"
        except _jwt.ExpiredSignatureError:
            logger.debug("ddns_auth cookie expired")
            self._set_anonymous()
        except _jwt.InvalidTokenError:
            logger.debug("ddns_auth cookie invalid")
            self._set_anonymous()

    def _set_anonymous(self):
        self.user_email = ""
        self.user_name = ""
        self.user_avatar = ""
        self._is_approved = False
        self.auth_status = "anonymous"

    @rx.event
    async def reload_auth(self):
        """Force re-read of the auth cookie (e.g. after login redirect)."""
        self._auth_loaded = False
        await self.load_auth()

    def _load_dev_user(self):
        parts = DDNS_AUTH_DEV_USER.split(";")
        self.user_email = parts[0] if len(parts) > 0 else "dev@localhost"
        self.user_name = parts[1] if len(parts) > 1 else parts[0]
        self.user_avatar = parts[2] if len(parts) > 2 else ""
        self._is_approved = True
        self.auth_status = "authenticated"
