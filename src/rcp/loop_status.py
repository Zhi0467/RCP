"""Read-only loop identity and status shared by Runs, chats, and orchestrators.

These projections confer no watcher-maintenance or episode-control authority.
Checkout identity comes from the immutable worktree binding or an actual launch
receipt, never from a possibly moved current manifest.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import LOOP_OVERLAP_MAX_BYTES, LOOP_OVERLAP_MAX_ROWS
from rcp.storage import AgentTaskRecord, AppStore, EpisodeRecord
from rcp.storage.episodes import _LIVE_EPISODE_STATUSES
from rcp.storage.models import (
    AutoResearchSpaceRunEpisodeState,
    EpisodeEnding,
    EpisodeLoopMetadataSnapshot,
    EpisodeStatus,
    ExperimentControlProjectionSnapshot,
)


class EpisodeStarter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["human", "auto_research", "unknown"]
    human: AuthorizedHuman | None = None
    auto_research_episode_id: str | None = None


class LoopCheckout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["shared", "worktree"]
    available: bool = False
    execution_host: str | None = None
    repository_paths: list[str] = Field(default_factory=list)
    repository_alias: str | None = None
    isolation_owner_episode_id: str | None = None


class EpisodeLoopMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    started_by: EpisodeStarter
    auto_research_parent_episode_id: str | None = None
    stop_initiated_by: str | None = None
    stop_settled_at: str | None = None
    checkout: LoopCheckout


class LoopStarter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["human", "auto_research", "unknown"]
    id: str | None = None
    display_name: str | None = None


class LoopOverlapCheckout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["shared", "worktree"]
    execution_host: str | None = None
    repository_paths: list[str] = Field(default_factory=list)


class LoopStatusRow(BaseModel):
    """Compact identity shared by every overlap consumer."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    episode_id: str
    graph_target: GraphTargetRef
    started_by: LoopStarter
    state: Literal["live", "stopped", "completed", "unavailable"]
    checkout: LoopOverlapCheckout


class LoopOverlap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[LoopStatusRow] = Field(default_factory=list)
    omitted: int = 0


class LoopCurrentStatus(EpisodeLoopMetadata):
    """Full lifecycle metadata for the target's own loop."""

    node_id: str
    episode_id: str
    graph_target: GraphTargetRef
    state: Literal["live", "stopped", "completed", "unavailable"]
    status: EpisodeStatus
    ending: EpisodeEnding | None = None
    created_at: str
    stop_requested_at: str | None = None
    ended_at: str | None = None
    diagnostic: str | None = None


class LoopStatusProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    graph_target: GraphTargetRef
    state: Literal["live", "stopped", "completed", "unavailable", "none"]
    current: LoopCurrentStatus | None = None
    live_elsewhere: LoopOverlap = Field(default_factory=LoopOverlap)


def episode_loop_metadata(
    store: AppStore,
    episode: EpisodeRecord,
    *,
    tasks: list[AgentTaskRecord] | None = None,
) -> EpisodeLoopMetadata:
    """Keep the initiator distinct from authorization and isolation ownership."""

    owner_id = episode.isolation_owner_episode_id or episode.episode_id
    snapshot = EpisodeLoopMetadataSnapshot(
        route=store.auto_research_child_experiment(episode.episode_id),
        isolation=store.episode_isolation(episode.project_id, owner_id),
    )
    if snapshot.isolation is None or snapshot.isolation.worktree is None:
        if tasks is None:
            root = (
                store.agent_task(episode.root_operation_id) if episode.root_operation_id else None
            )
            tasks = [root] if root is not None else []
        if episode.root_operation_id is not None and any(
            task.operation_id == episode.root_operation_id for task in tasks
        ):
            snapshot.launch_receipts = store.agent_task_receipts_by_category(
                episode.root_operation_id, "agent_launch"
            )
    return episode_loop_metadata_from_snapshot(episode, snapshot)


def episode_loop_metadata_from_snapshot(
    episode: EpisodeRecord | AutoResearchSpaceRunEpisodeState,
    snapshot: EpisodeLoopMetadataSnapshot,
) -> EpisodeLoopMetadata:
    """Render either a single-episode or batch-hydrated durable metadata input."""

    route = snapshot.route
    parent_id = route.auto_research_episode_id if route is not None else None
    starter = EpisodeStarter(
        kind="auto_research" if parent_id else "human" if episode.authorized_by else "unknown",
        human=episode.authorized_by if parent_id is None else None,
        auto_research_episode_id=parent_id,
    )
    isolation = snapshot.isolation
    checkout = LoopCheckout(
        kind="worktree" if episode.code_worktree else "shared",
        isolation_owner_episode_id=episode.isolation_owner_episode_id,
    )
    if isolation is not None and isolation.worktree is not None:
        binding = isolation.worktree
        checkout = LoopCheckout(
            kind="worktree",
            available=True,
            execution_host=binding.execution_host,
            repository_paths=[binding.worktree_path],
            repository_alias=binding.repository_alias,
            isolation_owner_episode_id=binding.owner_episode_id,
        )
    else:
        for receipt in reversed(snapshot.launch_receipts):
            roots = receipt.payload.get("canonical_repository_roots")
            host = receipt.payload.get("execution_host")
            if (
                isinstance(roots, list)
                and all(isinstance(path, str) for path in roots)
                and isinstance(host, str)
            ):
                checkout = checkout.model_copy(
                    update={
                        "available": True,
                        "execution_host": host,
                        "repository_paths": list(roots),
                    }
                )
                break
    return EpisodeLoopMetadata(
        started_by=starter,
        auto_research_parent_episode_id=parent_id,
        stop_initiated_by=episode.stop_initiated_by,
        stop_settled_at=episode.stop_settled_at,
        checkout=checkout,
    )


