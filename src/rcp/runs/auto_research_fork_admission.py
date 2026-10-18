"""Publish a child fork and its frozen setup Patch from an atomic admission."""

from __future__ import annotations

import hashlib
import uuid
from copy import copy

from rcp.core.authority import AgentTaskAuthority, WriteAuthority
from rcp.history.branches import BranchHistoryManager, branch_parent_snapshot
from rcp.history.manager import HistoryManager
from rcp.history.queries import parse_head
from rcp.runs.auto_research import (
    AutoResearchCommandContext,
    AutoResearchCommandUnavailable,
    recorded_child_graph_target,
)
from rcp.runs.graph_forks import create_graph_fork
from rcp.runs.tasks.work import _apply_work_patch, _prepare_work_patch_candidate
from rcp.service import ProjectService
from rcp.storage import AgentCommandInvocationRecord, AppStore, GraphBranchRecord


def child_fork_id(child_id: str) -> str:
    # Branch identities require UUID4 shape; command identity supplies stable entropy.
    return str(
        uuid.UUID(bytes=hashlib.sha256(f"fork:{child_id}".encode()).digest()[:16], version=4)
    )


def child_seed_id(branch_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rcp:fork-seed:{branch_id}"))


def plan_child_fork(
    store: AppStore,
    service: ProjectService,
    context: AutoResearchCommandContext,
    *,
    at: str,
    child_id: str,
) -> GraphBranchRecord:
    """Resolve and prove a source without publishing or reserving anything."""
    main = (
        service.history.parent
        if isinstance(service.history, BranchHistoryManager)
        else service.history
    )
    branch_id = child_fork_id(child_id)
    parent_head = main.history_engine.resolve(
        parse_head(at), default_target=context.episode.graph_target
    )
    with main.workspace.transaction():
        branch_parent_snapshot(main, parent_head, branch_id)
    return GraphBranchRecord(
        project_id=context.episode.project_id,
        branch_id=branch_id,
        owner_episode_id=store.branch_owner(
            context.episode.project_id, context.episode.graph_target.branch_id or ""
        ),
        parent_head=parent_head,
        created_by=context.task.operation_id,
        created_at=store.now(),
        name=f"fork-{uuid.UUID(branch_id).hex[:24]}",
        auto_archive=True,
    )


def validate_child_seed(
    service: ProjectService, context: AutoResearchCommandContext, text: str
) -> None:
    candidate = _prepare_work_patch_candidate(
        service,
        text,
        run_truth_scope=list(context.request.run_truth_scope or ()),
        source_operation_id=context.task.operation_id,
        profile="orchestrator",
    )
    if not candidate.patch.ops:
        raise ValueError("A fork seed must contain a graph operation.")


def ensure_child_fork(
    store: AppStore,
    service: ProjectService,
    context: AutoResearchCommandContext,
    command: AgentCommandInvocationRecord,
) -> dict[str, object]:
    """Re-drive only the recorded identity and seed, before any child can launch."""
    arguments = command.start_payload.get("arguments")
    if not isinstance(arguments, dict) or arguments.get("at") is None:
        return {}
    child_id = command.start_payload.get(
        "planned_worker_id" if command.verb == "spawn" else "planned_episode_effect_id"
    )
    if not isinstance(child_id, str):
        raise ValueError("The fork admission lost its planned child identity.")
    admission = store.auto_research_child_admission(child_id)
    target = recorded_child_graph_target(command, context.episode)
    branch = store.graph_branch(context.episode.project_id, target.branch_id or "")
    owner = store.branch_owner(
        context.episode.project_id, context.episode.graph_target.branch_id or ""
    )
    if (
        admission is None
        or admission.state != "accepted"
        or admission.episode_id != context.episode.episode_id
        or admission.child_id != child_id
        or branch is None
        or branch.branch_id != child_fork_id(child_id)
        or branch.owner_episode_id != owner
        or branch.created_by != command.operation_id
        or branch.archived
        or not branch.auto_archive
        or context.task.operation_id != command.operation_id
        or context.request.role != "orchestrator"
        or context.episode.authorized_by is None
    ):
        raise ValueError("The fork lost its accepted orchestrator admission.")
    main = (
        service.history.parent
        if isinstance(service.history, BranchHistoryManager)
        else service.history
    )

    def require_admission_open() -> None:
        episode = store.episode(admission.episode_id)
        if episode is None:
            raise ValueError("The fork admission lost its parent episode.")
        store._validate_auto_research_parent_admission(episode)

    create_graph_fork(
        main,
        store,
        project_id=branch.project_id,
        branch_id=branch.branch_id,
        parent_head=branch.parent_head,
        authorized_by=context.episode.authorized_by,
        created_by=branch.created_by,
        owner_episode_id=branch.owner_episode_id,
        name=branch.name,
        auto_archive=True,
        before_publish=require_admission_open,
    )
    seed = command.start_payload.get("fork_seed")
    if seed is not None:
        if not isinstance(seed, dict):
            raise ValueError("The fork seed binding is malformed.")
        effect_id = child_seed_id(branch.branch_id)
        snapshot = store.auto_research_command_file(effect_id)
        if (
            snapshot is None
            or snapshot.kind != "apply"
            or snapshot.operation_id != command.operation_id
            or snapshot.episode_id != command.episode_id
            or snapshot.filename != arguments.get("seed_file")
            or seed
            != {"snapshot_id": effect_id, "filename": snapshot.filename, "sha256": snapshot.sha256}
        ):
            raise ValueError("The fork seed lost its immutable command snapshot.")

        def seed_authority(project_id: str, operation_id: str) -> AgentTaskAuthority:
            require_admission_open()
            authority = store.agent_task_authority(project_id, operation_id)
            if (
                project_id != branch.project_id
                or operation_id != command.operation_id
                or authority.episode_id != admission.episode_id
                or authority.apply_target != context.episode.graph_target
                or authority.dispatch_authority is None
                or authority.dispatch_authority.profile != "orchestrator"
            ):
                raise ValueError("The setup Patch lost its orchestrator authority.")
            return authority.model_copy(update={"apply_target": target})

        # A private history owner grants only this admitted setup operation its
        # target. The running root and ordinary child retain their own authorities.
        seed_main = HistoryManager(
            main.manifest,
            main.workspace,
            expected_space_id=main.expected_space_id,
            project_id=branch.project_id,
            write_authority=WriteAuthority(
                agent_authority_resolver=seed_authority,
                project_membership_check=main.write_authority.project_membership_check,
            ),
            on_accepted_branch_transition=main.on_accepted_branch_transition,
        )
        seed_service = copy(service)
        seed_service.history = seed_main
        seed_service = seed_service.for_graph_target(target, initialize=False)
        result, failure = _apply_work_patch(
            seed_service,
            None,
            snapshot.content,
            run_truth_scope=list(context.request.run_truth_scope or ()),
            source_operation_id=command.operation_id,
            source_effect_id=effect_id,
            profile="orchestrator",
        )
        if failure is not None:
            if failure.commit_status is not None:
                raise AutoResearchCommandUnavailable(failure.message)
            raise ValueError(failure.message)
        if result is None or result.status == "unavailable":
            raise AutoResearchCommandUnavailable("The fork seed commit is not yet proven.")
    elif arguments.get("seed_file") is not None:
        raise ValueError("The fork admission lost its seed binding.")
    return {"branch_id": branch.branch_id, "branch_name": branch.name}
