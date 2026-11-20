from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from rcp.core.models import (
    Ambiguity,
    AuthorizedHuman,
    Edge,
    GlossaryTerm,
    GraphState,
    Patch,
    ProjectNode,
    Proposal,
    graph_fields,
)
from rcp.core.operations import (
    CreateAmbiguitiesOperation,
    CreateEdgesOperation,
    CreateNodesOperation,
    CreateProposalsOperation,
    MergeNodesOperation,
    NewEdge,
    RemoveEdgesOperation,
    RemoveNodesOperation,
    ResolveAmbiguitiesOperation,
    ResolveProposalsOperation,
    SetCoverageOperation,
    SetOntologyOperation,
    SetProjectTruthScopeOperation,
    SetStandingOperation,
    SupersedeNodesOperation,
    UpdateNodesOperation,
    UpsertGlossaryOperation,
    WithdrawProposalsOperation,
)
from rcp.core.project_types import project_type_of

if TYPE_CHECKING:
    from rcp.history.queries import StepRecord


_MAX_TITLE_CHARS = 240
_GRAPH_ID_RE = re.compile(
    r"(?<![a-z0-9_/-])[a-z][a-z0-9]*(?:_[a-z0-9]+)*/"
    r"[a-z0-9]+(?:-[a-z0-9]+)*(?![a-z0-9_/-])"
)
_OPERATION_LABELS = {
    "create_nodes": "added research concepts",
    "update_nodes": "updated research concepts",
    "create_edges": "connected research concepts",
    "remove_edges": "removed graph relationships",
    "remove_nodes": "removed research concepts",
    "supersede_nodes": "superseded research concepts",
    "merge_nodes": "merged research concepts",
    "create_ambiguities": "recorded open questions",
    "resolve_ambiguities": "resolved open questions",
    "create_proposals": "recorded proposals",
    "resolve_proposals": "resolved proposals",
    "withdraw_proposals": "withdrew proposals",
    "upsert_glossary": "updated the glossary",
    "set_coverage": "updated source coverage",
    "set_standing": "updated review standing",
    "set_project_truth_scope": "updated the project truth scope",
    "set_ontology": "updated the project ontology",
}


class RevisionSummary(BaseModel):
    """Deterministic reader-facing prose for one accepted canonical patch."""

    model_config = ConfigDict(extra="forbid")

    from_revision: int = Field(ge=0)
    to_revision: int = Field(ge=1)
    kind: Literal["seed", "refresh", "chat", "work", "experiment_loop", "approval", "identity"]
    author: Literal["agent", "human"] | None
    producer: Literal["agent", "human", "system"]
    authorized_by: AuthorizedHuman | None = None
    profile: Literal["ordinary", "orchestrator"] | None = None
    task_id: str | None = None
    episode_id: str | None = None
    created_at: str
    sentences: list[str] = Field(min_length=1)


def build_revision_summaries(
    steps: Iterable[StepRecord],
    *,
    from_revision: int = 1,
    to_revision: int | None = None,
) -> list[RevisionSummary]:
    """Render accepted engine steps without exposing graph implementation labels."""

    return [
        render_revision_summary(step.before_state, step.patch, step.after_state)
        for step in steps
        if step.accepted
        and from_revision <= step.patch.revision
        and (to_revision is None or step.patch.revision <= to_revision)
    ]


def render_revision_summary(
    previous_state: GraphState,
    patch: Patch,
    state: GraphState,
) -> RevisionSummary:
    """Render one successfully applied patch without changing either replay state."""

    if patch.kind == "identity" and patch.project_identity is not None:
        home_space_id = patch.project_identity.home_space_id
        if patch.project_identity.action == "created":
            sentences = [f"Project created in {home_space_id}."]
        else:
            sentences = [f"Project identity adopted in {home_space_id}."]
    elif patch.kind == "identity" and patch.project_home_transfer is not None:
        transfer = patch.project_home_transfer
        sentences = [
            f"Project moved from {transfer.previous_home_space_id} to {transfer.new_home_space_id}."
        ]
    else:
        labels = _state_labels(previous_state) | _state_labels(state)
        sentences = [
            _plain_history_text(item, labels) for item in patch.change_summary if item.strip()
        ]
        sentences = [item for item in sentences if item]
        if not sentences:
            sentences = _operation_fallbacks(patch, previous_state, state, labels)
        sentences.extend(_edge_assessment_sentences(patch, previous_state, state, labels))
        sentences.extend(_proposal_consequence_sentences(patch, state, labels, sentences))
        sentences = _unique_sentences(_plain_history_text(item, labels) for item in sentences)
        if not sentences:
            sentences = ["Recorded a research graph revision."]
    return RevisionSummary(
        from_revision=max(0, patch.revision - 1),
        to_revision=patch.revision,
        kind=patch.kind,
        author=patch.author,
        producer=patch.producer,
        authorized_by=patch.authorized_by,
        profile=patch.profile,
        task_id=patch.task_id,
        episode_id=patch.episode_id,
        created_at=patch.created_at.isoformat(),
        sentences=sentences,
    )


