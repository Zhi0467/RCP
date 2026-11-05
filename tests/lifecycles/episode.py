"""The Experiment episode with watchers machine.

``Loop`` is the graceful-Stop tests' builder, moved here. ``start`` uses it to
seed one running Experiment-loop root turn ``R0`` with a bound native session
and a local stage inside the tree, plus one lone external observer that has
already failed four checks, so one more failure makes it ready as a degraded
observer would.

A world is one real backend over the explorer's tree. ``boot`` patches only
instance seams: ``store.now`` and the watcher poller's clock read the explorer
clock, provider admission, remote reconciliation and transport retry timers do
nothing, ``_spawn_record`` registers a worker without starting a thread, and
watcher delivery admission is opened as the lifespan opens it. ``seams`` points
``rcp.storage.rows.datetime`` and ``rcp.watchers._now`` at the explorer clock,
lets automatic launches start at once, makes
``RemoteRunStage.directory_exists`` raise while the outage flag is on and
return True once it is off, and stubs
``require_remote_provider_quiescence``, so no call reaches SSH.

Actions and the product calls they make:

- ``stop`` (member): ``stop_bound_experiment_episode``, the Stop route's body.
- ``settle <turn> succeeded|failed|exit``: the worker's verdict
  (``commit_experiment_episode_handoff``, the Work finalizer's watcher handoff
  with its session-replacement refusals, arming no new observer and carrying an
  ending signal for ``exit``, then ``complete_agent_task``; or
  ``fail_agent_task``), then its ``finally`` block:
  ``settle_ready_experiment_loop_stops`` and ``_task_settled``, which runs the
  app's ``after_task_settled`` and so reconciles the episode. The verdict and
  the ``finally`` block are one action: splitting them doubles the space past
  the budget, and the agent task explorer owns that race.
- ``report P<n> start|ready|error``: the report stream's
  ``allocate_episode_report_attempt`` and ``mark_episode_report_attempt_running``,
  then ``finish_episode_report_ready`` or ``record_episode_report_attempt_error``;
  the task's last call ends its worker.
- ``check active|complete|error``: one ``WatcherPoller.poll_once`` whose check
  reports that state, after the clock reaches the observer's due time.
  Delivery and ``after_watcher_poll`` run inside it.
- ``poll``: ``WatcherPoller.poll_once`` while a completed observer waits.
- ``restart``: ``recover_at_startup``, ``accept_watcher_notifications``, then
  ``reconcile_experiment_episode`` for each Experiment-loop episode, as the app's
  startup sequence runs them.
- ``outage``: seeds the live leaf's ``stage_machine_id``, turns the outage flag on and
  restarts; ``outage_ends`` turns it off.

To fit the budget, checks and polls are offered only while the episode runs
with no Stop and its turn succeeded (a completion that lands during a turn waits
for it either way, and one after a failed turn waits for the member's Retry,
which the model does not offer), restart only while Stop is pending or the episode wraps up
(a plain running loop's restart leaves the turn to the member's Resume, which
the model does not offer), and the member's retire action is dropped because
no rule reads it. Bound 3 with horizon 2 is the minimum that checks the goal
from the wrap-up the exit reaches at depth 1.

Views name each turn by lineage: ``R`` for the root's chain, ``K`` for the
wake's, ``P0`` for the hidden report task and ``P1`` for the task a restart
retries it on. Coverage reads only this machine's
own status types; ``AgentTaskStatus`` belongs to the agent task explorer, and
report attempt statuses share values with the episode's, which a flat
``unreached_values`` cannot tell apart.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any, NamedTuple, cast
from unittest import mock

from rcp.agents import AgentEvent
from rcp.core.models import Patch
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import WATCHER_GROUP_DIAGNOSTIC_ERROR_COUNT
from rcp.machine_upgrade import migrate_machine_identity
from rcp.runs.shared import _sse
from rcp.server_ops.application_validation import _rebind_local_stage_paths
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    EpisodeWrapupRecord,
    WatcherContinuation,
    WatcherRecord,
)
from rcp.storage.agent_tasks import AgentTaskStoreMixin
from rcp.storage.artifacts import ArtifactStoreMixin, migrate_artifacts
from rcp.storage.auto_research import AutoResearchStoreMixin
from rcp.storage.auto_research_children import AutoResearchChildrenStoreMixin
from rcp.storage.base import AppStoreBase
from rcp.storage.episodes import (
    EpisodeStoreMixin,
    _insert_legacy_wrapup,
    _migrate_campaign_episodes,
    _migrate_campaign_wrapup,
    _migrate_experiment_episodes,
    _remove_impossible_legacy_experiment_wrapup,
    compact_episode_receipt,
)
from rcp.storage.experiments import ExperimentStoreMixin
from rcp.storage.models import (
    EpisodeEnding,
    EpisodeStatus,
    EpisodeWrapupState,
    WatcherStatus,
)
from rcp.storage.projects import ProjectStoreMixin
from rcp.storage.transfer import ProjectTransferStoreMixin
from rcp.storage.watchers import WatcherStoreMixin
from tests.episode_report_helpers import stored_report
from tests.helpers import append_fixture_patch, authorized_human, seed_patch, signed_in_client
from tests.lifecycles.engine import RESTART, Action, Clock, Goal, Memory, Safety, Step

EXPERIMENT_ID = "exp/bounded-loop"
NODE_PATH = "exp%2Fbounded-loop"


def task_authority(request: RunRequest, *, kind: str = "node_chat"):
    authority = resolve_dispatch_authority(kind, request)
    assert authority is not None
    return authority


def experiment_patch(*, invocation_ceiling: int = 2) -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Added an experiment for graceful-stop tests.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": EXPERIMENT_ID,
                        "type": "experiment",
                        "title": "Bounded loop",
                        "objective": "Exercise the graceful-stop contract.",
                        "completion_criteria": ["The detached fixture exits cleanly."],
                        "invocation_ceiling": invocation_ceiling,
                    }
                ],
            }
        ],
    )


class Loop:
    """One project holding a bounded Experiment with a persisted loop episode."""

    def __init__(self, app, *, invocation_ceiling: int = 2) -> None:
        self.app = app
        self.service = app.state.service
        append_fixture_patch(self.service, seed_patch())
        append_fixture_patch(self.service, experiment_patch(invocation_ceiling=invocation_ceiling))
        self.control_revision = self.service.history.state().revision
        self.project_id = app.state.default_project_id
        self.store: AppStore = app.state.background_tasks.store
        self.authorizer = authorized_human(self.store)
        self.client = signed_in_client(app)
        self.chat_id = str(uuid.uuid4())
        self.episode_id = str(uuid.uuid4())
        self.invocation_ceiling = invocation_ceiling

    def root_request(self, *, invocation: int = 1) -> RunRequest:
        return RunRequest(
            provider="codex",
            model="gpt-5",
            reasoning="medium",
            run_on="backend",
            run_truth_scope=["repo-a"],
            chat_id=self.chat_id,
            chat_scope="node",
            node_id=EXPERIMENT_ID,
            message="Begin a bounded Experiment-loop episode.",
            mode="work",
            trigger="experiment_run",
            patch_kind="experiment_loop",
            control_node_id=EXPERIMENT_ID,
            control_revision=self.control_revision,
            control_episode_id=self.episode_id,
            control_invocation=invocation,
            control_invocation_ceiling=self.invocation_ceiling,
            control_decision_bundle=[],
            control_completion_criteria=["The detached fixture exits cleanly."],
        )

    def task_record(self, request: RunRequest, **fields) -> AgentTaskRecord:
        now = self.store.now()
        return AgentTaskRecord(
            project_id=self.project_id,
            kind="node_chat",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            dispatch_authority=task_authority(request),
            **fields,
        )

    def watcher_request(self, watcher_ids: list[str]) -> RunRequest:
        return self.root_request(invocation=2).model_copy(
            update={
                "trigger": "watcher",
                "session_id": "native-session-abc",
                "watcher_ids": watcher_ids,
            }
        )

    def seed_wake(self, operation_id, watcher_id, stage, **fields):
        self.arm_watcher(watcher_id, status="completed")
        request = self.watcher_request([watcher_id])
        self.create_watcher_invocation(
            self.task_record(
                request,
                operation_id=operation_id,
                native_session_id="native-session-abc",
                stage_machine_id="backend",
                stage_root=str(stage),
                **fields,
            ),
            [watcher_id],
        )
        return request

    def record_context_candidate(self, operation_id: str) -> None:
        self.store.record_agent_task_contract(
            operation_id,
            "experiment_episode_context_candidate",
            "{}",
            hashlib.sha256(b"{}").hexdigest(),
        )

    def _set_task_status(self, record: AgentTaskRecord) -> AgentTaskRecord:
        if record.status == "running":
            self.store.mark_agent_task_running(record.operation_id)
            self.store.update_agent_task_message(
                record.operation_id,
                record.status_message,
                phase=record.phase,
            )
        elif record.status == "succeeded":
            self.store.complete_agent_task(
                record.operation_id,
                applied_revision=record.applied_revision,
                result=record.result or {},
            )
        elif record.status == "paused":
            self.store.pause_agent_task(
                record.operation_id,
                detail=record.status_message,
                result=record.result,
            )
        elif record.status in {"failed", "interrupted"}:
            self.store.fail_agent_task(
                record.operation_id,
                record.error or record.status_message,
                status=record.status,
                result=record.result,
                failure_kind=record.failure_kind,
            )
        elif record.status != "queued":
            raise AssertionError(f"Unsupported fixture task status: {record.status}")
        stored = self.store.agent_task(record.operation_id)
        assert stored is not None
        return stored

    def start_episode(
        self,
        *,
        status: str = "succeeded",
        operation_id: str = "loop-root",
        request: RunRequest | None = None,
    ) -> str:
        now = self.store.now()
        request = request or self.root_request()
        dispatch_authority = task_authority(request)
        record = AgentTaskRecord(
            operation_id=operation_id,
            project_id=self.project_id,
            episode_id=request.control_episode_id,
            kind="node_chat",
            status=status,
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="Working on the bounded loop.",
            phase="agent",
            last_activity_at=now,
            authorized_by=self.authorizer,
            dispatch_authority=dispatch_authority,
        )
        self.store.create_experiment_episode_with_invocation(
            record.model_copy(update={"status": "queued", "phase": "queued"})
        )
        self._set_task_status(record)
        return operation_id

    def create_watcher_invocation(
        self,
        record: AgentTaskRecord,
        watcher_ids: list[str],
    ) -> AgentTaskRecord:
        queued = record.model_copy(
            update={
                "episode_id": self.episode_id,
                "status": "queued",
                "error": None,
                "result": None,
                "phase": "queued",
            }
        )
        stored = self.store.create_experiment_watcher_invocation(queued, watcher_ids)
        assert stored is not None
        return self._set_task_status(record)

    def create_recovery_task(
        self,
        record: AgentTaskRecord,
        *,
        continuation_cause: str = "resume",
    ) -> AgentTaskRecord:
        queued = record.model_copy(
            update={
                "episode_id": self.episode_id,
                "status": "queued",
                "error": None,
                "result": None,
                "phase": "queued",
            }
        )
        self.store.create_experiment_recovery_task(
            queued,
            continuation_cause=continuation_cause,
        )
        return self._set_task_status(record)

    def bind_session(
        self,
        stage_root: Path,
        *,
        provider: str = "codex",
        native_session_id: str = "native-session-abc",
        operation_id: str = "loop-root",
    ) -> None:
        stage_root.mkdir(parents=True, exist_ok=True)
        self.store.commit_experiment_episode_turn(
            episode_id=self.episode_id,
            project_id=self.project_id,
            control_node_id=EXPERIMENT_ID,
            provider=provider,
            machine_id="backend",
            native_session_id=native_session_id,
            stage_machine_id="backend",
            stage_root=str(stage_root),
            chat_id=self.chat_id,
            operation_id=operation_id,
            invocation=1,
            graph_result="applied",
            watcher_ids=[],
            context_baseline={},
        )

    def continuation(self, **overrides: object) -> WatcherContinuation:
        base: dict[str, object] = {
            "provider": "codex",
            "model": "gpt-5",
            "reasoning": "medium",
            "run_on": "backend",
            "run_truth_scope": ["repo-a"],
            "patch_kind": "experiment_loop",
            "control_node_id": EXPERIMENT_ID,
            "control_revision": self.control_revision,
            "control_episode_id": self.episode_id,
            "control_invocation": 1,
            "control_invocation_ceiling": self.invocation_ceiling,
            "control_decision_bundle": [],
            "control_completion_criteria": ["The detached fixture exits cleanly."],
        }
        base.update(overrides)
        return WatcherContinuation.model_validate(base)

    def settle_exhausted_ending(
        self, *, report: bool = False, ending: EpisodeEnding = "exhausted"
    ) -> None:
        """Put the episode where a spent ceiling leaves it: ended and settled.

        Fencing alone leaves the episode wrapping up, which is still live. The
        dead zone this covers only appears once wrap-up is finished, and a real
        episode usually finishes it holding a report.
        """

        diagnostic = "The authorized operational invocation ceiling was exhausted."
        self.store.fence_episode_ending(self.episode_id, ending, diagnostic=diagnostic)
        receipt_json, receipt_sha256 = compact_episode_receipt(
            {"ending": ending, "episode_id": self.episode_id}
        )
        now = self.store.now()
        if not report:
            self.store.fail_episode_wrapup_unlaunchable(
                self.episode_id,
                EpisodeWrapupRecord(
                    episode_id=self.episode_id,
                    ending=ending,
                    partial=True,
                    concluding_operation_id="loop-root",
                    receipt_json=receipt_json,
                    receipt_sha256=receipt_sha256,
                    state="failed",
                    diagnostic="The exact report continuation is unavailable.",
                    created_at=now,
                    updated_at=now,
                    finished_at=now,
                ),
                ending_diagnostic=diagnostic,
            )
            return
        allocation_operation_id = f"{self.episode_id}-report-allocation"
        self.store.begin_episode_wrapup(
            self.episode_id,
            EpisodeWrapupRecord(
                episode_id=self.episode_id,
                ending="exhausted",
                partial=True,
                concluding_operation_id="loop-root",
                allocation_operation_id=allocation_operation_id,
                provider="codex",
                machine_id="backend",
                native_session_id="native-session",
                stage_machine_id="backend",
                stage_root="/tmp/episode-stage",
                skill_id="episode-report",
                skill_version="1",
                output_name="episode-report.html",
                output_path="/tmp/episode-stage/episode-report.html",
                receipt_json=receipt_json,
                receipt_sha256=receipt_sha256,
                state="pending",
                diagnostic=diagnostic,
                created_at=now,
                updated_at=now,
            ),
            AgentTaskRecord(
                operation_id=allocation_operation_id,
                project_id=self.project_id,
                episode_id=self.episode_id,
                kind="episode_report",
                status="queued",
                request={"provider": "codex", "run_on": "backend"},
                created_at=now,
                updated_at=now,
                status_message="Wrapping up visualization and report",
                parent_operation_id="loop-root",
                native_session_id="native-session",
                stage_machine_id="backend",
                stage_root="/tmp/episode-stage",
                visible=False,
            ),
        )
        attempt = self.store.allocate_episode_report_attempt(self.episode_id)
        html = "<html><body><figure>Result</figure></body></html>"
        self.store.finish_episode_report_ready(
            attempt.attempt_id,
            stored_report(
                self.store,
                report_id=f"{self.episode_id}-report",
                episode_id=self.episode_id,
                attempt_id=attempt.attempt_id,
                allocation_operation_id=attempt.allocation_operation_id,
                ending="exhausted",
                sha256=hashlib.sha256(html.encode()).hexdigest(),
                html=html,
                created_at=self.store.now(),
            ),
        )

    def _watcher(
        self,
        watcher_id: str,
        *,
        continuation: WatcherContinuation | None = None,
        origin_operation_id: str = "loop-root",
        machine_id: str = "backend",
        group_id: str | None = None,
        graph_target: GraphTargetRef | None = None,
        chat_id: str | None = None,
    ) -> WatcherRecord:
        """Storage keeps group identity and label together."""
        return WatcherRecord(
            watcher_id=watcher_id,
            project_id=self.project_id,
            origin_operation_id=origin_operation_id,
            origin_task_kind="node_chat",
            chat_id=chat_id or self.chat_id,
            node_id=EXPERIMENT_ID,
            machine_id=machine_id,
            group_id=group_id,
            group_label=None if group_id is None else f"Array {group_id}",
            graph_target=graph_target or GraphTargetRef(),
            check_command="true",
            log_path=f"/tmp/{watcher_id}.log",
            cwd="/tmp",
            continuation=continuation or self.continuation(),
            status="active",
            created_at=self.store.now(),
        )

    def arm_watcher_group(self, group_id: str, watcher_ids: list[str]) -> None:
        """Arm one group in a single write; storage refuses a group of one."""

        self.store.create_watchers(
            [self._watcher(watcher_id, group_id=group_id) for watcher_id in watcher_ids]
        )

    def arm_watcher(
        self,
        watcher_id: str,
        *,
        status: str = "active",
        continuation: WatcherContinuation | None = None,
        origin_operation_id: str = "loop-root",
        machine_id: str = "backend",
        graph_target: GraphTargetRef | None = None,
        chat_id: str | None = None,
    ) -> WatcherRecord:
        self.store.create_watchers(
            [
                self._watcher(
                    watcher_id,
                    continuation=continuation,
                    origin_operation_id=origin_operation_id,
                    machine_id=machine_id,
                    graph_target=graph_target,
                    chat_id=chat_id,
                )
            ]
        )
        if status != "active":
            self.store.record_watcher_check(
                watcher_id,
                status=status,
                exit_code=0,
                error=None,
            )
        stored = self.store.watcher(watcher_id)
        assert stored is not None
        return stored

    def stop(self) -> dict[str, object]:
        response = self.client.post(
            f"/api/projects/{self.project_id}/experiments/{NODE_PATH}/stop", json={}
        )
        assert response.status_code == 200, response.text
        return response.json()

    def control(self) -> dict[str, object]:
        snapshot = self.client.get(f"/api/projects/{self.project_id}").json()
        return snapshot["experiment_control"][EXPERIMENT_ID]

    def deliver(self, *watcher_ids: str) -> None:
        deliver = self.app.state.watcher_poller.on_completed
        assert deliver is not None
        group = []
        for watcher_id in watcher_ids:
            record = self.store.watcher(watcher_id)
            assert record is not None
            group.append(record)
        deliver(group)

    def loop_task_ids(self) -> set[str]:
        return {
            record.operation_id
            for record in self.store.agent_tasks(self.project_id)
            if record.request.get("patch_kind") == "experiment_loop"
        }

    def capture_turn(self):
        observed = Event()
        captured: dict[str, object] = {}

        async def stream(_project_id, _kind, request, execution):
            captured.update(request=request, continuation=execution.continuation)
            observed.set()
            yield _sse(AgentEvent(event="done"))

        self.app.state.background_tasks.stream = stream
        return observed, captured

    def record_answers(self) -> list[RunRequest]:
        """Replace agent execution with a stub that only records its request."""

        seen: list[RunRequest] = []

        async def stream(_project_id, _kind, request, _execution):
            seen.append(request)
            yield _sse(AgentEvent(event="answer", text="Stub turn."))
            yield _sse(AgentEvent(event="done"))

        self.app.state.background_tasks.stream = stream
        return seen


_MIGRATIONS = (
    migrate_artifacts,
    AppStoreBase._backfill_watcher_episode_lineage,
    AppStoreBase._derive_branch_episode_isolation,
    AppStoreBase._migrate_child_work_watchers,
    AppStoreBase._migrate_episode_lineage,
    AppStoreBase._migrate_episode_report_titles,
    AppStoreBase._migrate_experiment_episode_state,
    _insert_legacy_wrapup,
    _migrate_campaign_episodes,
    _migrate_campaign_wrapup,
    _migrate_experiment_episodes,
    _remove_impossible_legacy_experiment_wrapup,
    migrate_machine_identity,
)
_AUTO_RESEARCH = (
    AutoResearchStoreMixin._insert_paid_auto_research_task,
    AutoResearchStoreMixin._settle_auto_research_watchers_in_connection,
    AutoResearchStoreMixin.activate_auto_research_reservation,
    AutoResearchStoreMixin.create_auto_research_episode_with_root_task,
    AutoResearchChildrenStoreMixin._create_auto_research_child_work_wake_task,
    AutoResearchChildrenStoreMixin.guard_auto_research_finish,
    AutoResearchChildrenStoreMixin.request_auto_research_child_work_stop,
)
_OTHER_MACHINES = {
    _rebind_local_stage_paths: (
        "application validation of a copied data directory, a catalog machine"
    ),
    AgentTaskStoreMixin._reserve_merge_invocation: "branch merge, a catalog machine",
    ArtifactStoreMixin._remove_artifact: "artifact deletion, a catalog machine",
    AutoResearchStoreMixin.detach_auto_research_for_restore: "restore, a catalog machine",
    EpisodeStoreMixin.detach_episode_reports_for_restore: "restore, a catalog machine",
    EpisodeStoreMixin.detach_experiment_episodes_for_restore: "restore, a catalog machine",
    WatcherStoreMixin.detach_watchers_for_restore: "restore, a catalog machine",
    ProjectStoreMixin.delete_project_records: "project deletion, a catalog machine",
    ProjectTransferStoreMixin._insert_transfer_episodes: "project transfer, a catalog machine",
    ProjectTransferStoreMixin._insert_transfer_watchers: "project transfer, a catalog machine",
    WatcherStoreMixin.claim_watcher_cancel: "human Cancel, the compute job catalog machine",
    WatcherStoreMixin.complete_watcher_cancel: "human Cancel, the compute job catalog machine",
    WatcherStoreMixin.record_watcher_cancel_result: (
        "human Cancel, the compute job catalog machine"
    ),
    WatcherStoreMixin._stop_chat_watchers: "closing an agent thread, the schedules explorer's",
    WatcherStoreMixin.create_watcher_notification_task: (
        "generic Work wakes; an Experiment wake is create_experiment_watcher_invocation"
    ),
    WatcherStoreMixin.resolve_watcher_delivery_authorizer: (
        "generic Work wakes; an Experiment wake takes its episode's authorizer"
    ),
}
_UNMODELED = {
    EpisodeStoreMixin._insert_episode: "start seeds the one episode",
    ExperimentStoreMixin.create_experiment_episode_with_invocation: "start seeds the one episode",
    WatcherStoreMixin._insert_watcher: "start arms the one observer",
    EpisodeStoreMixin.end_episode_without_report: (
        "an ending with no provider session; the model's episode is bound to one"
    ),
    EpisodeStoreMixin.fail_episode_report_allocation_unlaunchable: (
        "the report task always launches in this model"
    ),
    EpisodeStoreMixin.fail_episode_wrapup_unlaunchable: "wrap-up admission never fails here",
    ExperimentStoreMixin.create_experiment_question_invocation: "human questions are not modeled",
    WatcherStoreMixin._stop_watcher_for_loop: (
        "re-arming a stopped observer happens in a turn's handoff, which no action stages"
    ),
    WatcherStoreMixin._validate_and_apply_agent_watcher_stops: (
        "agent stop items come from a turn's handoff, which no action stages"
    ),
    WatcherStoreMixin._record_graph_watcher_result_locked: "the observer is not a graph condition",
    WatcherStoreMixin.initialize_graph_watcher_baseline: "the observer is not a graph condition",
    WatcherStoreMixin.initialize_graph_watcher_target_baselines: (
        "the observer is not a graph condition"
    ),
    WatcherStoreMixin.record_watcher_wake_error: "delivery never raises in this model",
    WatcherStoreMixin.stop_watchers: (
        "the member's retire action is dropped to fit the budget; no rule reads it"
    ),
}

_ROOT = "loop-root"
_WATCHER = "observer"
_STAGE_HOST = "gpu-host"
_LIVE = frozenset({"queued", "running", "pausing"})
_SETTLE_OUTCOMES = ("succeeded", "failed", "exit")
_CHECKS = {"active": (0, None), "complete": (0, None), "error": (2, "squeue unreachable")}
_SEEDED_ERRORS = WATCHER_GROUP_DIAGNOSTIC_ERROR_COUNT - 1


class View(NamedTuple):
    episode: tuple
    turns: tuple
    watcher: tuple
    attempts: tuple
    outage: bool


@dataclass
class World:
    """One backend over the tree, plus the explorer's outage flag."""

    app: Any
    clock: Clock
    names: Names
    outage: bool = False

    @property
    def background(self):
        return self.app.state.background_tasks

    @property
    def store(self) -> AppStore:
        return self.background.store

    @property
    def poller(self):
        return self.app.state.watcher_poller

    @property
    def reconciler(self):
        """The ``EpisodeReconciler`` the app wired into watcher delivery."""

        return self.poller.on_completed.__self__._reconcile_experiment_episode.__self__

    def episode(self):
        episode = self.store.episode(self.names.episode_id)
        assert episode is not None
        return episode

    def lineage(self) -> dict[str, str]:
        """Each task's positional name: ``R`` the root, ``K`` then ``L`` wakes, ``P`` the report.

        A report task's digit counts the report tasks it retries.
        """

        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT operation_id,parent_operation_id,kind FROM graph_runs "
                "WHERE episode_id = ? ORDER BY created_at, operation_id",
                (self.names.episode_id,),
            ).fetchall()
        parents = {row[0]: row[1] for row in rows}
        kinds = {row[0]: row[2] for row in rows}
        wakes = [op for op in parents if parents[op] is None and op != _ROOT]
        roots = {_ROOT: "R", **{op: "KLMN"[index] for index, op in enumerate(wakes)}}
        names: dict[str, str] = {}
        for operation_id in parents:
            if kinds[operation_id] == "episode_report":
                depth, cursor = 0, parents[operation_id]
                while kinds.get(cursor) == "episode_report":
                    depth, cursor = depth + 1, parents[cursor]
                names[operation_id] = f"P{depth}"
                continue
            depth, cursor = 0, operation_id
            while parents.get(cursor) is not None:
                depth, cursor = depth + 1, parents[cursor]
            names[operation_id] = f"{roots[cursor]}{depth}"
        if len(set(names.values())) != len(names):
            raise AssertionError(f"two tasks share a lineage name: {sorted(names.values())}")
        return names


