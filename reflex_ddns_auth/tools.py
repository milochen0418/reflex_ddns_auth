"""Tools: what an AI may do in this app, for the AI Hub and, through it, for any agent.

    from reflex_ddns_auth.tools import tool, image

    @tool(app, "cad.run_preview", kind="write",
          description="Builds the room's model and shows it in every tab of the room.",
          params={"room": "The room, as in /room/<room>."})
    async def run_preview(ctx, room: str) -> dict:
        ...
        return {"ok": ok, "message": message}

A tool is an async function. Its parameters after ``ctx`` are what the AI passes, as JSON,
typed with ``str``, ``int``, ``float``, ``bool``, ``list[...]``, ``dict``, ``Literal[...]``
or ``X | None``; one with a default may be left out. ``params`` describes them for the AI,
the docstring or ``description`` the tool. What the tool returns (anything JSON-able, or
``image(png_bytes)``) goes back to the AI; an exception becomes an error the AI sees.
``ctx.user`` is the person the AI acts for.

``kind`` says what the tool does to the app: ``"read"`` (nothing), ``"write"`` (changes
things, as a person editing would) or ``"danger"`` (deletes, or can't be undone); the AI Hub
asks the person before a ``"danger"`` tool runs.

The app serves its tools at ``GET /_ddns_tool/manifest`` and runs one at
``POST /_ddns_tool/call/<name>`` (body ``{"args": {...}}``), on its backend, where the AI Hub
reaches it over the Docker network. A call carries ``Authorization: Bearer <token>`` from
:func:`call_token`: a JWT signed with the platform's ``DDNS_AUTH_SECRET`` for this app and
this person, valid for a few minutes. Calls without one are refused.
"""

from __future__ import annotations

import base64
import inspect
import json
import logging
import os
import time
import types
import typing
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import reflex as rx
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
MANIFEST_PATH = "/_ddns_tool/manifest"
CALL_PATH = "/_ddns_tool/call/{name}"
AUDIENCE = "ddns-tool"
KINDS = ("read", "write", "danger")
IMAGE_KEY = "__ddns_image__"
_TOKEN_TTL = 300

# name -> {"fn", "title", "description", "kind", "input_schema", "params"}
_tools: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class ToolContext:
    """Who a tool call is for."""

    user: str  # the person the AI acts for
    caller: str  # what sent the call, e.g. "ai-hub"
    call_id: str = ""


def image(data: bytes, mime: str = "image/png") -> dict:
    """A picture as a tool result (the AI sees the picture, not its bytes)."""
    return {IMAGE_KEY: True, "mime": mime, "data": base64.b64encode(data).decode("ascii")}


def own_app() -> str:
    """This app's subdomain, as the App Store installed it ("" outside the platform)."""
    return os.environ.get("SERVICE_SUBDOMAIN", "").strip().lower()


def call_token(app: str, user: str, caller: str = "ai-hub", ttl: int = _TOKEN_TTL) -> str:
    """The bearer token for calling ``app``'s tools on behalf of ``user``."""
    import jwt

    now = int(time.time())
    claims = {
        "iss": caller,
        "aud": AUDIENCE,
        "app": app.strip().lower(),
        "sub": user,
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, _secret(), algorithm="HS256")


def _secret() -> str:
    secret = os.environ.get("DDNS_AUTH_SECRET", "")
    if not secret:
        raise PermissionError("DDNS_AUTH_SECRET is not set, so tools can't be called")
    return secret


