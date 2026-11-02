"""Per-project admission and Apply for a Space turn; no project lifecycle verbs."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import BulletinCommandRequest, CommandRequest, CommandResponse
from rcp.agents.context import ChatContext
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.agents.project_repositories import write_repositories
from rcp.agents.write_scope import (
    CompositeWriteScope,
    ProjectWriteScope,
    resolve_project_write_scope,
)
from rcp.background import AgentTaskExecution
from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope
from rcp.core.transition_models import GraphTargetRef
from rcp.history.manager import ReplayHalted
from rcp.limits import PATCH_SELF_CHECK_MAX_REQUEST_BYTES
from rcp.runs.bulletin import handle_bulletin
from rcp.runs.graph_history_queries import handle_graph_history_command
from rcp.runs.patch_validator import PatchValidationResult
from rcp.runs.tasks.compute_commands import compute_recovery_lineage
from rcp.service import ProjectService, RunRequest
from rcp.transport import StateUnavailable

if TYPE_CHECKING:
    from rcp.runs.space_chat import SpaceThreadOwner

SPACE_PROJECT_VERBS = frozenset({"validate", "graph_log", "graph_diff", "bulletin"})


@dataclass(frozen=True)
class SpaceProjectTurn:
    project_id: str
    service: ProjectService
    context: ChatContext
    scope: ProjectWriteScope
    operation_id: str
    turn_id: str

    @property
    def patch_path(self) -> Path:
        return Path(self.scope.workspace_root) / "patch.json"

    @property
    def source_effect_id(self) -> str:
        return f"space:{self.turn_id}:{self.project_id}"

    def candidate(self, text: str):
        from rcp.runs.tasks.work import _prepare_work_patch_candidate

        return _prepare_work_patch_candidate(
            self.service,
            text,
            run_truth_scope=self.context.run_truth_scope,
            read_scope=self.context.read_scope,
            source_operation_id=self.operation_id,
            source_effect_id=self.source_effect_id,
        )

    def validate(self, text: str) -> PatchValidationResult:
        from rcp.runs.tasks.work_turn_runtime import validate_work_patch_live

        return validate_work_patch_live(
            self.service,
            text,
            prepare_candidate=self.candidate,
            bounded_messages=lambda *messages: list(messages),
        )

    def command(self, request: CommandRequest, identity: CommandTurnIdentity) -> CommandResponse:
        if request.verb not in SPACE_PROJECT_VERBS:
            return CommandResponse(
                request_id=request.request_id,
                status="invalid",
                message="Space project clients cannot create project lifecycles",
            )
        if isinstance(request, BulletinCommandRequest):
            return handle_bulletin(
                self.service.paper.store,
                self.operation_id,
                request,
                post_allowed=True,
                turn_artifacts=False,
                project_id=self.project_id,
            )
        return handle_graph_history_command(
            self.service, GraphReadScope.resolve(self.project_id, self.service.history), request
        )


def prepare_space_projects(
    owner: SpaceThreadOwner, request: RunRequest, execution: AgentTaskExecution, stage: Path
) -> tuple[list[SpaceProjectTurn], CompositeWriteScope]:
    projects: list[SpaceProjectTurn] = []
    turn_id = compute_recovery_lineage(
        owner.store, execution.operation_id, continuation=execution.continuation
    )[-1]
    for project_id in sorted(owner.store.member_project_ids(owner.thread.user_id)):
        try:
            owner.store.require_project_accepts_new_work(project_id)
            if not owner.catalog.same_machine(project_id):
                continue
            service = owner.catalog.open_execution(project_id)
            machine = service.manifest.machine_map[service.manifest.base_machine]
            if machine.host:
                continue
            truth = write_repositories(service.history.state())
            context = service.assemble_chat(
                request.model_copy(update={"run_on": machine.alias, "run_truth_scope": truth})
            )
            workspace = stage / "projects" / project_id
            workspace.mkdir(parents=True, exist_ok=True)
            scope = resolve_project_write_scope(
                manifest=service.manifest,
                truth_scope=truth,
                project_id=project_id,
                execution_machine=machine.alias,
                capability="work_auto",
                stage_root=str(workspace),
                workspace_root=str(workspace),
                admitted_aliases=context.run_truth_scope,
                repository_pointers=context.repositories,
                remote_stage=None,
                app_data_dir=owner.catalog.data_dir,
                repository_inventory=service.repository_ownership_inventory(project_id=project_id),
            )
            dispatch = AgentDispatchAuthority(
                profile="ordinary",
                task_contract="work_auto",
                scope=AgentDispatchScope(
                    run_truth_scope=sorted(context.run_truth_scope),
                    chat_scope="project",
                    chat_id=owner.thread_id,
                    patch_kind="work",
                ),
            )
            values = (
                execution.operation_id,
                project_id,
                dispatch.model_dump_json(),
                scope.fingerprint,
                GraphTargetRef().model_dump_json(),
            )
            with owner.store.connection() as connection:
                existing = connection.execute(
                    "SELECT * FROM space_turn_project_bindings WHERE operation_id=? AND project_id=?",
                    values[:2],
                ).fetchone()
                if existing is not None and tuple(existing) != values:
                    raise ValueError("Space project binding changed within a turn")
                connection.execute(
                    "INSERT OR IGNORE INTO space_turn_project_bindings VALUES(?,?,?,?,?)", values
                )
            projects.append(
                SpaceProjectTurn(
                    project_id, service, context, scope, execution.operation_id, turn_id
                )
            )
        except (KeyError, OSError, ValueError, StateUnavailable, ReplayHalted) as exc:
            owner.store.record_agent_task_receipt(
                execution.operation_id,
                "space_project_not_admitted",
                {"project_id": project_id, "reason": str(exc)},
            )
    composite = CompositeWriteScope(
        stage_root=str(stage), projects=tuple(project.scope for project in projects)
    )
    if composite.fingerprint is not None:
        owner.store.bind_agent_task_write_scope(
            execution.operation_id,
            project_id=owner.store.space_id,
            stage_host="",
            stage_root=str(stage),
            fingerprint=composite.fingerprint,
        )
    execution.write_scope_fingerprint = composite.fingerprint
    owner.store.record_agent_task_receipt(
        execution.operation_id,
        "space_project_set",
        {
            "project_ids": [project.project_id for project in projects],
            "write_scope_fingerprint": composite.fingerprint,
        },
    )
    return projects, composite


def read_space_output(path: Path) -> str | None:
    if path.parent.resolve() != path.parent:
        raise ValueError("Space output parent changed")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > PATCH_SELF_CHECK_MAX_REQUEST_BYTES:
            raise ValueError("Space output is not a bounded regular file")
        return handle.read()


def clear_space_output(path: Path) -> None:
    """Remove one stage output, including an agent-made directory, without following links."""
    if path.parent.resolve() != path.parent:
        raise ValueError("Space output parent changed")
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def collect_space_projects(
    owner: SpaceThreadOwner,
    execution: AgentTaskExecution,
    projects: list[SpaceProjectTurn],
    stage: Path,
) -> dict[str, Literal["applied", "rejected", "no_patch"]]:
    results: dict[str, Literal["applied", "rejected", "no_patch"]] = {}
    admitted = {project.patch_path for project in projects}
    for name in ("patch.json", "watch.json"):
        for path in stage.rglob(name):
            if path in admitted:
                continue
            receipt: dict[str, object] = {"path": str(path.relative_to(stage))}
            try:
                receipt["text"] = read_space_output(path)
            except (OSError, ValueError) as exc:
                receipt["reason"] = str(exc)
            owner.store.record_agent_task_receipt(
                execution.operation_id,
                "space_stray_output_discarded",
                receipt,
            )
            try:
                clear_space_output(path)
            except (OSError, ValueError) as exc:
                owner.store.record_agent_task_receipt(
                    execution.operation_id,
                    "space_stray_output_kept",
                    {"path": receipt["path"], "reason": str(exc)},
                )
    for project in sorted(projects, key=lambda item: item.project_id):
        try:
            # This exact binding is checked even when an earlier retry committed.
            owner.store.agent_task_authority(project.project_id, execution.operation_id)
            text = read_space_output(project.patch_path)
            if text is None:
                results[project.project_id] = "no_patch"
                continue
            candidate = project.candidate(text).patch
            owner.store.record_agent_task_receipt(
                execution.operation_id,
                "space_project_patch",
                {"project_id": project.project_id, "patch": text},
            )
            with project.service.history.workspace.run_lock() as lease:
                lease.assert_owned()
                owner.store.require_project_accepts_new_work(project.project_id)
                if not owner.store.is_project_member(project.project_id, owner.thread.user_id):
                    raise ValueError("Project membership ended before Space Apply")
                previous = [
                    patch
                    for patch in project.service.history.load_patches()
                    if patch.source_effect_id == candidate.source_effect_id
                    and patch.admission == "accepted"
                ]
                if previous:
                    if (
                        len(previous) != 1
                        or previous[0].source_effect_sha256 != candidate.source_effect_sha256
                    ):
                        raise ValueError("Space Apply effect conflicts with its committed Patch")
                else:
                    project.service.history.append(candidate, discard_on_reject=True)
            results[project.project_id] = "applied"
        except (KeyError, OSError, ValueError, StateUnavailable, ReplayHalted) as exc:
            results[project.project_id] = "rejected"
            owner.store.record_agent_task_receipt(
                execution.operation_id,
                "space_project_rejected",
                {"project_id": project.project_id, "reason": str(exc)},
            )
        # Invariant 9: settled output must not become a later attempt's input.
        try:
            clear_space_output(project.patch_path)
        except (OSError, ValueError) as exc:
            owner.store.record_agent_task_receipt(
                execution.operation_id,
                "space_project_cleanup_failed",
                {"project_id": project.project_id, "reason": str(exc)},
            )
    owner.store.record_agent_task_receipt(
        execution.operation_id, "space_project_results", {"project_results": results}
    )
    return results


def commit_space_seen_heads(
    owner: SpaceThreadOwner,
    execution: AgentTaskExecution,
    projects: list[SpaceProjectTurn],
    session_id: str,
    results: dict[str, Literal["applied", "rejected", "no_patch"]],
) -> None:
    """Record only the shown snapshot plus this turn's own accepted transition."""
    from rcp.runs.session_master import staged_graph_head

    for project in projects:
        revision = project.context.graph_revision
        if results[project.project_id] == "applied":
            applied = [
                patch.revision
                for patch in project.service.history.load_patches()
                if patch.source_effect_id == project.source_effect_id
                and patch.admission == "accepted"
            ]
            if applied:
                revision = max(applied)
        head = staged_graph_head(project.service, GraphTargetRef(), revision)
        previous = owner.store.graph_seen_head_record(
            project.project_id, owner.thread_id, session_id, GraphTargetRef()
        )
        owner.store.commit_graph_seen_head(
            project_id=project.project_id,
            owner_id=owner.thread_id,
            native_session_id=session_id,
            head=head,
            operation_id=execution.operation_id,
            expected_operation_id=previous[1] if previous else None,
        )