@dataclass(frozen=True)
class Names:
    """The ids ``start`` seeded."""

    episode_id: str
    project_id: str
    member: Any


class Episode:
    name = "episode"
    bound = 3
    horizon = 2
    covers = {
        EpisodeStatus: ("episode", 0),
        EpisodeWrapupState: ("episode", 2),
        WatcherStatus: ("watcher", 0),
    }
    tables = frozenset(
        {
            "episodes",
            "experiment_episode_state",
            "watchers",
            "episode_wrapups",
            "episode_report_attempts",
            "episode_reports",
        }
    )
    writers = (
        EpisodeStoreMixin._fence_episode_ending_in_connection,
        EpisodeStoreMixin._finish_episode_report_attempt_error,
        EpisodeStoreMixin._insert_episode_wrapup,
        EpisodeStoreMixin._mark_episode_stop_skipped_in_connection,
        EpisodeStoreMixin._request_episode_stop_in_connection,
        EpisodeStoreMixin.allocate_episode_report_attempt,
        EpisodeStoreMixin.begin_episode_wrapup,
        EpisodeStoreMixin.finish_episode_report_ready,
        EpisodeStoreMixin.mark_episode_report_attempt_running,
        EpisodeStoreMixin.retry_episode_report_allocation,
        ExperimentStoreMixin._claim_experiment_watchers,
        ExperimentStoreMixin._commit_experiment_episode_turn,
        ExperimentStoreMixin._settle_experiment_loop_stop,
        ExperimentStoreMixin.create_experiment_watcher_invocation,
        ExperimentStoreMixin.record_experiment_episode_diagnostic,
        ExperimentStoreMixin.settle_experiment_episode_wrapup,
        ProjectStoreMixin.migrate_legacy_project_data,
        WatcherStoreMixin.record_watcher_check,
        *_MIGRATIONS,
        *_AUTO_RESEARCH,
        *_OTHER_MACHINES,
        *_UNMODELED,
    )
    unreached_values = {
        "queued": "start seeds a running episode",
        "needs_action": "an exit always signals completed, never human_pause",
        "failed": (
            "a failed turn with a bound session waits for the member's Retry, and three "
            "report errors after the exit take seven actions"
        ),
        "legacy_unavailable": "only migrated legacy wrap-ups carry it",
    }
    unreached_writers = {
        ExperimentStoreMixin.record_experiment_episode_diagnostic: (
            "startup records it only for an unusable or refused recovery, and the model's "
            "session and stage stay usable"
        ),
        **dict.fromkeys(_MIGRATIONS, "a storage migration; the model's store starts current"),
        **dict.fromkeys(_AUTO_RESEARCH, "the model runs one Experiment loop, no Auto-research"),
        **_OTHER_MACHINES,
        **_UNMODELED,
    }

    def __init__(self) -> None:
        self.names: Names | None = None
        self.manifest_path: Path | None = None
        self.clock = Clock()
        self.outage = False

    @contextmanager
    def seams(self) -> Iterator[None]:
        machine = self

        class ExplorerDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return machine.clock.now.astimezone(tz)

        def directory_exists(_stage, _path):
            if machine.outage:
                raise ConnectionError("stage host unreachable")
            return True

        with (
            mock.patch("rcp.machine_sleep.AwakeStreak.seconds_until_automatic_launch", lambda _: 0),
            mock.patch("rcp.storage.rows.datetime", ExplorerDatetime),
            mock.patch("rcp.watchers._now", lambda: machine.clock.iso()),
            mock.patch("rcp.transport.RemoteRunStage.directory_exists", directory_exists),
            mock.patch(
                "rcp.runs.experiment_recovery.require_remote_provider_quiescence",
                lambda *_args: None,
            ),
        ):
            yield

    def databases(self, root: Path) -> list[Path]:
        return [root / "data" / "rcp.sqlite3"]

    def start(self, root: Path, clock: Clock) -> World:
        from tests.helpers import create_named_app, write_test_manifest

        self.clock = clock
        manifest = write_test_manifest(root)
        app = create_named_app(str(manifest.path), data_dir=root / "data")
        app.state.background_tasks.store.now = clock.iso
        loop = Loop(app)
        loop.start_episode(status="running")
        stage = root / "stage"
        loop.bind_session(stage)
        loop.store.checkpoint_agent_task(
            _ROOT,
            native_session_id="native-session-abc",
            stage_machine_id="backend",
            stage_root=str(stage),
        )
        loop.record_context_candidate(_ROOT)
        loop.store.record_agent_task_contract(
            _ROOT, "work", "task contract", hashlib.sha256(b"task contract").hexdigest()
        )
        loop.arm_watcher(_WATCHER)
        for _ in range(_SEEDED_ERRORS):
            loop.store.record_watcher_check(
                _WATCHER, status="degraded", exit_code=2, error="squeue unreachable"
            )
        loop.store.close()
        self.names = Names(loop.episode_id, loop.project_id, loop.authorizer)
        self.manifest_path = manifest.path
        world = self.boot(root, clock)
        with world.background._controls_lock:
            world.background._workers[_ROOT] = threading.current_thread()
        return world

    def boot(self, root: Path, clock: Clock) -> World:
        from tests.helpers import create_named_app

        assert self.names is not None and self.manifest_path is not None
        app = create_named_app(str(self.manifest_path), data_dir=root / "data")
        world = World(app, clock, self.names)
        self.clock = clock
        background = world.background
        store = background.store
        store.now = clock.iso
        world.poller.clock = clock.iso
        background.admit_provider_task = lambda *args, **kwargs: None
        background._schedule_remote_reconciliation = lambda **kwargs: None
        background._schedule_transport_retry = lambda *args, **kwargs: None

        def spawn(record, request, **kwargs):
            current = store.agent_task(record.operation_id) or record
            if current.status == "queued":
                with background._controls_lock:
                    background._workers[current.operation_id] = threading.current_thread()
            return current

        background._spawn_record = spawn
        background.accept_watcher_notifications()
        return world

    def close(self, world: World) -> None:
        world.store.close()

    def save_memory(self, world: World) -> Memory:
        names = world.lineage()
        failures = tuple(sorted(world.reconciler._unclassified_failures.items()))
        return Memory(
            (frozenset(names[op] for op in world.background._workers), failures),
            world.outage,
        )

    def restore_memory(self, world: World, memory: Memory) -> None:
        world.outage = self.outage = memory.environment
        if memory.engine is None:
            return
        workers, failures = memory.engine
        operation = {name: op for op, name in world.lineage().items()}
        background = world.background
        with background._controls_lock:
            for name in workers:
                background._workers[operation[name]] = threading.current_thread()
        world.reconciler._unclassified_failures.update(dict(failures))

    def actions(self, view: View) -> list[Action]:
        ((status, _ending, wrapup, stop_requested, stop_settled, _used),) = view.episode
        found: list[Action] = []
        turns = [turn[1] for turn in view.turns if not turn[0].startswith("P")]
        live = any(task_status in _LIVE for task_status in turns)
        idle = not live and "failed" not in turns
        if status == "running" and not stop_requested:
            found.append(Action("stop", environment=False))
        for name, task_status, _receipts, worker in view.turns:
            if not worker or task_status not in _LIVE:
                continue
            if name.startswith("P"):
                if view.attempts and view.attempts[-1][0] == "running":
                    found.extend(
                        Action("report", (name, outcome)) for outcome in ("ready", "error")
                    )
                elif wrapup in {"pending", "running"}:
                    found.append(Action("report", (name, "start")))
            else:
                found.extend(Action("settle", (name, outcome)) for outcome in _SETTLE_OUTCOMES)
        ((watcher, notified, _errors, _claimed, pollable),) = view.watcher
        if idle and status == "running" and not stop_requested:
            if pollable:
                found.extend(Action("check", (state,)) for state in _CHECKS)
            elif watcher == "completed" and not notified:
                found.append(Action("poll"))
        if stop_requested and not stop_settled:
            if live and not view.outage:
                found.append(Action("outage"))
            found.append(Action(RESTART))
        elif status == "wrapping_up":
            found.append(Action(RESTART))
        if view.outage:
            found.append(Action("outage_ends"))
        return found

    def apply(self, world: World, action: Action) -> None:
        operation = {name: op for op, name in world.lineage().items()}
        if action.name == "stop":
            _stop(world)
        elif action.name == "settle":
            _settle(world, operation[action.args[0]], action.args[1])
        elif action.name == "report":
            _report(world, operation[action.args[0]], action.args[1])
        elif action.name == "check":
            _check(world, action.args[0])
        elif action.name == "poll":
            world.poller.poll_once()
        elif action.name == "outage":
            leaf = next(
                op
                for name, op in operation.items()
                if not name.startswith("P")
                and (task := world.store.agent_task(op)) is not None
                and task.status in _LIVE
            )
            with world.store.connection() as connection:
                connection.execute(
                    "UPDATE graph_runs SET stage_machine_id = ? WHERE operation_id = ?",
                    (_STAGE_HOST, leaf),
                )
            world.outage = self.outage = True
            self._reboot(world)
            self.restart(world)
        elif action.name == "outage_ends":
            world.outage = self.outage = False
        else:
            raise ValueError(f"unknown action {action}")

    def _reboot(self, world: World) -> None:
        """Replace the backend in place, as the engine's restart does."""

        world.store.close()
        world.clock.advance(1)
        root = world.store.path.parent.parent
        fresh = self.boot(root, world.clock)
        world.app = fresh.app

    def restart(self, world: World) -> None:
        background = world.background
        background.recover_at_startup()
        background.accept_watcher_notifications()
        for episode in world.store.episodes(world.names.project_id, limit=None):
            if episode.mode == "experiment_loop":
                world.reconciler.reconcile_experiment_episode(episode.episode_id, source="startup")

    def view(self, world: World) -> View:
        episode = world.episode()
        names = world.lineage()
        background = world.background
        turns = []
        for operation_id, name in sorted(names.items(), key=lambda item: item[1]):
            task = world.store.agent_task(operation_id)
            assert task is not None
            receipts = tuple(
                sorted(
                    receipt.category
                    for receipt in world.store.agent_task_receipts(operation_id)
                    if receipt.category in _VIEWED_RECEIPTS
                )
            )
            turns.append((name, task.status, receipts, operation_id in background._workers))
        watcher = world.store.watcher(_WATCHER)
        assert isinstance(watcher, WatcherRecord)
        pollable = any(
            record.watcher_id == _WATCHER
            for record in world.store.pollable_watchers(as_of="9999-12-31T00:00:00+00:00")
        )
        claimed = names.get(watcher.notification_operation_id or "")
        attempts = tuple(
            (attempt.status,) for attempt in world.store.episode_report_attempts(episode.episode_id)
        )
        return View(
            (
                (
                    episode.status,
                    episode.ending,
                    episode.wrapup_state,
                    episode.stop_requested_at is not None,
                    episode.stop_settled_at is not None,
                    episode.invocations_used,
                ),
            ),
            tuple(turns),
            (
                (
                    watcher.status,
                    watcher.notified,
                    watcher.consecutive_error_count,
                    claimed,
                    pollable,
                ),
            ),
            attempts,
            world.outage,
        )

    @property
    def rules(self):
        return RULES


