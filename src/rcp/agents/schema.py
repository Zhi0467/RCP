from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from rcp.core.authority import HYPOTHESIS_PROPOSAL_FIELDS
from rcp.core.models import (
    ExperimentAttempt,
    GatedCard,
    Patch,
    SourceRef,
)

_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_NODE_ID = rf"[a-z][a-z0-9]*(?:_[a-z0-9]+)*/{_SLUG}"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentSourceRef(SourceRef):
    model_config = ConfigDict(extra="forbid")


class AgentExperimentAttempt(ExperimentAttempt):
    model_config = ConfigDict(extra="forbid")

    source_refs: list[AgentSourceRef] = Field(default_factory=list)


class AgentGatedCard(GatedCard):
    model_config = ConfigDict(extra="forbid")


class AgentNode(_StrictModel):
    id: str = Field(pattern=rf"^{_NODE_ID}$")
    title: str
    extension_type: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
    )
    extension_fields: dict[str, str | float | bool | list[str]] = Field(default_factory=dict)
    source_refs: list[AgentSourceRef] = Field(default_factory=list)


class AgentResearchQuestion(AgentNode):
    type: Literal["research_question"]
    question: str
    motivation: str = ""
    scope: str = ""
    status: Literal["open", "answered", "abandoned", "superseded"] = "open"


class AgentHypothesis(AgentNode):
    type: Literal["hypothesis"]
    statement: str
    rationale: str = ""
    predictions: list[str] = Field(default_factory=list)
    scope: str = ""
    status: Literal["proposed"] = "proposed"


class AgentDecision(AgentNode):
    type: Literal["decision"]
    question: str
    options: list[str] = Field(default_factory=list)
    selected_option: None = None
    rationale: str | None = None
    consequences: list[str] = Field(default_factory=list)
    status: Literal["open", "ready"] = "open"


class AgentExperiment(AgentNode):
    type: Literal["experiment"]
    objective: str
    design: str = ""
    expected_outcomes: list[str] = Field(default_factory=list)
    interpretation_rules: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    invocation_ceiling: int = Field(default=5, ge=1)
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
    attempts: list[AgentExperimentAttempt] = Field(default_factory=list)
    current_summary: str = ""
    next_action: str | None = None


class AgentEvidence(AgentNode):
    type: Literal["evidence"]
    observation: str
    interpretation: str = ""
    strength: Literal["diagnostic", "preliminary", "supporting", "confirmatory"] = "preliminary"
    validity: Literal["valid", "qualified", "invalid", "superseded"] = "valid"
    origin: Literal[
        "internal_run", "external_publication", "external_instance", "analytic", "unknown"
    ]
    artifact_refs: list[str] = Field(default_factory=list)


class AgentBlocker(AgentNode):
    type: Literal["blocker"]
    description: str
    blocker_type: Literal[
        "scientific", "design", "data", "implementation", "infrastructure", "unknown"
    ] = "unknown"
    status: Literal["open", "resolved", "superseded"] = "open"
    resolution_condition: str = ""
    recommended_action: str | None = None


AgentProjectNode = Annotated[
    AgentResearchQuestion
    | AgentHypothesis
    | AgentDecision
    | AgentExperiment
    | AgentEvidence
    | AgentBlocker,
    Field(discriminator="type"),
]


class NewEdge(_StrictModel):
    id: str | None = None
    source: str
    target: str
    relation: str = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
    explanation: str = ""


class EvidenceEdgeCause(_StrictModel):
    kind: Literal["evidence_edge"]
    ref_id: str


class NodeUpdate(_StrictModel):
    id: str
    changes: dict[str, Any]
    cause: EvidenceEdgeCause | None = None


class SupersedeNode(_StrictModel):
    id: str
    superseded_by: str | None = None
    explanation: str = ""
    cause: EvidenceEdgeCause | None = None


class NodeMerge(_StrictModel):
    duplicate: str
    canonical: str
    explanation: str = ""
    cause: EvidenceEdgeCause | None = None


class AgentGlossaryTerm(_StrictModel):
    term: str
    plain_definition: str
    where_defined: str | None = None


class CreateNodesOperation(_StrictModel):
    op: Literal["create_nodes"]
    nodes: list[AgentProjectNode] = Field(min_length=1)


class UpdateNodesOperation(_StrictModel):
    op: Literal["update_nodes"]
    nodes: list[NodeUpdate] = Field(min_length=1)


class CreateEdgesOperation(_StrictModel):
    op: Literal["create_edges"]
    edges: list[NewEdge] = Field(min_length=1)


class RemoveEdgesOperation(_StrictModel):
    op: Literal["remove_edges"]
    edge_ids: list[str] = Field(min_length=1)


class RemoveNodesOperation(_StrictModel):
    op: Literal["remove_nodes"]
    node_ids: list[str] = Field(min_length=1)


class SupersedeNodesOperation(_StrictModel):
    op: Literal["supersede_nodes"]
    nodes: list[SupersedeNode] = Field(min_length=1)


class MergeNodesOperation(_StrictModel):
    op: Literal["merge_nodes"]
    merges: list[NodeMerge] = Field(min_length=1)


class UpsertGlossaryOperation(_StrictModel):
    op: Literal["upsert_glossary"]
    terms: list[AgentGlossaryTerm] = Field(min_length=1)


class ProposalNodeUpdate(NodeUpdate):
    cause: EvidenceEdgeCause

    @model_validator(mode="after")
    def validate_agent_authority_shape(self) -> ProposalNodeUpdate:
        fields = frozenset(self.changes)
        if fields != HYPOTHESIS_PROPOSAL_FIELDS:
            raise ValueError("An agent Proposal may change only Hypothesis status.")
        return self


