from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class Standing(StrEnum):
    ASSERTED = "asserted"
    ACCEPTED = "accepted"
    CONTESTED = "contested"


class SourceRef(BaseModel):
    machine: str
    truth_repository: str
    source: Literal["claude", "codex", "app_chat"]
    session_id: str
    record_uuid: str
    timestamp: datetime
    excerpt: str = Field(max_length=800)


class ExperimentAttempt(BaseModel):
    id: str
    sequence: int = Field(ge=1)
    purpose: str
    configuration: str = ""
    status: Literal[
        "planned", "submitted", "running", "failed", "completed", "cancelled", "superseded"
    ] = "planned"
    job_refs: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    outcome: str | None = None
    failure_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class BaseNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    extension_type: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
    )
    extension_fields: dict[str, str | float | bool | list[str]] = Field(default_factory=dict)
    standing: Standing = Standing.ASSERTED
    created_rev: int = 0
    updated_rev: int = 0
    source_refs: list[SourceRef] = Field(default_factory=list)


class ResearchQuestion(BaseNode):
    type: Literal["research_question"]
    question: str
    motivation: str = ""
    scope: str = ""
    status: Literal["open", "answered", "abandoned", "superseded"] = "open"


class Hypothesis(BaseNode):
    type: Literal["hypothesis"]
    statement: str
    rationale: str = ""
    predictions: list[str] = Field(default_factory=list)
    scope: str = ""
    status: Literal["proposed", "active", "supported", "weakened", "rejected", "superseded"] = (
        "proposed"
    )


class Decision(BaseNode):
    type: Literal["decision"]
    question: str
    options: list[str] = Field(default_factory=list)
    selected_option: str | None = None
    rationale: str | None = None
    consequences: list[str] = Field(default_factory=list)
    status: Literal["open", "decided", "revisit", "superseded"] = "open"


class Experiment(BaseNode):
    type: Literal["experiment"]
    objective: str
    design: str = ""
    expected_outcomes: list[str] = Field(default_factory=list)
    interpretation_rules: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    status: Literal[
        "proposed",
        "designing",
        "implementing",
        "debugging",
        "running",
        "analyzing",
        "completed",
        "blocked",
        "abandoned",
        "superseded",
    ] = "proposed"
    attempts: list[ExperimentAttempt] = Field(default_factory=list)
    current_summary: str = ""
    next_action: str | None = None


class Evidence(BaseNode):
    type: Literal["evidence"]
    observation: str
    interpretation: str = ""
    strength: Literal["diagnostic", "preliminary", "supporting", "confirmatory"] = "preliminary"
    validity: Literal["valid", "qualified", "invalid", "superseded"] = "valid"
    origin: Literal[
        "internal_run", "external_publication", "external_instance", "analytic", "unknown"
    ] = "unknown"
    artifact_refs: list[str] = Field(default_factory=list)


class Blocker(BaseNode):
    type: Literal["blocker"]
    description: str
    blocker_type: Literal[
        "scientific", "design", "data", "implementation", "infrastructure", "unknown"
    ] = "unknown"
    status: Literal["open", "resolved", "superseded"] = "open"
    resolution_condition: str = ""
    recommended_action: str | None = None


ProjectNode = Annotated[
    ResearchQuestion | Hypothesis | Decision | Experiment | Evidence | Blocker,
    Field(discriminator="type"),
]


HUMAN_EDITABLE_NODE_FIELDS: dict[str, frozenset[str]] = {
    "research_question": frozenset({"title", "question", "motivation", "scope"}),
    "hypothesis": frozenset({"title", "statement", "rationale", "predictions", "scope"}),
    "decision": frozenset({"title", "question", "options", "rationale", "consequences"}),
    "experiment": frozenset(
        {
            "title",
            "objective",
            "design",
            "expected_outcomes",
            "interpretation_rules",
            "completion_criteria",
            "current_summary",
            "next_action",
        }
    ),
    "evidence": frozenset({"title", "observation", "interpretation"}),
    "blocker": frozenset({"title", "description", "resolution_condition", "recommended_action"}),
}