_VIEWED_RECEIPTS = frozenset(
    {"experiment_recovery_abandoned", "experiment_stop_recovery", "experiment_loop_exit"}
)


def _stop(world: World) -> None:
    from rcp.api.experiments import stop_bound_experiment_episode

    stop_bound_experiment_episode(
        world.names.project_id,
        world.episode(),
        store=world.store,
        catalog=world.app.state.catalog,
        initiated_by=f"human:{world.names.member.user_id}",
    )


def _settle(world: World, operation_id: str, outcome: str) -> None:
    """Run one turn's worker to its end: its verdict, then its ``finally`` block."""

    store = world.store
    task = store.agent_task(operation_id)
    assert task is not None
    if task.status == "queued":
        store.mark_agent_task_running(operation_id)
    if outcome == "failed":
        store.fail_agent_task(operation_id, "provider error")
    else:
        episode_id = world.names.episode_id
        ending = (
            {
                "episode_id": episode_id,
                "ending": "completed",
                "partial": False,
                "receipt": {"summary": "The detached fixture exited cleanly."},
            }
            if outcome == "exit"
            else None
        )
        _hand_off(world, task, ending)
        store.complete_agent_task(operation_id, applied_revision=None, result={})
    _finish(world, operation_id)


def _hand_off(world: World, task: AgentTaskRecord, ending: dict | None) -> None:
    """The turn's watcher handoff as the Work finalizer commits it, arming no new observer."""

    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution
    from rcp.runs.experiment_loop import (
        commit_experiment_episode_handoff,
        root_experiment_loop_operation_id,
    )
    from rcp.watchers import WatcherBinding

    store = world.store
    request = world.background._request_from_record(task)
    assert isinstance(request, RunRequest) and request.run_on is not None
    execution = AgentTaskExecution(
        operation_id=task.operation_id,
        store=store,
        control=AgentProcessControl(),
        runtime_id=task.runtime_id,
        stage_machine_id=task.stage_machine_id,
        stage_root=task.stage_root,
        continuation=cast(Any, store.agent_task_continuation_cause(task.operation_id)),
    )
    values = request.model_dump(mode="json")
    binding = WatcherBinding(
        project_id=task.project_id,
        origin_operation_id=root_experiment_loop_operation_id(execution),
        origin_task_kind="node_chat",
        chat_id=request.chat_id or "",
        node_id=request.node_id,
        episode_id=request.control_episode_id,
        graph_target=task.graph_target,
        machine_id=request.run_on,
        continuation=WatcherContinuation.model_validate(
            {
                name: values[name]
                for name in WatcherContinuation.model_fields
                if values.get(name) is not None
            }
        ),
    )
    commit_experiment_episode_handoff(
        execution,
        request,
        [],
        binding,
        native_session_id=task.native_session_id,
        stage_machine_id=execution.stage_machine_id,
        stage_root=execution.stage_root,
        graph_result="applied",
        context_baseline={},
        stops=[],
        ending_signal=ending,
    )


