"""Which owner finalizes a recorded result, and what each waiting task is owed.

The routing is one table of owner-owned launch contracts. A chat kind does not
identify its owner; the finalization context retained at launch does. Every
owner writes under the operation id that opened the pass.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from rcp.agents.launcher import AgentProcessControl
from rcp.runs.remote_reconciliation import (
    Reconciliation,
    read_remote_journal,
    reconcile_remote_pass,
)
from rcp.runs.tasks.discuss import (
    DISCUSS_FINALIZATION_CONTEXT_ROLE,
    finalize_recorded_discuss_result,
)
from rcp.runs.tasks.work import WORK_FINALIZATION_CONTEXT_ROLE, finalize_recorded_work_result

if TYPE_CHECKING:
    from rcp.agents.launcher import AgentLauncher
    from rcp.background import AgentTaskExecution
    from rcp.runs.recorded_turn import RecordedProviderTurn
    from rcp.service import ProjectService
    from rcp.storage import AgentTaskRecord, AgentTaskRequest, AppStore


class RecordedFinalizer(Protocol):
    """What every owner must accept to settle a turn it did not watch.

    The recorded pass itself, not an answer extracted from it. An owner handed a
    string would have to go back to the stage for the rest, and the stage is
    mutable -- which is the whole reason the record was verified once on the way
    in. Anything an owner needs about this turn is in that value or in the task
    it already owns.
    """

    def __call__(
        self,
        service: ProjectService,
        launcher: AgentLauncher,
        request: AgentTaskRequest,
        data_dir: Path,
        execution: AgentTaskExecution,
        recorded: RecordedProviderTurn,
    ) -> AsyncIterator[str]: ...


#: Retained finalization contract -> the owner that wrote and can consume it.
RECORDED_FINALIZERS: dict[str, RecordedFinalizer] = {
    WORK_FINALIZATION_CONTEXT_ROLE: finalize_recorded_work_result,
    DISCUSS_FINALIZATION_CONTEXT_ROLE: finalize_recorded_discuss_result,
}


def recorded_finalizer(store: AppStore, operation_id: str) -> RecordedFinalizer | None:
    owners = [
        finalizer
        for role, finalizer in RECORDED_FINALIZERS.items()
        if store.agent_task_contract(operation_id, role) is not None
    ]
    return owners[0] if len(owners) == 1 else None


@dataclass(frozen=True)
class WaitingTask:
    """One task holding a remote pass, and what reconciliation says to do."""

    record: AgentTaskRecord
    reconciliation: Reconciliation
    finalizer: RecordedFinalizer | None

    @property
    def actionable(self) -> bool:
        """Whether this can be acted on now, by an owner that exists."""

        if self.reconciliation.action in {"wait", "settled"}:
            return False
        return self.reconciliation.action == "fail" or self.finalizer is not None


def plan_remote_reconciliation(
    store: AppStore,
    *,
    stopped: Callable[[str, str], bool | None] = AgentProcessControl.remote_stopped,
    read_journal: Callable[[str, str], dict[str, object] | None] = read_remote_journal,
) -> list[WaitingTask]:
    """Ask the table, once, of every task waiting on a remote result.

    Planning is separated from doing so the decision can be read and tested
    without a host. A task with no retained owner contract is visibly left
    waiting rather than quietly failed.
    """

    planned = []
    for operation_id in store.operation_ids_awaiting_remote_result():
        record = store.agent_task(operation_id)
        if record is None:
            continue
        reconciliation = reconcile_remote_pass(
            store, record, stopped=stopped, read_journal=read_journal
        )
        finalizer = recorded_finalizer(store, operation_id)
        if reconciliation.action == "finalize" and finalizer is None:
            reconciliation = Reconciliation(
                "wait",
                f"No recorded-result owner is registered for a {record.kind} turn.",
                reconciliation.pid_file,
                reconciliation.recorded,
            )
        planned.append(WaitingTask(record, reconciliation, finalizer))
    return planned
