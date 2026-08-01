from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass
from typing import Literal

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.artifacts import AgentArtifactDescriptor
from rcp.limits import CHAT_ARTIFACT_MAX_COUNT
from rcp.providers import classify_terminal_error
from rcp.service import CoachRequest, GraphUpdateResult, RunRequest
from rcp.storage import AgentTaskKind, AgentTaskRecord, AppStore

AgentTaskRequest = RunRequest | CoachRequest
AgentTaskContinuation = Literal["fresh", "resume", "correction", "handoff", "graph_repair"]


@dataclass
class AgentTaskExecution:
    operation_id: str
    store: AppStore
    control: AgentProcessControl
    stage_host: str | None = None
    stage_root: str | None = None
    continuation: AgentTaskContinuation = "fresh"
    retry_feedback: tuple[str, ...] = ()

    @property
    def reuses_native_checkpoint(self) -> bool:
        return self.continuation in {"resume", "correction", "graph_repair"}

    def checkpoint_stage(self, host: str, root: str) -> None:
        self.stage_host = host or None
        self.stage_root = root
        self.store.checkpoint_agent_task(
            self.operation_id,
            stage_host=host or None,
            stage_root=root,
        )
        self.store.record_agent_task_receipt(
            self.operation_id,
            "stage_checkpoint",
            {"remote": bool(host), "stage_available": bool(root)},
            tier="diagnostic",
        )


AgentTaskStream = Callable[
    [str, AgentTaskKind, AgentTaskRequest, AgentTaskExecution], AsyncIterator[str]
]


@dataclass(frozen=True)
class AgentTaskOutcome:
    applied_revision: int | None
    messages: list[str]
    artifacts: list[AgentArtifactDescriptor]
    graph_update: GraphUpdateResult | None = None


class TaskPaused(RuntimeError):
    pass


class TaskFailed(RuntimeError):
    """A task that failed after producing output worth keeping.

    A chat turn can answer the human and only then have its graph change rejected.
    The answer is already written and already useful, so it travels with the
    failure instead of being dropped with the stream.
    """

    def __init__(
        self,
        message: str,
        messages: list[str],
        artifacts: list[AgentArtifactDescriptor],
    ) -> None:
        super().__init__(message)
        self.messages = messages
        self.artifacts = artifacts