def _finish(world: World, operation_id: str) -> None:
    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution

    background = world.background
    record = background.store.agent_task(operation_id)
    assert record is not None
    request = background._request_from_record(record)
    execution = AgentTaskExecution(
        operation_id=operation_id, store=background.store, control=AgentProcessControl()
    )
    try:
        if isinstance(request, RunRequest) and request.patch_kind == "experiment_loop":
            background.store.settle_ready_experiment_loop_stops()
    finally:
        try:
            background._task_settled(record, request, execution)
        finally:
            background._forget_control(operation_id)


def _report(world: World, operation_id: str, outcome: str) -> None:
    """The hidden report task starts a provider call, or settles the running one.

    A start allocates the next attempt and marks it running, as the report
    stream does before its provider call. A settled call that is the task's last
    ends its worker.
    """

    store = world.store
    episode_id = world.names.episode_id
    if outcome == "start":
        attempt = store.current_episode_report_attempt(episode_id)
        if attempt is None or attempt.status != "queued":
            attempt = store.allocate_episode_report_attempt(episode_id)
        store.mark_episode_report_attempt_running(attempt.attempt_id)
        return
    attempt = store.current_episode_report_attempt(episode_id)
    assert attempt is not None and attempt.status == "running"
    if outcome == "ready":
        html = "<html><body><figure>Result</figure></body></html>"
        store.finish_episode_report_ready(
            attempt.attempt_id,
            stored_report(
                store,
                report_id=f"{episode_id}-report",
                episode_id=episode_id,
                attempt_id=attempt.attempt_id,
                allocation_operation_id=attempt.allocation_operation_id,
                ending=world.episode().ending,
                sha256=hashlib.sha256(html.encode()).hexdigest(),
                html=html,
                created_at=store.now(),
            ),
        )
        store.complete_agent_task(operation_id, applied_revision=None, result={})
    else:
        episode, _attempt = store.record_episode_report_attempt_error(
            attempt.attempt_id, "codex produced no completed episode report result."
        )
        if episode.wrapup_state != "failed":
            return
        store.fail_agent_task(operation_id, episode.ending_diagnostic or "report failed")
    _finish(world, operation_id)


