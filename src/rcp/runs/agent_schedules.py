"""Member-requested private-thread schedules and combined wake dispatch."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rcp.limits import AGENT_SCHEDULE_MIN_GAP_MINUTES, AGENT_SCHEDULES_MAX_ACTIVE
from rcp.storage.agent_schedules import AgentSchedule, local_instant, repeat_fire, resume_fire
from rcp.storage.agent_threads import bound_thread

if TYPE_CHECKING:
    from rcp.storage.agent_threads import AgentThreadRecord

logger = logging.getLogger(__name__)

_MIN_GAP = timedelta(minutes=AGENT_SCHEDULE_MIN_GAP_MINUTES)
_MINUTES_PER_DAY = 24 * 60

# Store refusals keep their codes; the agent also gets what to do next.
_STORE_REASONS = {
    "schedule_limit": f"this thread already has {AGENT_SCHEDULES_MAX_ACTIVE} active "
    "schedules; cancel one first",
    "schedule_key_conflict": "this --key already names a different schedule; use a new key",
    "schedule_not_active": "only an active schedule pauses; run schedule list",
    "schedule_not_paused": "only a paused schedule resumes; run schedule list",
}


class ScheduleRefused(ValueError):
    """A refusal code with the plain reason the agent acts on."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.reason = reason


def _explain(exc: Exception) -> str:
    if isinstance(exc, KeyError):
        return "schedule_not_found: no schedule with that id on this thread; run schedule list"
    code = str(exc)
    return f"{code}: {_STORE_REASONS[code]}" if code in _STORE_REASONS else code


def _require_zone(timezone: str) -> None:
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleRefused(
            "schedule_timezone_invalid", f"{timezone!r} is not an IANA zone such as Europe/Berlin"
        ) from exc


def validate_schedule(
    *,
    cron: str | None,
    at: str | None,
    timezone: str,
    after: datetime,
    min_gap: timedelta | None = None,
    every_minutes: int | None = None,
    start: str | None = None,
    until: str | None = None,
    count: int | None = None,
) -> datetime:
    """The first fire, refusing repeats whose consecutive fires are closer than `min_gap`."""
    _require_zone(timezone)
    if sum(value is not None for value in (cron, every_minutes, at)) != 1:
        raise ScheduleRefused(
            "schedule_time_required", "give exactly one of --cron, --every or --at"
        )
    if at is not None:
        if (start, until, count) != (None, None, None):
            raise ScheduleRefused(
                "schedule_repeat_only", "--from, --until and --count need --cron or --every"
            )
        return _one_shot(at, timezone, after)
    anchor = local_instant(_local(start, "--from"), timezone) if start else None
    if every_minutes is not None:
        if min_gap is not None and timedelta(minutes=every_minutes) < min_gap:
            raise _too_close()
        first = repeat_fire(
            cron=None, every_minutes=every_minutes, timezone=timezone, after=after, anchor=anchor
        )
    else:
        assert cron is not None
        try:
            first = _cron_first_fire(cron, timezone, after, min_gap, anchor)
        except ScheduleRefused:
            raise
        except ValueError as exc:
            raise ScheduleRefused("schedule_cron_invalid", str(exc)) from exc
    if until and first > local_instant(_local(until, "--until"), timezone):
        raise ScheduleRefused(
            "schedule_until_before_first",
            "--until is before the first fire; pick a later --until or an earlier --from",
        )
    return first


def every_minutes(every: str) -> int:
    """`--every` such as 1m, 90m, 2h or 1d, in whole minutes."""
    match = re.fullmatch(r"([1-9][0-9]{0,5})([mhd])", every.strip())
    if match is None:
        raise ScheduleRefused(
            "schedule_every_invalid", "--every is a count and unit, such as 1m, 90m, 2h or 1d"
        )
    return int(match.group(1)) * {"m": 1, "h": 60, "d": _MINUTES_PER_DAY}[match.group(2)]


