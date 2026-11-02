"""Scheduled, run-bound graph consolidation and replayable outcome settlement."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from rcp.agents.project_repositories import write_repositories
from rcp.background import BackgroundAgentTasks, StartupEffectFence
from rcp.core.transition_models import GraphTargetRef
from rcp.history import ReplayHalted
from rcp.history.queries import HeadSpec, HistoryEngine, StepRecord
from rcp.limits import AGENT_SCHEDULE_SEARCH_DAYS, CONSOLIDATION_POLL_SECONDS
from rcp.machine_sleep import seconds_until_automatic_launch
from rcp.runs.consolidation import CONSOLIDATION_PRELAUNCH_ERRORS, consolidation_skill_selection
from rcp.runs.evidence_flag_commands import hold_consolidation_additions
from rcp.runs.task_policy import resolved_dispatch_authority
from rcp.service import ProjectService, RunRequest
from rcp.storage import AgentTaskRecord, AppStore
from rcp.storage.consolidation import target_ref
from rcp.transport import StateUnavailable

if TYPE_CHECKING:
    from rcp.server_ops.maintenance import RuntimeAdmissionGate
    from rcp.storage.consolidation import (
        ConsolidationRun,
        ConsolidationSchedule,
        ConsolidationTarget,
    )

logger = logging.getLogger(__name__)


def consolidation_chat_id(project_id: str, target: str = "main") -> str:
    suffix = "" if target == "main" else f":{target}"
    return str(uuid5(NAMESPACE_URL, f"rcp:consolidation:{project_id}{suffix}"))


def occurrence_at(local_date: date, local_time: str, timezone: str) -> datetime:
    """Resolve a civil occurrence at its first fold, or first valid gap minute."""
    zone = ZoneInfo(timezone)
    candidate = datetime.combine(local_date, time.fromisoformat(local_time))
    while True:
        local = candidate.replace(tzinfo=zone, fold=0)
        instant = local.astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == candidate:
            return instant
        candidate += timedelta(minutes=1)


CRON_FORMAT = (
    "use five fields: minute hour day-of-month month day-of-week, each a number, "
    "range, step, or list (names like MON or JAN work), or @hourly, @daily, @weekly, "
    "@monthly, @yearly"
)
_CRON_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_CRON_FIELDS = (
    ("minute", 0, 59, ()),
    ("hour", 0, 23, ()),
    ("day-of-month", 1, 31, ()),
    (
        "month",
        1,
        12,
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
    ),
    ("day-of-week", 0, 7, ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")),
)


def cron_parts(expression: str) -> list[str]:
    """Five raw fields after alias expansion; names become numbers."""
    fields = _CRON_ALIASES.get(expression.strip().lower(), expression).split()
    if len(fields) != 5:
        raise ValueError(f"cron {expression!r} is not five fields; {CRON_FORMAT}")
    return [
        _numbered(field, names, low)
        for field, (_, low, _, names) in zip(fields, _CRON_FIELDS, strict=True)
    ]


def _numbered(field: str, names: tuple[str, ...], low: int) -> str:
    def number(match: re.Match[str]) -> str:
        name = match.group(0).upper()
        if name not in names:
            return match.group(0)
        return str(names.index(name) + (low if low == 1 else 0))

    return re.sub(r"[A-Za-z]{3}", number, field)


def _cron_span(base: str, stride: str | None, low: int, high: int) -> tuple[int, int]:
    if base == "*":
        return low, high
    bounds = base.split("-")
    # A stepped single start runs through the field maximum, as in cron.
    end = high if len(bounds) == 1 and stride is not None else int(bounds[-1])
    return int(bounds[0]), end


def _cron_field(field: str, name: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        match = re.fullmatch(r"(\*|[0-9]+(?:-[0-9]+)?)(?:/([0-9]+))?", part)
        if match is None:
            raise ValueError(f"cron {name} field {field!r} is invalid; {CRON_FORMAT}")
        base, stride = match.groups()
        step = int(stride or 1)
        start, end = _cron_span(base, stride, low, high)
        if step < 1 or not low <= start <= end <= high:
            raise ValueError(f"cron {name} field {field!r} must stay within {low}-{high}")
        values.update(range(start, end + 1, step))
    return values


def cron_fields(expression: str) -> tuple[set[int], ...]:
    """Parse five cron fields; Sunday is 0 or 7."""
    parsed = [
        _cron_field(field, name, low, high)
        for field, (name, low, high, _) in zip(cron_parts(expression), _CRON_FIELDS, strict=True)
    ]
    parsed[4] = {value % 7 for value in parsed[4]}
    return tuple(parsed)


def cron_matches_date(expression: str, fields: tuple[set[int], ...], local_date: date) -> bool:
    """Vixie cron: a day field starting with * is unrestricted, even when stepped."""
    dom = local_date.day in fields[2]
    dow = (local_date.weekday() + 1) % 7 in fields[4]
    raw = cron_parts(expression)
    restricted = not raw[2].startswith("*") and not raw[4].startswith("*")
    day_matches = (dom or dow) if restricted else (dom and dow)
    return local_date.month in fields[3] and day_matches


def cron_occurrence_count(expression: str, timezone: str, start: datetime, end: datetime) -> int:
    """Count distinct civil fires inclusively, without replaying missed minute jobs."""
    fields = cron_fields(expression)
    zone = ZoneInfo(timezone)
    day = start.astimezone(zone).date()
    last = end.astimezone(zone).date()
    times = _civil_times(fields)
    count = 0
    while day <= last:
        if cron_matches_date(expression, fields, day):
            midnight = datetime.combine(day, time(), zone)
            tomorrow = datetime.combine(day + timedelta(days=1), time(), zone)
            if (
                midnight.astimezone(UTC) >= start
                and tomorrow.astimezone(UTC) <= end
                and midnight.utcoffset() == tomorrow.utcoffset()
            ):
                count += len(times)
            else:
                # Gap times coalesce at the next valid minute, exactly as repeated
                # next_occurrence calls do; folds select their first instance.
                instants = {occurrence_at(day, civil, timezone) for civil in times}
                count += sum(start <= instant <= end for instant in instants)
        day += timedelta(days=1)
    return count


def _civil_times(fields: tuple[set[int], ...]) -> list[str]:
    return [f"{hour:02}:{minute:02}" for hour in sorted(fields[1]) for minute in sorted(fields[0])]


def _first_on(
    local_date: date, times: list[str], timezone: str, after: datetime
) -> datetime | None:
    local_after = after.astimezone(ZoneInfo(timezone))
    for civil_time in times:
        if local_date == local_after.date() and civil_time <= local_after.strftime("%H:%M"):
            continue
        candidate = occurrence_at(local_date, civil_time, timezone)
        if candidate > after:
            return candidate
    return None


def next_occurrence(local_time: str, timezone: str, after: datetime) -> datetime:
    """First daily or cron civil occurrence strictly after an aware instant."""
    if after.tzinfo is None:
        raise ValueError("schedule handling time must be timezone-aware")
    local_date = after.astimezone(ZoneInfo(timezone)).date()
    # A daily consolidation time is HH:MM; anything else is a cron expression.
    fields = None if ":" in local_time else cron_fields(local_time)
    times = [local_time] if fields is None else _civil_times(fields)
    for _ in range(AGENT_SCHEDULE_SEARCH_DAYS):
        matches = fields is None or cron_matches_date(local_time, fields, local_date)
        if matches and (candidate := _first_on(local_date, times, timezone, after)):
            return candidate
        local_date += timedelta(days=1)
    raise ValueError("schedule has no occurrence within eight years")


class ConsolidationPoller:
    def __init__(
        self,
        store: AppStore,
        tasks: BackgroundAgentTasks,
        *,
        service_for: Callable[[str], ProjectService],
        clock: Callable[[], str] | None = None,
        interval: float = CONSOLIDATION_POLL_SECONDS,
        admission: RuntimeAdmissionGate | None = None,
        startup_effect_fence: StartupEffectFence | None = None,
    ) -> None:
        self.store = store
        self.tasks = tasks
        self.service_for = service_for
        self.clock = clock or store.now
        self.interval = interval
        self.admission = admission
        self.startup_effect_fence = startup_effect_fence
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_lock = threading.Lock()
        # The last retryable launch error recorded per task, so a lasting failure
        # adds one event rather than one per poll.
        self._launch_errors: dict[str, str] = {}

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rcp-consolidation", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval if timeout is None else timeout)
            if not self._thread.is_alive():
                self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def poll_once(self) -> None:
        if self.startup_effect_fence is not None and self.startup_effect_fence.active:
            return
        if self.admission is not None and self.admission.closed:
            return
        with (
            self._poll_lock,
            self.admission.mutation("graph consolidation") if self.admission else nullcontext(),
        ):
            now = datetime.fromisoformat(self.clock())
            pending = self._advance_agent_schedules(now)
            cooling = seconds_until_automatic_launch() > 0
            # Wakes go first, each thread in its own error boundary, so SSH-bound
            # consolidation never delays them.
            if not cooling:
                self.dispatch_agent_schedules(now, pending)
            self.reconcile_outcomes()
            if not cooling:
                self.dispatch_due(now)

    def _advance_agent_schedules(self, now: datetime):
        """One unschedulable row is disabled; it never blocks consolidation."""
        try:
            return self.store.advance_agent_schedules(now)
        except Exception:
            logger.exception("Agent schedule advance failed; checking each due schedule")
        with self.store.connection() as connection:
            due = connection.execute(
                "SELECT schedule_id, cron, timezone FROM agent_schedules "
                "WHERE state='active' AND next_fire_at<=?",
                (now.isoformat(),),
            ).fetchall()
        for row in due:
            try:
                if row["cron"]:
                    next_occurrence(row["cron"], row["timezone"], now)
            except Exception:
                logger.exception("Disabling unschedulable agent schedule %s", row["schedule_id"])
                with self.store.connection() as connection:
                    connection.execute(
                        "UPDATE agent_schedules SET state='disabled', "
                        "disabled_reason='unschedulable', pending_since=NULL, next_fire_at=NULL "
                        "WHERE schedule_id=? AND state='active'",
                        (row["schedule_id"],),
                    )
        try:
            return self.store.advance_agent_schedules(now)
        except Exception:
            logger.exception("Agent schedules skipped this tick")
            return []

    def dispatch_agent_schedules(self, now: datetime, pending) -> None:
        from rcp.runs.agent_schedules import dispatch_agent_schedules

        dispatch_agent_schedules(self, now, pending)

    def dispatch_due(self, now: datetime) -> None:
        """Launch queued runs, then admit and launch every schedule due at `now`.

        The update rehearsal calls this on copied state at each due instant.
        """
        for run in self.store.consolidation_runs(unsettled_only=True):
            if run.operation_id:
                task = self.store.agent_task(run.operation_id)
                if task is not None and task.status == "queued":
                    self._launch(task.operation_id)
        for schedule in self.store.consolidation_schedules():
            if (
                datetime.fromisoformat(schedule.next_due_at) > now
                or datetime.fromisoformat(schedule.expires_at) <= now
            ):
                continue
            try:
                self._handle_due(schedule, now)
            except Exception:
                logger.exception("Consolidation admission failed for %s", schedule.project_id)

    def _handle_due(self, schedule: ConsolidationSchedule, now: datetime) -> None:
        targets = [("main", "main", None)] + [
            (branch.branch_id, branch.name, branch.parent_head.revision)
            for branch in self.store.graph_branches(schedule.project_id)
            if not branch.archived
        ]
        for target, name, initial_covered in targets:
            try:
                coverage = self.store.ensure_consolidation_target(
                    schedule, target=target, covered_head=initial_covered
                )
                if datetime.fromisoformat(coverage.next_due_at) <= now:
                    self._handle_target(schedule, now, target, name, coverage)
            except Exception:
                logger.exception(
                    "Consolidation admission failed for %s target %s", schedule.project_id, target
                )
        self.store.finish_consolidation_occurrence(
            schedule,
            targets=[target for target, _, _ in targets],
            next_due_at=next_occurrence(schedule.local_time, schedule.timezone, now).isoformat(),
        )

    def _handle_target(
        self,
        schedule: ConsolidationSchedule,
        now: datetime,
        target: str,
        name: str,
        coverage: ConsolidationTarget,
    ) -> None:
        if any(
            run.outcome_settled_at is None
            for run in self.store.consolidation_runs(
                schedule.project_id, target=target, unsettled_only=True
            )
        ):
            return
        due = datetime.fromisoformat(coverage.next_due_at)
        occurrence_date = due.astimezone(ZoneInfo(schedule.timezone)).date().isoformat()
        next_due = next_occurrence(schedule.local_time, schedule.timezone, now).isoformat()
        graph_target = target_ref(target)
        input_head = 0
        task = None
        execution_host = ""
        skipped = False
        error_code = None
        error_message = None
        failure_code = "consolidation_project_unavailable"
        try:
            self.store.require_project_accepts_new_work(schedule.project_id)
            service = self.service_for(schedule.project_id)
            failure_code = "history_unavailable"
            input_head, steps = _target_steps(service, graph_target)
            # Only a reported run checked its own commits; a failed run's edits
            # still need the next night.
            consolidation_operations = {
                run.operation_id
                for run in self.store.consolidation_runs(schedule.project_id)
                if run.operation_id is not None and run.kind == "report"
            }
            covered = coverage.covered_head
            changed = covered is None or any(
                step.after_head.revision > covered
                and step.patch.source_operation_id not in consolidation_operations
                for step in steps
            )
            if changed:
                failure_code = "consolidation_launch_unavailable"
                task = self._task(schedule, service, target, name)
                execution_host = service.manifest.machine_map[task.request["run_on"]].host
            else:
                skipped = True
        except (ValueError, KeyError, OSError, ReplayHalted, StateUnavailable) as exc:
            task = None
            error_code, error_message = failure_code, str(exc)
        run = self.store.claim_consolidation_occurrence(
            schedule,
            target=target,
            occurrence_date=occurrence_date,
            next_due_at=next_due,
            input_head=input_head,
            now=now.isoformat(),
            task=task,
            execution_host=execution_host,
            skipped=skipped,
            error_code=error_code,
            error_message=error_message,
        )
        if run is not None and run.operation_id is not None:
            self._launch(run.operation_id)

    def _task(
        self,
        schedule: ConsolidationSchedule,
        service: ProjectService,
        target: str,
        target_name: str,
    ) -> AgentTaskRecord:
        graph_target = target_ref(target)
        profile = service.resolve_agent_profile("project_chat")
        request = RunRequest(
            provider=profile.provider,
            model=profile.model,
            reasoning=profile.reasoning,
            run_on=profile.run_on,
            run_truth_scope=write_repositories(
                service.for_graph_target(graph_target, initialize=False).history.state()
            ),
            chat_scope="project",
            chat_id=consolidation_chat_id(schedule.project_id, target),
            session_id=None,
            mode="work",
            trigger="schedule",
            message=(
                f"Run the graph-consolidation workflow on branch {target_name}."
                if graph_target.kind == "branch"
                else "Run the graph-consolidation workflow on main."
            ),
            workflow_ids=["graph-consolidation"],
            skill_ids=[],
            invoked_workflow_ids=["graph-consolidation"],
        )
        selection = consolidation_skill_selection()
        request = request.model_copy(
            update={"resolved_skill_packages": selection.resolved_skill_packages}
        )
        operation_id = str(uuid4())
        authority = resolved_dispatch_authority(
            self.store,
            self.tasks.dispatch_authority_resolver,
            "project_chat",
            request,
            project_id=schedule.project_id,
            operation_id=operation_id,
        )
        now = self.clock()
        return AgentTaskRecord(
            operation_id=operation_id,
            project_id=schedule.project_id,
            graph_target=graph_target,
            kind="project_chat",
            status="queued",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="Waiting for nightly graph consolidation.",
            authorized_by=schedule.authorized_by,
            dispatch_authority=authority,
        )

    def _launch(self, operation_id: str) -> None:
        try:
            run = self.store.consolidation_run_for_operation(operation_id)
            if run is None:
                raise ValueError("consolidation_binding_required")
            self.store.drop_consolidation_request_target(operation_id)
            self.tasks.launch_admitted(operation_id)
            self._launch_errors.pop(operation_id, None)
        except (TypeError, ValueError) as exc:
            # Launch validation judged the durable row, which no later pass changes.
            # Failing the task lets outcome reconciliation settle the run visibly.
            logger.warning("Consolidation task %s cannot launch: %s", operation_id, exc)
            self.store.fail_agent_task(operation_id, str(exc))
        except Exception as exc:
            # Admission is already durable. A later pass retries exactly this task.
            logger.exception("Consolidation dispatch failed for %s", operation_id)
            message = f"Launch failed and will be retried: {exc}"
            if self._launch_errors.get(operation_id) != message:
                # Remember only a recorded event, so a failed write is tried again.
                self.store.record_agent_task_event(operation_id, message, level="warning")
                self._launch_errors[operation_id] = message

    def reconcile_outcomes(self) -> None:
        for run in self.store.consolidation_runs(unsettled_only=True):
            if run.operation_id is None:
                continue
            task = self.store.agent_task(run.operation_id)
            if task is None or (not task.finished and task.status != "paused"):
                continue
            try:
                self._settle(run, task)
            except Exception:
                logger.exception("Consolidation outcome reconciliation failed for %s", run.run_id)

    def _settle(self, run: ConsolidationRun, task: AgentTaskRecord) -> None:
        # Every finished turn, failed or not, reruns the keyed holds. A run whose
        # applied Evidence cannot be checked yet stays unsettled for the next pass.
        if not hold_consolidation_additions(
            lambda: self.service_for(run.project_id), self.store, task.operation_id
        ):
            return
        revisions: list[dict[str, object]] = []
        proposals = 0
        covered = run.input_head
        prelaunch_failure = task.status == "failed" and not self.store.agent_task_has_receipt(
            task.operation_id, "agent_launch"
        )
        verified = prelaunch_failure
        committed_digests: set[str | None] = set()
        if not prelaunch_failure:
            try:
                _, steps = _target_steps(self.service_for(run.project_id), run.graph_target)
                revisions, proposals, covered = operation_outcome(
                    steps, task.operation_id, run.input_head
                )
                committed_digests = {
                    step.patch.source_effect_sha256
                    for step in steps
                    if step.patch.source_operation_id == task.operation_id
                }
                verified = True
            except Exception:
                logger.exception("Consolidation history unavailable for %s", run.run_id)
        artifact = next(
            (
                item
                for item in self.store.artifacts(run.project_id)
                if item.supplier == "turn"
                and item.origin_operation_id == task.operation_id
                and item.source_name == "consolidation-report.html"
                and item.media_type == "text/html"
            ),
            None,
        )
        if artifact is not None:
            try:
                self.store.read_artifact_bytes(artifact.artifact_id)
            except (OSError, ValueError, KeyError):
                artifact = None
        error_code = None
        error_message = None
        graph_update = (task.result or {}).get("graph_update")
        unresolved_apply = any(
            (receipt.get("last_failure") or {}).get("status") == "unavailable"
            and receipt["sha256"] not in committed_digests
            for receipt in self.store.list_consolidation_apply_receipts(task.operation_id)
        )
        if run.error_code == "restored_run_detached":
            error_code, error_message = run.error_code, run.error_message
        elif task.status in {"paused", "interrupted"}:
            error_code, error_message = (
                task.status,
                task.error or "The consolidation task ended before completion.",
            )
        elif task.status != "succeeded":
            error_code = "task_failed"
            if prelaunch_failure:
                error_code = (
                    task.error
                    if task.error in CONSOLIDATION_PRELAUNCH_ERRORS
                    else "consolidation_launch_failed"
                )
            error_message = task.error or "The consolidation task failed."
        elif isinstance(graph_update, dict) and graph_update.get("status") in {
            "rejected",
            "unavailable",
        }:
            error_code, error_message = "graph_update_failed", "The graph update did not settle."
        elif not verified:
            error_code, error_message = "history_unavailable", "Canonical history is unavailable."
        elif unresolved_apply:
            error_code, error_message = (
                "keyed_apply_unavailable",
                "A keyed graph update has no verified canonical commit.",
            )
        elif artifact is None:
            error_code, error_message = (
                "report_missing",
                "No viewable consolidation report was captured.",
            )
        self.store.settle_consolidation_run(
            run.run_id,
            kind="failure" if error_code else "report",
            report_artifact_id=artifact.artifact_id if artifact and not error_code else None,
            report_title=(artifact.display_title or artifact.source_name)
            if artifact and not error_code
            else None,
            applied_revisions=revisions,
            revisions_verified=verified,
            proposals_created=proposals,
            error_code=error_code,
            error_message=error_message,
            covered_head=covered if not error_code else None,
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("Consolidation polling pass failed")
            self._stop.wait(self.interval)


def _target_steps(service: ProjectService, target: GraphTargetRef) -> tuple[int, list[StepRecord]]:
    engine: HistoryEngine = service.history_engine
    head = engine.resolve(HeadSpec(revision="head"), default_target=target)
    if engine.state_at(head).replay_status != "complete":
        raise ValueError("Canonical graph history is incomplete")
    return head.revision, [step for step in engine.steps(head) if step.accepted]


def operation_outcome(
    steps: list[StepRecord],
    operation_id: str,
    input_head: int,
) -> tuple[list[dict[str, object]], int, int]:
    revisions: list[dict[str, object]] = []
    proposals: set[str] = set()
    covered = input_head
    for step in steps:
        if not step.accepted or step.patch.source_operation_id != operation_id:
            continue
        revisions.append({"revision": step.after_head.revision, "summary": step.patch.summary})
        proposals.update(set(step.after_state.proposals) - set(step.before_state.proposals))
        if step.after_head.revision == covered + 1:
            covered = step.after_head.revision
    return revisions, len(proposals), covered