def _check(world: World, state: str) -> None:
    """One poll pass whose check of the observer reports ``state``, after waiting until it is due."""

    from rcp.watchers import WatcherCheckResult

    watcher = world.store.watcher(_WATCHER)
    assert isinstance(watcher, WatcherRecord)
    if watcher.next_check_at is not None:
        due = datetime.fromisoformat(watcher.next_check_at)
        if due > world.clock.now:
            world.clock.advance((due - world.clock.now).total_seconds())
    exit_code, error = _CHECKS[state]
    world.poller.check_runner = lambda _spec, _host, _timeout: WatcherCheckResult(
        state=state, checked_at=world.clock.iso(), exit_code=exit_code, error=error
    )
    world.poller.poll_once()


def _tasks(world: World) -> dict[str, AgentTaskRecord]:
    """The episode's visible Experiment turns by lineage name."""

    found = {}
    for operation_id, name in world.lineage().items():
        task = world.store.agent_task(operation_id)
        assert task is not None
        if task.visible:
            found[name] = task
    return found


def _leaves(world: World) -> dict[str, AgentTaskRecord]:
    tasks = _tasks(world)
    parents = {task.parent_operation_id for task in tasks.values()}
    return {name: task for name, task in tasks.items() if task.operation_id not in parents}


def _abandoned(world: World, task: AgentTaskRecord) -> bool:
    return world.store.agent_task_has_receipt(task.operation_id, "experiment_recovery_abandoned")