def _local(value: str, flag: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value) is None:
        raise ScheduleRefused("schedule_time_invalid", f"{flag} must be local YYYY-MM-DDTHH:MM")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ScheduleRefused(
            "schedule_time_invalid", f"{flag} {value!r} is not a calendar time"
        ) from exc
    return value


def _too_close() -> ScheduleRefused:
    return ScheduleRefused(
        "schedule_too_frequent",
        f"a wake may only schedule fires at least {AGENT_SCHEDULE_MIN_GAP_MINUTES} minutes "
        "apart; ask the member, whose own message may schedule more often",
    )


def _cron_first_fire(
    cron: str,
    timezone: str,
    after: datetime,
    min_gap: timedelta | None,
    anchor: datetime | None,
) -> datetime:
    from rcp.consolidation import cron_fields, next_occurrence

    fields = cron_fields(cron)
    first = repeat_fire(
        cron=cron, every_minutes=None, timezone=timezone, after=after, anchor=anchor
    )
    if min_gap is None:
        return first
    daily = len(fields[0]) * len(fields[1])
    if daily > _MINUTES_PER_DAY // AGENT_SCHEDULE_MIN_GAP_MINUTES:
        raise _too_close()
    fire = first
    # Two days of fires cover every gap, including the one across midnight.
    for _ in range(2 * daily + 1):
        following = next_occurrence(cron, timezone, fire)
        if following - fire < min_gap:
            raise _too_close()
        fire = following
    return first


def _one_shot(at: str, timezone: str, after: datetime) -> datetime:
    due = local_instant(_local(at, "--at"), timezone)
    if due <= after:
        raise ScheduleRefused(
            "schedule_in_past", "that local time has passed; pick a later --at in the member's zone"
        )
    return due


def member_timezone(store, task) -> str | None:
    """The turn's composer zone, else the member's latest composer zone.

    Requests carry the zone as the browser reported it, so a value that is not an
    IANA zone counts as unknown rather than failing the launch.
    """
    zone = _iana_zone(task.request.get("timezone"))
    if zone or task.authorized_by is None:
        return zone
    with store.connection() as connection:
        row = connection.execute(
            "SELECT json_extract(request_json, '$.timezone') FROM graph_runs "
            "WHERE authorized_user_id=? AND json_extract(request_json, '$.timezone') IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (task.authorized_by.user_id,),
        ).fetchone()
    return _iana_zone(row[0]) if row else None


def _iana_zone(zone: object) -> str | None:
    if not isinstance(zone, str) or not zone:
        return None
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return zone


def schedule_clock(timezone: str | None, now: datetime) -> dict[str, str | None]:
    if timezone is None:
        return {"timezone": None, "now_utc": now.isoformat(timespec="minutes")}
    local = now.astimezone(ZoneInfo(timezone))
    return {"timezone": timezone, "now_local": local.isoformat(timespec="minutes")}


def member_turn(store, task) -> bool:
    """A turn carrying the member's own words, rather than a wake or follow-up.

    A delivered handoff speaks for the member only when the Space turn that sent
    it did; one sent from a scheduled wake stays a wake.
    """
    source = task.request.get("source")
    if not (
        bool(str(task.request.get("message") or "").strip())
        and task.request.get("trigger", "human") == "human"
        and source in (None, "voice", "handoff")
        and store.question_for_followup(task.operation_id) is None
    ):
        return False
    if source != "handoff":
        return True
    handoff = store.agent_handoff_delivered_as(task.operation_id)
    sender = store.agent_task(handoff.source_operation_id) if handoff else None
    return sender is not None and member_turn(store, sender)


