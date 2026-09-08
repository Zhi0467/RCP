"""Read models for a branch displayed through the ordinary graph workspace."""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from rcp.core.attention import project_graph_mutation_availability
from rcp.core.materialize import MaterializationResult
from rcp.core.models import Edge, GraphState, ProjectNode
from rcp.core.transition_models import GraphHeadRef, GraphMutationAvailability, GraphTargetRef
from rcp.history.branches import BranchHistoryManager
from rcp.history.delta import render_revision_summary
from rcp.runs.branch_merge import build_semantic_delta
from rcp.storage import ACTIVE_AGENT_TASK_STATUSES, AppStore


class GraphChangeSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int
    producer: Literal["human", "agent", "system"]
    summary: str
    task_id: str | None
    episode_id: str | None


class NodeChange(BaseModel):
    node_id: str
    change: Literal["created", "updated", "removed"]
    before: ProjectNode | None
    after: ProjectNode | None
    history: list[GraphChangeSource]


class EdgeChange(BaseModel):
    edge_id: str
    change: Literal["created", "updated", "removed"]
    before: Edge | None
    after: Edge | None
    history: list[GraphChangeSource]


class GraphBranchChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_id: str
    base_head: GraphHeadRef
    head: GraphHeadRef
    nodes: list[NodeChange]
    edges: list[EdgeChange]
    changed_node_ids: list[str]
    context_node_ids: list[str]


def branch_changes(
    history: BranchHistoryManager,
) -> tuple[MaterializationResult, GraphBranchChanges]:
    """Project one coherent branch replay, including actual per-change authorship."""

    with history.workspace.snapshot_lock:
        metadata = history.branch_metadata()
        result, boundaries = history.accepted_patch_boundaries()
        base = boundaries[0][0] if boundaries else result.state
        head = history.head_ref(result)
        delta = build_semantic_delta(
            base, result.state, base_head=metadata.base_head, branch_head=head
        )
        sources = [
            (
                before,
                after,
                GraphChangeSource(
                    revision=patch.revision,
                    producer=patch.producer,
                    summary=" ".join(render_revision_summary(before, patch, after).sentences),
                    task_id=patch.task_id,
                    episode_id=patch.episode_id,
                ),
            )
            for before, patch, after in boundaries
        ]
        nodes = [
            NodeChange(
                **item.model_dump(mode="python"),
                history=[
                    source
                    for before, after, source in sources
                    if before.nodes.get(item.node_id) != after.nodes.get(item.node_id)
                ],
            )
            for item in delta.nodes
        ]
        edges = [
            EdgeChange(
                **item.model_dump(mode="python"),
                history=[
                    source
                    for before, after, source in sources
                    if before.edges.get(item.edge_id) != after.edges.get(item.edge_id)
                ],
            )
            for item in delta.edges
        ]
        changed_ids = {item.node_id for item in nodes}
        for item in edges:
            for edge in (item.before, item.after):
                if edge is not None:
                    changed_ids.update((edge.source, edge.target))
        for item in delta.proposals:
            for proposal in (item.before, item.after):
                if proposal is not None:
                    changed_ids.update(proposal.related_node_ids)
        context_ids = set(changed_ids)
        for edge in (*base.edges.values(), *result.state.edges.values()):
            if edge.source in changed_ids or edge.target in changed_ids:
                context_ids.update((edge.source, edge.target))
        return result, GraphBranchChanges(
            branch_id=history.branch_id,
            base_head=metadata.base_head,
            head=head,
            nodes=nodes,
            edges=edges,
            changed_node_ids=sorted(changed_ids),
            context_node_ids=sorted(context_ids),
        )


def active_merge(store: AppStore, project_id: str, target: GraphTargetRef) -> bool:
    if target.kind != "branch":
        return False
    return any(
        task.kind == "branch_merge"
        and task.graph_target == target
        and task.status in ACTIVE_AGENT_TASK_STATUSES
        for task in store.episode_tasks(target.branch_id)
        if task.project_id == project_id
    )


def require_graph_edit_admission(store: AppStore, project_id: str, target: GraphTargetRef) -> None:
    """Called under the same project admission lock as human merge dispatch."""

    if active_merge(store, project_id, target):
        raise HTTPException(
            status_code=409, detail="Wait for the graph merge to finish before editing."
        )


def graph_mutation_availability(
    store: AppStore, project_id: str, target: GraphTargetRef, state: GraphState
) -> GraphMutationAvailability:
    if active_merge(store, project_id, target):
        return GraphMutationAvailability(
            available=False, reason="Wait for the graph merge to finish before editing."
        )
    return project_graph_mutation_availability(state)