NodeType = Literal[
    "research_question", "hypothesis", "decision", "experiment", "evidence", "blocker"
]
BaseRelation = Literal[
    "has_subquestion",
    "has_hypothesis",
    "has_decision",
    "tests",
    "governed_by",
    "produces",
    "blocked_by",
    "supports",
    "weakens",
    "contradicts",
    "refutes",
    "inconclusive",
    "requires_decision",
    "supersedes",
    "duplicate_of",
]
RelationLayer = Literal["epistemic", "action", "seam", "meta"]
ALL_NODE_TYPES: frozenset[str] = frozenset(
    {"research_question", "hypothesis", "decision", "experiment", "evidence", "blocker"}
)


class OntologyTypeDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
    definition: str = Field(min_length=1)
    base_type: NodeType
    layer: Literal["epistemic", "action"]
    deprecated: bool = False


class OntologyFieldDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_type: str = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
    name: str = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
    definition: str = Field(min_length=1)
    kind: Literal["text", "number", "boolean", "text_list"]
    required: bool = False
    agent_writable: bool = True
    deprecated: bool = False


class OntologyRelationDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
    definition: str = Field(min_length=1)
    source_types: list[str] = Field(min_length=1)
    target_types: list[str] = Field(min_length=1)
    layer: Literal["epistemic", "action"]
    deprecated: bool = False