def schedule_instructions(store, operation_id: str, usages: tuple[str, ...] = ()) -> str:
    """The one schedule rule, rendered from the clock and gap enforcement uses."""
    from rcp.consolidation import CRON_FORMAT

    task = store.agent_task(operation_id)
    timezone = member_timezone(store, task) if task else None
    clock = schedule_clock(timezone, datetime.fromisoformat(store.now()))
    # The same decision `_add_schedule` enforces, so the turn knows its own limit.
    limit = (
        "This turn carries the member's own message, so a repeat may fire as often as "
        "every minute. "
        if task is not None and member_turn(store, task)
        else f"This turn is a wake, so cron or --every fires must be at least "
        f"{AGENT_SCHEDULE_MIN_GAP_MINUTES} minutes apart; the member's own message may "
        "schedule more often. "
    )
    text = (
        "Add, pause, resume or cancel a schedule when the member asks for a reminder or a "
        "recurring check; any turn, including a wake, may do so. "
        + limit
        + f"Cron: {CRON_FORMAT}. Bound one repeating schedule with --until or "
        f"--count rather than adding one-time schedules ({AGENT_SCHEDULES_MAX_ACTIVE} active "
        "at most); local times are in the schedule's zone. "
        "Pass --timezone only when the member named a zone; otherwise their zone applies"
        + ("" if timezone else "; it is unknown, so ask the member first")
        + f". Member clock: {json.dumps(clock)}."
    )
    return text + (" Commands: " + "; ".join(usages) if usages else "")


def schedule_thread(store, operation_id: str) -> dict | None:
    """The open thread this turn belongs to, while its member still sees the project."""
    task = store.agent_task(operation_id)
    thread = bound_thread(store, task) if task is not None else None
    if thread is None or thread.closed_at is not None:
        return None
    if thread.project_id and not store.is_project_member(thread.project_id, thread.user_id):
        return None
    return thread.model_dump()


def lineage_root(store, task) -> str:
    """Resume and Retry continue one lineage, so a re-run re-adds idempotently."""
    seen = {task.operation_id}
    while task.parent_operation_id and task.parent_operation_id not in seen:
        parent = store.agent_task(task.parent_operation_id)
        if parent is None:
            break
        seen.add(parent.operation_id)
        task = parent
    return task.operation_id


def _add_schedule(store, task, thread: dict, command) -> dict[str, object]:
    args = command.arguments
    timezone = args.timezone or member_timezone(store, task)
    if not timezone:
        raise ScheduleRefused(
            "schedule_timezone_required",
            "the member's time zone is unknown; ask the member, then pass --timezone",
        )
    now = datetime.fromisoformat(store.now())
    interval = every_minutes(args.every) if args.every is not None else None
    due = validate_schedule(
        cron=args.cron,
        at=args.at,
        timezone=timezone,
        after=now,
        min_gap=None if member_turn(store, task) else _MIN_GAP,
        every_minutes=interval,
        start=args.start,
        until=args.until,
        count=args.count,
    )
    schedule = store.add_agent_schedule(
        thread_id=thread["thread_id"],
        operation_id=lineage_root(store, task),
        key=command.idempotency_key,
        cron=args.cron,
        at=args.at,
        timezone=timezone,
        note=args.note,
        next_fire_at=due.isoformat(),
        every_minutes=interval,
        start=args.start,
        until=args.until,
        count=args.count,
    )
    return {"schedule": schedule.model_dump(mode="json"), **schedule_clock(timezone, now)}


def _schedule_result(store, task, thread: dict, command) -> dict[str, object]:
    args = command.arguments
    if args.action == "add":
        return _add_schedule(store, task, thread, command)
    if args.action == "cancel":
        store.cancel_agent_schedule(thread["thread_id"], args.schedule_id)
        return {"cancelled": args.schedule_id}
    if args.action == "pause":
        store.pause_agent_schedule(thread["thread_id"], args.schedule_id)
        return {"paused": args.schedule_id}
    if args.action == "resume":
        schedule = resume_schedule(store, thread["thread_id"], args.schedule_id)
        return {"schedule": schedule.model_dump(mode="json")}
    return {
        "schedules": [
            item.model_dump(mode="json") for item in store.agent_schedules(thread["thread_id"])
        ],
        **schedule_clock(member_timezone(store, task), datetime.fromisoformat(store.now())),
    }


