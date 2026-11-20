from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AliasChoices, BaseModel, Field

from rcp.agents.group_context import GroupContext
from rcp.agents.project_repositories import (
    RepositoryReadScope,
    readable_repositories,
    resolve_read_scope,
    write_repositories,
)
from rcp.config import Manifest
from rcp.core.models import GraphState, Patch
from rcp.core.operations import SetCoverageOperation
from rcp.core.transition_models import GraphTargetRef

if TYPE_CHECKING:
    from rcp.storage.models import SpaceMachineRecord


class RepositoryPointer(BaseModel):
    """Where an agent reads one repository: `machine` is the card id, `host` its route."""

    alias: str
    machine: str
    host: str = ""
    path: str


class ChatRelation(BaseModel):
    relation: str
    direction: Literal["outgoing", "incoming"]
    other_node_id: str
    other_node_type: str
    other_node_title: str
    explanation: str = ""


class ChatContext(BaseModel):
    """Graph and exact repository context for one conversation turn."""

    project_name: str
    run_truth_scope: list[str]
    repositories: list[RepositoryPointer]
    # The target this conversation's graph service was resolved for, the one admission binds.
    graph_target: GraphTargetRef
    graph_path: str
    parent_graph_path: str | None = Field(
        default=None, validation_alias=AliasChoices("parent_graph_path", "main_graph_path")
    )
    branch_name: str | None = None
    branch_parent: str | None = None
    shared_repositories: list[RepositoryPointer] = Field(default_factory=list)
    group_context: GroupContext | None = None
    loop_status: dict[str, Any] | None = None
    research_md_path: str
    paper_path: str | None
    glossary_path: str
    facts_dir: str
    state_repository: str
    ontology_extensions: bool
    graph_revision: int
    node: dict[str, Any] | None
    relations: list[ChatRelation]

    @property
    def read_scope(self) -> RepositoryReadScope:
        """What this turn may read: its repositories as shown, plus available siblings'."""

        project_id = self.group_context.project_id if self.group_context is not None else None
        return resolve_read_scope(project_id, self.repositories, self.group_context)


class ContextAssembler:
    """Builds launch context from the manifest and the space's cards, keyed by card id."""

    def __init__(
        self,
        manifest: Manifest,
        *,
        cards: Mapping[str, SpaceMachineRecord],
        graph_root: Path | None = None,
        paper_path: str | None = None,
    ) -> None:
        self.manifest = manifest
        self.cards = cards
        self.graph_root = graph_root if graph_root is not None else manifest.research_dir
        self.paper_path = paper_path

    def _repository_pointers(self) -> list[RepositoryPointer]:
        return [
            RepositoryPointer(
                alias=item.alias,
                machine=item.machine,
                host=self.cards[item.machine].host,
                path=item.path,
            )
            for item in readable_repositories(self.manifest)
        ]

    @staticmethod
    def _writable(state: GraphState, writable: list[str] | None) -> list[str]:
        """The task's admitted write set, else the whole truth scope.

        Admission stamps each task once; a resumed task keeps what it was authorized.
        """

        truth = write_repositories(state)
        if writable is None:
            return truth
        if not writable or not set(writable).issubset(truth):
            raise ValueError("A run writes only inside this project's truth scope.")
        return list(writable)

    def chat_context(
        self,
        state: GraphState,
        *,
        graph_target: GraphTargetRef,
        node_id: str | None = None,
        writable: list[str] | None = None,
    ) -> ChatContext:
        """Every repository to read; `writable` is the turn's admitted write set."""

        writable = self._writable(state, writable)
        if node_id is not None and node_id not in state.nodes:
            raise ValueError(f"unknown node: {node_id}")
        repositories = self._repository_pointers()
        root = self.graph_root
        return ChatContext(
            project_name=self.manifest.name,
            run_truth_scope=writable,
            repositories=repositories,
            graph_target=graph_target,
            graph_path=str(root / "graph.json"),
            research_md_path=str(root / "research.md"),
            paper_path=self.paper_path,
            glossary_path=str(root / "glossary.json"),
            facts_dir=str(self.manifest.research_dir / "facts"),
            state_repository=self.manifest.state.repository,
            ontology_extensions=_has_ontology_extensions(state),
            graph_revision=state.revision,
            node=state.nodes[node_id].model_dump(mode="json") if node_id else None,
            relations=_one_hop_relations(state, node_id) if node_id else [],
        )


def validate_work_patch(patch: Patch) -> None:
    """Work may reflect graph changes, but it never advances ingest state."""

    if patch.processed_cursors:
        raise ValueError(
            "A Work patch must not claim processed_cursors; only retired ingestion runs "
            "recorded them."
        )
    if any(isinstance(operation, SetCoverageOperation) for operation in patch.ops):
        raise ValueError("A Work patch must not set coverage; coverage reporting is retired.")


def _has_ontology_extensions(state: GraphState) -> bool:
    """Extension authoring rules are dead weight where no extension was ever defined."""

    ontology = state.ontology
    return bool(ontology.types or ontology.fields or ontology.relations)


def _one_hop_relations(state: GraphState, node_id: str) -> list[ChatRelation]:
    relations = []
    for edge in state.edges.values():
        if edge.source == node_id:
            other_id, direction = edge.target, "outgoing"
        elif edge.target == node_id:
            other_id, direction = edge.source, "incoming"
        else:
            continue
        other = state.nodes.get(other_id)
        if other is None:
            continue
        relations.append(
            ChatRelation(
                relation=edge.relation,
                direction=direction,
                other_node_id=other_id,
                other_node_type=other.type,
                other_node_title=other.title,
                explanation=edge.explanation,
            )
        )
    return sorted(relations, key=lambda item: (item.direction, item.relation, item.other_node_id))