def _state_labels(state: GraphState) -> dict[str, str]:
    return {
        **{node.id: node.title for node in state.nodes.values()},
        **{proposal.id: proposal.title for proposal in state.proposals.values()},
        **{ambiguity.id: ambiguity.question for ambiguity in state.ambiguities.values()},
    }


def _operation_fallbacks(
    patch: Patch,
    previous_state: GraphState,
    state: GraphState,
    labels: dict[str, str],
) -> list[str]:
    sentences: list[str] = []
    for operation in patch.ops:
        if isinstance(operation, CreateNodesOperation):
            for node in operation.nodes:
                title = _object_label(node.id, labels, node.title)
                noun = node.extension_type or node.type
                sentences.append(f"Recorded a {noun.replace('_', ' ')}: {_quoted(title)}.")
        elif isinstance(operation, UpdateNodesOperation):
            sentences.extend(
                f"Updated {_quoted(_object_label(item.id, labels))}." for item in operation.nodes
            )
        elif isinstance(operation, CreateEdgesOperation):
            for edge in operation.edges:
                source = _object_label(edge.source, labels)
                target = _object_label(edge.target, labels)
                sentences.append(f"Connected {_quoted(source)} with {_quoted(target)}.")
        elif isinstance(operation, RemoveEdgesOperation):
            for edge_id in operation.edge_ids:
                edge = previous_state.edges.get(edge_id) or state.edges.get(edge_id)
                if edge is None:
                    sentences.append("Removed a graph relationship.")
                    continue
                sentences.append(
                    f"Removed the relationship between "
                    f"{_quoted(_object_label(edge.source, labels))} and "
                    f"{_quoted(_object_label(edge.target, labels))}."
                )
        elif isinstance(operation, RemoveNodesOperation):
            sentences.extend(
                f"Removed {_quoted(_object_label(node_id, labels))}."
                for node_id in operation.node_ids
            )
        elif isinstance(operation, SupersedeNodesOperation):
            for item in operation.nodes:
                current = _quoted(_object_label(item.id, labels))
                replacement_id = item.superseded_by
                if replacement_id:
                    replacement = _quoted(_object_label(replacement_id, labels))
                    sentences.append(f"Superseded {current} with {replacement}.")
                else:
                    sentences.append(f"Superseded {current}.")
        elif isinstance(operation, MergeNodesOperation):
            for item in operation.merges:
                duplicate = _quoted(_object_label(item.duplicate, labels))
                canonical = _quoted(_object_label(item.canonical, labels))
                sentences.append(f"Merged {duplicate} into {canonical}.")
        elif isinstance(operation, CreateAmbiguitiesOperation):
            for item in operation.ambiguities:
                label = _object_label(item.id, labels, item.question)
                sentences.append(f"Recorded an open question: {_quoted(label)}.")
        elif isinstance(operation, ResolveAmbiguitiesOperation):
            for item in operation.resolutions:
                label = _quoted(_object_label(item.id, labels))
                verb = "Resolved" if item.status == "resolved" else "Dismissed"
                sentences.append(f"{verb} the open question {label}.")
        elif isinstance(operation, CreateProposalsOperation):
            for item in operation.proposals:
                label = _object_label(item.id, labels, item.title)
                sentences.append(f"Recorded a proposal: {_quoted(label)}.")
        elif isinstance(operation, ResolveProposalsOperation):
            for item in operation.resolutions:
                label = _quoted(_object_label(item.id, labels))
                status = item.status.replace("_", " ").title()
                sentences.append(f"{status} proposal {label}.")
        elif isinstance(operation, WithdrawProposalsOperation):
            for item in operation.proposals:
                label = _quoted(_object_label(item.id, labels))
                sentences.append(f"Withdrew proposal {label}.")
        elif isinstance(operation, UpsertGlossaryOperation):
            sentences.extend(
                f"Updated the glossary entry {_quoted(item.term.strip())}."
                for item in operation.terms
                if item.term.strip()
            )
        elif isinstance(operation, SetCoverageOperation):
            sentences.append("Updated source coverage.")
        elif isinstance(operation, SetStandingOperation):
            label = _quoted(_object_label(operation.node_id, labels))
            sentences.append(f"Marked {label} {operation.standing}.")
        elif isinstance(operation, SetProjectTruthScopeOperation):
            sentences.append("Updated the project truth scope.")
        elif isinstance(operation, SetOntologyOperation):
            sentences.append("Updated the project ontology.")
        else:
            sentences.append("Updated the research graph.")
    return sentences