def resume_schedule(store, thread_id: str, schedule_id: str) -> AgentSchedule:
    """Resume at the next fire after now; missed fires are skipped, and none left retires it."""
    schedule = thread_schedule(store, thread_id, schedule_id)
    due = resume_fire(schedule, datetime.fromisoformat(store.now()))
    return store.reactivate_agent_schedule(
        thread_id, schedule_id, due.isoformat() if due else None, was="paused"
    )


def thread_schedule(store, thread_id: str, schedule_id: str) -> AgentSchedule:
    schedule = next(
        (item for item in store.agent_schedules(thread_id) if item.schedule_id == schedule_id),
        None,
    )
    if schedule is None:
        raise KeyError(schedule_id)
    return schedule


def handle_schedule(store, operation_id: str, command):
    from rcp.agents.command_protocol import CommandResponse

    try:
        thread = schedule_thread(store, operation_id)
        task = store.agent_task(operation_id)
        if thread is None or task is None:
            raise ScheduleRefused(
                "schedule_thread_required",
                "schedules live on the member's private agent thread, which this turn is not",
            )
        result = _schedule_result(store, task, thread, command)
    except (ValueError, KeyError) as exc:
        return CommandResponse(
            request_id=command.request_id, status="invalid", message=_explain(exc)
        )
    return CommandResponse(
        request_id=command.request_id,
        status="ok",
        message="Schedules." if command.arguments.action == "list" else "Schedule updated.",
        result=result,
    )


def schedule_wake_message(schedules, now: datetime) -> str:
    """Each note's due time and now, in its own zone, and the missed fires the advance counted."""
    notes = []
    for schedule in schedules:
        due = datetime.fromisoformat(schedule.pending_since)
        assert schedule.pending_fires is not None
        zone = ZoneInfo(schedule.timezone)
        notes.append(
            {
                "schedule_id": schedule.schedule_id,
                "note": schedule.note,
                "timezone": schedule.timezone,
                "due_at": due.astimezone(zone).isoformat(timespec="minutes"),
                "now": now.astimezone(zone).isoformat(timespec="minutes"),
                "missed_fires": schedule.pending_fires - 1,
            }
        )
    return "Scheduled member requests (combined after downtime or a busy turn):\n" + json.dumps(
        notes, ensure_ascii=False
    )


def _queued_schedule_wakes(store) -> list[tuple[str, bool]]:
    """Claimed, unlaunched wakes and whether their thread is still open."""
    with store.connection() as connection:
        return [
            (row["operation_id"], row["closed_at"] is None)
            for row in connection.execute(
                "SELECT operation_id, agent_threads.closed_at FROM graph_runs JOIN agent_threads "
                "ON agent_threads.thread_id=json_extract(request_json, '$.chat_id') "
                "WHERE status='queued' AND json_extract(request_json, '$.trigger')='schedule'"
            ).fetchall()
        ]


def queued_schedule_wakes(store) -> list[str]:
    """Claimed schedule wakes not yet launched; they own their consumed notes."""
    return [operation_id for operation_id, _ in _queued_schedule_wakes(store)]


def _relaunch_queued_wakes(poller) -> None:
    # A crash or transient launch refusal after the atomic claim keeps the same
    # queued operation; a closed thread settles it instead of leaving it queued.
    for operation_id, open_thread in _queued_schedule_wakes(poller.store):
        try:
            if open_thread:
                poller.tasks.launch_admitted(operation_id)
            else:
                poller.store.fail_agent_task(
                    operation_id, "The agent thread closed before this scheduled wake started."
                )
        except Exception:
            logger.exception("Queued agent schedule launch failed: %s", operation_id)