def _awaiting_startup_recovery(world: World, task: AgentTaskRecord) -> bool:
    """A turn a restart interrupted while Stop waited on it, with no recovery verdict yet."""

    episode = world.episode()
    state = world.store.experiment_episode(world.names.episode_id)
    return (
        task.status == "interrupted"
        and episode.stop_requested_at is not None
        and (task.finished_at or "") >= episode.stop_requested_at
        and state is not None
        and state.session_diagnostic is None
        and not _abandoned(world, task)
    )


def _always(*_args) -> bool:
    return True


def _stop_requested(world: World) -> bool:
    return world.episode().stop_requested_at is not None


def _stop_pending_before(before: View, _world: World) -> bool:
    ((_status, _ending, _wrapup, requested, settled, _used),) = before.episode
    return requested and not settled


def _stop_requested_before(before: View, _world: World) -> bool:
    return before.episode[0][3]


def _watcher_unclaimed_before(before: View, _world: World) -> bool:
    return before.watcher[0][3] is None


def _stop_settles_here(before: View, world: World) -> bool:
    return not before.episode[0][4] and world.episode().stop_settled_at is not None


def one_graceful_stop_fence(before: View, world: World) -> list[str]:
    """After Stop no invocation is spent, and the only new turn resumes its parent's session and stage."""

    found = []
    used = before.episode[0][5]
    if world.episode().invocations_used != used:
        found.append(f"Stop let the episode spend invocation {world.episode().invocations_used}")
    known = {turn[0] for turn in before.turns}
    tasks = {task.operation_id: task for task in _tasks(world).values()}
    for name, task in _tasks(world).items():
        if name in known:
            continue
        parent = tasks.get(task.parent_operation_id or "")
        if (
            parent is None
            or task.request.get("session_id") != parent.native_session_id
            or task.stage_root != parent.stage_root
        ):
            found.append(f"{name} started behind Stop without its parent's session and stage")
    return found


