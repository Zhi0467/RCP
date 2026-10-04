"""Apply again: re-apply a Work turn's retained Patch after canonical state was unreachable.

The agent does not run again. The retained Patch text is revalidated against
the graph as it is now, through the same Apply path and canonical-binding check
the turn used, and the outcome is recorded on the same task.
"""

from __future__ import annotations

import threading
import time
from typing import Literal, cast

from rcp.core.models import AuthorizedHuman
from rcp.limits import APPLY_AGAIN_RUN_LOCK_WAIT_SECONDS
from rcp.runs.chat import _append_chat_graph_receipt
from rcp.runs.task_policy import load_stored_request
from rcp.runs.tasks.work import (
    _apply_work_patch,
    _bounded_graph_messages,
    record_work_graph_failure,
)
from rcp.runs.tasks.work_turn_runtime import failed_graph_update
from rcp.service import ProjectService, RunRequest
from rcp.storage import AgentTaskRecord, AppStore
from rcp.transport import RunLockCancelled, StateUnavailable

_APPLY_AGAIN_GUARD = threading.Lock()


def apply_again_refusal(store: AppStore, record: AgentTaskRecord) -> str | None:
    """Why this task cannot apply its graph update again, or None when it can."""

    if store.consolidation_run_for_operation(record.operation_id) is not None:
        return "consolidation_continuation_forbidden"
    graph_update = record.result.get("graph_update") if record.result else None
    if not isinstance(graph_update, dict) or graph_update.get("status") != "unavailable":
        return "This task has no graph update waiting for canonical state."
    if record.history_only:
        return "This task is retained as history and cannot be controlled or continued."
    request = record.request
    if (
        record.status != "succeeded"
        or record.kind not in {"node_chat", "project_chat"}
        or request.get("mode") != "work"
        or request.get("patch_kind") == "experiment_loop"
        or store.auto_research_child_work_for_operation(record.operation_id) is not None
    ):
        return "Only an ordinary Work turn can apply its graph update again."
    refusal = _active_chat_refusal(store, record)
    if refusal is not None:
        return refusal
    # Only an `absent` commit is refused here. A `present` or `unknown` one may
    # already be in history; that is decided under the run lock, after refresh.
    if _commit_status(record) == "absent" and store.later_chat_turn_may_have_committed(record):
        return _LATER_TURN_REFUSAL
    if store.agent_task_patch_output(record.operation_id) is None:
        return "The retained patch is no longer available. Start a new Work turn instead."
    return None


_LATER_TURN_REFUSAL = (
    "A later turn in this chat applied, or may have applied, a graph update, so this "
    "older one cannot be applied after it. Start a new Work turn instead."
)


def _commit_status(record: AgentTaskRecord) -> object:
    graph_update = record.result.get("graph_update") if record.result else None
    return graph_update.get("commit_status") if isinstance(graph_update, dict) else None


def _active_chat_refusal(store: AppStore, record: AgentTaskRecord) -> str | None:
    chat_id = record.request.get("chat_id")
    if isinstance(chat_id, str) and store.has_active_chat_task(
        record.project_id,
        cast(Literal["node_chat", "project_chat"], record.kind),
        chat_id,
    ):
        return "Wait for the running turn in this chat to finish."
    return None


class _ApplyAgainRefused(Exception):
    """Raised under the run lock; deliberately not a ValueError, which Apply rejects."""


