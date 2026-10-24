"""Change a SharedState room from outside its tabs' events, so that all its tabs see it.

    from reflex_ddns_auth import rooms

    async with rooms.change(CadState, "default") as cad:
        cad.output_message = "Built by the AI"

For a background job, a Starlette route or a tool call (see :mod:`reflex_ddns_auth.tools`).
Reflex passes a room's changes on to its tabs when they are made through a tab linked to the
room, and the room keeps the client tokens of the tabs that linked to it, so ``change`` goes
through one of them (ReflexGrid's ``push`` and dicom_viewer's ``with_room`` do the same with
lists of their own). With no tab in the room, it changes the room's own state: nobody sees it
now, the next tab that links does.

``room_cls`` is the ``rx.SharedState`` class and ``room_id`` the token tabs link it to
(``_link_to(room_id)``).
"""

from __future__ import annotations

import contextlib
import logging
from typing import AsyncIterator, TypeVar

import reflex as rx

logger = logging.getLogger(__name__)

RoomT = TypeVar("RoomT", bound=rx.State)


def _app():
    from reflex.utils.prerequisites import get_app

    return get_app().app


async def tabs(room_cls: type[RoomT], room_id: str) -> list[str]:
    """Client tokens of the tabs that linked to the room (some may have closed since)."""
    app = _app()
    root = await app.state_manager.get_state(rx.BaseStateToken(ident=room_id, cls=room_cls))
    room = await root.get_state(room_cls)
    return list(getattr(room, "_linked_from", None) or ())


@contextlib.asynccontextmanager
async def change(room_cls: type[RoomT], room_id: str) -> AsyncIterator[RoomT]:
    """The room's state, to change; everyone in the room sees the change when the block ends."""
    app = _app()
    for token in await tabs(room_cls, room_id):
        context = app.modify_state(rx.BaseStateToken(ident=token, cls=room_cls))
        try:
            root = await context.__aenter__()
        except Exception as e:  # noqa: BLE001 — that tab's state can't be had: try the next one
            logger.warning("Cannot reach a tab of room %s: %s", room_id, e)
            continue
        try:
            room = await root.get_state(room_cls)
            linked = getattr(room, "_linked_to", "") == room_id
        except BaseException as error:
            if not await context.__aexit__(type(error), error, error.__traceback__):
                raise
            continue
        if not linked:  # the tab has moved on to another room
            await context.__aexit__(None, None, None)
            continue
        try:
            yield room
        except BaseException as error:
            if not await context.__aexit__(type(error), error, error.__traceback__):
                raise
        else:
            await context.__aexit__(None, None, None)
        return
    async with app.modify_state(rx.BaseStateToken(ident=room_id, cls=room_cls)) as root:
        yield await root.get_state(room_cls)