def _verify(request: Request) -> dict[str, Any]:
    import jwt

    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise PermissionError("a tool call needs Authorization: Bearer <token>")
    try:
        claims = jwt.decode(
            token,
            _secret(),
            algorithms=["HS256"],
            audience=AUDIENCE,
            options={"require": ["exp", "aud", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise PermissionError(f"invalid tool token: {e}") from e
    me = own_app()
    if me and str(claims.get("app", "")).lower() != me:
        raise PermissionError("the tool token is for another app")
    return claims


# ------------------------------------------------------------ input schema

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _schema_of(hint: Any) -> dict[str, Any]:
    """JSON Schema for a parameter's type hint."""
    if hint in _JSON_TYPES:
        return {"type": _JSON_TYPES[hint]}
    if hint is Any or hint is inspect.Parameter.empty:
        return {}
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin is typing.Literal:
        values = list(args)
        schema: dict[str, Any] = {"enum": values}
        kinds = {type(v) for v in values}
        if len(kinds) == 1 and next(iter(kinds)) in _JSON_TYPES:
            schema["type"] = _JSON_TYPES[next(iter(kinds))]
        return schema
    if origin in (typing.Union, types.UnionType):
        options = [a for a in args if a is not type(None)]
        if len(options) == 1:
            return _schema_of(options[0])
        return {"anyOf": [_schema_of(a) for a in options]}
    if hint is list or origin is list:
        return {"type": "array", "items": _schema_of(args[0])} if args else {"type": "array"}
    if hint is dict or origin is dict:
        return {"type": "object"}
    raise TypeError(f"unsupported parameter type for a tool: {hint!r}")


def _input_schema(fn: Callable, params: dict[str, str]) -> dict[str, Any]:
    hints = typing.get_type_hints(fn)
    names = list(inspect.signature(fn).parameters.values())[1:]  # after ctx
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in names:
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise TypeError(f"tool {fn.__name__!r}: *args / **kwargs can't be described to an AI")
        schema = _schema_of(hints.get(p.name, inspect.Parameter.empty))
        if p.name in params:
            schema["description"] = params[p.name]
        if p.default is inspect.Parameter.empty:
            required.append(p.name)
        else:
            schema["default"] = p.default
        properties[p.name] = schema
    unknown = set(params) - set(properties)
    if unknown:
        raise ValueError(f"tool {fn.__name__!r}: params describes unknown parameters {sorted(unknown)}")
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _matches(value: Any, schema: dict[str, Any]) -> bool:
    if "enum" in schema:
        return value in schema["enum"]
    if "anyOf" in schema:
        return any(_matches(value, s) for s in schema["anyOf"])
    kind = schema.get("type")
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        items = schema.get("items")
        return isinstance(value, list) and (not items or all(_matches(v, items) for v in value))
    if kind == "object":
        return isinstance(value, dict)
    return True


def _check_args(spec: dict[str, Any], args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ValueError("args must be a JSON object")
    schema = spec["input_schema"]
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise ValueError(f"unknown arguments: {sorted(unknown)}")
    missing = [name for name in schema["required"] if name not in args]
    if missing:
        raise ValueError(f"missing arguments: {missing}")
    for name, value in args.items():
        if value is None and name not in schema["required"]:
            continue
        if not _matches(value, schema["properties"][name]):
            raise ValueError(f"argument {name!r} has the wrong type")
    return args


# ------------------------------------------------------------ endpoints


def manifest() -> dict[str, Any]:
    """The tools this app offers, as served at ``MANIFEST_PATH``."""
    return {
        "v": PROTOCOL_VERSION,
        "app": own_app(),
        "tools": [
            {
                "name": name,
                "title": spec["title"],
                "description": spec["description"],
                "kind": spec["kind"],
                "input_schema": spec["input_schema"],
            }
            for name, spec in _tools.items()
        ],
    }


async def _manifest_endpoint(request: Request) -> JSONResponse:
    return JSONResponse(manifest())


async def _call_endpoint(request: Request) -> JSONResponse:
    name = request.path_params.get("name", "")
    spec = _tools.get(name)
    if spec is None:
        return JSONResponse({"ok": False, "error": f"no tool named {name!r}"}, status_code=404)
    try:
        claims = _verify(request)
    except PermissionError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=401)
    try:
        raw = await request.body()
        body = json.loads(raw) if raw.strip() else {}
        args = _check_args(spec, body.get("args", {}) if isinstance(body, dict) else None)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    ctx = ToolContext(user=str(claims["sub"]), caller=str(claims.get("iss", "")), call_id=str(claims.get("jti", "")))
    try:
        result = await spec["fn"](ctx, **args)
    except Exception as e:  # noqa: BLE001 — the AI sees what went wrong
        logger.exception("Tool %s failed", name)
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return JSONResponse({"ok": True, "result": result})


def _add_routes(app: rx.App) -> None:
    api = getattr(app, "_api", None)
    if api is None:
        raise RuntimeError("this Reflex App has no backend API to serve tools on")
    paths = {getattr(r, "path", None) for r in api.routes}
    if MANIFEST_PATH not in paths:
        api.add_route(MANIFEST_PATH, _manifest_endpoint, methods=["GET"])
    if CALL_PATH not in paths:
        api.add_route(CALL_PATH, _call_endpoint, methods=["POST"])


def tool(
    app: rx.App,
    name: str,
    *,
    description: str | None = None,
    title: str | None = None,
    kind: str = "read",
    params: dict[str, str] | None = None,
) -> Callable[[Callable], Callable]:
    """Offer the decorated async function to the AI Hub as the tool ``name``."""
    if kind not in KINDS:
        raise ValueError(f"tool {name!r}: kind must be one of {KINDS}")

    def decorator(fn: Callable) -> Callable:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"tool {name!r} must be an async function")
        source = f"{fn.__module__}.{fn.__qualname__}"
        existing = _tools.get(name)
        # The same function registering again is a module reload, not a clash.
        if existing is not None and existing["source"] != source:
            raise ValueError(f"tool {name!r} is already registered by {existing['source']}")
        _tools[name] = {
            "fn": fn,
            "source": source,
            "title": title or name,
            "description": (description or inspect.getdoc(fn) or "").strip(),
            "kind": kind,
            "input_schema": _input_schema(fn, params or {}),
        }
        _add_routes(app)
        return fn

    return decorator
