from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rcp.config import RepositoryConfig
from rcp.core.models import (
    Ambiguity,
    Blocker,
    CoverageBoundary,
    Decision,
    Evidence,
    Experiment,
    GatedCard,
    GlossaryTerm,
    Hypothesis,
    OntologyState,
    Patch,
    Proposal,
    ResearchQuestion,
    Standing,
)

_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_NODE_ID = rf"[a-z][a-z0-9]*(?:_[a-z0-9]+)*/{_SLUG}"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentResearchQuestion(ResearchQuestion):
    id: str = Field(pattern=rf"^{_NODE_ID}$")
    standing: Literal[Standing.ASSERTED] = Standing.ASSERTED
    created_rev: Literal[0] = 0
    updated_rev: Literal[0] = 0


class AgentHypothesis(Hypothesis):
    id: str = Field(pattern=rf"^{_NODE_ID}$")
    standing: Literal[Standing.ASSERTED] = Standing.ASSERTED
    created_rev: Literal[0] = 0
    updated_rev: Literal[0] = 0


class AgentDecision(Decision):
    id: str = Field(pattern=rf"^{_NODE_ID}$")
    standing: Literal[Standing.ASSERTED] = Standing.ASSERTED
    created_rev: Literal[0] = 0
    updated_rev: Literal[0] = 0


class AgentExperiment(Experiment):
    id: str = Field(pattern=rf"^{_NODE_ID}$")
    standing: Literal[Standing.ASSERTED] = Standing.ASSERTED
    created_rev: Literal[0] = 0
    updated_rev: Literal[0] = 0


class AgentEvidence(Evidence):
    id: str = Field(pattern=rf"^{_NODE_ID}$")
    origin: Literal[
        "internal_run", "external_publication", "external_instance", "analytic", "unknown"
    ]
    standing: Literal[Standing.ASSERTED] = Standing.ASSERTED
    created_rev: Literal[0] = 0
    updated_rev: Literal[0] = 0


class AgentBlocker(Blocker):
    id: str = Field(pattern=rf"^{_NODE_ID}$")
    standing: Literal[Standing.ASSERTED] = Standing.ASSERTED
    created_rev: Literal[0] = 0
    updated_rev: Literal[0] = 0


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


class DecisionCause(_StrictModel):
    kind: Literal["decision"]
    ref_id: str


class ProposalResolutionCause(_StrictModel):
    kind: Literal["proposal_resolution"]
    ref_id: str


BeliefCause = Annotated[
    EvidenceEdgeCause | DecisionCause | ProposalResolutionCause,
    Field(discriminator="kind"),
]


class NodeUpdate(_StrictModel):
    id: str
    changes: dict[str, Any]
    cause: BeliefCause | None = None


class SupersedeNode(_StrictModel):
    id: str
    superseded_by: str | None = None
    explanation: str = ""
    cause: BeliefCause | None = None


class NodeMerge(_StrictModel):
    duplicate: str
    canonical: str
    explanation: str = ""
    cause: BeliefCause | None = None


class AmbiguityResolution(_StrictModel):
    id: str
    status: Literal["resolved", "dismissed"]


class ProposalWithdrawal(_StrictModel):
    id: str
    status: Literal["withdrawn"]
    reason: str | None = None


class AgentAmbiguity(Ambiguity):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=rf"^amb/{_SLUG}$")
    raised_rev: Literal[0] = 0


class AgentGlossaryTerm(GlossaryTerm):
    model_config = ConfigDict(extra="forbid")
    updated_rev: Literal[0] = 0


class AgentCoverageBoundary(CoverageBoundary):
    model_config = ConfigDict(extra="forbid")


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


class SupersedeNodesOperation(_StrictModel):
    op: Literal["supersede_nodes"]
    nodes: list[SupersedeNode] = Field(min_length=1)


class MergeNodesOperation(_StrictModel):
    op: Literal["merge_nodes"]
    merges: list[NodeMerge] = Field(min_length=1)


class CreateAmbiguitiesOperation(_StrictModel):
    op: Literal["create_ambiguities"]
    ambiguities: list[AgentAmbiguity] = Field(min_length=1)


class ResolveAmbiguitiesOperation(_StrictModel):
    op: Literal["resolve_ambiguities"]
    resolutions: list[AmbiguityResolution] = Field(min_length=1)