def _thread_available(store, thread) -> bool:
    if thread is None or thread["closed_at"] is not None:
        return False
    return not thread["project_id"] or store.is_project_member(
        thread["project_id"], thread["user_id"]
    )


def _dispatch_thread(poller, thread_id: str, schedules, now: datetime) -> None:
    from rcp.runs.agent_thread_commands import _thread_busy
    from rcp.runs.provider_login import AdmissionRefused
    from rcp.service import RunRequest
    from rcp.storage import AgentTaskAdmissionConflict

    store = poller.store
    with store.connection() as connection:
        thread = connection.execute(
            "SELECT * FROM agent_threads WHERE thread_id=?", (thread_id,)
        ).fetchone()
        if not _thread_available(store, thread):
            # Done one-shots keep their pending note; it fires if access returns.
            connection.execute(
                "UPDATE agent_schedules SET state='disabled', disabled_reason='access_lost', "
                "pending_since=NULL, next_fire_at=NULL WHERE thread_id=? AND state='active'",
                (thread_id,),
            )
            return
    record = store.agent_thread(thread_id)
    # A wake waits for the current turn to settle or be resumed; notes stay pending.
    if record is None or _thread_busy(store, record):
        return
    try:
        task = schedule_wake_task(poller, record, schedule_wake_message(schedules, now), now)
        poller.tasks.admit_provider_task(
            task.project_id,
            RunRequest.model_validate(task.request),
            authorized_by=task.authorized_by,
        )
        claimed = store.claim_agent_schedule_wake(schedules, task)
    except (AdmissionRefused, AgentTaskAdmissionConflict, ValueError) as exc:
        # Signed out, machine offline, archived, or moving: keep the notes and say why.
        store.defer_agent_schedules(schedules, str(exc))
        return
    if claimed:
        poller.tasks.launch_admitted(task.operation_id)


def dispatch_agent_schedules(poller, now: datetime, pending) -> None:
    from collections import defaultdict

    _relaunch_queued_wakes(poller)
    groups = defaultdict(list)
    for schedule in pending:
        groups[schedule.thread_id].append(schedule)
    for thread_id, schedules in groups.items():
        try:
            _dispatch_thread(poller, thread_id, schedules, now)
        except Exception:
            logger.exception("Agent schedule wake failed for thread %s", thread_id)


def _latest_member_turn(store, thread: AgentThreadRecord, project_id: str):
    """The thread's latest turn, bound to its still-present member."""
    user = store.space_user(thread.user_id)
    if user is None or user.removed_at is not None or user.removal_started_at is not None:
        raise ValueError("schedule_member_unavailable")
    with store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM graph_runs WHERE project_id=? AND json_extract(request_json, '$.chat_id')=? "
            "ORDER BY created_at DESC, operation_id DESC LIMIT 1",
            (project_id, thread.thread_id),
        ).fetchone()
    if row is None:
        raise ValueError("schedule_thread_has_no_admitted_turn")
    previous = store._agent_task_record(row)
    # A restored (history-only) turn still names the binding; session resolution
    # records the fresh start when its session cannot continue.
    if previous.authorized_by is None or previous.authorized_by.user_id != user.user_id:
        raise ValueError("schedule_thread_binding_unavailable")
    return previous


def schedule_wake_task(poller, thread: AgentThreadRecord, message: str, now: datetime):
    """Continue the thread's latest Work binding, like a question follow-up."""
    from uuid import uuid4

    from rcp.runs.agent_thread_commands import thread_agent
    from rcp.runs.task_policy import resolved_dispatch_authority
    from rcp.service import RunRequest
    from rcp.storage import AgentTaskAdmissionConflict, AgentTaskRecord

    project_id = thread.project_id or poller.store.space_id
    if refusal := poller.store.chat_turn_refusal(project_id, thread.thread_id):
        raise AgentTaskAdmissionConflict(refusal)
    previous = _latest_member_turn(poller.store, thread, project_id)
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
