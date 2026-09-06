"""Compute effects shared by the concrete Work and Experiment-loop owners."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import (
    CancelCommandRequest,
    CommandRequest,
    CommandResponse,
    JobStatusCommandRequest,
    LaunchCommandRequest,
)
from rcp.agents.write_scope import ProjectWriteScope, _canonical_directories
from rcp.background import AgentTaskExecution
from rcp.compute_jobs.backend_context import ComputeProbeStaleError
from rcp.compute_jobs.jobs import (
    cancel_compute_job,
    launch_compute_job,
    read_job_log_tail,
    refresh_compute_job,
)
from rcp.compute_jobs.models import ComputeBackendProbe, ComputeLaunchRequest
from rcp.compute_jobs.probe import probe_compute_backend
from rcp.compute_jobs.text import safe_compute_diagnostic
from rcp.config import Manifest
from rcp.limits import COMPUTE_COMMAND_LOG_TAIL_MAX_BYTES
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
        if not isinstance(
            request, (LaunchCommandRequest, JobStatusCommandRequest, CancelCommandRequest)
        ):
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

    def _execute(self, request: CommandRequest) -> CommandResponse:
        store = self.execution.store
        if isinstance(request, LaunchCommandRequest):
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
            machine = self.write_scope.execution_machine
            probe = store.compute_backend_probe(self.write_scope.project_id, machine)
            if probe is None or not probe.ready:
                probe = probe_compute_backend(self.manifest, machine, data_dir=self.data_dir)
                store.record_compute_backend_probe(self.write_scope.project_id, probe)
            launch = launch.model_copy(update={"cwd": str(cwd)})
            try:
                return self._launch(request, launch, probe)
            except ComputeProbeStaleError:
                probe = probe_compute_backend(self.manifest, machine, data_dir=self.data_dir)
                store.record_compute_backend_probe(self.write_scope.project_id, probe)
                return self._launch(request, launch, probe)
        else:
            assert isinstance(request, (JobStatusCommandRequest, CancelCommandRequest))
            job = store.compute_job(request.arguments.job_id)
            if job is None or job.project_id != self.write_scope.project_id:
                raise ValueError("Compute job does not belong to this project.")
            if isinstance(request, CancelCommandRequest):
                job = cancel_compute_job(
                    store,
                    self.manifest,
                    job.job_id,
                    self.execution.operation_id,
                    data_dir=self.data_dir,
                )
                result = {"job_id": job.job_id, "status": job.status}
            else:
                job = refresh_compute_job(store, self.manifest, job.job_id, data_dir=self.data_dir)
                result = {
                    "status": job.status,
                    "exit_status": job.exit_status,
                    "started_at": job.started_at,
                    "ended_at": job.ended_at,
                    "log_tail": read_job_log_tail(
                        store,
                        self.manifest,
                        job.job_id,
                        data_dir=self.data_dir,
                        max_bytes=COMPUTE_COMMAND_LOG_TAIL_MAX_BYTES,
                    ),
                }
        return CommandResponse(request_id=request.request_id, status="ok", result=result)

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
            probe=probe,
        )
        result = {"job_id": job.job_id, "log_path": job.log_path, "backend_id": job.backend_id}
        return CommandResponse(request_id=request.request_id, status="ok", result=result)

    def validate_handoff(self, observed_job_ids: set[str]) -> None:
        unobserved = []
        for job in self.execution.store.running_compute_jobs():
            if (
                job.project_id != self.write_scope.project_id
                or job.origin_operation_id != self.execution.operation_id
            ):
                continue
            job = refresh_compute_job(
                self.execution.store, self.manifest, job.job_id, data_dir=self.data_dir
            )
            if job.status == "running" and job.job_id not in observed_job_ids:
                unobserved.append(job.job_id)
        if unobserved:
            raise ValueError(
                "Running compute jobs require job observers in watch.json: "
                + ", ".join(sorted(unobserved))
            )
