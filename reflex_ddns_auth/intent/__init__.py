"""DDNS Intent: let *.reflex-ddns.com apps open each other's pages as dialogs.

Provider (the app that owns the page):

    @intent(app, action="profile.view", on_open=ProfileState.open_intent,
            params={"user": str}, required=("user",))
    def profile_intent():
        return profile_view()

Caller:

    rx.button("Profile", on_click=Intent.start("relack", "profile.view", user=email))
    rx.button("Pick", on_click=Intent.start("relack", "people.pick", on_result=S.picked))
    rx.button("Call", on_click=Intent.start(None, "call.join", private={"room": rid},
                                            keep_alive=True, single=True))
    rx.button("Call", on_click=Intent.choose_app("call.join", on_result=S.app_chosen))
    intent_host()  # once per page, or install_intent_host(app) once for all pages

``on_result`` receives the provider's answer; ``on_update`` its news while
the dialog stays open (the provider sends them with ``intent_update(data)``);
``on_cancel`` runs when the dialog closes without an answer, with the
``reason``. ``app=None`` lets the
registry pick the app that provides the action (the user chooses when several
do); ``Intent.choose_app`` only asks, and hands ``{"app": ...}`` to its
``on_result``, so that everyone joining the same session (a call) can then
start that very app by name. ``private`` params stay out of the URL. Several
dialogs can be open: one in front, and ``keep_alive`` ones minimized to a tray
where they keep running; dragged up from there (or with their ⧉ button), one
becomes a small picture-in-picture window, and its page is told so
(``<html data-intent-view="pip">``).

"Open with" for collection items: a provider declares the item kinds an
action opens (``@intent(..., accepts=("dicom.series",))``, the ref arrives as
the ``ref`` param), and ``await resolve_openers(kind)`` lists those actions.

What the host page shows now, for a dialog that wants to talk about it (the AI
Hub's talking AI, say): the page sets ``window.__ddnsContext`` (a value or a
function, maybe async) and the dialog asks with ``window.__ddnsIntentPage.ask()``;
or the host sends it with ``Intent.context(action, data)``. A provider declared
with ``@intent(..., on_context=State.handler)`` gets it in Python too. See
``reflex_ddns_auth.companion`` for the one-line button that calls that AI up.
"""

from reflex_ddns_auth.intent.host import Intent, IntentState, install_intent_host, intent_host
from reflex_ddns_auth.intent.provider import IntentPage, intent, intent_update
from reflex_ddns_auth.intent.registry import resolve_openers, resolve_providers

__all__ = [
    "Intent",
    "IntentState",
    "intent_host",
    "install_intent_host",
    "IntentPage",
    "intent",
    "intent_update",
    "resolve_openers",
    "resolve_providers",
]