def stop_settles_once_no_leaf_is_live(world: World) -> list[str]:
    """A requested Stop is settled once no leaf is live or awaiting its startup recovery."""

    episode = world.episode()
    if episode.stop_settled_at is not None:
        return []
    live = [
        name
        for name, task in _leaves(world).items()
        if task.status in _LIVE or _awaiting_startup_recovery(world, task)
    ]
    return [] if live else ["Stop is unsettled with no live leaf"]


def stop_keeps_a_pending_recovery(before: View, world: World) -> list[str]:
    """Stop never abandons a turn its restart interrupted until startup recovery rules on it."""

    state = world.store.experiment_episode(world.names.episode_id)
    if state is not None and state.session_diagnostic is not None:
        return []
    abandoned_before = {
        turn[0] for turn in before.turns if "experiment_recovery_abandoned" in turn[2]
    }
    episode = world.episode()
    return [
        f"Stop abandoned {name}, which startup left for a later recovery"
        for name, task in _leaves(world).items()
        if name not in abandoned_before
        and task.status == "interrupted"
        and episode.stop_requested_at is not None
        and (task.finished_at or "") >= episode.stop_requested_at
        and _abandoned(world, task)
    ]


def stop_skips_the_report(world: World) -> list[str]:
    """A stopped episode opens no report wrap-up and spends no report attempt."""

    episode = world.episode()
    found = []
    if episode.wrapup_state not in {"not_started", "skipped"}:
        found.append(f"Stop left wrap-up {episode.wrapup_state}")
    if world.store.episode_report_attempts(episode.episode_id):
        found.append("Stop spent a report attempt")
    return found


