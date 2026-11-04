"""A machine is its space card; every other record names it by `machine_id`.

These are the shared reads and the one invalidation every owner uses, so no
caller looks a machine up by host or keeps its own copy of a route.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from rcp.storage import AppStore
    from rcp.storage.models import SpaceMachineRecord

BACKEND_MACHINE_ID = "backend"
"""The backend's own machine, the card with no host, in every space.

A fixed id lets a local manifest name the backend before any store exists.
"""


class LoginKey(NamedTuple):
    """One provider login on one space card; `login_id` "" is the machine sign-in."""

    provider: str
    machine_id: str
    login_id: str


def card(store: AppStore, machine_id: str) -> SpaceMachineRecord:
    """The space card for `machine_id`; KeyError when the space has none.

    The backend card always exists: it is made on first read.
    """

    if machine_id == BACKEND_MACHINE_ID:
        return store.backend_space_machine()
    return store.space_machine(machine_id)


def host_of(store: AppStore, machine_id: str) -> str:
    """The card's SSH host, "" for the backend's own machine."""

    return card(store, machine_id).host


def invalidate_machine(
    store: AppStore,
    machine_id: str,
    *,
    project_ids: Iterable[str],
    forget_display_snapshot: Callable[[str], None],
) -> None:
    """Forget everything learned about the card's old route after its host or account changed.

    Clears provider readiness, skill inventories and every project's compute
    probes on the card, then each listed project's display snapshot. Durable
    records hold only the id, so nothing else is rewritten or torn down.
    """

    store.clear_machine_caches(machine_id)
    for project_id in project_ids:
        forget_display_snapshot(project_id)