def _loop_status_row(
    store: AppStore,
    snapshot: ExperimentControlProjectionSnapshot,
) -> LoopCurrentStatus | None:
    projected = snapshot.episode
    if projected is None:
        return None
    episode = projected.episode
    if episode.control_node_id is None:
        raise ValueError("loop status requires an Experiment episode")
    metadata = episode_loop_metadata(store, episode, tasks=projected.tasks)
    state = (
        "live"
        if episode.status in _LIVE_EPISODE_STATUSES
        else "stopped"
        if episode.ending == "stopped" or episode.status == "stopped"
        else "unavailable"
        if snapshot.runtime.projection_diagnostic
        else "completed"
    )
    return LoopCurrentStatus(
        **metadata.model_dump(),
        node_id=episode.control_node_id,
        episode_id=episode.episode_id,
        graph_target=episode.graph_target,
        state=state,
        status=episode.status,
        ending=episode.ending,
        created_at=episode.created_at,
        stop_requested_at=episode.stop_requested_at,
        ended_at=episode.ended_at,
        diagnostic=snapshot.runtime.projection_diagnostic,
    )


def other_branch_loops(
    store: AppStore,
    project_id: str,
    *,
    graph_target: GraphTargetRef,
    node_id: str | None = None,
) -> LoopOverlap:
    """Live loops off this target; optionally restrict to kickoff's node."""

    snapshots = store.project_experiment_control_projection_snapshots(project_id)
    candidates = sorted(
        (
            (snapshot.episode.episode, snapshot)
            for snapshot in snapshots.values()
            if snapshot.episode is not None
            and snapshot.episode.episode.status in _LIVE_EPISODE_STATUSES
            and snapshot.episode.episode.graph_target != graph_target
            and (node_id is None or snapshot.episode.episode.control_node_id == node_id)
        ),
        key=lambda item: (
            item[0].control_node_id or "",
            item[0].graph_target.key,
            item[0].episode_id,
        ),
    )
    overlap = LoopOverlap(omitted=len(candidates))
    for _episode, snapshot in candidates[:LOOP_OVERLAP_MAX_ROWS]:
        current = _loop_status_row(store, snapshot)
        if current is None:
            continue
        candidate = LoopOverlap(
            rows=[*overlap.rows, compact_loop_status(current)],
            omitted=overlap.omitted - 1,
        )
        # Bound the wire encoding too: unusually long paths must not exhaust a
        # command response's budget. Never truncate an identity or checkout path.
        if len(json.dumps(candidate.model_dump(mode="json")).encode()) > LOOP_OVERLAP_MAX_BYTES:
            break
        overlap = candidate
    return overlap


def compact_loop_status(row: LoopCurrentStatus) -> LoopStatusRow:
    """Use one compact identity projection in prompts and overlap responses."""

    starter = row.started_by
    return LoopStatusRow(
        node_id=row.node_id,
        episode_id=row.episode_id,
        graph_target=row.graph_target,
        state=row.state,
        started_by=LoopStarter(
            kind=starter.kind,
            id=(starter.human.user_id if starter.human else starter.auto_research_episode_id),
            display_name=starter.human.display_name if starter.human else None,
        ),
        checkout=LoopOverlapCheckout(
            kind=row.checkout.kind,
            execution_host=row.checkout.execution_host,
            repository_paths=row.checkout.repository_paths,
        ),
    )


def chat_loop_status(status: LoopStatusProjection) -> dict[str, object]:
    """Small current-turn evidence without repeating the chat's own target."""

    current = status.current
    row = (
        compact_loop_status(current).model_dump(mode="json", exclude_none=True) if current else None
    )
    if current is not None and row is not None:
        for field in ("stop_initiated_by", "stop_requested_at", "stop_settled_at", "ended_at"):
            value = getattr(current, field)
            if value is not None:
                row[field] = value
    result: dict[str, object] = {
        "state": status.state,
        "live_elsewhere": status.live_elsewhere.model_dump(mode="json", exclude_none=True),
    }
    if row is not None:
        result["current"] = row
    return result


def loop_status_projection(
    store: AppStore,
    project_id: str,
    node_id: str,
    *,
    graph_target: GraphTargetRef,
) -> LoopStatusProjection:
    """Fresh target-local status, including empty state, plus live overlap rows."""

    snapshot = store.experiment_control_projection_snapshots(
        project_id, [node_id], graph_target=graph_target
    ).get(node_id)
    current = _loop_status_row(store, snapshot) if snapshot is not None else None
    return LoopStatusProjection(
        node_id=node_id,
        graph_target=graph_target,
        state=current.state if current else "none",
        current=current,
        live_elsewhere=other_branch_loops(
            store, project_id, graph_target=graph_target, node_id=node_id
        ),
    )
