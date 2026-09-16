"""Confirm prior remote provider calls before reusing their execution stage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rcp.agents.launcher import AgentProcessControl
from rcp.runs.turn_collection import can_collect

if TYPE_CHECKING:
    from rcp.storage import AppStore


def remote_stage_awaits_collection(store: AppStore, host: str, root: str) -> bool:
    """Whether this stage still holds a finished turn nobody has collected.

    Transient by construction: it clears as soon as that turn is collected.
    Work that can wait should wait rather than spend anything on it.
    """

    if not host or not root:
        return False
    for operation_id in store.uncollected_remote_task_ids():
        record = store.agent_task(operation_id)
        if (
            record is not None
            and record.stage_host == host
            and record.stage_root == root
            and can_collect(store, record)
        ):
            return True
    return False


def require_remote_provider_quiescence(store: AppStore, host: str, root: str) -> None:
    """A task status or SSH exit is never a substitute for remote process absence."""
    if remote_stage_awaits_collection(store, host, root):
        raise ValueError("Collect the prior provider turn before reusing this workspace.")
    for operation_id, pid_file in store.unresolved_remote_provider_passes(host, root):
        stopped = AgentProcessControl.remote_stopped(host, pid_file)
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