def apply_work_graph_update_again(
    service: ProjectService,
    store: AppStore,
    operation_id: str,
    *,
    authorized_by: AuthorizedHuman | None,
) -> AgentTaskRecord:
    """Apply one retained Work Patch once more and record the outcome on its task."""

    if not _APPLY_AGAIN_GUARD.acquire(blocking=False):
        raise ValueError("Another Apply again is already running. Try again when it finishes.")
    try:
        record = store.agent_task(operation_id)
        if record is None:
            raise KeyError(operation_id)
        refusal = apply_again_refusal(store, record)
        if refusal is not None:
            raise ValueError(refusal)
        assert record.result is not None
        expected = record.result["graph_update"]
        assert isinstance(expected, dict)
        patch_text = store.agent_task_patch_output(operation_id)
        assert patch_text is not None
        request = load_stored_request(RunRequest, record.request, operation_id=operation_id)
        assert isinstance(request, RunRequest)
        deadline = time.monotonic() + APPLY_AGAIN_RUN_LOCK_WAIT_SECONDS

        def recheck_chat_order() -> None:
            # A later turn can be admitted, and can commit, while this waits for
            # the lock. Under the lock no other commit can land until it is released.
            refusal = _active_chat_refusal(store, record)
            if refusal is not None:
                raise _ApplyAgainRefused(refusal)
            # The source-binding check must see a commit that landed while the link
            # was down, including through another device on a remote host.
            service.history.workspace.refresh()
            # This turn's own commit already landed: Apply again only records it,
            # so a later turn cannot be overtaken (invariants 6 and 6b).
            landed = any(
                item.source_operation_id == operation_id and item.admission == "accepted"
                for item in service.history.load_patches()
            )
            if not landed and store.later_chat_turn_may_have_committed(record):
                raise _ApplyAgainRefused(_LATER_TURN_REFUSAL)

        prior = expected.get("commit_status")
        prior_commit_status: Literal["present", "unknown"] | None = (
            "present" if prior == "present" else "unknown" if prior == "unknown" else None
        )
        try:
            # The same source binding as the turn: if the earlier commit did land,
            # the canonical-binding check records it instead of appending twice.
            result, failure = _apply_work_patch(
                service,
                None,
                patch_text,
                run_truth_scope=list(
                    request.run_truth_scope or service.manifest.agent.default_run_truth_scope
                ),
                source_operation_id=operation_id,
                cancelled=lambda: time.monotonic() > deadline,
                under_lock=recheck_chat_order,
                # A failure before this attempt reads history keeps what is known.
                prior_commit_status=prior_commit_status,
            )
        except _ApplyAgainRefused as exc:
            raise ValueError(str(exc)) from exc
        except RunLockCancelled as exc:
            raise ValueError(
                "Another graph-writing run holds canonical state. Try again when it finishes."
            ) from exc
        correction_rounds = expected.get("correction_rounds")
        rounds = correction_rounds if isinstance(correction_rounds, int) else 0
        if result is None:
            assert failure is not None
            graph_update = failed_graph_update(
                failure,
                bounded_messages=_bounded_graph_messages,
                correction_rounds=rounds,
                repairable=bool(
                    failure.correctable and record.native_session_id and record.stage_root
                ),
            )
        else:
            graph_update = result.model_copy(update={"correction_rounds": rounds})
        # Swap first: a refused swap must leave no receipt or event behind.
        store.replace_agent_task_graph_update(
            operation_id,
            expected=expected,
            graph_update=graph_update.model_dump(mode="json"),
        )
        if result is None:
            record_work_graph_failure(store, operation_id, graph_update)
        else:
            store.record_agent_task_event(
                operation_id,
                f"Apply again applied the retained graph update at revision "
                f"{graph_update.applied_revision}.",
            )
        store.record_agent_task_receipt(
            operation_id,
            "work_graph_update_applied_again",
            {
                "status": graph_update.status,
                "applied_revision": graph_update.applied_revision,
                "commit_status": graph_update.commit_status,
                "authorized_user_id": authorized_by.user_id if authorized_by else None,
            },
        )
        # Every outcome gets a receipt, so the chat's latest one offers the right
        # recovery (Repair after a rejection); earlier receipts stay as history.
        try:
            _append_chat_graph_receipt(
                service,
                request,
                record.native_session_id,
                graph_update,
                operation_id,
            )
        except (OSError, StateUnavailable, ValueError) as exc:
            store.record_agent_task_event(
                operation_id,
                f"Apply again completed but its chat receipt could not be written: {exc}",
                level="warning",
            )
        updated = store.agent_task(operation_id)
        assert updated is not None
        return updated
    finally:
        _APPLY_AGAIN_GUARD.release()