class BackgroundAgentTasks:
    def __init__(self, store: AppStore, stream: AgentTaskStream) -> None:
        self.store = store
        self.stream = stream
        self._controls: dict[str, AgentProcessControl] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._controls_lock = threading.Lock()
        self.store.interrupt_active_agent_tasks()

    def start(
        self,
        project_id: str,
        kind: AgentTaskKind,
        request: AgentTaskRequest,
    ) -> AgentTaskRecord:
        if kind in {"seed", "refresh"} and request.session_id:
            raise ValueError(
                "Seed and refresh sessions can only be resumed from an RCP background "
                "task checkpoint."
            )
        self._validate_request_type(kind, request)
        request_data = request.model_dump(mode="json")
        estimate, samples = self.store.agent_task_estimate(project_id, kind, request_data)
        return self._create_and_spawn(
            project_id,
            kind,
            request,
            estimate_seconds=estimate,
            estimate_samples=samples,
        )

    def resume(self, operation_id: str) -> AgentTaskRecord:
        previous = self._require_operation(operation_id)
        if not previous.can_resume or not previous.native_session_id:
            raise ValueError(
                "This task has no resumable native agent checkpoint. Retry it instead."
            )
        if not self._session_is_rcp_owned(previous):
            raise ValueError(
                "This task's native session was not checkpointed or validated by RCP. "
                "Retry it instead."
            )
        request = self._request_from_record(previous).model_copy(
            update={"session_id": previous.native_session_id}
        )
        return self._create_and_spawn(
            previous.project_id,
            previous.kind,
            request,
            parent=previous,
            continuation="resume",
            estimate_seconds=previous.estimate_seconds,
            estimate_samples=previous.estimate_samples,
            stage_host=previous.stage_host,
            stage_root=previous.stage_root,
        )

    def retry(
        self,
        operation_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        run_on: str | None = None,
    ) -> AgentTaskRecord:
        previous = self._require_operation(operation_id)
        if not previous.can_retry:
            raise ValueError("Only a paused, interrupted, or failed task can be retried.")
        original = self._request_from_record(previous)
        updates = {
            key: value
            for key, value in {
                "provider": provider,
                "model": model,
                "reasoning": reasoning,
                "run_on": run_on,
            }.items()
            if value is not None
        }
        request = type(original).model_validate(
            {
                **original.model_dump(mode="json"),
                **updates,
                "session_id": None,
            }
        )
        same_provider = request.provider == original.provider
        same_execution_host = request.run_on == original.run_on
        session_limit = self._failure_is_session_limit(previous)
        continuation_context_unavailable = self._continuation_context_is_unavailable(previous)
        owned_checkpoint = (
            bool(previous.native_session_id)
            and bool(previous.stage_root)
            and self._session_is_rcp_owned(previous)
        )
        resume_same_provider = (
            previous.status == "failed"
            and same_provider
            and same_execution_host
            and owned_checkpoint
            and not session_limit
            and not continuation_context_unavailable
        )
        if resume_same_provider:
            request = request.model_copy(update={"session_id": previous.native_session_id})
            return self._create_and_spawn(
                previous.project_id,
                previous.kind,
                request,
                parent=previous,
                continuation="correction",
                estimate_seconds=previous.estimate_seconds,
                estimate_samples=previous.estimate_samples,
                stage_host=previous.stage_host,
                stage_root=previous.stage_root,
            )
        estimate, samples = self.store.agent_task_estimate(
            previous.project_id,
            previous.kind,
            request.model_dump(mode="json"),
        )
        retried = self._create_and_spawn(
            previous.project_id,
            previous.kind,
            request,
            parent=previous,
            continuation="handoff",
            estimate_seconds=estimate,
            estimate_samples=samples,
        )
        if same_provider and session_limit:
            self.store.record_agent_task_receipt(
                retried.operation_id,
                "native_resume_skipped",
                {"classification": "session_limit"},
                tier="diagnostic",
            )
            self.store.record_agent_task_event(
                retried.operation_id,
                "The provider session limit was exhausted; starting a clean retry.",
                level="warning",
            )
        elif previous.status == "failed" and same_provider and not resume_same_provider:
            reason = (
                "execution host changed"
                if not same_execution_host
                else "the saved continuation context is unavailable"
                if continuation_context_unavailable
                else "the prior task has no complete RCP-owned native checkpoint and stage"
            )
            self.store.record_agent_task_receipt(
                retried.operation_id,
                "native_resume_unavailable",
                {"reason": reason},
                tier="diagnostic",
            )
            self.store.record_agent_task_event(
                retried.operation_id,
                f"Native resume is unavailable because {reason}; starting a clean retry.",
                level="warning",
            )
        return retried

    def repair_graph_update(self, operation_id: str) -> AgentTaskRecord:
        """Create one idempotent patch-only continuation for a rejected Work result."""

        previous = self._require_operation(operation_id)
        if previous.kind not in {"node_chat", "project_chat"}:
            raise ValueError("Only a conversation Work task can repair a graph update.")
        request = self._request_from_record(previous)
        if not isinstance(request, RunRequest) or request.mode != "work":
            raise ValueError("Only a Work turn can repair a graph update.")
        if (
            not previous.native_session_id
            or not previous.stage_root
            or not self._session_is_rcp_owned(previous)
        ):
            raise ValueError(
                "The rejected graph update has no retained RCP-owned session and stage. "
                "Start a new Work turn instead."
            )
        previous = self.store.claim_agent_task_graph_repair(operation_id)
        request = request.model_copy(
            update={"session_id": previous.native_session_id, "message": None}
        )
        try:
            return self._create_and_spawn(
                previous.project_id,
                previous.kind,
                request,
                parent=previous,
                continuation="graph_repair",
                estimate_seconds=previous.estimate_seconds,
                estimate_samples=previous.estimate_samples,
                stage_host=previous.stage_host,
                stage_root=previous.stage_root,
            )
        except Exception:
            self.store.restore_agent_task_graph_repair(operation_id)
            raise

    def pause(self, operation_id: str) -> AgentTaskRecord:
        record = self.store.request_agent_task_pause(operation_id)
        with self._controls_lock:
            control = self._controls.get(operation_id)
        if control is not None:
            control.request_pause()
        return record

    def shutdown(self, *, timeout: float = 7.0) -> None:
        """Pause live subprocesses before the web process exits."""
        with self._controls_lock:
            active = list(self._controls.items())
            workers = [self._workers.get(operation_id) for operation_id, _ in active]
        for operation_id, control in active:
            with suppress(ValueError):
                self.store.request_agent_task_pause(operation_id, requested_by="shutdown")
            control.request_pause()
        deadline = time.monotonic() + timeout
        for worker in workers:
            if worker is None or worker is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)

    def _create_and_spawn(
        self,
        project_id: str,
        kind: AgentTaskKind,
        request: AgentTaskRequest,
        *,
        estimate_seconds: float,
        estimate_samples: int,
        parent: AgentTaskRecord | None = None,
        continuation: AgentTaskContinuation = "fresh",
        stage_host: str | None = None,
        stage_root: str | None = None,
    ) -> AgentTaskRecord:
        operation_id = str(uuid.uuid4())
        now = self.store.now()
        reuses_native_checkpoint = continuation in {"resume", "correction", "graph_repair"}
        verb = (
            "repair its graph update"
            if continuation == "graph_repair"
            else "resume"
            if continuation == "resume"
            else "retry"
            if parent
            else "start"
        )
        record = self.store.create_agent_task(
            AgentTaskRecord(
                operation_id=operation_id,
                project_id=project_id,
                kind=kind,
                status="queued",
                request=request.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
                status_message=f"Waiting for the background worker to {verb}.",
                attempt=(parent.attempt + 1) if parent else 1,
                parent_operation_id=parent.operation_id if parent else None,
                native_session_id=request.session_id,
                stage_host=stage_host,
                stage_root=stage_root,
                estimate_seconds=estimate_seconds,
                estimate_samples=estimate_samples,
                phase="queued",
                last_activity_at=now,
            )
        )
        self.store.record_agent_task_receipt(
            operation_id,
            "operation_created",
            {
                "kind": kind,
                "attempt": record.attempt,
                "has_parent": parent is not None,
                "continuation_cause": continuation,
                "resumed": reuses_native_checkpoint,
            },
        )
        if parent:
            action = (
                "Repairing the graph update from"
                if continuation == "graph_repair"
                else "Resuming"
                if continuation == "resume"
                else "Retrying"
            )
            feedback = "" if continuation == "resume" else " with prior failure diagnostics"
            self.store.record_agent_task_event(
                operation_id,
                f"{action} task {parent.operation_id[:8]} as attempt {record.attempt}{feedback}.",
            )
        else:
            self.store.record_agent_task_event(operation_id, "Agent task queued.")
        control = AgentProcessControl()
        with self._controls_lock:
            self._controls[operation_id] = control
        worker = threading.Thread(
            target=self._run,
            args=(record, request, control, continuation),
            name=f"rcp-{kind}-{operation_id[:8]}",
            daemon=True,
        )
        with self._controls_lock:
            self._workers[operation_id] = worker
        worker.start()
        return record

    def _run(
        self,
        record: AgentTaskRecord,
        request: AgentTaskRequest,
        control: AgentProcessControl,
        continuation: AgentTaskContinuation,
    ) -> None:
        operation_id = record.operation_id
        current = self.store.agent_task(operation_id)
        if current is None:
            return
        if current.status == "pausing" or control.pause_requested.is_set():
            self.store.pause_agent_task(operation_id)
            self._forget_control(operation_id)
            return
        self.store.mark_agent_task_running(operation_id)
        execution = AgentTaskExecution(
            operation_id=operation_id,
            store=self.store,
            control=control,
            stage_host=record.stage_host,
            stage_root=record.stage_root,
            continuation=continuation,
            retry_feedback=(
                ()
                if continuation in {"fresh", "resume", "graph_repair"}
                else self._retry_feedback(record)
            ),
        )
        try:
            outcome = asyncio.run(self._consume(record.project_id, record.kind, request, execution))
        except TaskPaused:
            self.store.pause_agent_task(operation_id)
        except Exception as exc:  # The persisted task is the API error boundary.
            self.store.record_agent_task_receipt(
                operation_id,
                "operation_exception",
                {"exception_type": type(exc).__name__},
                tier="diagnostic",
            )
            partial = exc.messages if isinstance(exc, TaskFailed) else []
            artifacts = exc.artifacts if isinstance(exc, TaskFailed) else []
            result: dict[str, object] = {"messages": partial}
            if artifacts:
                result["artifacts"] = [item.model_dump(mode="json") for item in artifacts]
            self.store.fail_agent_task(
                operation_id,
                str(exc),
                result=result if partial or artifacts else None,
            )
        else:
            # Only ingest runs owe a graph revision. A chat turn answers a
            # question; changing the graph is the exception, not the contract.
            if record.kind in {"seed", "refresh"} and outcome.applied_revision is None:
                if control.pause_requested.is_set():
                    self.store.pause_agent_task(operation_id)
                else:
                    self.store.record_agent_task_receipt(
                        operation_id,
                        "missing_applied_revision",
                        {"agent_stream_completed": True},
                        tier="diagnostic",
                    )
                    self.store.fail_agent_task(
                        operation_id,
                        "The agent stopped without applying a graph revision.",
                    )
            else:
                result: dict[str, object] = {"messages": outcome.messages}
                if outcome.artifacts:
                    result["artifacts"] = [
                        item.model_dump(mode="json") for item in outcome.artifacts
                    ]
                if outcome.graph_update is not None:
                    result["graph_update"] = outcome.graph_update.model_dump(mode="json")
                self.store.complete_agent_task(
                    operation_id,
                    applied_revision=outcome.applied_revision,
                    result=result,
                )
        finally:
            self._forget_control(operation_id)

    async def _consume(
        self,
        project_id: str,
        kind: AgentTaskKind,
        request: AgentTaskRequest,
        execution: AgentTaskExecution,
    ) -> AgentTaskOutcome:
        applied_revision: int | None = None
        messages: list[str] = []
        artifacts: list[AgentArtifactDescriptor] = []
        graph_update: GraphUpdateResult | None = None
        # `aclosing` so an error or a pause closes the run generator here rather
        # than leaving it suspended for the garbage collector: its `finally` is
        # what releases the canonical run lock and retains the scratch folder.
        async with aclosing(self.stream(project_id, kind, request, execution)) as stream:
            async for frame in stream:
                event = _event_from_sse(frame)
                if event.event == "error":
                    raise TaskFailed(event.text or "The agent task failed.", messages, artifacts)
                if event.event == "paused":
                    raise TaskPaused(event.text)
                if event.event == "session" and event.session_id:
                    self.store.checkpoint_agent_task(
                        execution.operation_id,
                        native_session_id=event.session_id,
                    )
                    self.store.record_agent_task_receipt(
                        execution.operation_id,
                        "native_agent_checkpoint",
                        {
                            "provider": request.provider,
                            "run_on": request.run_on,
                            "native_session_id": event.session_id,
                            "continuation_cause": execution.continuation,
                            "resumed": execution.reuses_native_checkpoint,
                        },
                        tier="diagnostic",
                    )
                    self.store.update_agent_task_message(
                        execution.operation_id,
                        "The agent is reading project evidence.",
                        phase="agent",
                        event=True,
                    )
                if event.event == "message":
                    revision = _applied_revision(event.text)
                    parsed_graph_update = _graph_update(event.text)
                    if parsed_graph_update is not None:
                        graph_update = parsed_graph_update
                    if revision is not None:
                        applied_revision = revision
                        self.store.update_agent_task_message(
                            execution.operation_id,
                            "Applying the graph update.",
                            phase="applying",
                            event=True,
                        )
                    elif parsed_graph_update is None and event.text.strip() and len(messages) < 32:
                        messages.append(event.text.strip()[:16_000])
                if event.event == "answer" and event.text.strip() and len(messages) < 32:
                    messages.append(event.text.strip()[:16_000])
                if (
                    event.event == "artifact"
                    and event.artifact is not None
                    and len(artifacts) < CHAT_ARTIFACT_MAX_COUNT
                    and event.artifact.artifact_id not in {item.artifact_id for item in artifacts}
                ):
                    artifacts.append(event.artifact)
                if event.event == "raw" and event.text.startswith(
                    "Omitted oversized provider event"
                ):
                    self.store.record_agent_task_event(
                        execution.operation_id,
                        event.text,
                        level="warning",
                    )
                    self.store.record_agent_task_receipt(
                        execution.operation_id,
                        "provider_event_omitted",
                        {"reason": "provider_event_exceeded_stream_limit"},
                        tier="trace",
                    )
        return AgentTaskOutcome(
            applied_revision=applied_revision,
            messages=messages,
            artifacts=artifacts,
            graph_update=graph_update,
        )

    def _require_operation(self, operation_id: str) -> AgentTaskRecord:
        record = self.store.agent_task(operation_id)
        if record is None:
            raise KeyError(operation_id)
        return record

    def _session_is_rcp_owned(self, record: AgentTaskRecord) -> bool:
        if record.kind in {"node_chat", "project_chat", "paper_coach"}:
            return bool(record.native_session_id)
        seen: set[str] = set()
        current = record
        while current.operation_id not in seen:
            seen.add(current.operation_id)
            request = self._request_from_record(current)
            if request.session_id is None:
                return bool(current.native_session_id)
            if not current.parent_operation_id:
                return False
            parent = self.store.agent_task(current.parent_operation_id)
            if (
                parent is None
                or parent.project_id != current.project_id
                or parent.kind != current.kind
                or parent.native_session_id != request.session_id
            ):
                return False
            current = parent
        return False

    def _retry_feedback(self, record: AgentTaskRecord) -> tuple[str, ...]:
        feedback: list[str] = []
        seen_operations: set[str] = set()
        seen_errors: set[str] = set()
        parent_id = record.parent_operation_id
        while parent_id and parent_id not in seen_operations and len(feedback) < 3:
            seen_operations.add(parent_id)
            parent = self.store.agent_task(parent_id)
            if (
                parent is None
                or parent.project_id != record.project_id
                or parent.kind != record.kind
            ):
                break
            if parent.error:
                detail = " ".join(parent.error.split())[:1600]
                if detail and detail not in seen_errors:
                    feedback.append(
                        f"Attempt {parent.attempt} ({parent.status}) failed with: {detail}"
                    )
                    seen_errors.add(detail)
            parent_id = parent.parent_operation_id
        return tuple(feedback)

    def _failure_is_session_limit(self, record: AgentTaskRecord) -> bool:
        classified_receipt = any(
            receipt.category == "provider_terminal_error"
            and receipt.payload.get("classification") == "session_limit"
            for receipt in self.store.agent_task_receipts(record.operation_id)
        )
        return classified_receipt or (
            bool(record.error) and classify_terminal_error(record.error or "") == "session_limit"
        )

    def _continuation_context_is_unavailable(self, record: AgentTaskRecord) -> bool:
        return any(
            receipt.category == "continuation_context_unavailable"
            and receipt.payload.get("retry_required") is True
            for receipt in self.store.agent_task_receipts(record.operation_id)
        )

    @staticmethod
    def _request_from_record(record: AgentTaskRecord) -> AgentTaskRequest:
        if record.kind == "paper_coach":
            return CoachRequest.model_validate(record.request)
        return RunRequest.model_validate(record.request)

    @staticmethod
    def _validate_request_type(kind: AgentTaskKind, request: AgentTaskRequest) -> None:
        if kind == "paper_coach" and not isinstance(request, CoachRequest):
            raise TypeError("paper_coach requires a CoachRequest")
        if kind != "paper_coach" and not isinstance(request, RunRequest):
            raise TypeError(f"{kind} requires a RunRequest")

    def _forget_control(self, operation_id: str) -> None:
        with self._controls_lock:
            self._controls.pop(operation_id, None)
            self._workers.pop(operation_id, None)


def _event_from_sse(frame: str) -> AgentEvent:
    data = next(
        (line[6:] for line in frame.splitlines() if line.startswith("data: ")),
        "",
    )
    if not data:
        raise ValueError("The background task emitted an invalid event.")
    return AgentEvent.model_validate_json(data)


def _applied_revision(text: str) -> int | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or "applied_revision" not in value:
        return None
    try:
        return int(value["applied_revision"])
    except (TypeError, ValueError):
        return None


def _graph_update(text: str) -> GraphUpdateResult | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or "graph_update" not in value:
        return None
    try:
        return GraphUpdateResult.model_validate(value["graph_update"])
    except (TypeError, ValueError):
        return None
