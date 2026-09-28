"""Common episode isolation admission and immutable binding resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rcp.agents.write_scope import ProjectWriteScope, _ExecutionPathSemantics
from rcp.config import load_manifest
from rcp.conversation_worktrees import (
    _require_episode_git,
    ensure_episode_worktree,
    plan_episode_worktree,
    worktree_command,
)
from rcp.core.models import EpisodeIsolation
from rcp.core.transition_models import GraphTargetRef
from rcp.keyed_locks import KeyedLocks
from rcp.storage import AppStore

if TYPE_CHECKING:
    from rcp.runs.auto_research import AutoResearchRunRequest, AutoResearchStartRequest
    from rcp.service import ProjectService, RunRequest

isolation_locks = KeyedLocks()


def _check_grants(store: AppStore, host: str, shared_path: str) -> None:
    card = store.space_machine_for(host)
    grants = list(card.writable_paths) if card else []
    if not grants:
        return
    facts = worktree_command(
        store,
        host=host,
        operation="canonicalize",
        paths=[shared_path, *grants],
        shared_path=shared_path,
    )
    canonical = facts["canonical"]
    semantics = _ExecutionPathSemantics.for_execution(remote=bool(host))
    if any(semantics.overlaps(canonical[path], canonical[shared_path]) for path in grants):
        raise ValueError("episode_isolation_grant_overlap")


def validate_episode_admission(
    store: AppStore,
    project_id: str,
    request: RunRequest | AutoResearchRunRequest | AutoResearchStartRequest,
    *,
    episode_id: str | None = None,
    graph_target: GraphTargetRef | None = None,
) -> None:
    """Refuse a changed binding or unsupported new Run before allocation."""
    episode_id = (
        episode_id
        or getattr(request, "episode_id", None)
        or getattr(request, "control_episode_id", None)
    )
    episode = store.episode(episode_id) if episode_id else None
    owner = episode
    if episode and episode.isolation_owner_episode_id:
        owner = store.episode(episode.isolation_owner_episode_id)
        if owner is None or owner.project_id != project_id:
            raise ValueError("episode_isolation_owner_missing")
    if owner is None and graph_target is not None and graph_target.kind == "branch":
        branch = store.episode(graph_target.branch_id or "")
        if branch is not None:
            owner = store.episode(branch.isolation_owner_episode_id or branch.episode_id)
    if episode is not None and episode.project_id != project_id:
        raise ValueError("episode_isolation_project_mismatch")
    isolation = store.episode_isolation(project_id, owner.episode_id) if owner else None
    code = owner.code_worktree if owner else request.code_worktree
    owns_request = (
        getattr(request, "episode_id", None) == episode_id
        or getattr(request, "control_episode_id", None) == episode_id
    )
    if (
        owner is not None
        and episode is not None
        and owner.episode_id == episode.episode_id
        and owns_request
        and episode.isolation_owner_episode_id is not None
        and (
            request.code_worktree != episode.code_worktree
            or request.graph_isolation != episode.graph_isolation
        )
    ):
        raise ValueError("episode_isolation_choices_changed")
    if not code:
        return
    project = store.project(project_id)
    if project is None:
        raise KeyError(project_id)
    manifest = load_manifest(project.locator)
    aliases = (
        request.run_truth_scope
        if request.run_truth_scope is not None
        else manifest.agent.default_run_truth_scope
    )
    if len(aliases) != 1:
        raise ValueError("episode_isolation_requires_one_repository")
    repository = manifest.repository_map[aliases[0]]
    host = manifest.machine_map[request.run_on or "local"].host
    if repository.machine != request.run_on:
        raise ValueError("episode_isolation_host_mismatch")
    if isolation and isolation.worktree:
        binding = isolation.worktree
        if host != binding.execution_host or repository.alias != binding.repository_alias:
            raise ValueError("episode_isolation_host_mismatch")
        state = store.episode_isolation_state(project_id, isolation.owner_episode_id)
        if state is None or state.status not in {"creating", "ready"}:
            raise ValueError("episode_isolation_unavailable")
        if state.status == "ready":
            worktree_command(store, host=host, operation="inspect", binding=binding)
    elif owner is not None and owner.episode_id != episode_id:
        raise ValueError("episode_isolation_owner_missing")
    _require_episode_git(store, host)
    _check_grants(store, host, repository.path)


def resolve_auto_research_code_worktree(
    store: AppStore, project_id: str, request: AutoResearchStartRequest
) -> bool:
    """Resolve the omitted toggle once, before persisting the owner's choices."""
    if request.code_worktree is not None:
        return request.code_worktree
    try:
        validate_episode_admission(
            store, project_id, request.model_copy(update={"code_worktree": True})
        )
    except ValueError as exc:
        if str(exc) not in {
            "episode_isolation_requires_one_repository",
            "episode_isolation_git_version",
            "episode_isolation_grant_overlap",
        }:
            raise
        return False
    return True


def ensure_episode_isolation(
    service: ProjectService,
    store: AppStore,
    episode_id: str,
    request: RunRequest | AutoResearchRunRequest,
) -> EpisodeIsolation:
    """Persist one owner identity before creation and every provider launch."""
    episode = store.episode(episode_id)
    if episode is None:
        raise ValueError("episode_isolation_episode_missing")
    owner_id = episode.isolation_owner_episode_id or episode.episode_id
    validate_episode_admission(store, episode.project_id, request, episode_id=episode_id)
    with isolation_locks(f"{store.path}:{episode.project_id}:{owner_id}"):
        isolation = store.episode_isolation(episode.project_id, owner_id)
        if isolation is None:
            if owner_id != episode_id:
                legacy_owner = store.episode(owner_id)
                if (
                    legacy_owner is None
                    or legacy_owner.isolation_owner_episode_id is not None
                    or legacy_owner.code_worktree
                ):
                    raise ValueError("episode_isolation_owner_missing")
            isolation = EpisodeIsolation(
                owner_episode_id=owner_id,
                graph_branch_id=episode.graph_target.branch_id,
                worktree=(
                    plan_episode_worktree(service, store, episode.project_id, request, owner_id)
                    if episode.code_worktree
                    else None
                ),
            )
            store.create_episode_isolation(episode.project_id, isolation)
        if isolation.worktree:
            _check_grants(store, isolation.worktree.execution_host, isolation.worktree.shared_path)
        ensure_episode_worktree(service, store, episode.project_id, request, isolation)
        return isolation


def validate_episode_launch(store: AppStore, episode_id: str, scope: ProjectWriteScope) -> None:
    """Recheck live binding proofs and machine grants for correction launches too."""
    episode = store.episode(episode_id)
    if episode is None:
        raise ValueError("episode_isolation_episode_missing")
    owner_id = episode.isolation_owner_episode_id or episode_id
    isolation = store.episode_isolation(episode.project_id, owner_id)
    if isolation is None:
        if episode.code_worktree or episode.isolation_owner_episode_id:
            raise ValueError("episode_isolation_owner_missing")
        return
    state = store.episode_isolation_state(episode.project_id, owner_id)
    if state is None or state.status != "ready":
        raise ValueError("episode_isolation_unavailable")
    binding = isolation.worktree
    if binding is None:
        return
    if scope.execution_host != binding.execution_host or scope.repository_roots not in (
        [],
        [binding.worktree_path],
    ):
        raise ValueError("episode_isolation_scope_mismatch")
    _check_grants(store, binding.execution_host, binding.shared_path)
    worktree_command(store, host=binding.execution_host, operation="inspect", binding=binding)
