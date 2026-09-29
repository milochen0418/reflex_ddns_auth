"""DDNS Intent: let *.reflex-ddns.com apps open each other's pages as dialogs.

Provider (the app that owns the page):

    @intent(app, action="profile.view", on_open=ProfileState.open_intent)
    def profile_intent():
        return profile_view()

Caller:

    rx.button("Profile", on_click=Intent.start("relack", "profile.view", user=email))
    intent_host()  # once per page
"""

from reflex_ddns_auth.intent.host import Intent, IntentState, intent_host
from reflex_ddns_auth.intent.provider import IntentPage, intent

__all__ = ["Intent", "IntentState", "intent_host", "IntentPage", "intent"]
