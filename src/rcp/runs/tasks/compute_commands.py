"""Compute effects shared by the concrete Work and Experiment-loop owners."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import (
    CommandRequest,
    CommandResponse,
    LaunchCommandRequest,
)
from rcp.agents.write_scope import ProjectWriteScope, _canonical_directories
from rcp.background import AgentTaskExecution
from rcp.compute_jobs.backend_context import ComputeProbeStaleError
from rcp.compute_jobs.jobs import (
    helper_watch_spec,
    launch_compute_job,
    refresh_compute_job,
)
from rcp.compute_jobs.models import ComputeBackendProbe, ComputeLaunchRequest
from rcp.compute_jobs.probe import probe_compute_backend
from rcp.compute_jobs.text import safe_compute_diagnostic
from rcp.config import Manifest
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
            return CommandResponse(
                request_id=request.request_id,
                status="invalid",
                message="This Work turn does not authorize that command.",
            )
        if identity.authority != "broker" or identity.task_id != self.execution.operation_id:
            return CommandResponse(
                request_id=request.request_id,
                status="invalid",
                message="Compute requires this Work turn's broker authority.",
            )
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
        previous = store.compute_command_receipts(self.execution.operation_id, request.verb, key)
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
                    else CommandResponse(
                        request_id=request.request_id,
                        status="unavailable",
                        message="The earlier command has an uncertain outcome; inspect its retained receipt.",
                    )
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

    @property
    def allowed_verbs(self) -> frozenset[str]:
        machine = self.manifest.machine_map[self.write_scope.execution_machine]
        return (
            frozenset()
            if machine.compute and machine.compute.job_manager
            else frozenset({"launch"})
        )

    def execution_instructions(self, launch_command: str) -> str:
        waiting = (
            "Short compute jobs can run normally without a watcher. For long-running work "
            "(roughly over 10 minutes), hand off waiting with watch.json instead of repeatedly polling. "
            "This is planning guidance, not an enforced time limit. "
        )
        if not self.allowed_verbs:
            return waiting + (
                "This machine uses Slurm. Submit directly with your own commands or scripts; "
                "choose the job's resources and submission arguments yourself. Slurm owns the "
                "detached job. Supply a shell check_command, log_path and cwd in watch.json, "
                "and a cancel_command such as scancel for the exact submitted job if human Cancel "
                "should be available. Queued jobs are still active. RCP checks account prerequisites "
                "but does not submit, size, or interpret scheduler jobs."
            )
        return waiting + (
            "For work that must outlive this agent turn, use the process launch helper: "
            f"`{launch_command}`. Copy its returned watcher object into watch.json's external list "
            "before ending the turn while that work is running. The helper provides check_command, "
            "log_path, cwd and cancel_command. A repeated launch must use the same idempotency key "
            "and arguments. If submission is uncertain, inspect the retained receipt; do not submit "
            "again under a new key. RCP refuses helper launches without reliable process ownership."
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
        if any(
            cwd == PurePosixPath(root) or PurePosixPath(root) in cwd.parents
            for root in self.write_scope.protected_write_paths
        ):
            raise ValueError("Compute cwd is a protected write path.")
        launch = launch.model_copy(update={"cwd": str(cwd)})
        machine = self.write_scope.execution_machine
        probe = probe_compute_backend(self.manifest, machine, data_dir=self.data_dir)
        self.execution.store.record_compute_backend_probe(self.write_scope.project_id, probe)
        try:
            return self._launch(request, launch, probe)
        except ComputeProbeStaleError:
            probe = probe_compute_backend(self.manifest, machine, data_dir=self.data_dir)
            self.execution.store.record_compute_backend_probe(self.write_scope.project_id, probe)
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
        result = {"watcher": helper_watch_spec(job)}
        return CommandResponse(request_id=request.request_id, status="ok", result=result)

    def validate_handoff(self, observed_check_commands: set[str]) -> None:
        store = self.execution.store
        operation_ids = {self.execution.operation_id}
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
            operation_ids.add(parent.operation_id)
            current = parent
            cause = store.agent_task_continuation_cause(current.operation_id)
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