class ProposalUpdateNodesOperation(_StrictModel):
    op: Literal["update_nodes"]
    nodes: list[ProposalNodeUpdate] = Field(min_length=1, max_length=1)


class AgentProposal(_StrictModel):
    id: str = Field(pattern=rf"^prop/{_SLUG}$")
    title: str
    card: AgentGatedCard
    ops: list[ProposalUpdateNodesOperation] = Field(min_length=1, max_length=1)


class CreateProposalsOperation(_StrictModel):
    op: Literal["create_proposals"]
    proposals: list[AgentProposal] = Field(min_length=1)


class AgentProposalWithdrawal(_StrictModel):
    id: str = Field(pattern=rf"^prop/{_SLUG}$")
    reason: str = ""


class WithdrawProposalsOperation(_StrictModel):
    op: Literal["withdraw_proposals"]
    proposals: list[AgentProposalWithdrawal] = Field(min_length=1)


AgentOperation = Annotated[
    CreateNodesOperation
    | UpdateNodesOperation
    | CreateEdgesOperation
    | RemoveEdgesOperation
    | RemoveNodesOperation
    | SupersedeNodesOperation
    | MergeNodesOperation
    | CreateProposalsOperation
    | WithdrawProposalsOperation
    | UpsertGlossaryOperation,
    Field(discriminator="op"),
]


class AgentPatch(_StrictModel):
    summary: str
    ops: list[AgentOperation]
    repositories_read: list[str] = Field(default_factory=list)
    change_summary: list[str] = Field(default_factory=list)


def agent_output_schema() -> dict[str, object]:
    return AgentPatch.model_json_schema()


def parse_agent_patch_json(value: str) -> AgentPatch:
    """Parse one semantic deliverable while preserving actionable schema diagnostics."""

    try:
        return AgentPatch.model_validate_json(value)
    except ValidationError as exc:
        raise _agent_patch_shape_error(exc) from exc


def validate_agent_patch_shape(patch: AgentPatch | Patch) -> None:
    value: AgentPatch | dict[str, Any]
    if isinstance(patch, AgentPatch):
        value = patch
    else:
        value = {
            "summary": patch.summary,
            "ops": _strip_rcp_bookkeeping(patch.ops),
            "repositories_read": patch.repositories_read,
            "change_summary": patch.change_summary,
        }
    try:
        AgentPatch.model_validate(value)
    except ValidationError as exc:
        raise _agent_patch_shape_error(exc) from exc


def _agent_patch_shape_error(exc: ValidationError) -> ValueError:
    details: list[str] = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"])
        detail = f"{location}: {error['msg']}" if location else error["msg"]
        if detail not in details:
            details.append(detail)
        if len(details) == 8:
            break
    suffix = "" if len(exc.errors()) <= len(details) else " Additional shape errors omitted."
    return ValueError(
        "Agent patch does not match the graph operation schema: " + "; ".join(details) + suffix
    )


def prepare_agent_patch(
    draft: AgentPatch,
    *,
    kind: Literal["seed", "refresh", "work", "experiment_loop"],
    run_truth_scope: list[str],
    source_operation_id: str | None = None,
) -> Patch:
    """Wrap one semantic agent deliverable in RCP-owned canonical metadata."""

    operations = draft.model_dump(mode="python", exclude_none=True, exclude_unset=True)["ops"]
    for operation in operations:
        if operation.get("op") != "create_proposals":
            continue
        for proposal in operation.get("proposals", []):
            proposal.update(
                {
                    "related_node_ids": [],
                    "related_config_keys": [],
                    "base_rev": 0,
                    "status": "pending",
                    "created_by": "agent",
                    "created_by_operation_id": source_operation_id,
                    "raised_rev": 0,
                    "resolved_rev": None,
                    "resolved_by": None,
                    "resolved_by_operation_id": None,
                    "resolution_reason": None,
                    "rejection_reason": None,
                }
            )
    return Patch(
        kind=kind,
        author="agent",
        summary=draft.summary,
        ops=operations,
        run_truth_scope=list(run_truth_scope),
        repositories_read=list(draft.repositories_read),
        change_summary=list(draft.change_summary),
        processed_cursors={},
        source_operation_id=source_operation_id,
    )


def _strip_rcp_bookkeeping(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stripped: list[dict[str, Any]] = []
    for operation in operations:
        item = {key: value for key, value in operation.items()}
        name = item.get("op")
        if name == "create_nodes":
            item["nodes"] = [
                {
                    key: value
                    for key, value in node.items()
                    if key not in {"standing", "created_rev", "updated_rev"}
                }
                for node in item.get("nodes", [])
            ]
        elif name == "create_edges":
            item["edges"] = [
                {key: value for key, value in edge.items() if key not in {"layer", "created_rev"}}
                for edge in item.get("edges", [])
            ]
        elif name == "create_ambiguities":
            item["ambiguities"] = [
                {key: value for key, value in ambiguity.items() if key != "raised_rev"}
                for ambiguity in item.get("ambiguities", [])
            ]
        elif name == "create_proposals":
            item["proposals"] = [
                {
                    key: value
                    for key, value in proposal.items()
                    if key
                    not in {
                        "related_node_ids",
                        "related_config_keys",
                        "base_rev",
                        "status",
                        "created_by",
                        "created_by_operation_id",
                        "raised_rev",
                        "resolved_rev",
                        "resolved_by",
                        "resolved_by_operation_id",
                        "resolution_reason",
                        "rejection_reason",
                    }
                }
                for proposal in item.get("proposals", [])
            ]
        elif name == "upsert_glossary":
            item["terms"] = [
                {key: value for key, value in term.items() if key != "updated_rev"}
                for term in item.get("terms", [])
            ]
        stripped.append(item)
    return stripped
