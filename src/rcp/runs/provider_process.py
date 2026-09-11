"""Confirm prior remote provider calls before reusing their execution stage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rcp.agents.launcher import AgentProcessControl

if TYPE_CHECKING:
    from rcp.storage import AppStore


def require_remote_provider_quiescence(store: AppStore, host: str, root: str) -> None:
    """A task status or SSH exit is never a substitute for remote process absence."""
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
