"""Provider side: expose a page of this app as an intent other apps can open.

    from reflex_ddns_auth.intent import intent, IntentPage

    @intent(app, action="call.join", on_open=CallState.open_intent,
            params={"room": str, "title": str}, required=("room",))
    def call_intent():
        return call_view()

``on_open`` receives the caller's params (``dict``). Without a ``params``
schema these are all query params, as strings. With one, only the declared
params arrive, converted to their type (``str``, ``int``, ``float`` or
``bool``); a missing ``required`` param or a bad value shows an error with a
Close button instead of calling ``on_open``. Params the caller passed with
``Intent.start(private=...)`` arrive the same way, after the dialog is ready.

Inside the page, ``IntentPage.is_active`` tells whether it is embedded in a
caller's dialog; ``IntentPage.finish(data)`` / ``IntentPage.cancel`` answer it.

An app with intents also serves its manifest at ``/_ddns_intent/manifest``;
re-ddns reads it to tell callers which app provides an action.
"""

import json
from typing import Any, Callable

import reflex as rx
from starlette.requests import Request
from starlette.responses import JSONResponse

from reflex_ddns_auth.intent.protocol import (
    ID_PARAM,
    ORIGIN_PARAM,
    PROTOCOL_VERSION,
    WAIT_PARAM,
    intent_route,
    is_trusted_origin,
    provider_bridge_js,
    provider_post_js,
)
from reflex_ddns_auth.intent.registry import MANIFEST_PATH

ROOT_ID = "ddns-intent-root"
PARAMS_BUTTON_ID = "ddns-intent-params"

_PARAM_TYPES = {str: "str", int: "int", float: "float", bool: "bool"}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

# action -> {"source", "title", "params", "required", "on_open"}
_actions: dict[str, dict[str, Any]] = {}


def _convert(value: str, kind: type) -> Any:
    if kind is bool:
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise ValueError(value)
    return kind(value)


def _check_params(action: str, raw: dict[str, str]) -> tuple[dict[str, Any], str]:
    """The params ``on_open`` gets, or an error message when they break the schema."""
    spec = _actions.get(action)
    if spec is None or spec["params"] is None:
        return dict(raw), ""
    params: dict[str, Any] = {}
    for name, kind in spec["params"].items():
        value = raw.get(name, "")
        if value == "":
            if name in spec["required"]:
                return {}, f"Missing required parameter: {name}"
            continue
        try:
            params[name] = _convert(value, kind)
        except ValueError:
            return {}, f"Invalid value for {name}: expected {_PARAM_TYPES[kind]}"
    return params, ""


def manifest() -> dict[str, Any]:
    """The actions this app provides, as served at ``MANIFEST_PATH``."""
    actions = {}
    for action, spec in _actions.items():
        schema = None
        if spec["params"] is not None:
            schema = {
                name: {"type": _PARAM_TYPES[kind], "required": name in spec["required"]}
                for name, kind in spec["params"].items()
            }
        actions[action] = {"title": spec["title"], "route": intent_route(action), "params": schema}
    return {"v": PROTOCOL_VERSION, "actions": actions}


async def _manifest_endpoint(request: Request) -> JSONResponse:
    return JSONResponse(manifest())


def _add_manifest_route(app: rx.App) -> None:
    api = getattr(app, "_api", None)
    if api is None or any(getattr(r, "path", None) == MANIFEST_PATH for r in api.routes):
        return
    api.add_route(MANIFEST_PATH, _manifest_endpoint, methods=["GET"])