class OntologyState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    types: list[OntologyTypeDefinition] = Field(default_factory=list)
    fields: list[OntologyFieldDefinition] = Field(default_factory=list)
    relations: list[OntologyRelationDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_unique_names(self) -> OntologyState:
        for label, names in (
            ("type", [item.name for item in self.types]),
            ("field", [f"{item.owner_type}.{item.name}" for item in self.fields]),
            ("relation", [item.name for item in self.relations]),
        ):
            if len(names) != len(set(names)):
                raise ValueError(f"ontology contains duplicate {label} definitions")
        return self


@dataclass(frozen=True)
class RelationSpec:
    source_types: frozenset[str]
    target_types: frozenset[str]
    layer: RelationLayer
    same_type: bool = False


RELATION_SPEC: dict[BaseRelation, RelationSpec] = {
    "has_subquestion": RelationSpec(
        frozenset({"research_question"}), frozenset({"research_question"}), "epistemic"
    ),
    "has_hypothesis": RelationSpec(
        frozenset({"research_question"}), frozenset({"hypothesis"}), "epistemic"
    ),
    "supports": RelationSpec(frozenset({"evidence"}), frozenset({"hypothesis"}), "epistemic"),
    "weakens": RelationSpec(frozenset({"evidence"}), frozenset({"hypothesis"}), "epistemic"),
    "refutes": RelationSpec(frozenset({"evidence"}), frozenset({"hypothesis"}), "epistemic"),
    "inconclusive": RelationSpec(frozenset({"evidence"}), frozenset({"hypothesis"}), "epistemic"),
    "contradicts": RelationSpec(
        frozenset({"evidence", "hypothesis"}), frozenset({"hypothesis"}), "epistemic"
    ),
    "tests": RelationSpec(frozenset({"experiment"}), frozenset({"hypothesis"}), "seam"),
    "produces": RelationSpec(frozenset({"experiment"}), frozenset({"evidence"}), "seam"),
    "has_decision": RelationSpec(
        frozenset({"research_question"}), frozenset({"decision"}), "action"
    ),
    "governed_by": RelationSpec(frozenset({"experiment"}), frozenset({"decision"}), "action"),
    "blocked_by": RelationSpec(
        frozenset({"experiment", "decision", "research_question"}),
        frozenset({"blocker"}),
        "action",
    ),
    "requires_decision": RelationSpec(frozenset({"blocker"}), frozenset({"decision"}), "action"),
    "supersedes": RelationSpec(ALL_NODE_TYPES, ALL_NODE_TYPES, "meta", same_type=True),
    "duplicate_of": RelationSpec(ALL_NODE_TYPES, ALL_NODE_TYPES, "meta", same_type=True),
}


class Edge(BaseModel):
    # Base-relation layers are overwritten during parsing. Custom relation
    # layers are resolved from the materialized ontology before an Edge is built.
    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    target: str
    relation: str = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
    layer: RelationLayer
    explanation: str = ""
    created_rev: int = 0

    @model_validator(mode="before")
    @classmethod
    def derive_base_relation_layer(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        relation = value.get("relation")
        spec = RELATION_SPEC.get(relation) if isinstance(relation, str) else None
        if spec is None:
            return value
        return {**value, "layer": spec.layer}


class GatedCard(BaseModel):
    situation_cold: str = ""
    why_human_now: str = ""
    consequences: str = ""
    decision_needed: str = ""


class Proposal(BaseModel):
    id: str
    title: str
    card: GatedCard
    ops: list[dict[str, Any]]
    related_node_ids: list[str] = Field(default_factory=list)
    related_config_keys: list[str] = Field(default_factory=list)
    base_rev: int
    status: Literal["pending", "approved", "rejected", "withdrawn"] = "pending"
    raised_rev: int = 0
    resolved_rev: int | None = None
    rejection_reason: str | None = None


class Ambiguity(BaseModel):
    id: str
    question: str
    why_it_matters: str
    candidates: list[str] = Field(default_factory=list)
    related_node_ids: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    status: Literal["open", "resolved", "dismissed"] = "open"
    raised_rev: int = 0


class GlossaryTerm(BaseModel):
    term: str
    plain_definition: str
    where_defined: str | None = None
    updated_rev: int = 0


class CoverageBoundary(BaseModel):
    repositories_seen: list[str] = Field(default_factory=list)
    repositories_never_seen: list[str] = Field(default_factory=list)
    sessions_read: list[str] = Field(default_factory=list)
    sessions_skipped: list[str] = Field(default_factory=list)
    earliest_timestamp: datetime | None = None
    note: str = "No seed has completed."


class ValidationMessage(BaseModel):
    level: Literal["flag", "reject"]
    code: str
    message: str
    patch_revision: int | None = None
    related_node_ids: list[str] = Field(default_factory=list)
    related_edge_ids: list[str] = Field(default_factory=list)


class BeliefTransition(BaseModel):
    hypothesis_id: str
    from_status: str
    to_status: str
    revision: int
    cause: dict[str, Any]


class ReplayFailure(BaseModel):
    revision: int
    created_at: datetime
    code: str
    message: str


class GraphState(BaseModel):
    revision: int = 0
    project_truth_scope: list[str] = Field(default_factory=list)
    config_revisions: dict[str, int] = Field(default_factory=dict)
    nodes: dict[str, ProjectNode] = Field(default_factory=dict)
    edges: dict[str, Edge] = Field(default_factory=dict)
    proposals: dict[str, Proposal] = Field(default_factory=dict)
    ambiguities: dict[str, Ambiguity] = Field(default_factory=dict)
    glossary: dict[str, GlossaryTerm] = Field(default_factory=dict)
    ontology: OntologyState = Field(default_factory=OntologyState)
    coverage: CoverageBoundary = Field(default_factory=CoverageBoundary)
    validation_messages: list[ValidationMessage] = Field(default_factory=list)
    belief_transitions: list[BeliefTransition] = Field(default_factory=list)
    replay_status: Literal["complete", "degraded"] = "complete"
    replay_failure: ReplayFailure | None = None
    last_refresh_at: datetime | None = None


class Patch(BaseModel):
    revision: int = 0
    kind: Literal["seed", "refresh", "chat", "work", "approval"]
    author: Literal["agent", "human"]
    created_at: datetime = Field(default_factory=utc_now)
    summary: str
    ops: list[dict[str, Any]]
    run_truth_scope: list[str] = Field(default_factory=list)
    repositories_read: list[str] = Field(default_factory=list)
    processed_cursors: dict[str, str] = Field(default_factory=dict)
    change_summary: list[str] = Field(default_factory=list)
    admission: Literal["accepted", "rejected"] = "accepted"
    admission_messages: list[ValidationMessage] = Field(default_factory=list)
