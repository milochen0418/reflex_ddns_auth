"""Asking the AI Hub's models from an app: the one way an app on the platform talks to an AI.

    from reflex_ddns_auth import ai

    reply = ai.stream("coder", system="You edit one cell...", messages=[{"role": "user", "content": "..."}])
    async for piece in reply:
        ...                     # the answer, as it is written
    reply.model                 # e.g. "Claude (claude-opus-5-5)", once the hub has said
    reply.stop                  # "end_turn", "max_tokens" or "refusal", once the answer is done

The app says only what it needs: a *role* from the hub's vocabulary (``smart``, ``fast``,
``local`` or ``coder``), the rules (``system``) and the conversation (``messages``, as
``{"role": "user" | "assistant", "content": text}``). Which model answers a role is set on the
AI Hub's page, so changing it there changes every app at once; the app holds no API key.

    ai.uses(app, smart="The assistant panel's answers", fast="The grey suggestions while typing")

says once what the app asks each role for, in a few plain words: the hub's page lists the app's
AI by them from the moment it is installed, so whoever picks the models there knows what each
choice changes.

The hub is found through re-ddns (its service list, ``RE_DDNS_API_URL``; the hub is the app
``ai``), or at ``DDNS_AI_HUB_URL``. Each request carries a short-lived token signed with the
platform's ``DDNS_AUTH_SECRET`` (as the hub's own calls into the apps do: see
:mod:`reflex_ddns_auth.tools`), naming this app and the person asking; nothing long-lived is
kept in the app's container. :func:`verify` is the hub's side of that token.

Without a hub (``hub_url()`` is ""), or when it can't answer, :func:`stream` raises
:class:`HubUnavailable`, and the app may fall back to an AI of its own.

Music is asked for the same way, from the music engine the hub looks after (ACE-Step on the
host, installed with re_ddns's ``macos_music_engine.sh``):

    song = ai.music({"caption": "upbeat synthwave, female vocals", "lyrics": "...", "language": "zh", "duration": 60})
    async for event in song:   # {"status": words} while it works, then {"done": True, ...}
        ...
    mp3 = await song.audio()   # the song, once done

``music_status()`` says whether there is an engine to ask (``{"ok": bool, "installed",
"server", "tier", "why"}``), and ``uses(app, music="...")`` tells the hub's page what the app
makes with it.

On the wire: ``POST <hub>/api/ai/music`` takes the song's spec as JSON and answers with lines of
JSON too (``{"status"}``, ``{"secs"}``, then ``{"done": true, "id", "metas"}`` or ``{"error"}``),
``GET <hub>/api/ai/music/<id>/audio`` is the MP3, ``GET <hub>/api/ai/music/status`` the engine's
state. ``POST <hub>/api/ai/stream`` with JSON ``{role, system, messages, max_tokens,
effort}`` answers with lines of JSON (NDJSON): ``{"model": label}`` first, ``{"text": piece}``
as the answer comes, ``{"done": true, "stop": reason}`` last; ``{"error": message}`` when it
fails (as the status of a 4xx/5xx answer, or as a line once the answer has begun).
``GET <hub>/api/ai/status?role=`` says what answers a role: ``{"ok", "model", "provider",
"local"}``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator, Sequence

import httpx

if TYPE_CHECKING:
    import reflex as rx

logger = logging.getLogger(__name__)

AUDIENCE = "ai-hub"
STREAM_PATH = "/api/ai/stream"
STATUS_PATH = "/api/ai/status"
ROLES = ("smart", "fast", "local", "coder")
USES = (*ROLES, "music")  # what an app may say it uses: the roles, and the music engine
MUSIC_PATH = "/api/ai/music"
MUSIC_STATUS_PATH = "/api/ai/music/status"
HUB_SUBDOMAIN = "ai"
_TOKEN_TTL = 300
_CACHE_SECONDS = 30.0
_LOOKUP_TIMEOUT = 3.0
STATUS_TIMEOUT = 5.0
# An answer may take its minutes (a local model on a big notebook); connecting may not.
TIMEOUT = httpx.Timeout(300.0, connect=5.0)
# A song takes longer still: the engine may have to load its models first.
MUSIC_TIMEOUT = httpx.Timeout(1800.0, connect=5.0)

_hub_cache: tuple[float, str] = (0.0, "")
# For tests: a transport for every client made here (None: the network).
_transport: httpx.AsyncBaseTransport | None = None
# What this app asks each role for, in plain words (``uses``).
_uses: dict[str, str] = {}


class HubUnavailable(Exception):
    """There is no AI Hub here, or it couldn't answer."""


