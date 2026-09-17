"""Which owner finalizes a recorded result, and what each waiting task is owed.

The routing is one table. Task and episode kinds pick the owner that applies the
result; they never turn recovery into a new task attempt, and every owner writes
under the operation id that opened the pass.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rcp.runs.remote_reconciliation import Reconciliation, reconcile_remote_pass
from rcp.runs.tasks.work import finalize_recorded_work_result

if TYPE_CHECKING:
    from rcp.storage import AgentTaskKind, AgentTaskRecord, AppStore

#: Task kind -> the owner's recorded-result finalizer. A kind absent from this
#: table has no recorded path yet, and its pass keeps waiting rather than being
#: settled by an owner that never agreed to settle it.
RECORDED_FINALIZERS: dict[str, Callable[..., object]] = {
    "node_chat": finalize_recorded_work_result,
    "project_chat": finalize_recorded_work_result,
}


def recorded_finalizer(kind: AgentTaskKind) -> Callable[..., object] | None:
    return RECORDED_FINALIZERS.get(kind)


@dataclass(frozen=True)
class WaitingTask:
    """One task holding a remote pass, and what reconciliation says to do."""

    record: AgentTaskRecord
    reconciliation: Reconciliation
    finalizer: Callable[..., object] | None

    @property
    def actionable(self) -> bool:
        """Whether this can be acted on now, by an owner that exists."""

        if self.reconciliation.action in {"wait", "settled"}:
            return False
        return self.reconciliation.action == "fail" or self.finalizer is not None


def plan_remote_reconciliation(store: AppStore, **probes) -> list[WaitingTask]:
    """Ask the table, once, of every task waiting on a remote result.

    Planning is separated from doing so the decision can be read and tested
    without a host, and so a kind with no recorded owner is visibly left waiting
    rather than quietly failed.
    """

    planned = []
    for operation_id in store.operation_ids_awaiting_remote_result():
        record = store.agent_task(operation_id)
        if record is None:
            continue
        reconciliation = reconcile_remote_pass(store, record, **probes)
        finalizer = recorded_finalizer(record.kind)
        if reconciliation.action == "finalize" and finalizer is None:
            reconciliation = Reconciliation(
                "wait",
                f"No recorded-result owner is registered for a {record.kind} turn.",
                reconciliation.pid_file,
                reconciliation.recorded,
            )
        planned.append(WaitingTask(record, reconciliation, finalizer))
    return planned
