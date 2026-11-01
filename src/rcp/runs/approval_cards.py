"""Approval preparation reads live facts; only the human HTTP route executes them."""

from __future__ import annotations

from typing import Any

from rcp.agents.launcher import work_like_launch_problem
from rcp.approval_actions import ApprovalAction
from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphTargetRef
from rcp.core.validation.proposals import proposal_is_stale
from rcp.service import ProjectService
from rcp.storage import AppStore


def private_project_thread_turn(store: AppStore, operation_id: str) -> bool:
    task = store.agent_task(operation_id)
    if task is None or task.kind != "project_chat" or task.authorized_by is None:
        return False
    with store.connection() as connection:
        return (
            connection.execute(
                """SELECT 1 FROM agent_threads WHERE thread_id=?
            AND user_id=? AND project_id=? AND kind='project' AND closed_at IS NULL""",
                (task.request.get("chat_id"), task.authorized_by.user_id, task.project_id),
            ).fetchone()
            is not None
        )


def approval_card_usage(client: str) -> str:
    return (
        f"Protected actions in this private project thread use {client} ask --key <key> "
        "--question <reason> --action '<JSON>'. Do not supply choices or multiple; "
        "RCP creates Approve and Decline controls. Action kind is proposal (proposal_id, "
        "decision approved/rejected, optional reason), truth_scope (alias, writable), "
        "auto_research (invocation_ceiling and optional provider/model/reasoning, "
        "starting_instruction, branch_name, code_worktree, worktree_repository), "
        "merge_branch (branch_id), or merge_episode (episode_id and merge options). "
        "Every action includes this thread's project_id. Only the member's card tap "
        "executes the action; a plain answer grants no authority."
    )


def approval_pins(
    service: ProjectService, store: AppStore, action: ApprovalAction, human: AuthorizedHuman
) -> dict[str, Any]:
    """Pin content and authority-relevant facts, excluding volatile probe timestamps."""
    from rcp.api.episode_routes import _resolved_auto_research_start_request
    from rcp.api.episodes import StartEpisodeBody
    from rcp.runs.branch_merge_context import merge_history
    from rcp.runs.episodes.merge import branch_merge_preview, merge_owner, merge_preview

    if action.kind == "proposal":
        state = service.history.state()
        proposal = state.proposals.get(action.proposal_id)
        if proposal is None or proposal.status != "pending" or proposal_is_stale(state, proposal):
            raise ValueError("proposal_not_pending_or_stale")
        return {"proposal": proposal.model_dump(mode="json")}
    if action.kind == "truth_scope":
        manifest = service.manifest
        repository = manifest.repository_map.get(action.alias)
        if repository is None:
            raise ValueError("repository_not_found")
        writable = action.alias in service.history.state().project_truth_scope
        if writable == action.writable:
            raise ValueError("truth_scope_unchanged")
        return {"repository": repository.model_dump(mode="json"), "writable": writable}
    if action.kind == "auto_research":
        body = StartEpisodeBody.model_validate(
            {"mode": "auto_research", **action.model_dump(exclude={"kind", "project_id"})}
        )
        profile = _resolved_auto_research_start_request(
            service, body, store=store, project_id=action.project_id
        )
        if profile.provider is None or profile.run_on is None:
            raise ValueError("auto_research_profile_unavailable")
        machine = service.manifest.machine_map[profile.run_on]
        readiness = service.launcher.member_readiness(
            profile.provider,
            host=machine.host,
            binary=service.launcher.provider_binary(profile.provider, machine.host),
            member_id=human.user_id,
            refresh=True,
        )
        return {
            "profile": profile.model_dump(mode="json"),
            "readiness": {
                "installed": readiness.installed,
                "authenticated": readiness.authenticated,
                "reason": readiness.reason,
                "work_problem": work_like_launch_problem(readiness),
            },
        }
    if action.kind == "merge_branch":
        source = merge_history(service, GraphTargetRef(kind="branch", branch_id=action.branch_id))
        preview = branch_merge_preview(service, store, action.branch_id, authorized_by=human)
    else:
        episode = store.episode(action.episode_id)
        if episode is None or episode.project_id != action.project_id:
            raise ValueError("episode_not_found")
        owner = merge_owner(store, episode)
        source = merge_history(service, owner.graph_target)
        preview = merge_preview(
            service, store, owner, authorized_by=human, target_branch=action.target_branch
        )
    return {
        "source_head": source.head_ref().model_dump(mode="json"),
        "main_head": service.history.head_ref().model_dump(mode="json"),
        "preview": preview.model_dump(mode="json"),
    }
