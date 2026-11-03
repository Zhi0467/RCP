"""The Experiment episode with watchers machine."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from threading import Event

from rcp.agents import AgentEvent
from rcp.core.models import Patch
from rcp.core.transition_models import GraphTargetRef
from rcp.runs.shared import _sse
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    EpisodeWrapupRecord,
    WatcherContinuation,
    WatcherRecord,
)
from rcp.storage.episodes import compact_episode_receipt
from rcp.storage.models import EpisodeEnding
from tests.episode_report_helpers import stored_report
from tests.helpers import append_fixture_patch, authorized_human, seed_patch, signed_in_client

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
            run_on="laptop",
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
            execution_machine="laptop",
            execution_host="",
            native_session_id=native_session_id,
            stage_host=None,
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
            "run_on": "laptop",
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
                run_on="laptop",
                execution_host="",
                native_session_id="native-session",
                stage_host=None,
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
                request={"provider": "codex", "run_on": "laptop", "execution_host": ""},
                created_at=now,
                updated_at=now,
                status_message="Wrapping up visualization and report",
                parent_operation_id="loop-root",
                native_session_id="native-session",
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
        execution_host: str = "",
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
            execution_host=execution_host,
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
        execution_host: str = "",
        graph_target: GraphTargetRef | None = None,
        chat_id: str | None = None,
    ) -> WatcherRecord:
        self.store.create_watchers(
            [
                self._watcher(
                    watcher_id,
                    continuation=continuation,
                    origin_operation_id=origin_operation_id,
                    execution_host=execution_host,
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