# ------------------------------------------------------------ finding the hub


def _clear_cache() -> None:
    global _hub_cache
    _hub_cache = (0.0, "")


async def hub_url() -> str:
    """The hub's backend, e.g. ``http://smart-app-ai:8000`` ("" when there is none).

    ``DDNS_AI_HUB_URL`` names it outright (for running outside the platform); else the hub is
    looked up in re-ddns's service list as the app ``ai``, and the answer kept for a while."""
    global _hub_cache
    fixed = os.environ.get("DDNS_AI_HUB_URL", "").strip().rstrip("/")
    if fixed:
        return fixed
    registry = os.environ.get("RE_DDNS_API_URL", "").strip().rstrip("/")
    if not registry:
        return ""
    if time.monotonic() - _hub_cache[0] < _CACHE_SECONDS:
        return _hub_cache[1]
    url = ""
    try:
        async with httpx.AsyncClient(timeout=_LOOKUP_TIMEOUT, transport=_transport) as client:
            r = await client.get(f"{registry}/api/service/list")
            r.raise_for_status()
            for s in r.json():
                if s.get("subdomain") == HUB_SUBDOMAIN and s.get("upstream_host"):
                    url = f"http://{s['upstream_host']}:{s.get('backend_port', 8000)}"
                    break
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as e:
        logger.info("Cannot look up the AI Hub: %s", e)
    _hub_cache = (time.monotonic(), url)
    return url


# ------------------------------------------------------------ the token


def own_app() -> str:
    """This app's subdomain, as the App Store installed it ("" outside the platform)."""
    return os.environ.get("SERVICE_SUBDOMAIN", "").strip().lower()


def _secret() -> str:
    secret = os.environ.get("DDNS_AUTH_SECRET", "")
    if not secret:
        raise HubUnavailable("DDNS_AUTH_SECRET is not set, so the AI Hub can't be asked")
    return secret


def token(user: str = "", app: str = "", ttl: int = _TOKEN_TTL) -> str:
    """The bearer token for asking the hub as ``app`` (this app by default) for ``user``."""
    import jwt

    app = (app or own_app() or "app").strip().lower()
    now = int(time.time())
    claims = {
        "iss": app,
        "aud": AUDIENCE,
        "app": app,
        "sub": user or app,
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, _secret(), algorithm="HS256")


def verify(authorization: str) -> dict[str, Any]:
    """For the hub: the claims of a request's ``Authorization`` header (``app``: the app asking,
    ``sub``: the person); raises PermissionError for anything else."""
    import jwt

    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value:
        raise PermissionError("asking the AI Hub needs Authorization: Bearer <token>")
    try:
        claims = jwt.decode(
            value,
            _secret(),
            algorithms=["HS256"],
            audience=AUDIENCE,
            options={"require": ["exp", "aud", "sub", "app"]},
        )
    except HubUnavailable as e:
        raise PermissionError(str(e)) from e
    except jwt.PyJWTError as e:
        raise PermissionError(f"invalid token: {e}") from e
    return claims


# ------------------------------------------------------------ asking