def _edge_assessment_sentences(
    patch: Patch,
    previous_state: GraphState,
    state: GraphState,
    labels: dict[str, str],
) -> list[str]:
    rendered: list[str] = []
    for operation in patch.ops:
        if not isinstance(operation, CreateEdgesOperation):
            continue
        for edge in operation.edges:
            sentence = _edge_assessment_sentence(
                edge,
                previous_state,
                state,
                source=_object_label(edge.source, labels),
                target=_object_label(edge.target, labels),
            )
            if sentence is not None:
                rendered.append(sentence)
    return rendered


def _edge_assessment_sentence(
    edge: NewEdge,
    previous_state: GraphState,
    state: GraphState,
    *,
    source: str,
    target: str,
) -> str | None:
    if edge.expectation is not None:
        expectation = {
            "matched": "matched its expected outcomes",
            "diverged": "diverged from its expected outcomes",
            "no_expectation": "had no expected outcome to compare against",
        }[edge.expectation]
        sentence = f"The result {_quoted(target)} from {_quoted(source)} {expectation}"
        explanation = edge.explanation.strip().rstrip(".")
        return f"{sentence}: {explanation}." if explanation else f"{sentence}."
    source_node = state.nodes.get(edge.source) or previous_state.nodes.get(edge.source)
    target_node = state.nodes.get(edge.target) or previous_state.nodes.get(edge.target)
    project_type = project_type_of(state)
    is_evidence_hypothesis = (
        edge.relation in project_type.belief_outcome_relations
        and source_node is not None
        and project_type.is_outcome(source_node.type)
        and target_node is not None
        and project_type.is_belief(target_node.type)
    )
    assessment = edge.assessment
    if assessment is None:
        if not is_evidence_hypothesis:
            return None
        return (
            f"The {edge.relation} relation from {_quoted(source)} to {_quoted(target)} was "
            "recorded as an unassessed legacy relation."
        )

    details = [
        f"{assessment.relevance} relevance",
        f"{assessment.weight} weight",
    ]
    if assessment.scope is not None:
        details.append(f"scope {_quoted(assessment.scope)}")
    if assessment.qualifications:
        qualifications = "; ".join(_quoted(item) for item in assessment.qualifications)
        details.append(f"qualifications {qualifications}")
    return (
        f"The {edge.relation} relation from {_quoted(source)} to {_quoted(target)} was assessed "
        f"with {_reader_list(details)}."
    )


