"""Per-project admission and Apply for a Space turn; no project lifecycle verbs."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

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
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.transport import RunLockCancelled, StateUnavailable

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
) -> tuple[list[SpaceProjectTurn], CompositeWriteScope, dict[str, str]]:
    """Admit same-machine projects; return the others with why they are not writable."""
    projects: list[SpaceProjectTurn] = []
    excluded: dict[str, str] = {}
    turn_id = compute_recovery_lineage(
        owner.store, execution.operation_id, continuation=execution.continuation
    )[-1]
    for project_id in sorted(owner.store.member_project_ids(owner.thread.user_id)):
        try:
            owner.store.require_project_accepts_new_work(project_id)
            if not owner.catalog.same_machine(project_id):
                excluded[project_id] = "state lives on another machine"
                continue
            service = owner.catalog.open_execution(project_id)
            machine = service.manifest.machine_map[service.manifest.base_machine]
            if machine.host:
                excluded[project_id] = "base machine is remote"
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
            excluded[project_id] = str(exc) or type(exc).__name__
            owner.store.record_agent_task_receipt(
                execution.operation_id,
                "space_project_not_admitted",
                {"project_id": project_id, "reason": excluded[project_id]},
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
            "excluded": excluded,
        },
    )
    return projects, composite, excluded


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


def space_result_summary(results: dict[str, GraphUpdateResult]) -> dict[str, dict[str, object]]:
    """Each project's status and, when it did not apply, the first reason."""
    summary: dict[str, dict[str, object]] = {}
    for project_id, result in results.items():
        item: dict[str, object] = {"status": result.status}
        if result.status in {"rejected", "unavailable"} and result.validation_messages:
            item["reason"] = result.validation_messages[0]
        if result.commit_status is not None:
            item["commit_status"] = result.commit_status
        summary[project_id] = item
    return summary


def _space_stage_outputs(stage: Path) -> list[Path]:
    """Only the locations a Space agent could mean as a deliverable, not the whole stage."""
    paths = [stage / "patch.json", stage / "watch.json"]
    root = stage / "projects"
    if root.is_dir() and not root.is_symlink():
        for folder in sorted(root.iterdir()):
            paths.extend((folder / "patch.json", folder / "watch.json"))
    return [path for path in paths if path.is_symlink() or path.exists()]


def _apply_space_project(
    owner: SpaceThreadOwner,
    execution: AgentTaskExecution,
    project: SpaceProjectTurn,
    text: str,
) -> GraphUpdateResult:
    from rcp.runs.tasks.work import _bounded_graph_messages, _work_patch_proposal_ids
    from rcp.runs.tasks.work_turn_runtime import apply_work_patch, failed_graph_update

    def still_admitted() -> None:
        owner.store.require_project_accepts_new_work(project.project_id)
        if not owner.store.is_project_member(project.project_id, owner.thread.user_id):
            raise ValueError("Project membership ended before Space Apply")

    try:
        result, failure = apply_work_patch(
            project.service,
            execution,
            text,
            prepare_candidate=project.candidate,
            source_operation_id=project.operation_id,
            source_effect_id=project.source_effect_id,
            canonical_matches=lambda canonical, candidate: (
                canonical.source_effect_sha256 == candidate.source_effect_sha256
            ),
            canonical_binding_error="Space Apply effect conflicts with its committed Patch",
            rejected_patch_error="The graph rejected the Space project Patch.",
            proposal_ids_for_patch=_work_patch_proposal_ids,
            bounded_messages=_bounded_graph_messages,
            under_lock=still_admitted,
        )
    except RunLockCancelled as exc:
        # Paused before the lock was owned: nothing was appended.
        return GraphUpdateResult(
            status="unavailable", validation_messages=[str(exc)], commit_status="absent"
        )
    if failure is not None:
        return failed_graph_update(failure, bounded_messages=_bounded_graph_messages)
    assert result is not None
    return result


def collect_space_projects(
    owner: SpaceThreadOwner,
    execution: AgentTaskExecution,
    projects: list[SpaceProjectTurn],
    excluded: dict[str, str],
    stage: Path,
) -> dict[str, GraphUpdateResult]:
    """Apply each admitted project's Patch through Work Apply, in project-id order."""
    results: dict[str, GraphUpdateResult] = {}
    admitted = {project.patch_path for project in projects}
    for path in _space_stage_outputs(stage):
        if path in admitted:
            continue
        relative = str(path.relative_to(stage))
        receipt: dict[str, object] = {"path": relative}
        try:
            receipt["text"] = read_space_output(path)
        except (OSError, ValueError) as exc:
            receipt["reason"] = str(exc)
        project_id = path.parent.name if path.parent.parent == stage / "projects" else None
        if path.name == "patch.json" and project_id in excluded:
            # A project dropped at admission, including a changed binding, is a
            # rejection the member and the next turn can see, not a stray file.
            results[project_id] = GraphUpdateResult(
                status="rejected", validation_messages=[excluded[project_id]]
            )
            owner.store.record_agent_task_receipt(
                execution.operation_id,
                "space_project_rejected",
                {"project_id": project_id, "reason": excluded[project_id]},
            )
        else:
            owner.store.record_agent_task_receipt(
                execution.operation_id, "space_stray_output_discarded", receipt
            )
        try:
            clear_space_output(path)
        except (OSError, ValueError) as exc:
            owner.store.record_agent_task_receipt(
                execution.operation_id,
                "space_stray_output_kept",
                {"path": relative, "reason": str(exc)},
            )
    for project in sorted(projects, key=lambda item: item.project_id):
        try:
            # This exact binding is checked even when an earlier retry committed.
            owner.store.agent_task_authority(project.project_id, execution.operation_id)
            text = read_space_output(project.patch_path)
        except (KeyError, OSError, ValueError) as exc:
            result = GraphUpdateResult(
                status="rejected", validation_messages=[str(exc) or type(exc).__name__]
            )
        else:
            if text is None:
                result = GraphUpdateResult(status="none")
            else:
                owner.store.record_agent_task_receipt(
                    execution.operation_id,
                    "space_project_patch",
                    {"project_id": project.project_id, "patch": text},
                )
                result = _apply_space_project(owner, execution, project, text)
        results[project.project_id] = result
        if result.status in {"rejected", "unavailable"}:
            owner.store.record_agent_task_receipt(
                execution.operation_id,
                f"space_project_{result.status}",
                {"project_id": project.project_id, **space_result_summary({"": result})[""]},
            )
        if result.status == "unavailable":
            # The commit may be present or unknown, so the Patch stays for a
            # retry, whose effect key finds an earlier commit instead of repeating it.
            continue
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
        execution.operation_id,
        "space_project_results",
        {"project_results": space_result_summary(results)},
    )
    return results


def commit_space_seen_heads(
    owner: SpaceThreadOwner,
    execution: AgentTaskExecution,
    projects: list[SpaceProjectTurn],
    session_id: str,
    results: dict[str, GraphUpdateResult],
) -> None:
    """Record only the shown snapshot plus this turn's own accepted transition."""
    from rcp.runs.session_master import staged_graph_head

    for project in projects:
        applied = results[project.project_id].applied_revision
        revision = project.context.graph_revision if applied is None else applied
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