async def status(role: str = "smart", user: str = "") -> dict[str, Any] | None:
    """What answers ``role`` here: ``{"ok": True, "model": label, "provider", "local"}``, or
    ``{"ok": False, "error": why}`` when the hub has nothing for it; None without a hub. With
    ``user``, what answers that person (the hub's owner may have a model of their own)."""
    base = await hub_url()
    if not base:
        return None
    try:
        async with httpx.AsyncClient(timeout=STATUS_TIMEOUT, transport=_transport) as client:
            r = await client.get(f"{base}{STATUS_PATH}", params={"role": role}, headers=_auth(user))
            data = r.json()
    except HubUnavailable as e:
        return {"ok": False, "error": str(e)}
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "error": f"the AI Hub at {base} didn't answer: {e}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "the AI Hub gave an odd answer"}
    if r.status_code >= 400 or not data.get("ok"):
        return {"ok": False, "error": str(data.get("error") or f"HTTP {r.status_code}")}
    return data


def _auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(user)}"}


class Reply:
    """An answer as the hub streams it: iterate for its text; ``model`` and ``stop`` are known
    once said (``stop``: "end_turn", "max_tokens" or "refusal")."""

    def __init__(self, body: dict[str, Any], user: str) -> None:
        self._body = body
        self._user = user
        self.model = ""
        self.stop = ""

    def __aiter__(self) -> AsyncIterator[str]:
        return self._run()

    async def _run(self) -> AsyncIterator[str]:
        base = await hub_url()
        if not base:
            raise HubUnavailable("there is no AI Hub here")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=_transport) as client:
                async with client.stream("POST", f"{base}{STREAM_PATH}", json=self._body, headers=_auth(self._user)) as r:
                    if r.status_code >= 400:
                        raise HubUnavailable(_error_of(await r.aread(), r.status_code))
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if event.get("error"):
                            raise HubUnavailable(str(event["error"]))
                        if event.get("model"):
                            self.model = str(event["model"])
                        piece = event.get("text")
                        if piece:
                            yield str(piece)
                        if event.get("done"):
                            self.stop = str(event.get("stop") or "end_turn")
                            return
        except httpx.HTTPError as e:
            raise HubUnavailable(f"the AI Hub at {base} didn't answer: {e}") from e
        if not self.stop:
            raise HubUnavailable("the AI Hub stopped before the answer was done")


def _error_of(body: bytes, status: int) -> str:
    try:
        return str(json.loads(body).get("error") or f"HTTP {status}")
    except (ValueError, AttributeError):
        return f"the AI Hub answered HTTP {status}"


def stream(
    role: str,
    system: str,
    messages: Sequence[dict[str, Any]],
    *,
    max_tokens: int = 4096,
    effort: str = "medium",
    user: str = "",
) -> Reply:
    """The hub's answer for ``role``, streamed (see :class:`Reply`). ``effort`` ("low",
    "medium", "high") is a hint the models that take one follow; ``user`` is the person asking,
    for the hub's log."""
    body = {
        "role": role,
        "system": system,
        "messages": [{"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")} for m in messages],
        "max_tokens": int(max_tokens),
        "effort": effort,
    }
    return Reply(body, user)


async def complete(
    role: str,
    system: str,
    messages: Sequence[dict[str, Any]],
    *,
    max_tokens: int = 4096,
    effort: str = "medium",
    user: str = "",
) -> str:
    """The whole answer at once ("" when the model declined)."""
    reply = stream(role, system, messages, max_tokens=max_tokens, effort=effort, user=user)
    text = "".join([piece async for piece in reply])
    return "" if reply.stop == "refusal" else text


# ------------------------------------------------------------ music


async def music_status(user: str = "") -> dict[str, Any] | None:
    """The music engine as the hub sees it: ``{"ok": True, "installed": True, "server": "ready",
    "tier", ...}``, ``{"ok": False, "why": ...}`` when there is none or it isn't installed yet;
    None without a hub."""
    base = await hub_url()
    if not base:
        return None
    try:
        async with httpx.AsyncClient(timeout=STATUS_TIMEOUT, transport=_transport) as client:
            r = await client.get(f"{base}{MUSIC_STATUS_PATH}", headers=_auth(user))
            data = r.json()
    except HubUnavailable as e:
        return {"ok": False, "why": str(e)}
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "why": f"the AI Hub at {base} didn't answer: {e}"}
    if not isinstance(data, dict):
        return {"ok": False, "why": "the AI Hub gave an odd answer"}
    if r.status_code >= 400:
        return {"ok": False, "why": str(data.get("error") or f"HTTP {r.status_code}")}
    return data