def _reader_list(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _proposal_consequence_sentences(
    patch: Patch,
    state: GraphState,
    labels: dict[str, str],
    existing: list[str],
) -> list[str]:
    proposal_ids: list[str] = []
    for operation in patch.ops:
        if isinstance(operation, CreateProposalsOperation):
            proposal_ids.extend(item.id for item in operation.proposals)
        elif isinstance(operation, ResolveProposalsOperation):
            proposal_ids.extend(
                item.id for item in operation.resolutions if item.status == "approved"
            )

    rendered: list[str] = []
    for proposal_id in dict.fromkeys(proposal_ids):
        proposal = state.proposals.get(proposal_id)
        if proposal is None:
            continue
        title = proposal.title
        consequence = proposal.card.consequences
        plain_consequence = _plain_history_text(consequence, labels)
        if not plain_consequence or any(plain_consequence in item for item in existing):
            continue
        label = _object_label(proposal_id, labels, title)
        rendered.append(
            f"The proposal {_quoted(label)} records this consequence: {_quoted(plain_consequence)}"
        )
    return rendered


def _object_label(identifier: str, labels: dict[str, str], fallback: str = "") -> str:
    return labels.get(identifier) or fallback.strip() or identifier


def _plain_history_text(value: str, labels: dict[str, str]) -> str:
    def replace_identifier(match: re.Match[str]) -> str:
        identifier = match.group(0)
        label = labels.get(identifier)
        return _strip_internal_tokens(label) if label is not None else identifier

    rendered = _GRAPH_ID_RE.sub(replace_identifier, value.strip())
    operation_names = "|".join(re.escape(name) for name in _OPERATION_LABELS)
    rendered = re.sub(
        rf"\s+(?:through|via|using)\s+(?:{operation_names})\b",
        "",
        rendered,
        flags=re.IGNORECASE,
    )
    for operation, label in _OPERATION_LABELS.items():
        rendered = re.sub(rf"\b{re.escape(operation)}\b", label, rendered)
    return " ".join(rendered.split())


def _strip_internal_tokens(value: str) -> str:
    rendered = value
    for operation, label in _OPERATION_LABELS.items():
        rendered = re.sub(rf"\b{re.escape(operation)}\b", label, rendered)
    return " ".join(rendered.split())


def _quoted(value: str) -> str:
    return f"“{_strip_internal_tokens(value)}”"


def _unique_sentences(sentences: Iterable[str]) -> list[str]:
    unique: list[str] = []
    for sentence in sentences:
        if sentence and sentence not in unique:
            unique.append(sentence)
    return unique


# Main's transition recomputes guidance validity from its merged dependencies.
_SEMANTIC_NODE_BOOKKEEPING = frozenset().union(
    *(graph_fields(model, role="engine") for model in get_args(get_args(ProjectNode)[0]))
)
_SEMANTIC_EDGE_BOOKKEEPING = graph_fields(Edge, role="engine")
# A resolution's revision and operation are bookkeeping; its status, author, and
# reasons are what the Proposal now says.
_SEMANTIC_PROPOSAL_BOOKKEEPING = graph_fields(Proposal, role="engine")
_SEMANTIC_AMBIGUITY_BOOKKEEPING = graph_fields(Ambiguity, role="engine")
_SEMANTIC_GLOSSARY_BOOKKEEPING = graph_fields(GlossaryTerm, role="engine")


class _StrictSemanticModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeSemanticDelta(_StrictSemanticModel):
    change: Literal["created", "updated", "removed"]
    node_id: str
    before: ProjectNode | None = None
    after: ProjectNode | None = None

    @model_validator(mode="after")
    def require_shape(self) -> NodeSemanticDelta:
        _require_change_shape(self.change, self.before, self.after, self.node_id)
        return self


class EdgeSemanticDelta(_StrictSemanticModel):
    change: Literal["created", "updated", "removed"]
    edge_id: str
    before: Edge | None = None
    after: Edge | None = None

    @model_validator(mode="after")
    def require_shape(self) -> EdgeSemanticDelta:
        _require_change_shape(self.change, self.before, self.after, self.edge_id)
        return self


class ProposalSemanticDelta(_StrictSemanticModel):
    change: Literal["created", "updated", "removed"]
    proposal_id: str
    before: Proposal | None = None
    after: Proposal | None = None

    @model_validator(mode="after")
    def require_shape(self) -> ProposalSemanticDelta:
        _require_change_shape(self.change, self.before, self.after, self.proposal_id)
        return self


class AmbiguitySemanticDelta(_StrictSemanticModel):
    change: Literal["created", "updated", "removed"]
    ambiguity_id: str
    before: Ambiguity | None = None
    after: Ambiguity | None = None

    @model_validator(mode="after")
    def require_shape(self) -> AmbiguitySemanticDelta:
        _require_change_shape(self.change, self.before, self.after, self.ambiguity_id)
        return self


class GlossarySemanticDelta(_StrictSemanticModel):
    change: Literal["created", "updated", "removed"]
    term: str
    before: GlossaryTerm | None = None
    after: GlossaryTerm | None = None

    @model_validator(mode="after")
    def require_shape(self) -> GlossarySemanticDelta:
        _require_change_shape(self.change, self.before, self.after, self.term, id_field="term")
        return self


class GlobalSemanticDelta(_StrictSemanticModel):
    field: Literal["project_truth_scope", "ontology"]
    before: JsonValue
    after: JsonValue

    @model_validator(mode="after")
    def require_actual_change(self) -> GlobalSemanticDelta:
        if self.before == self.after:
            raise ValueError("a global semantic delta must change its field")
        return self


class SemanticDelta(_StrictSemanticModel):
    nodes: list[NodeSemanticDelta] = Field(default_factory=list)
    edges: list[EdgeSemanticDelta] = Field(default_factory=list)
    proposals: list[ProposalSemanticDelta] = Field(default_factory=list)
    ambiguities: list[AmbiguitySemanticDelta] = Field(default_factory=list)
    glossary: list[GlossarySemanticDelta] = Field(default_factory=list)
    globals: list[GlobalSemanticDelta] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(
            (self.nodes, self.edges, self.proposals, self.ambiguities, self.glossary, self.globals)
        )


def semantic_delta(base: GraphState, branch: GraphState) -> SemanticDelta:
    """Compare graph meaning without revision bookkeeping or target policy."""
    globals_: list[GlobalSemanticDelta] = []
    if base.project_truth_scope != branch.project_truth_scope:
        globals_.append(
            GlobalSemanticDelta(
                field="project_truth_scope",
                before=base.project_truth_scope,
                after=branch.project_truth_scope,
            )
        )
    if base.ontology != branch.ontology:
        globals_.append(
            GlobalSemanticDelta(
                field="ontology",
                before=base.ontology.model_dump(mode="json"),
                after=branch.ontology.model_dump(mode="json"),
            )
        )
    return SemanticDelta(
        nodes=_typed_collection_delta(
            base.nodes,
            branch.nodes,
            model=NodeSemanticDelta,
            id_field="node_id",
        ),
        edges=_typed_collection_delta(
            base.edges,
            branch.edges,
            model=EdgeSemanticDelta,
            id_field="edge_id",
        ),
        proposals=_typed_collection_delta(
            base.proposals,
            branch.proposals,
            model=ProposalSemanticDelta,
            id_field="proposal_id",
        ),
        ambiguities=_typed_collection_delta(
            base.ambiguities,
            branch.ambiguities,
            model=AmbiguitySemanticDelta,
            id_field="ambiguity_id",
        ),
        glossary=_typed_collection_delta(
            base.glossary,
            branch.glossary,
            model=GlossarySemanticDelta,
            id_field="term",
        ),
        globals=globals_,
    )


def _require_change_shape(
    change: str,
    before: BaseModel | None,
    after: BaseModel | None,
    identity: str,
    *,
    id_field: str = "id",
) -> None:
    if change == "created" and (before is not None or after is None):
        raise ValueError("created semantic deltas require only an after value")
    if change == "updated" and (before is None or after is None):
        raise ValueError("updated semantic deltas require before and after values")
    if change == "removed" and (before is None or after is not None):
        raise ValueError("removed semantic deltas require only a before value")
    for value in (before, after):
        if value is not None and getattr(value, id_field) != identity:
            raise ValueError("semantic delta identity does not match its payload")


def _typed_collection_delta(
    before: Mapping[str, BaseModel],
    after: Mapping[str, BaseModel],
    *,
    model: type[BaseModel],
    id_field: str,
) -> list[Any]:
    result: list[Any] = []
    for identity in sorted(set(before) | set(after)):
        old = before.get(identity)
        new = after.get(identity)
        if old is new:
            continue
        if old is None:
            result.append(
                model.model_validate({"change": "created", id_field: identity, "after": new})
            )
        elif new is None:
            result.append(
                model.model_validate({"change": "removed", id_field: identity, "before": old})
            )
        elif semantic_document(old) != semantic_document(new):
            result.append(
                model.model_validate(
                    {"change": "updated", id_field: identity, "before": old, "after": new}
                )
            )
    return result


def semantic_document(value: BaseModel) -> dict[str, JsonValue]:
    """Project declared content/identity, including nested field-role metadata.

    Identity stays in whole values (list elements and references need it). Callers
    comparing an entity's individual fields select its content-role fields.
    Persisted records and their serializers remain untouched.
    """
    document = value.model_dump(mode="json", exclude=set(graph_fields(type(value), role="engine")))
    for name in document:
        document[name] = _semantic_nested(getattr(value, name), document[name])
    return document


def _semantic_nested(value: object, serialized: JsonValue) -> JsonValue:
    if isinstance(value, BaseModel):
        return semantic_document(value)
    if isinstance(value, (list, tuple)) and isinstance(serialized, list):
        return [_semantic_nested(item, raw) for item, raw in zip(value, serialized, strict=True)]
    if isinstance(value, dict) and isinstance(serialized, dict):
        return {key: _semantic_nested(value[key], raw) for key, raw in serialized.items()}
    return serialized