def at_most_three_report_attempts(world: World) -> list[str]:
    """An episode spends at most three report attempts, and a third failure ends its wrap-up."""

    attempts = world.store.episode_report_attempts(world.names.episode_id)
    found = []
    if len(attempts) > 3:
        found.append(f"{len(attempts)} report attempts")
    if (
        len(attempts) == 3
        and all(attempt.status == "failed" for attempt in attempts)
        and world.episode().wrapup_state != "failed"
    ):
        found.append("three failed report attempts left wrap-up open")
    return found


def _has_attempts(world: World) -> bool:
    return bool(world.store.episode_report_attempts(world.names.episode_id))


def a_claim_takes_a_ready_unnotified_watcher(before: View, world: World) -> list[str]:
    """A wake claims only a completed watcher, or a degraded one at the error cap, not yet notified."""

    watcher = world.store.watcher(_WATCHER)
    assert isinstance(watcher, WatcherRecord)
    if watcher.notification_operation_id is None:
        return []
    ready = watcher.status == "completed" or (
        watcher.status == "degraded"
        and watcher.consecutive_error_count >= WATCHER_GROUP_DIAGNOSTIC_ERROR_COUNT
    )
    found = []
    if before.watcher[0][1]:
        found.append("the claimed watcher was already notified")
    if not ready or not watcher.notified:
        found.append(
            f"claimed a {watcher.status} watcher with {watcher.consecutive_error_count} errors"
        )
    return found


def a_lone_degraded_watcher_wakes_once(world: World) -> list[str]:
    """A lone observer at the error cap wakes its episode at most once."""

    wakes = [
        name
        for name, task in _tasks(world).items()
        if task.parent_operation_id is None and _WATCHER in (task.request.get("watcher_ids") or [])
    ]
    return [f"the observer woke {len(wakes)} turns"] if len(wakes) > 1 else []


def _watcher_degraded(world: World) -> bool:
    watcher = world.store.watcher(_WATCHER)
    return watcher is not None and watcher.status == "degraded"


def stop_retires_only_its_episodes_observations(before: View, world: World) -> list[str]:
    """Settling Stop retires every observer of its episode and no other episode's."""

    found = []
    for watcher in world.store.watchers(world.names.project_id):
        ours = watcher.episode_id == world.names.episode_id
        if ours and watcher.status != "stopped":
            found.append(f"{watcher.watcher_id} stays {watcher.status} after Stop")
        if not ours and watcher.stopped_by == "loop":
            found.append(f"Stop retired {watcher.watcher_id} of another episode")
    return found


def wrapping_up_ends(view: View) -> bool:
    """The episode is not wrapping up."""

    return view.episode[0][0] != "wrapping_up"


_SPEC = "conversations spec"
RULES = (
    Step(one_graceful_stop_fence, _stop_requested_before, "invariant 10g"),
    Safety(
        stop_settles_once_no_leaf_is_live,
        _stop_requested,
        f"{_SPEC}: Graceful Stop and recovery",
    ),
    Step(
        stop_keeps_a_pending_recovery,
        _stop_pending_before,
        "code guard at restart_stopping_experiment_recoveries",
    ),
    Safety(stop_skips_the_report, _stop_requested, f"{_SPEC}: Visual wrap-up"),
    Safety(at_most_three_report_attempts, _has_attempts, f"{_SPEC}: Visual wrap-up"),
    Step(
        a_claim_takes_a_ready_unnotified_watcher,
        _watcher_unclaimed_before,
        f"{_SPEC}: External observation",
    ),
    Safety(a_lone_degraded_watcher_wakes_once, _watcher_degraded, f"{_SPEC}: External observation"),
    Step(
        stop_retires_only_its_episodes_observations,
        _stop_settles_here,
        f"{_SPEC}: Graceful Stop and recovery",
    ),
    Goal(wrapping_up_ends, f"{_SPEC}: Visual wrap-up"),
)
