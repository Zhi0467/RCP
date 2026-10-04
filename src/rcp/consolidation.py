"""Scheduled, run-bound graph consolidation and replayable outcome settlement."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from rcp.background import BackgroundAgentTasks, StartupEffectFence
from rcp.history import ReplayHalted
from rcp.limits import CONSOLIDATION_POLL_SECONDS
from rcp.machine_sleep import seconds_until_automatic_launch
from rcp.runs.consolidation import CONSOLIDATION_PRELAUNCH_ERRORS, consolidation_skill_selection
from rcp.runs.task_policy import resolved_dispatch_authority
from rcp.service import ProjectService, RunRequest
from rcp.storage import AgentTaskRecord, AppStore
from rcp.transport import StateUnavailable

if TYPE_CHECKING:
    from rcp.core.models import GraphState, Patch
    from rcp.server_ops.maintenance import RuntimeAdmissionGate
    from rcp.storage.consolidation import ConsolidationRun, ConsolidationSchedule

logger = logging.getLogger(__name__)


def consolidation_chat_id(project_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"rcp:consolidation:{project_id}"))


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


def next_occurrence(local_time: str, timezone: str, after: datetime) -> datetime:
    """The first scheduled civil-date occurrence strictly after an aware instant."""
    if after.tzinfo is None:
        raise ValueError("schedule handling time must be timezone-aware")
    local_date = after.astimezone(ZoneInfo(timezone)).date()
    while True:
        candidate = occurrence_at(local_date, local_time, timezone)
        if candidate > after:
            return candidate
        local_date += timedelta(days=1)


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
            self.reconcile_outcomes()
            if seconds_until_automatic_launch() > 0:
                return
            for run in self.store.consolidation_runs(unsettled_only=True):
                if run.operation_id:
                    task = self.store.agent_task(run.operation_id)
                    if task is not None and task.status == "queued":
                        self._launch(task.operation_id)
            now = datetime.fromisoformat(self.clock())
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
        if any(
            run.outcome_settled_at is None
            for run in self.store.consolidation_runs(schedule.project_id, unsettled_only=True)
        ):
            return
        due = datetime.fromisoformat(schedule.next_due_at)
        occurrence_date = due.astimezone(ZoneInfo(schedule.timezone)).date().isoformat()
        next_due = next_occurrence(schedule.local_time, schedule.timezone, now).isoformat()
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
            replay, boundaries = service.history.accepted_patch_boundaries()
            if replay.state.replay_status != "complete":
                raise ValueError("Canonical graph history is incomplete")
            input_head = replay.state.revision
            consolidation_operations = {
                run.operation_id
                for run in self.store.consolidation_runs(schedule.project_id)
                if run.operation_id is not None
            }
            covered = schedule.covered_head
            changed = covered is None or any(
                state.revision > covered
                and patch.source_operation_id not in consolidation_operations
                for _, patch, state in boundaries
            )
            if changed:
                failure_code = "consolidation_launch_unavailable"
                task = self._task(schedule, service)
                execution_host = service.manifest.machine_map[task.request["run_on"]].host
            else:
                skipped = True
        except (ValueError, KeyError, OSError, ReplayHalted, StateUnavailable) as exc:
            task = None
            error_code, error_message = failure_code, str(exc)
        run = self.store.claim_consolidation_occurrence(
            schedule,
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

    def _task(self, schedule: ConsolidationSchedule, service: ProjectService) -> AgentTaskRecord:
        profile = service.resolve_agent_profile("project_chat")
        request = RunRequest(
            provider=profile.provider,
            model=profile.model,
            reasoning=profile.reasoning,
            run_on=profile.run_on,
            run_truth_scope=list(service.manifest.project.truth_scope),
            chat_scope="project",
            chat_id=consolidation_chat_id(schedule.project_id),
            session_id=None,
            mode="work",
            trigger="schedule",
            message="Run the graph-consolidation workflow on main.",
            workflow_ids=["graph-consolidation"],
            skill_ids=[],
            invoked_workflow_ids=["graph-consolidation"],
        )
        selection = consolidation_skill_selection()
        request = request.model_copy(
            update={"resolved_skill_packages": selection.resolved_skill_packages}
        )
        request = service.resolve_compute_request(request)
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
            self.store.set_chat_title(
                run.project_id,
                consolidation_chat_id(run.project_id),
                run.authorized_by.user_id,
                "Graph consolidation",
            )
            self.tasks.launch_admitted(operation_id)
        except Exception:
            # Admission is already durable. A later pass retries exactly this task.
            logger.exception("Consolidation dispatch failed for %s", operation_id)

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
        revisions: list[dict[str, object]] = []
        proposals = 0
        covered = run.input_head
        prelaunch_failure = (
            task.error in CONSOLIDATION_PRELAUNCH_ERRORS
            and not self.store.agent_task_has_receipt(task.operation_id, "agent_launch")
        )
        verified = prelaunch_failure
        committed_digests: set[str | None] = set()
        if not prelaunch_failure:
            try:
                replay, boundaries = self.service_for(
                    run.project_id
                ).history.accepted_patch_boundaries()
                if replay.state.replay_status != "complete":
                    raise ValueError("Canonical graph history is incomplete")
                revisions, proposals, covered = operation_outcome(
                    boundaries, task.operation_id, run.input_head
                )
                committed_digests = {
                    patch.source_effect_sha256
                    for _, patch, _ in boundaries
                    if patch.source_operation_id == task.operation_id
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
            error_code, error_message = (
                task.error if prelaunch_failure else "task_failed",
                task.error or "The consolidation task failed.",
            )
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


def operation_outcome(
    boundaries: list[tuple[GraphState, Patch, GraphState]],
    operation_id: str,
    input_head: int,
) -> tuple[list[dict[str, object]], int, int]:
    revisions: list[dict[str, object]] = []
    proposals: set[str] = set()
    covered = input_head
    for previous, patch, state in boundaries:
        if patch.source_operation_id != operation_id:
            continue
        revisions.append({"revision": state.revision, "summary": patch.summary})
        proposals.update(set(state.proposals) - set(previous.proposals))
        if state.revision == covered + 1:
            covered = state.revision
    return revisions, len(proposals), covered