class UpsertGlossaryOperation(_StrictModel):
    op: Literal["upsert_glossary"]
    terms: list[AgentGlossaryTerm] = Field(min_length=1)


class SetCoverageOperation(_StrictModel):
    op: Literal["set_coverage"]
    coverage: AgentCoverageBoundary


class SetProjectTruthScopeOperation(_StrictModel):
    op: Literal["set_project_truth_scope"]
    truth_scope: list[str] = Field(min_length=1)
    repository: RepositoryConfig | None = None


class SetOntologyOperation(_StrictModel):
    op: Literal["set_ontology"]
    ontology: OntologyState


ProposalReplayOperation = Annotated[
    CreateNodesOperation
    | UpdateNodesOperation
    | CreateEdgesOperation
    | RemoveEdgesOperation
    | SupersedeNodesOperation
    | SetProjectTruthScopeOperation
    | SetOntologyOperation,
    Field(discriminator="op"),
]


class AgentProposal(Proposal):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=rf"^prop/{_SLUG}$")
    card: GatedCard
    ops: list[ProposalReplayOperation] = Field(min_length=1)
    status: Literal["pending"] = "pending"
    raised_rev: Literal[0] = 0
    resolved_rev: None = None
    rejection_reason: None = None


class CreateProposalsOperation(_StrictModel):
    op: Literal["create_proposals"]
    proposals: list[AgentProposal] = Field(min_length=1)


class ResolveProposalsOperation(_StrictModel):
    op: Literal["resolve_proposals"]
    resolutions: list[ProposalWithdrawal] = Field(min_length=1)


AgentOperation = Annotated[
    CreateNodesOperation
    | UpdateNodesOperation
    | CreateEdgesOperation
    | RemoveEdgesOperation
    | SupersedeNodesOperation
    | MergeNodesOperation
    | CreateAmbiguitiesOperation
    | ResolveAmbiguitiesOperation
    | CreateProposalsOperation
    | ResolveProposalsOperation
    | UpsertGlossaryOperation
    | SetCoverageOperation,
    Field(discriminator="op"),
]


class AgentPatch(Patch):
    model_config = ConfigDict(extra="forbid")
    revision: Literal[0] = 0
    kind: Literal["seed", "refresh", "chat", "work"]
    author: Literal["agent"]
    ops: list[AgentOperation]


def agent_output_schema() -> dict[str, object]:
    schema = AgentPatch.model_json_schema()
    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        properties.pop("admission", None)
        properties.pop("admission_messages", None)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [
            name for name in required if name not in {"admission", "admission_messages"}
        ]
    definitions = schema.get("$defs", {})
    if isinstance(definitions, dict):
        definitions.pop("ValidationMessage", None)
    return schema


def validate_agent_patch_shape(patch: Patch) -> None:
    try:
        AgentPatch.model_validate(patch.model_dump(mode="python"))
    except ValidationError as exc:
        details: list[str] = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"])
            detail = f"{location}: {error['msg']}" if location else error["msg"]
            if detail not in details:
                details.append(detail)
            if len(details) == 8:
                break
        suffix = "" if len(exc.errors()) <= len(details) else " Additional shape errors omitted."
        raise ValueError(
            "Agent patch does not match the graph operation schema: " + "; ".join(details) + suffix
        ) from exc


def normalize_agent_patch_bookkeeping(patch: Patch) -> Patch:
    """Reset revision fields that are assigned by the history manager."""

    data = patch.model_dump(mode="python")
    data["revision"] = 0
    data["admission"] = "accepted"
    data["admission_messages"] = []
    for operation in data["ops"]:
        name = operation.get("op")
        if name == "create_nodes":
            for node in operation.get("nodes", []):
                node["created_rev"] = 0
                node["updated_rev"] = 0
        elif name == "create_ambiguities":
            for ambiguity in operation.get("ambiguities", []):
                ambiguity["raised_rev"] = 0
        elif name == "create_proposals":
            for proposal in operation.get("proposals", []):
                proposal["raised_rev"] = 0
        elif name == "upsert_glossary":
            for term in operation.get("terms", []):
                term["updated_rev"] = 0
    return Patch.model_validate(data)