class IntentPage(rx.State):
    """State of the intent page currently shown inside a caller's iframe."""

    action: str = ""
    params: dict[str, str] = {}
    call_id: str = ""
    # Why the page cannot open (bad params); shown instead of the page.
    error: str = ""
    _waiting: bool = False

    @rx.var
    def is_active(self) -> bool:
        """True when embedded by a trusted caller that is waiting for an answer.

        Also requires being on the intent page itself: this state can outlive the
        dialog (e.g. a session resumed elsewhere) and must not leak into other pages.
        """
        return self.call_id != "" and self.router.url.path == intent_route(self.action)

    def _open(self, raw: dict[str, str]) -> list:
        params, self.error = _check_params(self.action, raw)
        spec = _actions.get(self.action)
        if self.error or spec is None or spec["on_open"] is None:
            return []
        return [spec["on_open"](params)]

    @rx.event
    def init(self, action: str):
        query = dict(self.router.url.query_parameters)
        call_id = query.pop(ID_PARAM, "")
        parent_origin = query.pop(ORIGIN_PARAM, "")
        wait = query.pop(WAIT_PARAM, "") == "1"
        if not is_trusted_origin(parent_origin, own_origin=self.router.url.origin or ""):
            call_id = ""
        self.action = action
        self.params = query
        self.call_id = call_id
        self.error = ""
        # Private params come from a trusted caller only, once the bridge says ready.
        self._waiting = bool(call_id) and wait

        events = []
        if call_id:
            events.append(
                rx.call_script(
                    provider_bridge_js(parent_origin, call_id, ROOT_ID, PARAMS_BUTTON_ID, wait=self._waiting)
                )
            )
        if not self._waiting:
            events.extend(self._open(query))
        return events

    @rx.event
    def deliver(self, raw: str):
        """Receive the caller's private params and open the page with them."""
        if not self._waiting:
            return
        try:
            data = json.loads(raw or "null")
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        self._waiting = False
        private = {
            k: "" if v is None else v if isinstance(v, str) else json.dumps(v) for k, v in data.items()
        }
        return self._open({**self.params, **private})

    @rx.event
    def finish(self, data: dict):
        """Send ``data`` back to the caller's ``on_result`` and close the dialog."""
        return rx.call_script(provider_post_js("result", data))

    @rx.event
    def cancel(self):
        """Close the caller's dialog without a result."""
        return rx.call_script(provider_post_js("cancel"))

    @rx.event
    def cancel_with_error(self):
        """Close the caller's dialog, telling it why the page could not open."""
        return rx.call_script(provider_post_js("cancel", {"error": self.error}))


def _error_panel() -> rx.Component:
    return rx.el.div(
        rx.el.p("This dialog cannot open.", style={"fontWeight": "600", "margin": "0 0 4px"}),
        rx.el.p(IntentPage.error, style={"margin": "0 0 16px", "color": "#b91c1c"}),
        rx.el.button(
            "Close",
            on_click=IntentPage.cancel_with_error,
            style={
                "padding": "8px 16px",
                "border": "none",
                "borderRadius": "8px",
                "background": "#f3f4f6",
                "color": "#374151",
                "fontWeight": "600",
                "cursor": "pointer",
            },
        ),
        style={"padding": "24px 56px 24px 24px", "fontFamily": "system-ui, sans-serif"},
    )


def _layout(content: rx.Component) -> rx.Component:
    # The measured root must size to its content (no min-height), otherwise
    # the reported height would follow the iframe height instead.
    return rx.el.div(
        # Hidden button the bridge clicks to hand the private params to Python.
        rx.el.button(
            id=PARAMS_BUTTON_ID,
            on_click=IntentPage.deliver(
                rx.Var("JSON.stringify(window.__ddnsIntentPageInbox?.shift() ?? null)", _var_type=str)
            ),
            style={"display": "none"},
        ),
        rx.cond(IntentPage.error != "", _error_panel(), content),
        id=ROOT_ID,
    )


def intent(
    app: rx.App,
    action: str,
    on_open: Any = None,
    title: str | None = None,
    params: dict[str, type] | None = None,
    required: tuple[str, ...] | list[str] = (),
) -> Callable[[Callable[[], rx.Component]], Callable[[], rx.Component]]:
    """Register ``/intent/<action>`` on ``app`` rendering the decorated component.

    ``params`` maps each accepted param to ``str``, ``int``, ``float`` or
    ``bool``; ``required`` lists the ones that must be present.
    """
    if params is not None:
        unknown = [name for name, kind in params.items() if kind not in _PARAM_TYPES]
        if unknown:
            raise TypeError(f"Intent {action!r}: unsupported param types for {unknown}")
    missing = [name for name in required if params is None or name not in params]
    if missing:
        raise ValueError(f"Intent {action!r}: required params {missing} are not declared in params")

    def decorator(component: Callable[[], rx.Component]) -> Callable[[], rx.Component]:
        source = f"{component.__module__}.{component.__qualname__}"
        existing = _actions.get(action)
        # The same function registering again is a module reload, not a clash.
        if existing is not None and existing["source"] != source:
            raise ValueError(f"Intent action {action!r} is already registered by {existing['source']}")
        _actions[action] = {
            "source": source,
            "title": title or action,
            "params": dict(params) if params is not None else None,
            "required": tuple(required),
            "on_open": on_open,
        }
        _add_manifest_route(app)
        app.add_page(
            lambda: _layout(component()),
            route=intent_route(action),
            title=title or action,
            on_load=IntentPage.init(action),
        )
        return component

    return decorator