class Song:
    """A song as the engine makes it: iterate for its progress (``{"status": words, "secs"}``,
    the last event ``{"done": True, "id", "secs", "metas", "models"}``), then ``audio()`` for
    the MP3. ``id``, ``metas`` and ``models`` are known once it is done."""

    def __init__(self, spec: dict[str, Any], user: str) -> None:
        self._spec = spec
        self._user = user
        self.id = ""
        self.secs = 0.0
        self.metas: dict[str, Any] = {}
        self.models: dict[str, Any] = {}

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._run()

    async def _run(self) -> AsyncIterator[dict[str, Any]]:
        base = await hub_url()
        if not base:
            raise HubUnavailable("there is no AI Hub here")
        try:
            async with httpx.AsyncClient(timeout=MUSIC_TIMEOUT, transport=_transport) as client:
                async with client.stream("POST", f"{base}{MUSIC_PATH}", json=self._spec, headers=_auth(self._user)) as r:
                    if r.status_code >= 400:
                        raise HubUnavailable(_error_of(await r.aread(), r.status_code))
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if event.get("error"):
                            raise HubUnavailable(str(event["error"]))
                        if event.get("done"):
                            self.id = str(event.get("id") or "")
                            self.secs = float(event.get("secs") or 0)
                            self.metas = dict(event.get("metas") or {})
                            self.models = dict(event.get("models") or {})
                            yield event
                            return
                        yield event
        except httpx.HTTPError as e:
            raise HubUnavailable(f"the AI Hub at {base} didn't answer: {e}") from e
        if not self.id:
            raise HubUnavailable("the AI Hub stopped before the song was done")

    async def audio(self) -> bytes:
        """The finished song, as MP3 bytes."""
        if not self.id:
            raise HubUnavailable("the song isn't done")
        base = await hub_url()
        try:
            async with httpx.AsyncClient(timeout=MUSIC_TIMEOUT, transport=_transport) as client:
                r = await client.get(f"{base}{MUSIC_PATH}/{self.id}/audio", headers=_auth(self._user))
        except httpx.HTTPError as e:
            raise HubUnavailable(f"the AI Hub at {base} didn't answer: {e}") from e
        if r.status_code >= 400:
            raise HubUnavailable(_error_of(r.content, r.status_code))
        return r.content


def music(spec: dict[str, Any], *, user: str = "") -> Song:
    """A song from the music engine, streamed as it is made (see :class:`Song`). ``spec``:
    ``caption`` (the music in words, English works best), ``lyrics`` ("[Instrumental]" for none),
    ``language`` of the singing (``zh``, ``en``, ``ja``...), ``duration`` (s), and to remake a
    recording ``task`` ("cover"), ``audio_b64`` + ``audio_format`` and ``cover_strength`` (0.2
    keeps the song and changes its style, 1 remakes it from its bones)."""
    return Song(dict(spec), user)


# ------------------------------------------------------------ what the app asks for


def uses(app: rx.App, **roles: str) -> None:
    """Says what this app asks each role for, in a few plain words (``smart="The assistant
    panel's answers"``), for the AI Hub's page. They are served with the app's tools, at
    :data:`reflex_ddns_auth.tools.MANIFEST_PATH`, where the hub reads them; calling again
    replaces them."""
    unknown = sorted(set(roles) - set(USES))
    if unknown:
        raise ValueError(f"unknown roles {unknown}: one of {', '.join(USES)}")
    from reflex_ddns_auth import tools

    _uses.clear()
    _uses.update({role: words.strip() for role, words in roles.items() if words.strip()})
    tools._add_routes(app)


def declared() -> dict[str, str]:
    """What :func:`uses` said, as the tool manifest serves it."""
    return dict(_uses)
