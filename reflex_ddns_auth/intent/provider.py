"""Provider side: expose a page of this app as an intent other apps can open.

    from reflex_ddns_auth.intent import intent, IntentPage

    @intent(app, action="profile.view", on_open=ProfileState.open_intent)
    def profile_intent():
        return profile_view()

``on_open`` receives the query params (``dict[str, str]``) the caller passed.
Inside the page, ``IntentPage.is_active`` tells whether it is embedded in a
caller's dialog; ``IntentPage.finish(data)`` / ``IntentPage.cancel`` answer it.
"""

from typing import Any, Callable

import reflex as rx

from reflex_ddns_auth.intent.protocol import (
    ID_PARAM,
    ORIGIN_PARAM,
    intent_route,
    is_trusted_origin,
    provider_bridge_js,
    provider_post_js,
)

ROOT_ID = "ddns-intent-root"

_on_open_handlers: dict[str, Any] = {}


class IntentPage(rx.State):
    """State of the intent page currently shown inside a caller's iframe."""

    action: str = ""
    params: dict[str, str] = {}
    call_id: str = ""

    @rx.var
    def is_active(self) -> bool:
        """True when embedded by a trusted caller that is waiting for an answer."""
        return self.call_id != ""

    @rx.event
    def init(self, action: str):
        query = dict(self.router.url.query_parameters)
        call_id = query.pop(ID_PARAM, "")
        parent_origin = query.pop(ORIGIN_PARAM, "")
        if not is_trusted_origin(parent_origin, own_origin=self.router.url.origin or ""):
            call_id = ""
        self.action = action
        self.params = query
        self.call_id = call_id

        events = []
        if call_id:
            events.append(rx.call_script(provider_bridge_js(parent_origin, call_id, ROOT_ID)))
        on_open = _on_open_handlers.get(action)
        if on_open is not None:
            events.append(on_open(query))
        return events

    @rx.event
    def finish(self, data: dict):
        """Send ``data`` back to the caller's ``on_result`` and close the dialog."""
        return rx.call_script(provider_post_js("result", data))

    @rx.event
    def cancel(self):
        """Close the caller's dialog without a result."""
        return rx.call_script(provider_post_js("cancel"))


def _layout(content: rx.Component) -> rx.Component:
    # The measured root must size to its content (no min-height), otherwise
    # the reported height would follow the iframe height instead.
    return rx.el.div(content, id=ROOT_ID)


def intent(
    app: rx.App,
    action: str,
    on_open: Any = None,
    title: str | None = None,
) -> Callable[[Callable[[], rx.Component]], Callable[[], rx.Component]]:
    """Register ``/intent/<action>`` on ``app`` rendering the decorated component."""

    def decorator(component: Callable[[], rx.Component]) -> Callable[[], rx.Component]:
        if on_open is not None:
            _on_open_handlers[action] = on_open
        app.add_page(
            lambda: _layout(component()),
            route=intent_route(action),
            title=title or action,
            on_load=IntentPage.init(action),
        )
        return component

    return decorator
