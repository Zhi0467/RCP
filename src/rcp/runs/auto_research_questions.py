"""Orchestrator questions use ordinary human mail and paid mail admission."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from rcp.limits import AUTO_RESEARCH_MAIL_MAX_MESSAGES, AUTO_RESEARCH_QUESTION_SNAPSHOT_MAX_RECORDS
from rcp.storage import AppStore, AutoResearchMessageRecord, EpisodeNotRunning
from rcp.storage.question_models import QuestionOrigin

if TYPE_CHECKING:
    from rcp.runs.auto_research import AutoResearchCommandContext


def orchestrator_question_origin(context: AutoResearchCommandContext) -> QuestionOrigin:
    from rcp.runs.auto_research import AutoResearchCommandUnavailable

    task = context.task
    if (
        not task.native_session_id
        or not task.stage_root
        or not task.write_scope_fingerprint
        or context.request.provider is None
    ):
        raise AutoResearchCommandUnavailable(
            "The orchestrator session and write scope are not bound."
        )
    return QuestionOrigin(
        owner_kind="episode",
        project_id=task.project_id,
        owner_id=context.episode.episode_id,
        operation_id=task.operation_id,
        provider=context.request.provider,
        native_session_id=task.native_session_id,
        stage_root=task.stage_root,
        stage_host=task.stage_host,
        capability="orchestrate",
        write_scope_fingerprint=task.write_scope_fingerprint,
        graph_target=task.graph_target,
    )


def _answer_mail_id(question_id: str, revision: int) -> str:
    return f"question:{question_id}:answer:{revision}"


def record_auto_research_question_answer(
    store: AppStore, question_id: str
) -> AutoResearchMessageRecord | None:
    """API entry point after a human resolution; dismissal never creates mail.

    The stable message id makes retries and concurrent requests idempotent. The
    ordinary mail reconciler owns wake admission, budget and busy-session deferral.
    Full answer data lives in the mail itself, including when the orchestrator
    harvests it during the asking turn rather than waiting for a later wake.
    """
    from rcp.runs.auto_research_delivery import record_auto_research_message

    question = store.get_question(question_id)
    if question is None:
        raise KeyError(question_id)
    if question.origin.owner_kind != "episode" or question.state != "answered":
        return None
    episode = store.episode(question.origin.owner_id)
    if episode is None or episode.mode != "auto_research":
        return None
    message_id = _answer_mail_id(question_id, question.answer_revision)
    existing = store.readdress_auto_research_question_answer(question_id)
    if existing is not None:
        return existing
    while continuation := store.episode_continuation(episode.episode_id):
        episode = continuation
    if (
        question.withdrawn_readonly
        or episode.status != "running"
        or episode.ending is not None
        or episode.stop_requested_at is not None
        or episode.root_operation_id is None
    ):
        return None
    try:
        return record_auto_research_message(
            store,
            message_id=message_id,
            episode_id=episode.episode_id,
            sender_role="human",
            sender_task_id=None,
            authorized_by=question.resolved_by,
            recipient_task_id=episode.root_operation_id,
            body=json.dumps(
                {
                    "question_id": question_id,
                    "answer_revision": question.answer_revision,
                    "answer": question.answer,
                    "chosen_choices": question.chosen_choices,
                    "graph_authority": "none",
                    "epistemic_status": "hearsay",
                },
                ensure_ascii=False,
            ),
        )
    except EpisodeNotRunning:
        # The ending fence won the race. Keep the answer for reauthorization.
        return None
    except sqlite3.IntegrityError:
        existing = store.auto_research_message(message_id)
        if existing is None:
            raise
        return existing


def reconcile_auto_research_question_answers(store: AppStore) -> None:
    """Recover a crash between human resolution and mail insertion."""
    for question in store.list_questions(owner_kind="episode"):
        if question.state == "answered" and not question.withdrawn_readonly:
            record_auto_research_question_answer(store, question.question_id)


def auto_research_question_snapshot(
    store: AppStore, *, project_id: str, episode_id: str, operation_id: str
) -> tuple[str, list[str]]:
    """Fresh store data, never transcripts; only this wake's answers are included.

    Open/dismissed records and answers use bounded prefixes. Every field is
    bounded by the question schema; mail retains every full answer. Large
    snapshots are staged as input files rather than expanded into the prompt.
    """
    questions = store.episode_questions(project_id, episode_id)
    notices = [
        question
        for question in questions
        if not question.withdrawn_readonly
        and (
            question.state == "pending"
            or (question.state == "dismissed" and question.dismissal_delivered_at is None)
        )
    ]
    notices.sort(key=lambda question: question.state != "dismissed")
    selected = notices[:AUTO_RESEARCH_QUESTION_SNAPSHOT_MAX_RECORDS]
    answers = []
    for question in questions:
        if question.state != "answered":
            continue
        mail = store.auto_research_message(
            _answer_mail_id(question.question_id, question.answer_revision)
        )
        if mail is not None and mail.delivery_operation_id == operation_id:
            answers.append(question)
    records = [
        {
            "question_id": question.question_id,
            "state": question.state,
            "question": question.question,
            "choices": question.choices,
            "multiple": question.multiple,
            **(
                {
                    "answer": question.answer,
                    "chosen_choices": question.chosen_choices,
                    "answer_revision": question.answer_revision,
                }
                if question.state == "answered"
                else {}
            ),
        }
        for question in [*answers[:AUTO_RESEARCH_MAIL_MAX_MESSAGES], *selected]
    ]
    return json.dumps(
        {
            "graph_authority": "none",
            "epistemic_status": "hearsay",
            "questions": records,
            "additional_open_or_dismissed": len(notices) - len(selected),
            "additional_answered": max(0, len(answers) - AUTO_RESEARCH_MAIL_MAX_MESSAGES),
        },
        ensure_ascii=False,
    ), [question.question_id for question in selected if question.state == "dismissed"]


def mark_auto_research_question_snapshot_delivered(store: AppStore, operation_id: str) -> None:
    """A failed provider launch leaves dismissals eligible for the next wake."""
    receipts = [
        receipt
        for receipt in store.agent_task_receipts(operation_id)
        if receipt.category == "question_snapshot"
    ]
    if not receipts:
        return
    for question_id in receipts[-1].payload.get("dismissal_ids", []):
        store.mark_question_dismissal_delivered(question_id)
