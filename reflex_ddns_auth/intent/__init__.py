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
    intent_host()  # once per page, or install_intent_host(app) once for all pages

``on_result`` receives the provider's answer; ``on_cancel`` runs when the
dialog closes without one, with the ``reason``. ``app=None`` lets the
registry pick the app that provides the action; ``private`` params stay out
of the URL. Several dialogs can be open: one in front, and ``keep_alive`` ones
minimized to a tray where they keep running.
"""

from reflex_ddns_auth.intent.host import Intent, IntentState, install_intent_host, intent_host
from reflex_ddns_auth.intent.provider import IntentPage, intent
from reflex_ddns_auth.intent.registry import resolve_providers

__all__ = [
    "Intent",
    "IntentState",
    "intent_host",
    "install_intent_host",
    "IntentPage",
    "intent",
    "resolve_providers",
]
