"""Compute effects shared by the concrete Work and Experiment-loop owners."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path, PurePosixPath

from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import (
    CommandRequest,
    CommandResponse,
    LaunchCommandRequest,
)
from rcp.agents.write_scope import ProjectWriteScope, _canonical_directories
from rcp.background import AgentTaskExecution
from rcp.compute_jobs.backend_context import (
    ComputeBackendProfile,
    ComputeProbeStaleError,
    resolve_context,
)
from rcp.compute_jobs.job_managers import JOB_MANAGERS
from rcp.compute_jobs.jobs import (
    helper_startup_state,
    helper_watch_spec,
    launch_compute_job,
    refresh_compute_job,
)
from rcp.compute_jobs.models import ComputeBackendProbe, ComputeLaunchRequest
from rcp.compute_jobs.probe import probe_compute_backend
from rcp.compute_jobs.text import safe_compute_diagnostic
from rcp.config import Manifest
from rcp.limits import COMPUTE_JOB_STARTUP_CHECK_SECONDS
from rcp.transport import RemoteRunStage


@dataclass(frozen=True)
class WorkComputeCommands:
    execution: AgentTaskExecution
    manifest: Manifest
    write_scope: ProjectWriteScope
    remote_stage: RemoteRunStage | None
    episode_id: str | None

    @property
    def data_dir(self) -> Path:
        return self.execution.store.path.parent

    def __call__(self, request: CommandRequest, identity: CommandTurnIdentity) -> CommandResponse:
        if not isinstance(request, LaunchCommandRequest) or request.verb not in self.allowed_verbs:
            return self._refused(request, "This Work turn does not authorize that command.")
        if identity.authority != "broker" or identity.task_id != self.execution.operation_id:
            return self._refused(request, "Compute requires this Work turn's broker authority.")
        store = self.execution.store
        key = request.idempotency_key
        arguments = hashlib.sha256(
            json.dumps(
                request.arguments.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        # A Resume or Retry is a new task attempt; the key still names the first launch.
        previous = store.compute_command_receipts(self.recovery_lineage, request.verb, key)
        # An attempt that proved it submitted nothing leaves the key free after it.
        free_after = max((i for i, item in enumerate(previous) if _not_submitted(item)), default=-1)
        previous = previous[free_after + 1 :]
        response = None
        if previous:
            if previous[0]["arguments_sha256"] != arguments:
                response = CommandResponse(
                    request_id=request.request_id,
                    status="invalid",
                    message="This key already names different arguments.",
                )
            else:
                result = next(
                    (
                        item.get("response")
                        for item in reversed(previous)
                        if item.get("response") is not None
                    ),
                    None,
                )
                response = (
                    CommandResponse.model_validate({**result, "request_id": request.request_id})
                    if result
                    else _uncertain_launch(request, previous[0])
                )
            store.record_agent_task_receipt(
                self.execution.operation_id,
                "compute_command_replayed",
                {
                    "verb": request.verb,
                    "key": key,
                    "arguments_sha256": arguments,
                    "status": response.status,
                    "message": (response.message or "")[:600],
                },
                tier="diagnostic",
            )
        if response is None:
            store.record_agent_task_receipt(
                self.execution.operation_id,
                "compute_command_started",
                {"verb": request.verb, "key": key, "arguments_sha256": arguments},
                tier="diagnostic",
            )
            try:
                response = self._execute(request)
            except (ValueError, KeyError) as exc:
                response = CommandResponse(
                    request_id=request.request_id,
                    status="invalid",
                    message=safe_compute_diagnostic(str(exc)),
                )
            except Exception as exc:
                response = CommandResponse(
                    request_id=request.request_id,
                    status="unavailable",
                    message=safe_compute_diagnostic(str(exc)),
                )
            store.record_agent_task_receipt(
                self.execution.operation_id,
                "compute_command_result",
                {
                    "verb": request.verb,
                    "key": key,
                    "arguments_sha256": arguments,
                    "response": response.model_dump(mode="json"),
                },
                tier="diagnostic",
            )
        store.record_agent_task_event(
            self.execution.operation_id,
            f"Compute {request.verb}: {response.status}. Replayed: {bool(previous)}.",
            level="info" if response.status == "ok" else "warning",
        )
        return response

    @cached_property
    def helper_backend(self) -> ComputeBackendProfile | None:
        try:
            _context, backend = resolve_context(self.manifest, self.write_scope.execution_machine)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return None
        return backend

    @property
    def allowed_verbs(self) -> frozenset[str]:
        return frozenset({"launch"}) if self.helper_backend is not None else frozenset()

    def execution_instructions(self, launch_command: str) -> str:
        waiting = (
            "Short compute jobs can run normally without a watcher. For long-running work "
            "(roughly over 10 minutes), hand off waiting with watch.json instead of repeatedly polling. "
            "This is planning guidance, not an enforced time limit. "
        )
        helper = (
            "A long-lived process such as a dashboard uses the ordinary launch and its watcher; "
            "the human stops it with Cancel. "
            "For work that must outlive this agent turn, use the process launch helper: "
            f"`{launch_command}`. Copy its returned watcher object into watch.json's external list "
            "before ending the turn while that work is running. The helper provides check_command, "
            "log_path, cwd and cancel_command; check_command is for RCP's watcher and may not run "
            "inside your sandbox. The launch response's startup object reports the job a moment "
            "after start: running means RCP saw it alive, unknown means RCP could not check it "
            "(see diagnostic), and any other status means it already ended and log_tail says why. "
            "Confirm success from startup, not from a port or URL another process could answer. "
            "A repeated launch must use the same idempotency key and arguments, including after "
            "a Resume or Retry of this turn. When a launch returns result.delivery `unknown`, RCP "
            "may still be working on it: repeat the exact same command to get its result. "
            "`submitted: false` means nothing started; the same key runs again once the owner is "
            "ready. A `disposition` of `uncertain` means it may be running: never submit it again "
            "under any key; tell the human. RCP refuses helper launches without reliable process "
            "ownership."
        )

        availability = (
            f"Helper owner: {self.helper_backend.id}."
            if "launch" in self.allowed_verbs
            else "No helper owner resolves on this machine; launch is unavailable."
        )
        machine = self.manifest.machine_map[self.write_scope.execution_machine]
        manager = JOB_MANAGERS.get(machine.compute.job_manager) if machine.compute else None
        return "\n\n".join(
            part
            for part in (waiting + helper, availability, manager.instructions if manager else "")
            if part
        )

    def _execute(self, request: LaunchCommandRequest) -> CommandResponse:
        launch = ComputeLaunchRequest.model_validate(request.arguments.model_dump())
        canonical, _home = _canonical_directories(
            [launch.cwd],
            remote_stage=self.remote_stage,
            require_writable=True,
        )
        cwd = PurePosixPath(canonical[launch.cwd])
        if not any(
            cwd == PurePosixPath(root) or PurePosixPath(root) in cwd.parents
            for root in self.write_scope.writable_roots
        ):
            raise ValueError("Compute cwd must be inside this turn's writable roots.")
        workspace = PurePosixPath(self.write_scope.workspace_root)
        in_workspace = cwd == workspace or workspace in cwd.parents
        if any(
            (cwd == PurePosixPath(root) or PurePosixPath(root) in cwd.parents)
            # RCP storage covered by a grant can hold this launch's own
            # workspace, which stays writable; a deeper input still refuses.
            and not (in_workspace and PurePosixPath(root) in workspace.parents)
            for root in self.write_scope.protected_write_paths
        ):
            raise ValueError("Compute cwd is a protected write path.")
        launch = launch.model_copy(update={"cwd": str(cwd)})
        machine = self.write_scope.execution_machine
        probe = probe_compute_backend(self.manifest, machine, "helper", data_dir=self.data_dir)
        self.execution.store.record_compute_backend_probe(
            self.write_scope.project_id, probe, "helper"
        )
        try:
            return self._launch(request, launch, probe)
        except ComputeProbeStaleError:
            probe = probe_compute_backend(self.manifest, machine, "helper", data_dir=self.data_dir)
            self.execution.store.record_compute_backend_probe(
                self.write_scope.project_id, probe, "helper"
            )
            return self._launch(request, launch, probe)

    def _launch(
        self,
        request: LaunchCommandRequest,
        launch: ComputeLaunchRequest,
        probe: ComputeBackendProbe,
    ) -> CommandResponse:
        if not probe.ready:
            return CommandResponse(
                request_id=request.request_id,
                status="unavailable",
                message=probe.diagnostic,
                result={
                    "diagnostic": probe.diagnostic,
                    "required_action": probe.required_action,
                    # Nothing was submitted, so the same key may run again once ready.
                    "submitted": False,
                },
            )
        job = launch_compute_job(
            self.execution.store,
            self.manifest,
            launch,
            data_dir=self.data_dir,
            project_id=self.write_scope.project_id,
            origin_operation_id=self.execution.operation_id,
            episode_id=self.episode_id,
            execution_machine=self.write_scope.execution_machine,
            writable_roots=self.write_scope.writable_roots,
            protected_paths=self.write_scope.protected_write_paths,
            probe=probe,
        )
        time.sleep(COMPUTE_JOB_STARTUP_CHECK_SECONDS)
        job = refresh_compute_job(
            self.execution.store, self.manifest, job.job_id, data_dir=self.data_dir
        )
        result = {
            "watcher": helper_watch_spec(job),
            "startup": helper_startup_state(self.manifest, job),
        }
        return CommandResponse(request_id=request.request_id, status="ok", result=result)

    @cached_property
    def recovery_lineage(self) -> tuple[str, ...]:
        """This attempt and the earlier attempts of the same turn it recovers, newest first."""

        store = self.execution.store
        operation_ids = [self.execution.operation_id]
        current = store.agent_task(self.execution.operation_id)
        cause = self.execution.continuation
        while current and cause in {"resume", "retry", "graph_repair", "handoff"}:
            parent = (
                store.agent_task(current.parent_operation_id)
                if current.parent_operation_id
                else None
            )
            if (
                parent is None
                or parent.operation_id in operation_ids
                or parent.project_id != self.write_scope.project_id
                or parent.kind != current.kind
            ):
                break
            operation_ids.append(parent.operation_id)
            current = parent
            cause = store.agent_task_continuation_cause(current.operation_id)
        return tuple(operation_ids)

    def _refused(self, request: CommandRequest, message: str) -> CommandResponse:
        self.execution.store.record_agent_task_event(
            self.execution.operation_id,
            f"Compute {request.verb} refused: {message}",
            level="warning",
        )
        return CommandResponse(request_id=request.request_id, status="invalid", message=message)

    def validate_handoff(self, observed_check_commands: set[str]) -> None:
        store = self.execution.store
        operation_ids = set(self.recovery_lineage)
        unobserved = []
        for job in store.running_compute_jobs():
            if (
                job.project_id != self.write_scope.project_id
                or job.origin_operation_id not in operation_ids
            ):
                continue
            job = refresh_compute_job(store, self.manifest, job.job_id, data_dir=self.data_dir)
            watcher = helper_watch_spec(job)
            if job.status == "running" and watcher["check_command"] not in observed_check_commands:
                unobserved.append(watcher)
        if unobserved:
            raise ValueError(
                "Running helper jobs require shell watchers in watch.json: "
                + json.dumps(unobserved)
            )


def _not_submitted(receipt: dict[str, object]) -> bool:
    response = receipt.get("response")
    result = response.get("result") if isinstance(response, dict) else None
    return isinstance(result, dict) and result.get("submitted") is False


def _uncertain_launch(request: CommandRequest, started: dict[str, object]) -> CommandResponse:
    """A launch that started but never recorded a result: never resubmit it blindly."""

    operation_id = started.get("operation_id")
    return CommandResponse(
        request_id=request.request_id,
        status="unavailable",
        message=(
            "An earlier launch with this key started but RCP never recorded its result, so it "
            "may be running. Do not launch it again under any key; ask the human to check this "
            f"project's compute jobs in Runs for task {operation_id}."
        ),
        result={
            "disposition": "uncertain",
            "original_operation_id": operation_id,
            "key": request.idempotency_key,
        },
    )
