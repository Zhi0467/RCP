"""Member-requested private-thread schedules and combined wake dispatch."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from rcp.storage.agent_threads import AgentThreadRecord


def validate_schedule(
    *, cron: str | None, at: str | None, timezone: str, after: datetime
) -> datetime:
    from rcp.consolidation import cron_fields, next_occurrence, occurrence_at

    ZoneInfo(timezone)
    if (cron is None) == (at is None):
        raise ValueError("exactly one of cron or at is required")
    if cron is not None:
        cron_fields(cron)
        return next_occurrence(cron, timezone, after)
    assert at is not None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", at) is None:
        raise ValueError("at must be YYYY-MM-DDTHH:MM")
    local = datetime.fromisoformat(at)
    due = occurrence_at(local.date(), local.strftime("%H:%M"), timezone)
    if due <= after:
        raise ValueError("schedule must be in the future")
    return due


def schedule_thread(store, operation_id: str) -> dict | None:
    task = store.agent_task(operation_id)
    if task is None or task.authorized_by is None:
        return None
    with store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM agent_threads WHERE thread_id=? AND user_id=? AND closed_at IS NULL",
            (task.request.get("chat_id"), task.authorized_by.user_id),
        ).fetchone()
    if (
        row is not None
        and row["project_id"]
        and not store.is_project_member(row["project_id"], row["user_id"])
    ):
        return None
    return dict(row) if row else None


def handle_schedule(store, operation_id: str, command):
    from rcp.agents.command_protocol import CommandResponse

    try:
        thread = schedule_thread(store, operation_id)
        if thread is None:
            raise ValueError("schedule_thread_required")
        result: dict[str, object]
        args = command.arguments
        task = store.agent_task(operation_id)
        assert task is not None
        if args.action == "add":
            # Admission persists the member input and provenance on the task.
            # Display transcripts are not an authority source for runtime commands.
            if (
                not str(task.request.get("message") or "").strip()
                or task.request.get("trigger", "human") != "human"
                or task.request.get("source") not in (None, "voice", "handoff")
                or store.question_for_followup(operation_id) is not None
            ):
                raise ValueError("schedule_member_message_required")
            timezone = args.timezone or task.request.get("timezone")
            if not timezone:
                raise ValueError("schedule_timezone_required")
            previous = next(
                (
                    item
                    for item in store.agent_schedules(thread["thread_id"])
                    if item.created_operation_id == operation_id
                    and item.idempotency_key == command.idempotency_key
                ),
                None,
            )
            due = (
                datetime.fromisoformat(previous.next_fire_at or previous.created_at)
                if previous
                else validate_schedule(
                    cron=args.cron,
                    at=args.at,
                    timezone=timezone,
                    after=datetime.fromisoformat(store.now()),
                )
            )
            schedule = store.add_agent_schedule(
                thread_id=thread["thread_id"],
                operation_id=operation_id,
                key=command.idempotency_key,
                cron=args.cron,
                at=args.at,
                timezone=timezone,
                note=args.note,
                next_fire_at=due.isoformat(),
            )
            result = {"schedule": schedule.model_dump(mode="json")}
        elif args.action == "cancel":
            store.cancel_agent_schedule(thread["thread_id"], args.schedule_id)
            result = {"cancelled": args.schedule_id}
        else:
            result = {
                "schedules": [
                    item.model_dump(mode="json")
                    for item in store.agent_schedules(thread["thread_id"])
                ]
            }
        return CommandResponse(
            request_id=command.request_id,
            status="ok",
            message="Schedule updated." if args.action != "list" else "Schedules.",
            result=result,
        )
    except (ValueError, KeyError) as exc:
        return CommandResponse(request_id=command.request_id, status="invalid", message=str(exc))


def schedule_wake_message(schedules, now: datetime) -> str:
    import json

    from rcp.consolidation import cron_occurrence_count

    notes = []
    for schedule in schedules:
        count = (
            cron_occurrence_count(
                schedule.cron,
                schedule.timezone,
                datetime.fromisoformat(schedule.pending_since),
                now,
            )
            if schedule.cron
            else 1
        )
        notes.append(
            {"schedule_id": schedule.schedule_id, "note": schedule.note, "missed_fires": count - 1}
        )
    return "Scheduled member requests (combined after downtime or a busy turn):\n" + json.dumps(
        notes, ensure_ascii=False
    )


def queued_schedule_wakes(store) -> list[str]:
    """Claimed schedule wakes not yet launched; they own their consumed notes."""
    with store.connection() as connection:
        return [
            row["operation_id"]
            for row in connection.execute(
                "SELECT operation_id FROM graph_runs JOIN agent_threads "
                "ON agent_threads.thread_id=json_extract(request_json, '$.chat_id') "
                "WHERE status='queued' AND json_extract(request_json, '$.trigger')='schedule'"
            ).fetchall()
        ]


def dispatch_agent_schedules(poller, now: datetime, pending) -> None:
    import logging
    from collections import defaultdict

    from rcp.runs.agent_thread_commands import _thread_busy
    from rcp.storage import AgentTaskAdmissionConflict

    # A crash or transient launch refusal after the atomic claim keeps the same
    # queued operation; never manufacture another wake for its consumed notes.
    for operation_id in queued_schedule_wakes(poller.store):
        try:
            poller.tasks.launch_admitted(operation_id)
        except Exception:
            logging.getLogger(__name__).exception(
                "Queued agent schedule launch failed: %s", operation_id
            )
    groups = defaultdict(list)
    for schedule in pending:
        groups[schedule.thread_id].append(schedule)
    for thread_id, schedules in groups.items():
        try:
            with poller.store.connection() as connection:
                thread = connection.execute(
                    "SELECT * FROM agent_threads WHERE thread_id=?", (thread_id,)
                ).fetchone()
                if (
                    thread is None
                    or thread["closed_at"] is not None
                    or (
                        thread["project_id"]
                        and not poller.store.is_project_member(
                            thread["project_id"], thread["user_id"]
                        )
                    )
                ):
                    connection.execute(
                        "UPDATE agent_schedules SET state='disabled', disabled_reason='access_lost', pending_since=NULL, next_fire_at=NULL WHERE thread_id=? AND state IN ('active','done')",
                        (thread_id,),
                    )
                    continue
            record = poller.store.agent_thread(thread_id)
            # A wake waits for the current turn to settle or be resumed; notes stay pending.
            if record is None or _thread_busy(poller.store, record):
                continue
            task = schedule_wake_task(poller, record, schedule_wake_message(schedules, now), now)
            if poller.store.claim_agent_schedule_wake(schedules, task):
                poller.tasks.launch_admitted(task.operation_id)
        except AgentTaskAdmissionConflict:
            # Archived, or an unresolved turn awaits the member; try again next poll.
            continue
        except Exception:
            logging.getLogger(__name__).exception(
                "Agent schedule wake failed for thread %s", thread_id
            )


def schedule_wake_task(poller, thread: AgentThreadRecord, message: str, now: datetime):
    """Continue the thread's latest admitted Work binding, like a question follow-up."""
    from uuid import uuid4

    from rcp.runs.agent_thread_commands import thread_agent
    from rcp.runs.task_policy import resolved_dispatch_authority
    from rcp.service import RunRequest
    from rcp.storage import AgentTaskAdmissionConflict, AgentTaskRecord

    user = poller.store.space_user(thread.user_id)
    if user is None or user.removed_at is not None or user.removal_started_at is not None:
        raise ValueError("schedule_member_unavailable")
    project_id = thread.project_id or poller.store.space_id
    if refusal := poller.store.chat_turn_refusal(project_id, thread.thread_id):
        raise AgentTaskAdmissionConflict(refusal)
    with poller.store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM graph_runs WHERE project_id=? AND json_extract(request_json, '$.chat_id')=? "
            "ORDER BY created_at DESC, operation_id DESC LIMIT 1",
            (project_id, thread.thread_id),
        ).fetchone()
    if row is None:
        raise ValueError("schedule_thread_has_no_admitted_turn")
    previous = poller.store._agent_task_record(row)
    if (
        previous.history_only
        or previous.authorized_by is None
        or previous.authorized_by.user_id != user.user_id
    ):
        raise ValueError("schedule_thread_binding_unavailable")
    request = RunRequest.model_validate(previous.request).model_copy(
        update={
            # The thread's stored pick, else its default agent.
            **thread_agent(poller.store, thread),
            "session_id": None,
            "browser_requested": poller.store.chat_browser_requested(project_id, thread.thread_id),
            "message": message,
            "trigger": "schedule",
            "source": None,
            "attachments": [],
            "references": [],
            "artifact_context": None,
            "artifact_edit": None,
            "attachment_batch_id": None,
            "attachment_set_id": None,
            "attachment_client_id": None,
            "watcher_ids": [],
        }
    )
    operation_id = str(uuid4())
    authority = resolved_dispatch_authority(
        poller.store,
        poller.tasks.dispatch_authority_resolver,
        previous.kind,
        request,
        project_id=project_id,
        operation_id=operation_id,
    )
    return AgentTaskRecord(
        operation_id=operation_id,
        project_id=project_id,
        kind=previous.kind,
        status="queued",
        status_message="Queued scheduled member requests",
        request=request.model_dump(mode="json"),
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        graph_target=previous.graph_target,
        runtime_id=previous.runtime_id,
        authorized_by=previous.authorized_by,
        dispatch_authority=authority,
    )
