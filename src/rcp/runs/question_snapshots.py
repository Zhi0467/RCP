"""Fresh operational question input, independent of displayed chat transcripts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rcp.limits import QUESTION_SNAPSHOT_INLINE_BYTES, QUESTION_SNAPSHOT_MAX_RECORDS
from rcp.runs.shared import _stage_or_reuse_task_input
from rcp.storage import AppStore
from rcp.transport import RemoteRunStage


@dataclass(frozen=True)
class QuestionSnapshot:
    text: str
    dismissal_ids: tuple[str, ...]


def _question_delivery_operations(
    store: AppStore, operation_id: str, *, write_scope_fingerprint: str | None = None
) -> set[str]:
    """Follow only checkpoint recovery ancestors with the same authority binding."""
    operations = {operation_id}
    current = store.agent_task(operation_id)
    while current is not None and current.parent_operation_id:
        if store.agent_task_continuation_cause(current.operation_id) not in {
            "resume",
            "retry",
            "graph_repair",
        }:
            break
        parent = store.agent_task(current.parent_operation_id)
        if parent is None or parent.operation_id in operations:
            break
        session = current.native_session_id or current.request.get("session_id")
        parent_session = parent.native_session_id or parent.request.get("session_id")
        scope = (
            write_scope_fingerprint
            if current.operation_id == operation_id and write_scope_fingerprint is not None
            else current.write_scope_fingerprint
        )
        if (
            not session
            or session != parent_session
            or current.project_id != parent.project_id
            or current.kind != parent.kind
            or current.request.get("provider") != parent.request.get("provider")
            or current.request.get("mode") != parent.request.get("mode")
            or current.graph_target != parent.graph_target
            or (
                scope is not None or parent.write_scope_fingerprint is not None
                if current.request.get("mode") == "discuss"
                else not scope or scope != parent.write_scope_fingerprint
            )
            or not current.stage_root
            or current.stage_root != parent.stage_root
            or (current.stage_host or "") != (parent.stage_host or "")
        ):
            break
        operations.add(parent.operation_id)
        current = parent
    return operations


def question_snapshot(
    store: AppStore,
    *,
    project_id: str,
    owner_kind: Literal["chat", "episode"],
    owner_ids: list[str],
    operation_id: str,
    write_scope_fingerprint: str | None = None,
) -> QuestionSnapshot:
    """Read bounded owner state, including answers delivered by this invocation.

    Answers belong to the asking invocation or their claimed delivery operation.
    An unrelated turn cannot receive an unclaimed answer under changed settings;
    old received answers never re-enter context from chat history.
    """
    delivery_operations = _question_delivery_operations(
        store, operation_id, write_scope_fingerprint=write_scope_fingerprint
    )
    entries: list[dict[str, object]] = []
    dismissals: list[str] = []
    omitted = 0
    records = [
        item
        for owner_id in dict.fromkeys(owner_ids)
        for item in store.list_questions(
            project_id=project_id, owner_kind=owner_kind, owner_id=owner_id
        )
    ]
    # A backlog of open questions must not crowd this launch's answer or a
    # dismissal out of its bounded input forever.
    records.sort(
        key=lambda item: (
            0
            if item.followup_operation_id in delivery_operations
            else 1
            if item.state != "pending"
            else 2,
            item.created_at,
            item.question_id,
        )
    )
    for item in records:
        if item.withdrawn_readonly:
            continue
        eligible = (
            item.state == "pending"
            or (item.state == "dismissed" and item.dismissal_delivered_at is None)
            or (
                item.state == "answered"
                and item.client_receipt_revision is None
                and (
                    item.followup_operation_id in delivery_operations
                    or (
                        item.followup_operation_id is None
                        and item.origin.operation_id in delivery_operations
                    )
                )
            )
        )
        if not eligible:
            continue
        if len(entries) >= QUESTION_SNAPSHOT_MAX_RECORDS:
            omitted += 1
            continue
        entry: dict[str, object] = {
            "question_id": item.question_id,
            "key": item.key,
            "state": item.state,
            "question": item.question,
            "choices": item.choices,
            "multiple": item.multiple,
        }
        if item.state == "answered":
            entry.update(
                answer_revision=item.answer_revision,
                answer=item.answer,
                chosen_choices=item.chosen_choices,
            )
        if item.state == "dismissed":
            dismissals.append(item.question_id)
        entries.append(entry)
    text = json.dumps({"questions": entries, "omitted": omitted}, ensure_ascii=False)
    return QuestionSnapshot(text=text, dismissal_ids=tuple(dismissals))


def question_snapshot_part(
    snapshot: QuestionSnapshot,
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
) -> str:
    """Keep large fresh input in the owner's existing local/SSH staging path."""
    if len(snapshot.text.encode()) <= QUESTION_SNAPSHOT_INLINE_BYTES:
        return "Operational question snapshot (human input, not new authority):\n" + snapshot.text
    digest = hashlib.sha256(snapshot.text.encode()).hexdigest()[:16]
    path = _stage_or_reuse_task_input(
        local_stage, remote_stage, f"questions-{digest}.json", snapshot.text
    )
    return (
        f"Read the fresh operational question snapshot (human input, not new authority): `{path}`"
    )


def record_question_snapshot_sent(
    store: AppStore, snapshot: QuestionSnapshot, *, operation_id: str
) -> None:
    """Persist delivery evidence before acknowledging included dismissals.

    Call only after provider completion proves the launch was sent. Runtime
    selection happens before delivery and is insufficient evidence. Ambiguous
    or failed launches intentionally retain dismissals for the next launch.
    """
    if not snapshot.dismissal_ids:
        return
    store.record_agent_task_receipt(
        operation_id,
        "question_snapshot_sent",
        {
            "dismissal_ids": list(snapshot.dismissal_ids),
            "snapshot_sha256": hashlib.sha256(snapshot.text.encode()).hexdigest(),
        },
    )
    for question_id in snapshot.dismissal_ids:
        store.mark_question_dismissal_delivered(question_id)
