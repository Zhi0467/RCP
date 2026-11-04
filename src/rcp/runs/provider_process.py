"""Confirm prior remote provider calls before reusing their execution stage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rcp.agents.launcher import AgentProcessControl
from rcp.machine_cards import host_of

if TYPE_CHECKING:
    from rcp.storage import AppStore


def require_remote_provider_quiescence(store: AppStore, machine_id: str, root: str) -> None:
    """A task status or SSH exit is never a substitute for remote process absence.

    The card's current host is the route asked, so an edited host is asked afresh.

    A host the probe cannot reach raises `StateUnreachable`, the typed word for
    a lost link, so the task this fails is classified as transport loss rather
    than as an ordinary failure a reattempt could never fix.

    Every pass this reads belongs to a launch that has already ended, so a stage
    that still stands and holds no pidfile is the host reporting that the pass
    started nothing. Without that, a launch which failed before the remote ran
    would fence its conversation until someone cleared the record by hand.
    """
    passes = store.unresolved_remote_provider_passes(machine_id, root)
    try:
        host = host_of(store, machine_id) if passes else ""
    except KeyError as exc:
        raise ValueError(
            "The machine of a previous provider call was removed, so its process cannot be checked."
        ) from exc
    for operation_id, pid_file in passes:
        stopped = AgentProcessControl.remote_stopped(
            host, pid_file, raise_unreachable=True, absent_is_stopped=True
        )
        if stopped is not True:
            reason = (
                "A previous provider call is still running"
                if stopped is False
                else "The previous provider call's process state could not be verified"
            )
            raise ValueError(
                f"{reason}. Recovery cannot reuse this workspace until that call is confirmed stopped."
            )
        store.finish_remote_provider_pass(operation_id, pid_file)
        from rcp.runs.browser_lifecycle import finish_recorded_browser

        if not any(
            pending_id == operation_id
            for pending_id, _ in store.unresolved_remote_provider_passes(machine_id, root)
        ):
            finish_recorded_browser(store, operation_id)
