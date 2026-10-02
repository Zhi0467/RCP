"""Paid Experiment answer wakes keep the episode's saved authority and session."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rcp.runs.questions import reconcile_question_receipt
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord
from rcp.storage.question_models import question_followup_operation_id

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks


def reconcile_experiment_question_answers(
    tasks: BackgroundAgentTasks, *, project_id: str | None = None
) -> dict[str, str]:
    """Settlement/startup/API seam; refusals stay inspectable and questions stay unclaimed.

    Settlement and the answer API pass their project; startup passes none to sweep all.
    """
    store = tasks.store
    results: dict[str, str] = {}
    for question in store.questions_needing_experiment_reconciliation(project_id=project_id):
        origin = question.origin
        episode = store.episode(origin.owner_id)
        if episode is None or episode.mode != "experiment_loop":
            continue
        reconcile_question_receipt(store, question.question_id)
        question = store.get_question(question.question_id)
        assert question is not None
        if question.followup_operation_id is not None:
            claimed = store.agent_task(question.followup_operation_id)
            if claimed is not None and claimed.status == "queued":
                try:
                    tasks.launch_admitted(claimed.operation_id)
                    results[question.question_id] = claimed.operation_id
                except (ValueError, RuntimeError) as exc:
                    results[question.question_id] = str(exc)
            elif claimed is None:
                results[question.question_id] = "question_followup_missing"
            elif claimed.status in {"failed", "paused", "interrupted"}:
                results[question.question_id] = (
                    claimed.error or claimed.status_message or claimed.status
                )
            continue
        if (
            question.state != "answered"
            or question.client_receipt_revision is not None
            or question.followup_operation_id is not None
            or question.withdrawn_readonly
        ):
            continue
        while (continuation := store.episode_continuation(episode.episode_id)) is not None:
            episode = continuation
        asking = store.agent_task(origin.operation_id)
        if asking is None:
            results[question.question_id] = "question_origin_missing"
            continue
        try:
            request = RunRequest.model_validate(asking.request).model_copy(
                update={
                    "session_id": origin.native_session_id,
                    "message": question.answer or "\n".join(question.chosen_choices),
                    "trigger": "watcher",
                    "watcher_ids": [],
                    "control_episode_id": episode.episode_id,
                    "control_invocation": episode.invocations_used + 1,
                    "control_invocation_ceiling": episode.invocation_ceiling,
                    "attachments": [],
                    "attachment_batch_id": None,
                    "attachment_set_id": None,
                    "attachment_client_id": None,
                }
            )
            # Continuation admission may have refreshed graph pins under explicit human authority.
            # Use that episode's pinned values, retaining the asking turn's execution binding.
            latest = store.experiment_episode(episode.episode_id)
            if latest is not None and latest.last_turn_operation_id:
                bound = store.agent_task(latest.last_turn_operation_id)
                if bound is not None:
                    from rcp.storage.models import _EXPERIMENT_EPISODE_PINNED_FIELDS

                    request = request.model_copy(
                        update={
                            key: bound.request[key]
                            for key in _EXPERIMENT_EPISODE_PINNED_FIELDS
                            if key in bound.request
                        }
                    )
            now = store.now()
            record = AgentTaskRecord(
                operation_id=question_followup_operation_id(
                    question.question_id, question.answer_revision
                ),
                project_id=origin.project_id,
                episode_id=episode.episode_id,
                kind="node_chat",
                status="queued",
                request=request.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
                status_message="Queued Experiment question answer",
                native_session_id=origin.native_session_id,
                stage_host=origin.stage_host,
                stage_root=origin.stage_root,
                graph_target=origin.graph_target,
                authorized_by=episode.authorized_by,
                dispatch_authority=resolve_dispatch_authority("node_chat", request),
                runtime_id=asking.runtime_id,
            )
            admitted = store.create_experiment_question_invocation(
                record, question_id=question.question_id, answer_revision=question.answer_revision
            )
            if admitted is None:
                results[question.question_id] = "deferred"
                continue
            tasks.launch_admitted(admitted.operation_id)
            results[question.question_id] = admitted.operation_id
        except (ValueError, RuntimeError) as exc:
            results[question.question_id] = str(exc)
    return results
