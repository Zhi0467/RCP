"""Graph-only semantic rebase and merge for one Auto-research branch.

This module deliberately owns no task allocation, branch history, API route, or
merge-receipt persistence. It consumes exact branch/main snapshots, builds ordinary
changes, uses a stage-only agent for residue, and uses HistoryManager's
single-transition validation/append boundary through a small structural port.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing, suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from rcp.agents import AgentEvent, AgentLauncher, agent_output_schema, parse_agent_patch_json
from rcp.agents.branch_merge_prompt import (
    branch_merge_correction_contract,
    branch_merge_rebase_contract,
    branch_merge_task_contract,
)
from rcp.agents.context import _has_ontology_extensions
from rcp.agents.schema import (
    AgentProjectNode,
    OrchestratorAgentPatch,
    _strip_rcp_bookkeeping,
    prepare_agent_patch,
)
from rcp.agents.write_scope import ProjectWriteScope
from rcp.core.authority import RESTRUCTURE_PROTECTED_EPISTEMIC, operation_actions
from rcp.core.materialize import apply_valid_patch
from rcp.core.models import (
    Ambiguity,
    AuthorizedHuman,
    BranchMergeProvenance,
    BranchMergeReceipt,
    Edge,
    GlossaryTerm,
    GraphBranchMetadata,
    GraphState,
    Hypothesis,
    Patch,
    ProjectNode,
    Proposal,
)
from rcp.core.operations import (
    CreateProposalsOperation,
    HumanEditCause,
    ProposalOperation,
    ProposalStandingChangeOperation,
    ProposalStatusChangeOperation,
    SetStandingOperation,
    UpdateNodesOperation,
    graph_operations_from_proposal,
)
from rcp.core.transition_models import (
    GraphHeadRef,
    TransitionConflictDetail,
    TransitionTrace,
)
from rcp.core.transitions import (
    TRANSITION_RULESET_TAG,
    CommittedTransition,
    GraphTransitionManager,
    PreparedTransition,
    project_transition_projection,
    transition_trigger_manifest,
)
from rcp.core.validation import validate_patch
from rcp.core.validation.constants import IMMUTABLE_NODE_UPDATE_FIELDS
from rcp.core.validation.proposals import proposal_is_stale
from rcp.history import (
    BranchMergeAlreadyCommitted,
    BranchMergeAlreadyResolved,
    PatchRejected,
    RevisionConflict,
)
from rcp.limits import PATCH_CORRECTION_MAX_ROUNDS
from rcp.runs.shared import (
    AgentOutputProblem,
    _collect_patch_text,
    _existing_patch_digest,
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _sse,
    _stage_json_task_input,
    _stage_task_contract,
    _stream_agent_events,
    _task_token,
)
from rcp.service import RunRequest, _proposal_judgment_patch
from rcp.transport import RemoteRunStage, StateUnavailable

MAX_BRANCH_MERGE_REBASE_ROUNDS = 3
# Main's transition recomputes guidance validity from its merged dependencies.
_SEMANTIC_NODE_BOOKKEEPING = frozenset(
    {"created_rev", "updated_rev", "current_summary_stale", "next_action_stale"}
)
_SEMANTIC_EDGE_BOOKKEEPING = frozenset({"created_rev"})
_SEMANTIC_PROPOSAL_BOOKKEEPING = frozenset(
    {
        "base_rev",
        "raised_rev",
        "resolved_rev",
        "created_by_operation_id",
        "resolved_by_operation_id",
    }
)
_SEMANTIC_AMBIGUITY_BOOKKEEPING = frozenset({"raised_rev"})
_SEMANTIC_GLOSSARY_BOOKKEEPING = frozenset({"updated_rev"})


class _StrictMergeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BranchPatchSummary(_StrictMergeModel):
    revision: int = Field(ge=0)
    transition_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    summary: str
    change_summary: list[str] = Field(default_factory=list)
    task_id: str | None = None
    profile: Literal["ordinary", "orchestrator"] | None = None


class BranchHumanReviewChange(_StrictMergeModel):
    revision: int = Field(ge=1)
    operation_index: int = Field(ge=0)
    operation: ProposalStandingChangeOperation | ProposalStatusChangeOperation


class BranchMergeReviewContract(_StrictMergeModel):
    protected_changes: Literal["pending_main_proposals"] = "pending_main_proposals"
    source_resolutions: Literal["remain_on_branch"] = "remain_on_branch"
    source_stale_proposals: Literal["remain_on_branch"] = "remain_on_branch"
    repeat_review: Literal["once_per_source_baseline_and_semantic_operation"] = (
        "once_per_source_baseline_and_semantic_operation"
    )
    same_node_bundle: Literal["one_content_one_status_one_standing"] = (
        "one_content_one_status_one_standing"
    )
    accepted_ordinary_removal: Literal["pending_main_proposal"] = "pending_main_proposal"
    human_changes: list[BranchHumanReviewChange] = Field(default_factory=list)


class BranchMergeEligibility(_StrictMergeModel):
    """Caller-supplied durable proof that no branch writer may race the merge."""

    branch_head: GraphHeadRef
    episode_ending: Literal["completed", "exhausted", "stopped", "failed", "human_pause"]
    active_branch_writer_task_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_quiescence(self) -> BranchMergeEligibility:
        if self.branch_head.target.kind != "branch":
            raise ValueError("merge eligibility must name one exact branch head")
        if self.active_branch_writer_task_ids:
            raise ValueError("a graph branch with an active writer is not merge eligible")
        return self


class NodeSemanticDelta(_StrictMergeModel):
    change: Literal["created", "updated", "removed"]
    node_id: str
    before: ProjectNode | None = None
    after: ProjectNode | None = None

    @model_validator(mode="after")
    def require_shape(self) -> NodeSemanticDelta:
        _require_change_shape(self.change, self.before, self.after, self.node_id)
        return self


class EdgeSemanticDelta(_StrictMergeModel):
    change: Literal["created", "updated", "removed"]
    edge_id: str
    before: Edge | None = None
    after: Edge | None = None

    @model_validator(mode="after")
    def require_shape(self) -> EdgeSemanticDelta:
        _require_change_shape(self.change, self.before, self.after, self.edge_id)
        return self


class ProposalSemanticDelta(_StrictMergeModel):
    change: Literal["created", "updated", "removed"]
    proposal_id: str
    before: Proposal | None = None
    after: Proposal | None = None

    @model_validator(mode="after")
    def require_shape(self) -> ProposalSemanticDelta:
        _require_change_shape(self.change, self.before, self.after, self.proposal_id)
        return self


class AmbiguitySemanticDelta(_StrictMergeModel):
    change: Literal["created", "updated", "removed"]
    ambiguity_id: str
    before: Ambiguity | None = None
    after: Ambiguity | None = None

    @model_validator(mode="after")
    def require_shape(self) -> AmbiguitySemanticDelta:
        _require_change_shape(self.change, self.before, self.after, self.ambiguity_id)
        return self


class GlossarySemanticDelta(_StrictMergeModel):
    change: Literal["created", "updated", "removed"]
    term: str
    before: GlossaryTerm | None = None
    after: GlossaryTerm | None = None

    @model_validator(mode="after")
    def require_shape(self) -> GlossarySemanticDelta:
        _require_change_shape(self.change, self.before, self.after, self.term, id_field="term")
        return self


class GlobalSemanticDelta(_StrictMergeModel):
    field: Literal["project_truth_scope", "ontology"]
    before: JsonValue
    after: JsonValue

    @model_validator(mode="after")
    def require_actual_change(self) -> GlobalSemanticDelta:
        if self.before == self.after:
            raise ValueError("a global semantic delta must change its field")
        return self


class GraphSemanticDelta(_StrictMergeModel):
    base_head: GraphHeadRef
    branch_head: GraphHeadRef
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


class BranchMergeConflict(_StrictMergeModel):
    code: Literal[
        "both_changed",
        "branch_created_main_created",
        "branch_removed_main_changed",
        "branch_changed_main_removed",
    ]
    collection: Literal[
        "nodes",
        "edges",
        "proposals",
        "ambiguities",
        "glossary",
        "project_truth_scope",
        "ontology",
    ]
    entity_id: str | None = None
    field_path: str
    base: JsonValue
    branch: JsonValue
    main: JsonValue
    message: str


class BranchMergeTransitionContract(_StrictMergeModel):
    ruleset_tag: str
    trigger_manifest: dict[str, JsonValue]
    transition_trace_schema: dict[str, JsonValue]
    orchestrator_patch_schema: dict[str, JsonValue]

    @classmethod
    def current(cls) -> BranchMergeTransitionContract:
        return cls(
            ruleset_tag=TRANSITION_RULESET_TAG,
            trigger_manifest=transition_trigger_manifest().model_dump(mode="json"),
            transition_trace_schema=TransitionTrace.model_json_schema(),
            orchestrator_patch_schema=agent_output_schema(profile="orchestrator"),
        )


class BranchMergeContext(_StrictMergeModel):
    """One closed, content-addressed base/branch/main merge input."""

    schema_generation: Literal[1] = 1
    context_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    merge_task_id: str = Field(min_length=1)
    authorized_by: AuthorizedHuman
    metadata: GraphBranchMetadata
    eligibility: BranchMergeEligibility
    base_graph: GraphState
    branch_graph: GraphState
    main_head: GraphHeadRef
    main_graph: GraphState
    run_truth_scope: list[str] = Field(min_length=1)
    semantic_delta: GraphSemanticDelta
    branch_patch_summaries: list[BranchPatchSummary] = Field(default_factory=list)
    deterministic_conflicts: list[BranchMergeConflict] = Field(default_factory=list)
    transition_contract: BranchMergeTransitionContract
    review_contract: BranchMergeReviewContract = Field(default_factory=BranchMergeReviewContract)
    previous_merge_receipt: BranchMergeReceipt | None = None
    previous_branch_graph: GraphState | None = None

    @property
    def review_base_head(self) -> GraphHeadRef:
        if self.previous_merge_receipt is not None:
            return self.previous_merge_receipt.provenance.branch_head
        return self.metadata.base_head

    @model_validator(mode="after")
    def require_exact_snapshots(self) -> BranchMergeContext:
        if self.run_truth_scope != sorted(set(self.run_truth_scope)):
            raise ValueError("branch merge run truth scope must be sorted and unique")
        if self.metadata.base_head.revision != self.base_graph.revision:
            raise ValueError("branch merge base graph does not match the exact base head")
        if self.metadata.head.revision != self.branch_graph.revision:
            raise ValueError("branch merge graph does not match the exact branch head")
        if (
            self.main_head.target.kind != "main"
            or self.main_head.revision != self.main_graph.revision
        ):
            raise ValueError("branch merge main graph does not match one exact main head")
        if self.eligibility.branch_head != self.metadata.head:
            raise ValueError("merge eligibility was proved for a different branch head")
        if self.semantic_delta.base_head != self.metadata.base_head:
            raise ValueError("semantic delta names a different immutable branch base")
        if self.semantic_delta.branch_head != self.metadata.head:
            raise ValueError("semantic delta names a different branch head")
        expected_delta = build_semantic_delta(
            self.base_graph,
            self.branch_graph,
            base_head=self.metadata.base_head,
            branch_head=self.metadata.head,
        )
        if self.semantic_delta != expected_delta:
            raise ValueError("branch merge semantic delta does not match its graph snapshots")
        if (self.previous_merge_receipt is None) != (self.previous_branch_graph is None):
            raise ValueError(
                "previous merge proof requires both its receipt and exact source graph"
            )
        if self.previous_merge_receipt is not None:
            receipt = self.previous_merge_receipt
            assert self.previous_branch_graph is not None
            if (
                receipt.provenance.branch_id != self.metadata.branch_id
                or receipt.provenance.episode_id != self.metadata.episode_id
                or receipt.provenance.branch_base_head != self.metadata.base_head
                or receipt.provenance.branch_head.target != self.metadata.head.target
                or receipt.provenance.branch_head.revision != self.previous_branch_graph.revision
                or self.previous_branch_graph.revision > self.branch_graph.revision
                or receipt.result_main_head.revision > self.main_graph.revision
            ):
                raise ValueError(
                    "previous merge receipt does not match this source lineage and main"
                )
        merge_base = self.previous_branch_graph or self.base_graph
        expected_conflicts = detect_branch_merge_conflicts(
            merge_base,
            _merge_branch_graph(merge_base, self.branch_graph),
            self.main_graph,
        )
        if self.deterministic_conflicts != expected_conflicts:
            raise ValueError("branch merge conflicts do not match its graph snapshots")
        revisions = [item.revision for item in self.branch_patch_summaries]
        if revisions != sorted(set(revisions)):
            raise ValueError("branch Patch summaries must be ordered by unique revision")
        if any(
            revision <= self.metadata.base_head.revision or revision > self.metadata.head.revision
            for revision in revisions
        ):
            raise ValueError("branch Patch summary lies outside the exact branch lineage")
        if self.transition_contract != BranchMergeTransitionContract.current():
            raise ValueError("branch merge context uses a stale transition-manager contract")
        for change in self.review_contract.human_changes:
            if (
                not self.metadata.base_head.revision
                < change.revision
                <= self.metadata.head.revision
            ):
                raise ValueError("human review change lies outside the exact branch lineage")
            _require_source_human_change(merge_base, self.branch_graph, change.operation)
        expected_id = _context_id(self.model_dump(mode="json", exclude={"context_id"}))
        if self.context_id != expected_id:
            raise ValueError("branch merge context id does not match its canonical content")
        return self

    @classmethod
    def create(
        cls,
        *,
        merge_task_id: str,
        authorized_by: AuthorizedHuman,
        metadata: GraphBranchMetadata,
        eligibility: BranchMergeEligibility,
        base_graph: GraphState,
        branch_graph: GraphState,
        main_head: GraphHeadRef,
        main_graph: GraphState,
        run_truth_scope: list[str],
        branch_patch_summaries: list[BranchPatchSummary] | None = None,
        human_review_changes: list[BranchHumanReviewChange] | None = None,
        previous_merge_receipt: BranchMergeReceipt | None = None,
        previous_branch_graph: GraphState | None = None,
    ) -> BranchMergeContext:
        if run_truth_scope != sorted(set(run_truth_scope)) or not run_truth_scope:
            raise ValueError("branch merge run truth scope must be non-empty, sorted, and unique")
        delta = build_semantic_delta(
            base_graph,
            branch_graph,
            base_head=metadata.base_head,
            branch_head=metadata.head,
        )
        payload: dict[str, object] = {
            "schema_generation": 1,
            "merge_task_id": merge_task_id,
            "authorized_by": authorized_by,
            "metadata": metadata,
            "eligibility": eligibility,
            "base_graph": base_graph,
            "branch_graph": branch_graph,
            "main_head": main_head,
            "main_graph": main_graph,
            "run_truth_scope": list(run_truth_scope),
            "semantic_delta": delta,
            "branch_patch_summaries": list(branch_patch_summaries or ()),
            "deterministic_conflicts": detect_branch_merge_conflicts(
                previous_branch_graph or base_graph,
                _merge_branch_graph(previous_branch_graph or base_graph, branch_graph),
                main_graph,
            ),
            "transition_contract": BranchMergeTransitionContract.current(),
            "review_contract": BranchMergeReviewContract(
                human_changes=list(human_review_changes or ())
            ),
            "previous_merge_receipt": previous_merge_receipt,
            "previous_branch_graph": previous_branch_graph,
        }
        encoded = _jsonable(payload)
        return cls.model_validate({**payload, "context_id": _context_id(encoded)})


class BranchMergeCandidateProblem(ValueError):
    def __init__(self, message: str, *, conflicts: list[TransitionConflictDetail] | None = None):
        self.message = " ".join(message.split())
        self.conflicts = list(conflicts or ())
        super().__init__(self.message)


class BranchMergeSemanticConflict(BranchMergeCandidateProblem):
    """A correctable candidate that cannot be prepared against current main."""


class BranchMergeSourceChanged(ValueError):
    """The supposedly quiescent source branch changed during its merge task."""


class _BranchMergeHistory(Protocol):
    def validate_candidate(
        self, patch: Patch, **kwargs: object
    ) -> tuple[Patch, Any, GraphState]: ...

    def append(self, patch: Patch, **kwargs: object) -> tuple[Patch, Any]: ...


@dataclass
class BranchMergeRunOutcome:
    status: Literal[
        "pending",
        "noop",
        "committed",
        "rejected",
        "paused",
        "retryable",
    ] = "pending"
    merge_id: str | None = None
    native_session_id: str | None = None
    source_branch_head: GraphHeadRef | None = None
    rebased_main_head: GraphHeadRef | None = None
    result_main_head: GraphHeadRef | None = None
    prepared: PreparedTransition | None = None
    committed: CommittedTransition | None = None
    receipt: BranchMergeReceipt | None = None
    correction_rounds: int = 0
    rebase_rounds: int = 0
    diagnostic: str | None = None


@dataclass(frozen=True)
class BranchMergeStage:
    local_stage: Path | None
    remote_stage: RemoteRunStage | None
    workspace: Path

    def __post_init__(self) -> None:
        if (self.local_stage is None) == (self.remote_stage is None):
            raise ValueError("exactly one local or remote branch merge stage is required")
        if self.remote_stage is not None:
            if self.remote_stage.root is None:
                raise ValueError("remote branch merge stage is not open")
            if str(self.workspace) != str(self.remote_stage.workspace):
                raise ValueError("branch merge workspace does not match its remote stage")
            return
        assert self.local_stage is not None
        if self.workspace.resolve() != (self.local_stage / "workspace").resolve():
            raise ValueError("branch merge workspace must be the exact local stage workspace")


def build_semantic_delta(
    base: GraphState,
    branch: GraphState,
    *,
    base_head: GraphHeadRef,
    branch_head: GraphHeadRef,
) -> GraphSemanticDelta:
    """Build the typed net semantic change, excluding RCP revision bookkeeping."""

    if base.revision != base_head.revision or branch.revision != branch_head.revision:
        raise ValueError("semantic delta snapshots do not match their exact heads")
    if base_head.target.kind != "main" or branch_head.target.kind != "branch":
        raise ValueError("semantic delta requires a main base and branch head")
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
    return GraphSemanticDelta(
        base_head=base_head,
        branch_head=branch_head,
        nodes=_typed_collection_delta(
            base.nodes,
            branch.nodes,
            model=NodeSemanticDelta,
            id_field="node_id",
            bookkeeping=_SEMANTIC_NODE_BOOKKEEPING,
        ),
        edges=_typed_collection_delta(
            base.edges,
            branch.edges,
            model=EdgeSemanticDelta,
            id_field="edge_id",
            bookkeeping=_SEMANTIC_EDGE_BOOKKEEPING,
        ),
        proposals=_typed_collection_delta(
            base.proposals,
            branch.proposals,
            model=ProposalSemanticDelta,
            id_field="proposal_id",
            bookkeeping=_SEMANTIC_PROPOSAL_BOOKKEEPING,
        ),
        ambiguities=_typed_collection_delta(
            base.ambiguities,
            branch.ambiguities,
            model=AmbiguitySemanticDelta,
            id_field="ambiguity_id",
            bookkeeping=_SEMANTIC_AMBIGUITY_BOOKKEEPING,
        ),
        glossary=_typed_collection_delta(
            base.glossary,
            branch.glossary,
            model=GlossarySemanticDelta,
            id_field="term",
            bookkeeping=_SEMANTIC_GLOSSARY_BOOKKEEPING,
        ),
        globals=globals_,
    )


def branch_human_review_changes(
    patches: list[Patch], base: GraphState, branch: GraphState
) -> list[BranchHumanReviewChange]:
    """Retain exact human initiating writes, never generated actions or summary claims."""

    latest: dict[tuple[str, str], BranchHumanReviewChange] = {}
    for patch in patches:
        if patch.admission != "accepted":
            continue
        if patch.author != "human":
            # The final writer owns the fact: any later non-human write to the same
            # field, initiating or derived, retires the human attribution even when
            # it restores the human's value.
            for operation in patch.ops:
                if isinstance(operation, SetStandingOperation):
                    latest.pop((operation.node_id, "standing"), None)
                elif isinstance(operation, UpdateNodesOperation):
                    for update in operation.nodes:
                        if "status" in update.changes:
                            latest.pop((update.id, "status"), None)
            continue
        indexes = (
            sorted(
                index
                for group in patch.transition.initiating_groups
                for index in group.operation_indexes
            )
            if patch.transition is not None
            else range(len(patch.ops))
        )
        for index in indexes:
            operation = patch.ops[index]
            review_ops: list[ProposalStandingChangeOperation | ProposalStatusChangeOperation] = []
            if isinstance(operation, SetStandingOperation):
                review_ops.append(
                    ProposalStandingChangeOperation(
                        op="set_standing",
                        intent="standing_change",
                        node_id=operation.node_id,
                        standing=operation.standing,
                    )
                )
            elif isinstance(operation, UpdateNodesOperation):
                for update in operation.nodes:
                    if "status" in update.changes and isinstance(
                        branch.nodes.get(update.id), Hypothesis
                    ):
                        review_ops.append(
                            ProposalStatusChangeOperation(
                                op="update_nodes",
                                intent="status_change",
                                nodes=[
                                    {
                                        "id": update.id,
                                        "changes": {"status": update.changes["status"]},
                                        "cause": {"kind": "human_edit"},
                                    }
                                ],
                            )
                        )
            for review_op in review_ops:
                node_id, field, _value = _human_review_value(review_op)
                latest[(node_id, field)] = BranchHumanReviewChange(
                    revision=patch.revision, operation_index=index, operation=review_op
                )
    result = []
    for key in sorted(latest):
        change = latest[key]
        try:
            _require_source_human_change(base, branch, change.operation)
        except ValueError:
            continue  # A later branch edit superseded this human value.
        result.append(change)
    return result


def _human_review_value(
    operation: ProposalStandingChangeOperation | ProposalStatusChangeOperation,
) -> tuple[str, str, object]:
    if isinstance(operation, ProposalStandingChangeOperation):
        return operation.node_id, "standing", operation.standing
    if len(operation.nodes) != 1 or set(operation.nodes[0].changes) != {"status"}:
        raise ValueError("a human review fact must name one exact status change")
    update = operation.nodes[0]
    if not isinstance(update.cause, HumanEditCause):
        raise ValueError("a human source status fact must carry its human_edit cause")
    return update.id, "status", update.changes["status"]


def _require_source_human_change(
    base: GraphState,
    branch: GraphState,
    operation: ProposalStandingChangeOperation | ProposalStatusChangeOperation,
) -> None:
    node_id, field, value = _human_review_value(operation)
    before, after = base.nodes.get(node_id), branch.nodes.get(node_id)
    if after is None or getattr(after, field, _MISSING) != value:
        raise ValueError("human review fact does not match the final source branch value")
    if before is not None and getattr(before, field, _MISSING) == value:
        raise ValueError("human review fact is not a change from the branch base")


def _merge_branch_graph(base: GraphState, branch: GraphState) -> GraphState:
    """Branch review decisions stay local; their actual graph effects merge separately."""

    proposals = dict(branch.proposals)
    for identity, proposal in branch.proposals.items():
        if proposal.status == "pending" and not proposal_is_stale(branch, proposal):
            continue
        if identity in base.proposals:
            proposals[identity] = base.proposals[identity]
        else:
            proposals.pop(identity)
    return branch.model_copy(update={"proposals": proposals})


def branch_merge_review_proposal_id(
    branch_id: str, proposal: Proposal, *, source_base_head: GraphHeadRef
) -> str:
    """Stable for one source baseline; a later delivered cycle gets a fresh review."""

    occurrence = _canonical_sha256(source_base_head.model_dump(mode="json"))[:32]
    return f"{_branch_review_semantic_id(branch_id, proposal)}-{occurrence}"


def _branch_review_semantic_id(branch_id: str, proposal: Proposal) -> str:
    return (
        "prop/branch-review-"
        + _canonical_sha256(
            {
                "branch_id": branch_id,
                "ops": [
                    operation.model_dump(mode="json", exclude_none=True)
                    for operation in _ordered_review_ops(proposal)
                ],
            }
        )[:32]
    )


def _ordered_review_ops(proposal: Proposal) -> list[ProposalOperation]:
    order = {"content_change": 0, "status_change": 1, "standing_change": 2}
    if all(operation.intent in order for operation in proposal.ops):
        return sorted(proposal.ops, key=lambda operation: order[operation.intent])
    return list(proposal.ops)


def detect_branch_merge_conflicts(
    base: GraphState,
    branch: GraphState,
    main: GraphState,
) -> list[BranchMergeConflict]:
    """Return deterministic field conflicts without choosing either side."""

    conflicts: list[BranchMergeConflict] = []
    for collection, base_values, branch_values, main_values, bookkeeping in (
        ("nodes", base.nodes, branch.nodes, main.nodes, _SEMANTIC_NODE_BOOKKEEPING),
        ("edges", base.edges, branch.edges, main.edges, _SEMANTIC_EDGE_BOOKKEEPING),
        (
            "proposals",
            base.proposals,
            branch.proposals,
            main.proposals,
            _SEMANTIC_PROPOSAL_BOOKKEEPING,
        ),
        (
            "ambiguities",
            base.ambiguities,
            branch.ambiguities,
            main.ambiguities,
            _SEMANTIC_AMBIGUITY_BOOKKEEPING,
        ),
        (
            "glossary",
            base.glossary,
            branch.glossary,
            main.glossary,
            _SEMANTIC_GLOSSARY_BOOKKEEPING,
        ),
    ):
        conflicts.extend(
            _collection_conflicts(
                collection,
                base_values,
                branch_values,
                main_values,
                bookkeeping,
            )
        )
    for field_name, base_value, branch_value, main_value in (
        (
            "project_truth_scope",
            base.project_truth_scope,
            branch.project_truth_scope,
            main.project_truth_scope,
        ),
        ("ontology", base.ontology, branch.ontology, main.ontology),
    ):
        base_json = _semantic_document(base_value, frozenset())
        branch_json = _semantic_document(branch_value, frozenset())
        main_json = _semantic_document(main_value, frozenset())
        for path, base_field, branch_field, main_field in _conflicting_fields(
            base_json,
            branch_json,
            main_json,
            prefix=field_name,
        ):
            conflicts.append(
                BranchMergeConflict(
                    code="both_changed",
                    collection=field_name,
                    field_path=path,
                    base=base_field,
                    branch=branch_field,
                    main=main_field,
                    message=(f"Branch and main changed {path} differently from the base."),
                )
            )
    return conflicts


def semantic_delta_is_subsumed(delta: GraphSemanticDelta, main: GraphState) -> bool:
    """Return whether main already contains every semantic change made by the branch.

    Main may also contain compatible changes to fields the branch did not touch. Revision and
    operation-id bookkeeping is excluded by the same rules used to build the semantic delta.
    """

    collection_checks = (
        (delta.nodes, main.nodes, "node_id", _SEMANTIC_NODE_BOOKKEEPING),
        (delta.edges, main.edges, "edge_id", _SEMANTIC_EDGE_BOOKKEEPING),
        (
            delta.proposals,
            main.proposals,
            "proposal_id",
            _SEMANTIC_PROPOSAL_BOOKKEEPING,
        ),
        (
            delta.ambiguities,
            main.ambiguities,
            "ambiguity_id",
            _SEMANTIC_AMBIGUITY_BOOKKEEPING,
        ),
        (delta.glossary, main.glossary, "term", _SEMANTIC_GLOSSARY_BOOKKEEPING),
    )
    for changes, values, identity_field, bookkeeping in collection_checks:
        for change in changes:
            identity = getattr(change, identity_field)
            if not _semantic_change_is_subsumed(
                change.change,
                change.before,
                change.after,
                values.get(identity),
                bookkeeping,
            ):
                return False

    main_globals: dict[str, object] = {
        "project_truth_scope": main.project_truth_scope,
        "ontology": main.ontology,
    }
    return all(
        _branch_changes_are_present(
            change.before,
            change.after,
            _semantic_document(main_globals[change.field], frozenset()),
        )
        for change in delta.globals
    )


SemanticWritePath = tuple[str, ...]

# Why one path still needs judgment, and what discharging it looks like.  The
# merge contract renders this exact mapping, so the agent is never asked to
# rediscover the policy that produced its own task.
MERGE_RESIDUE_REASONS: dict[str, str] = {
    "conflict": (
        "Branch and main changed this path differently. Resolve it from the supplied graph "
        "semantics; never resolve it by preferring one side wholesale."
    ),
    "node_conflict": (
        "Another path on this node conflicts, so the whole node moves as one coherent decision."
    ),
    "protected_node": (
        "The node is a ResearchQuestion or Hypothesis. Carry the change as one pending main "
        "Proposal instead of writing it."
    ),
    "decision_outcome": (
        "The Decision's status or selected_option changes. Write one coherent node update, and "
        "declare agent_action only when the operation actually chooses the Decision."
    ),
    "main_standing_changed": (
        "A human moved this node's standing on main after the branch forked. Carrying the branch "
        "edit would discard that judgment, so decide it explicitly."
    ),
    "system_field": (
        "RCP derives this field. Satisfy the path through the effect of an allowed operation; a "
        "direct write is rejected."
    ),
    "node_absent": (
        "The node is missing on one side. A branch removal of a node main still accepts is one "
        "pending removal Proposal."
    ),
    "unbuildable_create": (
        "The new node cannot be expressed in the agent node schema. Create what the schema allows "
        "and name what could not be carried in the final response."
    ),
    "collection": (
        "The branch changed a Proposal, ambiguity, or glossary entry. Carry it under the rules for "
        "that collection."
    ),
    "edge_change": (
        "The branch changed this existing edge in place. There is no edge update operation; carry "
        "it by removing and recreating the edge."
    ),
    "edge_endpoint_uncertain": (
        "An endpoint node is still in this list, so the edge belongs to that same decision."
    ),
    "protected_restructure": (
        "The edge restructures a protected epistemic relation. Carry it as one pending main "
        "Proposal."
    ),
}


def _node_path_residue_reason(
    path: SemanticWritePath,
    *,
    node: ProjectNode | None,
    current: ProjectNode | None,
    baseline: ProjectNode | None,
    field: str,
    allowed: set[SemanticWritePath],
    conflicts: set[SemanticWritePath],
) -> str | None:
    """Name the judgment one node path needs, or None when it can be built."""

    if node is None or current is None:
        return "node_absent"
    if node.type in {"research_question", "hypothesis"}:
        return "protected_node"
    # An agent edit resets standing, which is ordinary.  What is not ordinary is
    # a human moving standing on main after the fork: carrying the branch edit
    # would drop that judgment silently, so it stays a decision.
    if baseline is None or current.standing != baseline.standing:
        return "main_standing_changed"
    if field in IMMUTABLE_NODE_UPDATE_FIELDS:
        return "system_field"
    # Decision outcomes and conflicts may require one coherent node update.
    if node.type == "decision" and any(
        item[:2] == path[:2] and item[2] in {"status", "selected_option"} for item in allowed
    ):
        return "decision_outcome"
    if any(item[:2] == path[:2] for item in conflicts):
        return "node_conflict"
    return None


def build_deterministic_merge_ops(
    context: BranchMergeContext,
) -> tuple[list[dict[str, Any]], dict[SemanticWritePath, str]]:
    """Build ordinary source changes; leave review and conflict choices explicit.

    The residue maps every remaining path to its reason in `MERGE_RESIDUE_REASONS`.
    """

    base = context.previous_branch_graph or context.base_graph
    branch = _merge_branch_graph(base, context.branch_graph)
    allowed = _graph_semantic_write_paths(base, branch)
    conflicts = _branch_conflict_paths(context.deterministic_conflicts)
    mandatory = {
        path for path in allowed if not any(_semantic_path_covers(item, path) for item in conflicts)
    }
    source = _graph_semantic_document(branch)
    main = _graph_semantic_document(context.main_graph)
    residue: dict[SemanticWritePath, str] = {path: "conflict" for path in conflicts}
    creates: list[dict[str, Any]] = []
    updates: dict[str, dict[str, Any]] = {}
    edge_creates: list[dict[str, Any]] = []
    edge_removes: list[str] = []
    node_adapter = TypeAdapter(AgentProjectNode)
    for path in sorted(mandatory):
        value = _semantic_path_value(source, path)
        if value == _semantic_path_value(main, path):
            continue
        if path[0] != "nodes":
            residue[path] = "edge_change" if path[0] == "edges" else "collection"
            continue
        _, identity, field, *_ = path
        node = branch.nodes.get(identity)
        current = context.main_graph.nodes.get(identity)
        if field == "$" and node is not None and current is None and node.standing == "asserted":
            raw = node.model_dump(
                mode="json",
                exclude=_SEMANTIC_NODE_BOOKKEEPING | {"standing"},
                exclude_defaults=True,
            )
            try:
                node_adapter.validate_python(raw)
            except ValidationError:
                residue[path] = "unbuildable_create"
            else:
                creates.append(raw)
            continue
        reason = _node_path_residue_reason(
            path,
            node=node,
            current=current,
            baseline=base.nodes.get(identity),
            field=field,
            allowed=allowed,
            conflicts=conflicts,
        )
        if reason is not None:
            residue[path] = reason
            continue
        changes = updates.setdefault(identity, {})
        parts = path[3:-1] if path[-1] == "$" else path[3:]
        if not parts:
            changes[field] = deepcopy(value)
        else:
            target = changes.setdefault(field, deepcopy(_semantic_path_value(main, path[:3])))
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            if value is _MISSING:
                target.pop(parts[-1], None)
            else:
                target[parts[-1]] = deepcopy(value)

    # An agent update resets standing to asserted.  Where the branch did that
    # too, the built update already carries it; where branch and main still
    # agree on a stronger standing, the update must not quietly drop it.
    standing_restores: dict[str, str] = {}
    for identity in updates:
        node = branch.nodes.get(identity)
        current = context.main_graph.nodes.get(identity)
        if node is None or current is None:
            continue
        if node.standing == "asserted":
            if current.standing != "asserted":
                residue.pop(("nodes", identity, "standing"), None)
        elif node.standing == current.standing:
            standing_restores[identity] = str(node.standing)

    # Edges touching a node that still needs judgment belong to that same task.
    uncertain_nodes = {path[1] for path in residue if path[0] == "nodes"}
    for path in sorted(residue):
        if len(path) != 3 or path[0] != "edges" or path[-1] != "$" or path not in mandatory:
            continue
        edge = branch.edges.get(path[1]) or context.main_graph.edges.get(path[1])
        if edge is None:
            continue
        if {edge.source, edge.target} & uncertain_nodes:
            residue[path] = "edge_endpoint_uncertain"
            continue
        edge_op = (
            {
                "op": "create_edges",
                "edges": [edge.model_dump(mode="json", exclude={"created_rev", "layer"})],
            }
            if path[1] in branch.edges
            else {"op": "remove_edges", "edge_ids": [edge.id]}
        )
        probe = Patch(
            kind="work",
            author="agent",
            summary="Classify merge edge authority.",
            ops=([{"op": "create_nodes", "nodes": creates}] if creates else []) + [edge_op],
        )
        if RESTRUCTURE_PROTECTED_EPISTEMIC in operation_actions(
            context.main_graph, probe, probe.ops[-1]
        ):
            residue[path] = "protected_restructure"
            continue
        if path[1] in branch.edges:
            edge_creates.append(edge.model_dump(mode="json", exclude={"created_rev", "layer"}))
        else:
            edge_removes.append(edge.id)
        del residue[path]

    ops: list[dict[str, Any]] = []
    if creates:
        ops.append({"op": "create_nodes", "nodes": creates})
    if edge_removes:
        ops.append({"op": "remove_edges", "edge_ids": edge_removes})
    if updates:
        ops.append(
            {
                "op": "update_nodes",
                "nodes": [{"id": key, "changes": value} for key, value in updates.items()],
            }
        )
    if edge_creates:
        ops.append({"op": "create_edges", "edges": edge_creates})
    for identity, standing in sorted(standing_restores.items()):
        ops.append({"op": "set_standing", "node_id": identity, "standing": standing})
    return ops, residue


def render_merge_residue(residue: dict[SemanticWritePath, str]) -> str:
    """Render the remaining paths and the meaning of every reason they carry."""

    items = [
        {"path": _render_semantic_path(path), "reason": residue[path]} for path in sorted(residue)
    ]
    legend = "\n".join(
        f"- `{reason}` — {MERGE_RESIDUE_REASONS[reason]}"
        for reason in sorted(set(residue.values()))
    )
    return f"""```json
{json.dumps(items, indent=2)}
```

What each reason means:
{legend}"""


def _require_possible_merge_plan(
    context: BranchMergeContext,
    ops: list[dict[str, Any]],
    residue: dict[SemanticWritePath, str],
) -> None:
    """Do not ask a provider to repair fixed operations or mandatory source scope."""

    unsupported = sorted(path for path in residue if path[0] in {"ontology", "project_truth_scope"})
    if unsupported:
        raise BranchMergeCandidateProblem(
            "Branch merge cannot carry project configuration changes: "
            + ", ".join(_render_semantic_path(path) for path in unsupported[:8])
        )
    candidate = parse_branch_merge_candidate(
        json.dumps({"summary": "Check fixed merge operations.", "ops": []}),
        context,
        deterministic_ops=ops,
    )
    if ops:
        report = validate_patch(context.main_graph, candidate, context.run_truth_scope)
        if report.rejected:
            raise BranchMergeCandidateProblem(_validation_diagnostic(report)[0])
    conflicts = _branch_conflict_paths(context.deterministic_conflicts)
    proposals = [
        context.branch_graph.proposals[path[1]]
        for path in sorted(residue)
        if len(path) == 3
        and path[0] == "proposals"
        and path[-1] == "$"
        and path[1] in context.branch_graph.proposals
        and path[1] not in context.main_graph.proposals
        and not any(_semantic_path_covers(item, path) for item in conflicts)
    ]
    if proposals:
        source_ops = _strip_rcp_bookkeeping(
            [CreateProposalsOperation(op="create_proposals", proposals=proposals)]
        )
        probe = parse_branch_merge_candidate(
            json.dumps({"summary": "Check mandatory source Proposal scope.", "ops": source_ops}),
            context,
            deterministic_ops=ops,
        )
        report = validate_patch(context.main_graph, probe, context.run_truth_scope)
        # Other diagnostics may depend on nodes still in the residue. Source scope
        # cannot: a mandatory source Proposal must retain its exact provenance.
        for message in report.messages:
            if message.code == "source-outside-run-scope":
                raise BranchMergeCandidateProblem(message.message)


def validate_branch_merge_candidate_conformance(
    context: BranchMergeContext,
    current_main: GraphState,
    prepared: Patch,
    graph: GraphState,
) -> None:
    """Prove initiating actions stay inside and carry the branch semantic delta."""

    if prepared.transition is None:
        raise BranchMergeSemanticConflict("Prepared branch merge has no transition trace.")
    merge_base = context.previous_branch_graph or context.base_graph
    merge_branch = _merge_branch_graph(merge_base, context.branch_graph)
    allowed = _graph_semantic_write_paths(merge_base, merge_branch)
    conflicts = _branch_conflict_paths(context.deterministic_conflicts)
    mandatory = {
        path for path in allowed if not any(_semantic_path_covers(item, path) for item in conflicts)
    }
    timeline = current_main
    review_proposals: list[Proposal] = []
    restoring_standing: set[SemanticWritePath] = set()
    initiating_indexes = sorted(
        index
        for group in prepared.transition.initiating_groups
        for index in group.operation_indexes
    )
    for operation_index in initiating_indexes:
        operation = prepared.ops[operation_index]
        operation_allowed = allowed | restoring_standing
        if isinstance(operation, CreateProposalsOperation):
            for proposal in operation.proposals:
                source = merge_branch.proposals.get(proposal.id)
                if source is not None:
                    continue
                if proposal.id != branch_merge_review_proposal_id(
                    context.metadata.branch_id, proposal, source_base_head=context.review_base_head
                ):
                    raise BranchMergeSemanticConflict(
                        "Merge review Proposal lacks its stable identity."
                    )
                if any(
                    _same_proposal_ops(proposal, existing)
                    for existing in _main_review_proposals(context, current_main)
                ):
                    raise BranchMergeSemanticConflict(
                        "This source change is already represented by a main Proposal; omit its duplicate."
                    )
                _require_human_review_provenance(context, proposal)
                if graph.proposals[proposal.id].status != "pending":
                    raise BranchMergeSemanticConflict(
                        "A new merge review Proposal must remain pending."
                    )
                review_proposals.append(proposal)
                operation_allowed.add(("proposals", proposal.id, "$"))
        undeclared = sorted(
            path
            for path in _declared_bookkeeping_sensitive_write_paths(operation)
            if not any(
                _semantic_path_authorizes_declared_write(item, path) for item in operation_allowed
            )
        )
        if undeclared:
            rendered = ", ".join(_render_semantic_path(path) for path in undeclared[:8])
            raise BranchMergeSemanticConflict(
                "Branch merge candidate declares writes outside the source branch delta: "
                + rendered
            )
        one_action = prepared.model_copy(
            update={
                "ops": [operation],
                "transition": None,
            }
        )
        try:
            next_state = apply_valid_patch(timeline, one_action)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise BranchMergeSemanticConflict(
                f"Prepared branch merge action {operation_index} could not be replayed: {exc}"
            ) from exc
        # An ordinary human content edit preserves accepted standing. Agent
        # assertion resets it, so allow only that exact reset plus restoration
        # to the source value through existing orchestrator authority.
        if isinstance(operation, UpdateNodesOperation):
            for update in operation.nodes:
                old = timeline.nodes.get(update.id)
                new = next_state.nodes.get(update.id)
                source = merge_branch.nodes.get(update.id)
                if (
                    old is not None
                    and new is not None
                    and source is not None
                    and old.standing == source.standing != new.standing
                    and new.standing == "asserted"
                ):
                    path = ("nodes", update.id, "standing")
                    restoring_standing.add(path)
                    operation_allowed.add(path)
        if isinstance(operation, SetStandingOperation):
            path = ("nodes", operation.node_id, "standing")
            if path in restoring_standing:
                source = merge_branch.nodes[operation.node_id]
                if operation.standing != source.standing:
                    raise BranchMergeSemanticConflict(
                        "Standing restoration must match the source branch."
                    )
        unexpected = sorted(
            path
            for path in _graph_semantic_write_paths(timeline, next_state)
            if not any(_semantic_path_covers(item, path) for item in operation_allowed)
        )
        if unexpected:
            rendered = ", ".join(_render_semantic_path(path) for path in unexpected[:8])
            raise BranchMergeSemanticConflict(
                "Branch merge candidate writes outside the source branch delta: " + rendered
            )
        timeline = next_state

    mandatory |= restoring_standing
    reviewed_paths = _review_proposal_coverage(
        context,
        graph,
        allowed,
        mandatory,
        review_proposals,
        existing=list(_main_review_proposals(context, current_main)),
    )
    branch_document = _graph_semantic_document(merge_branch)
    result_document = _graph_semantic_document(graph)
    missing = sorted(
        path
        for path in mandatory
        if path not in reviewed_paths
        and _semantic_path_value(branch_document, path)
        != _semantic_path_value(result_document, path)
    )
    if missing:
        rendered = ", ".join(_render_semantic_path(path) for path in missing[:8])
        raise BranchMergeSemanticConflict(
            "Branch merge candidate omits non-conflicting source changes: " + rendered
        )


def branch_merge_can_resolve_without_patch(context: BranchMergeContext) -> bool:
    """Whether choosing current main on every conflict still carries all required changes."""

    merge_base = context.previous_branch_graph or context.base_graph
    branch = _merge_branch_graph(merge_base, context.branch_graph)
    allowed = _graph_semantic_write_paths(merge_base, branch)
    conflicts = _branch_conflict_paths(context.deterministic_conflicts)
    mandatory = {
        path for path in allowed if not any(_semantic_path_covers(item, path) for item in conflicts)
    }
    reviewed_paths = _review_proposal_coverage(
        context,
        context.main_graph,
        allowed,
        mandatory,
        [],
        existing=list(_main_review_proposals(context, context.main_graph)),
    )
    branch_document = _graph_semantic_document(branch)
    main_document = _graph_semantic_document(context.main_graph)
    return all(
        path in reviewed_paths
        or _semantic_path_value(branch_document, path) == _semantic_path_value(main_document, path)
        for path in mandatory
    )


def _branch_merge_is_represented(context: BranchMergeContext) -> bool:
    merge_base = context.previous_branch_graph or context.base_graph
    branch = _merge_branch_graph(merge_base, context.branch_graph)
    allowed = _graph_semantic_write_paths(merge_base, branch)
    return allowed <= _review_proposal_coverage(
        context,
        context.main_graph,
        allowed,
        allowed,
        [],
        existing=list(_main_review_proposals(context, context.main_graph)),
    )


def _same_proposal_ops(left: Proposal, right: Proposal) -> bool:
    return _ordered_review_ops(left) == _ordered_review_ops(right)


def _main_review_proposals(context: BranchMergeContext, main: GraphState):
    for proposal in main.proposals.values():
        if proposal.status != "pending" or proposal_is_stale(main, proposal):
            continue
        source = context.branch_graph.proposals.get(proposal.id)
        semantic_id, _, occurrence = proposal.id.rpartition("-")
        same_source_review = (
            semantic_id == _branch_review_semantic_id(context.metadata.branch_id, proposal)
            and len(occurrence) == 32
            and all(character in "0123456789abcdef" for character in occurrence)
        )
        if same_source_review or (source is not None and _same_proposal_ops(source, proposal)):
            yield proposal


def _require_human_review_provenance(context: BranchMergeContext, proposal: Proposal) -> None:
    for operation in proposal.ops:
        special = isinstance(operation, ProposalStandingChangeOperation) or (
            isinstance(operation, ProposalStatusChangeOperation)
            and any(isinstance(update.cause, HumanEditCause) for update in operation.nodes)
        )
        if special and not any(
            operation == change.operation for change in context.review_contract.human_changes
        ):
            raise BranchMergeSemanticConflict(
                "Merge review Proposal claims a human change absent from the canonical source operations."
            )


def _review_proposal_coverage(
    context: BranchMergeContext,
    graph: GraphState,
    allowed: set[SemanticWritePath],
    mandatory: set[SemanticWritePath],
    proposals: list[Proposal],
    *,
    existing: list[Proposal],
) -> set[SemanticWritePath]:
    """Return only paths represented by pending human review, never a graph to commit.

    The detached approval preview proves semantic effects. It never enters main
    history or replaces the actual merge projection. Resolved Proposals are not
    previewed; validated prior receipts account for previously delivered changes.
    """

    branch_document = _graph_semantic_document(context.branch_graph)
    preview_graph = graph.model_copy(deep=True)
    claimed: set[SemanticWritePath] = set()
    for index, proposal in enumerate([*proposals, *existing]):
        is_new = index < len(proposals)
        if proposal.status != "pending":
            raise BranchMergeSemanticConflict(
                "Only pending Proposals may represent new merge changes."
            )
        if is_new and proposal_is_stale(preview_graph, proposal):
            raise BranchMergeSemanticConflict(
                "Merge review Proposals would make one another stale; combine same-node "
                "content/status/standing changes or choose a compatible review order."
            )
        try:
            operations = graph_operations_from_proposal(proposal.ops)
            preview = Patch(
                kind="approval",
                author="human",
                revision=preview_graph.revision + 1,
                summary="Non-authoritative semantic review proof.",
                ops=operations,
            )
            for operation in operations:
                if any(
                    not any(
                        _semantic_path_authorizes_declared_write(item, path) for item in allowed
                    )
                    for path in _declared_bookkeeping_sensitive_write_paths(operation)
                ):
                    raise BranchMergeSemanticConflict(
                        "Merge Proposal declares writes outside the source branch delta."
                    )
            direct = apply_valid_patch(preview_graph, preview)
            paths = _graph_semantic_write_paths(preview_graph, direct)
            if not paths:
                if is_new:
                    raise BranchMergeSemanticConflict(
                        "Merge Proposal carries no unrepresented source change."
                    )
                continue
            if any(
                not any(_semantic_path_covers(item, path) for item in allowed) for path in paths
            ):
                raise BranchMergeSemanticConflict(
                    "Merge Proposal writes outside the source branch delta."
                )
            direct_document = _graph_semantic_document(direct)
            if any(
                any(_semantic_path_covers(item, path) for item in mandatory)
                and _semantic_path_value(direct_document, path)
                != _semantic_path_value(branch_document, path)
                for path in paths
            ):
                raise BranchMergeSemanticConflict(
                    "Merge Proposal does not carry the exact non-conflicting source change."
                )
            if is_new and any(
                _semantic_path_covers(left, right) or _semantic_path_covers(right, left)
                for left in claimed
                for right in paths
            ):
                raise BranchMergeSemanticConflict(
                    "Merge review Proposals overlap the same source change."
                )
            judgment = _proposal_judgment_patch(
                preview_graph, proposal, decision="approved", reason=None
            ).model_copy(update={"revision": preview_graph.revision + 1})
            projected = GraphTransitionManager().prepare_validated(preview_graph, [judgment])
        except (KeyError, ValueError) as exc:
            if not is_new:
                continue  # The pending review may address an older source value.
            if isinstance(exc, BranchMergeSemanticConflict):
                raise
            raise BranchMergeSemanticConflict(
                f"Merge review Proposal cannot be represented: {exc}"
            ) from exc
        preview_graph = projected.projection.graph
        claimed.update(paths)
    document = _graph_semantic_document(preview_graph)
    return {
        path
        for path in mandatory
        if _semantic_path_value(branch_document, path) == _semantic_path_value(document, path)
    }


def branch_merge_id(metadata: GraphBranchMetadata) -> str:
    """Hash only immutable branch lineage, so a moving main keeps one merge identity."""

    return _canonical_sha256(
        {
            "schema_generation": 1,
            "kind": "auto_research_graph_branch_merge",
            "branch_id": metadata.branch_id,
            "episode_id": metadata.episode_id,
            "project_id": metadata.project_id,
            "branch_kind": metadata.kind,
            "branch_base_head": metadata.base_head.model_dump(mode="json"),
            "branch_head": metadata.head.model_dump(mode="json"),
        }
    )


def branch_merge_provenance(context: BranchMergeContext) -> BranchMergeProvenance:
    return BranchMergeProvenance(
        merge_id=branch_merge_id(context.metadata),
        branch_id=context.metadata.branch_id,
        episode_id=context.metadata.episode_id,
        branch_base_head=context.metadata.base_head,
        branch_head=context.metadata.head,
        rebased_main_head=context.main_head,
        merge_task_id=context.merge_task_id,
    )


def parse_branch_merge_candidate(
    value: str,
    context: BranchMergeContext,
    *,
    deterministic_ops: list[dict[str, Any]] | None = None,
) -> Patch:
    """Combine fixed operations with semantic output and stamp merge provenance."""

    try:
        draft = parse_agent_patch_json(value, profile="orchestrator")
    except ValueError as exc:
        raise BranchMergeCandidateProblem(str(exc)) from exc
    if not isinstance(draft, OrchestratorAgentPatch):  # pragma: no cover - profile is fixed above
        raise BranchMergeCandidateProblem("branch merge output did not use orchestrator schema")
    if draft.repositories_read:
        raise BranchMergeCandidateProblem(
            "A graph-only branch merge must declare repositories_read as an empty list."
        )
    if deterministic_ops:
        draft = OrchestratorAgentPatch.model_validate(
            {**draft.model_dump(mode="python"), "ops": [*deterministic_ops, *draft.ops]}
        )
    patch = prepare_agent_patch(
        draft,
        kind="work",
        run_truth_scope=context.run_truth_scope,
        source_operation_id=context.merge_task_id,
        profile="orchestrator",
    )
    for operation in patch.ops:
        if not isinstance(operation, CreateProposalsOperation):
            continue
        for index, proposal in enumerate(operation.proposals):
            source = context.branch_graph.proposals.get(proposal.id)
            if (
                source is not None
                and source.status == "pending"
                and not proposal_is_stale(context.branch_graph, source)
            ):
                continue
            operation.proposals[index] = proposal.model_copy(
                update={
                    "id": branch_merge_review_proposal_id(
                        context.metadata.branch_id,
                        proposal,
                        source_base_head=context.review_base_head,
                    ),
                    "ops": _ordered_review_ops(proposal),
                }
            )
    return patch.model_copy(
        update={
            "revision": context.main_head.revision + 1,
            "authorized_by": context.authorized_by,
            "profile": "orchestrator",
            "task_id": context.merge_task_id,
            "episode_id": context.metadata.episode_id,
            "branch_merge": branch_merge_provenance(context),
        }
    )


def prepare_branch_merge_with_history(
    history: _BranchMergeHistory,
    candidate: Patch,
    *,
    expected_main_head: GraphHeadRef,
    context: BranchMergeContext,
) -> PreparedTransition:
    """Run HistoryManager's authoritative validation/transition preparation without writing."""

    if expected_main_head.target.kind != "main":
        raise ValueError("branch merge candidates can prepare only against main")
    try:
        prepared, report, current = history.validate_candidate(candidate)
    except PatchRejected as exc:
        raise BranchMergeSemanticConflict(str(exc)) from exc
    if current.revision != expected_main_head.revision:
        raise RevisionConflict(
            f"main moved from revision {expected_main_head.revision} to {current.revision}"
        )
    if getattr(report, "rejected", False):
        diagnostic, conflicts = _validation_diagnostic(report)
        raise BranchMergeSemanticConflict(diagnostic, conflicts=conflicts)
    _require_prepared_provenance(prepared, candidate, expected_main_head)
    assert prepared.transition is not None
    try:
        graph = apply_valid_patch(current, prepared)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise BranchMergeSemanticConflict(
            f"Prepared branch merge operations could not be applied atomically: {exc}"
        ) from exc
    validate_branch_merge_candidate_conformance(context, current, prepared, graph)
    return PreparedTransition(
        patch=prepared,
        projection=project_transition_projection(
            graph,
            prepared.transition,
            canonical=False,
        ),
    )


def commit_branch_merge_with_history(
    history: _BranchMergeHistory,
    candidate: Patch,
    *,
    expected_main_head: GraphHeadRef,
) -> CommittedTransition:
    """Append one prepared semantic merge at an exact main revision or append nothing."""

    if expected_main_head.target.kind != "main":
        raise ValueError("branch merge candidates can commit only to main")
    try:
        committed, result = history.append(
            candidate,
            discard_on_reject=True,
            expected_revision=expected_main_head.revision,
        )
    except PatchRejected as exc:
        raise BranchMergeSemanticConflict(str(exc)) from exc
    _require_prepared_provenance(committed, candidate, expected_main_head)
    assert committed.transition is not None
    state = result.state
    if state.revision != expected_main_head.revision + 1:
        raise RuntimeError("branch merge append did not produce exactly one main revision")
    return CommittedTransition(
        patch=committed,
        projection=project_transition_projection(
            state,
            committed.transition,
            canonical=True,
        ),
    )


def branch_merge_receipt_from_committed_patch(patch: Patch) -> BranchMergeReceipt:
    """Project one accepted idempotency winner into its durable branch receipt."""

    if (
        patch.admission != "accepted"
        or patch.branch_merge is None
        or patch.transition is None
        or patch.authorized_by is None
    ):
        raise ValueError("committed main branch merge lacks exact receipt provenance")
    return BranchMergeReceipt(
        outcome="committed",
        provenance=patch.branch_merge,
        result_main_head=GraphHeadRef(
            revision=patch.revision,
            transition_id=patch.transition.transition_id,
        ),
        authorized_by=patch.authorized_by,
        created_at=patch.created_at,
    )


def require_graph_only_merge_scope(
    scope: ProjectWriteScope,
    *,
    context: BranchMergeContext,
    stage: BranchMergeStage,
) -> None:
    """Fail closed unless the provider can write only its exact scratch workspace."""

    if scope.capability != "orchestrate":
        raise ValueError("branch merge requires orchestrate capability")
    if scope.project_id != context.metadata.project_id:
        raise ValueError("branch merge write scope belongs to a different project")
    if scope.repositories or scope.repository_roots:
        raise ValueError("branch merge agents receive no repository write roots")
    if scope.workspace_root != str(stage.workspace):
        raise ValueError("branch merge write scope does not name its exact workspace")
    if scope.writable_roots != [str(stage.workspace)]:
        raise ValueError("branch merge writable roots must contain only its scratch workspace")
    if stage.remote_stage is not None:
        assert stage.remote_stage.root is not None
        if scope.stage_root != str(stage.remote_stage.root):
            raise ValueError("branch merge scope does not match its remote stage")
        if scope.execution_host != stage.remote_stage.host:
            raise ValueError("branch merge scope does not match its execution host")
    else:
        assert stage.local_stage is not None
        if Path(scope.stage_root).resolve() != stage.local_stage.resolve():
            raise ValueError("branch merge scope does not match its local stage")
    if not any(PurePosixPath(path).name == ".research" for path in scope.protected_write_paths):
        raise ValueError("branch merge scope must explicitly protect canonical .research")


def classify_refreshed_context(
    previous: BranchMergeContext,
    current: BranchMergeContext,
) -> Literal["unchanged", "main_moved"]:
    """Permit only current-main movement; never retarget a task to another branch head."""

    if previous.run_truth_scope != current.run_truth_scope:
        raise BranchMergeSourceChanged(
            "Project truth membership changed while the merge task was running. "
            "Dispatch a new merge against current membership."
        )
    stable = (
        previous.merge_task_id == current.merge_task_id
        and previous.authorized_by == current.authorized_by
        and previous.metadata == current.metadata
        and previous.eligibility == current.eligibility
        and previous.base_graph == current.base_graph
        and previous.branch_graph == current.branch_graph
        and previous.branch_patch_summaries == current.branch_patch_summaries
        and previous.transition_contract == current.transition_contract
        and previous.review_contract == current.review_contract
        and previous.previous_merge_receipt == current.previous_merge_receipt
        and previous.previous_branch_graph == current.previous_branch_graph
    )
    if not stable:
        raise BranchMergeSourceChanged(
            "The source branch or its immutable base changed while the merge task was running."
        )
    if previous.main_head == current.main_head:
        if previous.context_id != current.context_id:
            raise BranchMergeSourceChanged(
                "Merge context changed without a corresponding main or branch head change."
            )
        return "unchanged"
    return "main_moved"


async def stream_branch_merge_run(
    request: RunRequest,
    launcher: AgentLauncher,
    *,
    load_context: Callable[[], BranchMergeContext],
    main_history: _BranchMergeHistory,
    stage: BranchMergeStage,
    write_scope: ProjectWriteScope,
    validator_command: str,
    outcome: BranchMergeRunOutcome,
    execution: Any | None = None,
    binary: str | None = None,
    max_main_rebases: int = MAX_BRANCH_MERGE_REBASE_ROUNDS,
) -> AsyncIterator[str]:
    """Run, correct, rebase, and atomically commit one graph-only branch merge.

    ``load_context`` must resolve the immutable base/branch snapshots and the
    live main head together. ``main_history`` must be the main-target history
    whose ``validate_candidate`` and ``append`` methods use the canonical
    transition manager and append lock.
    """

    if request.provider is None or request.run_on is None:
        raise ValueError("branch merge request must have a pinned provider and execution machine")
    if not validator_command.strip():
        raise ValueError("branch merge requires a staged live validator command")
    if max_main_rebases < 1:
        raise ValueError("branch merge main rebase bound must be positive")

    context = load_context()
    require_graph_only_merge_scope(write_scope, context=context, stage=stage)
    outcome.merge_id = branch_merge_id(context.metadata)
    outcome.source_branch_head = context.metadata.head
    outcome.rebased_main_head = context.main_head

    if _branch_merge_is_represented(context):
        provenance = branch_merge_provenance(context)
        outcome.receipt = BranchMergeReceipt(
            outcome="no_change",
            provenance=provenance,
            result_main_head=context.main_head,
            authorized_by=context.authorized_by,
        )
        outcome.status = "noop"
        yield _sse(
            AgentEvent(
                event="answer",
                text=(
                    "The branch head has no net semantic graph change to merge."
                    if context.semantic_delta.is_empty
                    else "Main already contains or has received this branch head's graph changes for review."
                ),
            )
        )
        yield _sse(AgentEvent(event="done"))
        return

    token = _task_token(execution)
    original_contract_path: str | None = None
    session_id: str | None = None
    candidate_text: str | None = None
    candidate: Patch | None = None
    candidate_problem: BranchMergeCandidateProblem | None = None
    first_turn = True

    while True:
        deterministic_ops, residue = build_deterministic_merge_ops(context)
        if residue:
            try:
                _require_possible_merge_plan(context, deterministic_ops, residue)
            except BranchMergeCandidateProblem as exc:
                outcome.status = "rejected"
                outcome.diagnostic = exc.message
                yield _sse(AgentEvent(event="error", text=exc.message))
                return
        if not residue:
            _clear_patch_candidates(stage)
            if first_turn:
                _stage_merge_context(stage, token, context, round_number=outcome.rebase_rounds)
            candidate_text = json.dumps(
                {"summary": "Carry the ordinary branch changes onto main.", "ops": []}
            )
            candidate = parse_branch_merge_candidate(
                candidate_text, context, deterministic_ops=deterministic_ops
            )
            # Retain the complete generated candidate if validation or append fails.
            saved = json.dumps({"summary": candidate.summary, "ops": deterministic_ops}, indent=2)
            if stage.remote_stage is not None:
                stage.remote_stage.write_workspace_text("patch.json", saved)
            else:
                (stage.workspace / "patch.json").write_text(saved, encoding="utf-8")
            first_turn = False
        if first_turn:
            _clear_patch_candidates(stage)
            context_path = _stage_merge_context(
                stage, token, context, round_number=outcome.rebase_rounds
            )
            patch_path = _patch_path(stage)
            contract = branch_merge_task_contract(
                context_path=context_path,
                context_id=context.context_id,
                patch_path=patch_path,
                validator_command=validator_command,
                review_contract_json=context.review_contract.model_dump_json(indent=2),
                plan_path=_stage_merge_plan(
                    stage, token, deterministic_ops, round_number=outcome.rebase_rounds
                ),
                residue_block=render_merge_residue(residue),
                ontology_extensions=_has_ontology_extensions(context.main_graph),
            )
            original_contract_path, prompt = _stage_task_contract(
                stage.local_stage,
                stage.remote_stage,
                f"task-{token}-branch-merge.md",
                contract,
                execution=execution,
                role="branch_merge",
            )
            _record_merge_launch(
                execution,
                request,
                prompt=prompt,
                contract_path=original_contract_path,
                write_scope=write_scope,
                context=context,
                continuation="initial",
                round_number=0,
            )
            provider, events = _provider_turn(
                launcher,
                request,
                prompt,
                stage=stage,
                write_scope=write_scope,
                execution=execution,
                binary=binary,
                session_id=None,
                required_session_id=None,
            )
            async with aclosing(events) as frames:
                async for frame in frames:
                    yield frame
            session_id = provider.session_id
            outcome.native_session_id = session_id
            if provider.paused:
                outcome.status = "paused"
                return
            if provider.failed or not provider.completed:
                outcome.status = "rejected"
                outcome.diagnostic = "The branch merge provider did not complete its first turn."
                return
            try:
                candidate_text = _read_candidate_text(stage)
                candidate = parse_branch_merge_candidate(
                    candidate_text, context, deterministic_ops=deterministic_ops
                )
                candidate_problem = None
            except (
                AgentOutputProblem,
                BranchMergeCandidateProblem,
                OSError,
                StateUnavailable,
            ) as exc:
                candidate_problem = BranchMergeCandidateProblem(str(exc))
                candidate = None
            first_turn = False

        fresh = load_context()
        try:
            refresh = classify_refreshed_context(context, fresh)
        except BranchMergeSourceChanged as exc:
            outcome.status = "rejected"
            outcome.diagnostic = str(exc)
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return
        if refresh == "main_moved":
            if semantic_delta_is_subsumed(fresh.semantic_delta, fresh.main_graph):
                context = fresh
                outcome.rebased_main_head = context.main_head
                outcome.receipt = BranchMergeReceipt(
                    outcome="no_change",
                    provenance=branch_merge_provenance(context),
                    result_main_head=context.main_head,
                    authorized_by=context.authorized_by,
                )
                outcome.status = "noop"
                yield _sse(
                    AgentEvent(
                        event="answer",
                        text="Main already contains this branch head's semantic graph change.",
                    )
                )
                yield _sse(AgentEvent(event="done"))
                return
            if (
                candidate is not None
                and not candidate.ops
                and branch_merge_can_resolve_without_patch(fresh)
            ):
                context = fresh
                outcome.rebased_main_head = context.main_head
                outcome.receipt = BranchMergeReceipt(
                    outcome="no_change",
                    provenance=branch_merge_provenance(context),
                    result_main_head=context.main_head,
                    authorized_by=context.authorized_by,
                )
                outcome.status = "noop"
                yield _sse(
                    AgentEvent(
                        event="answer",
                        text="The merge resolved conflicting branch changes to current main.",
                    )
                )
                yield _sse(AgentEvent(event="done"))
                return
            if outcome.rebase_rounds >= max_main_rebases or (session_id is None and residue):
                outcome.status = "retryable"
                outcome.diagnostic = (
                    "Main advanced while the merge was running; retry the merge task against "
                    "the new main head."
                )
                yield _sse(AgentEvent(event="error", text=outcome.diagnostic))
                return
            previous_context = context
            context = fresh
            outcome.rebase_rounds += 1
            outcome.rebased_main_head = context.main_head
            deterministic_ops, residue = build_deterministic_merge_ops(context)
            if session_id is None or not residue:
                first_turn = True
                continue
            try:
                _require_possible_merge_plan(context, deterministic_ops, residue)
            except BranchMergeCandidateProblem as exc:
                outcome.status = "rejected"
                outcome.diagnostic = exc.message
                yield _sse(AgentEvent(event="error", text=exc.message))
                return
            context_path = _stage_merge_context(
                stage,
                token,
                context,
                round_number=outcome.rebase_rounds,
            )
            _clear_patch_candidates(stage)
            assert original_contract_path is not None
            contract = branch_merge_rebase_contract(
                original_contract_path=original_contract_path,
                previous_context_id=previous_context.context_id,
                context_path=context_path,
                context_id=context.context_id,
                patch_path=_patch_path(stage),
                validator_command=validator_command,
                plan_path=_stage_merge_plan(
                    stage, token, deterministic_ops, round_number=outcome.rebase_rounds
                ),
                residue_block=render_merge_residue(residue),
                ontology_extensions=_has_ontology_extensions(context.main_graph),
            )
            contract_path, prompt = _stage_task_contract(
                stage.local_stage,
                stage.remote_stage,
                f"task-{token}-branch-merge-rebase-{outcome.rebase_rounds}.md",
                contract,
                execution=execution,
                role=f"branch_merge_rebase_{outcome.rebase_rounds}",
            )
            _record_merge_launch(
                execution,
                request,
                prompt=prompt,
                contract_path=contract_path,
                write_scope=write_scope,
                context=context,
                continuation="main_rebase",
                round_number=outcome.rebase_rounds,
            )
            provider, events = _provider_turn(
                launcher,
                request,
                prompt,
                stage=stage,
                write_scope=write_scope,
                execution=execution,
                binary=binary,
                session_id=session_id,
                required_session_id=session_id,
            )
            async with aclosing(events) as frames:
                async for frame in frames:
                    yield frame
            if provider.session_id:
                session_id = provider.session_id
                outcome.native_session_id = session_id
            if provider.paused:
                outcome.status = "paused"
                return
            if provider.failed or not provider.completed:
                outcome.status = "retryable"
                outcome.diagnostic = "The branch merge rebase turn did not complete."
                return
            try:
                candidate_text = _read_candidate_text(stage)
                candidate = parse_branch_merge_candidate(
                    candidate_text, context, deterministic_ops=deterministic_ops
                )
                candidate_problem = None
            except (
                AgentOutputProblem,
                BranchMergeCandidateProblem,
                OSError,
                StateUnavailable,
            ) as exc:
                candidate_problem = BranchMergeCandidateProblem(str(exc))
                candidate = None
            continue

        context = fresh
        if candidate is not None and not candidate.ops:
            if branch_merge_can_resolve_without_patch(context):
                outcome.receipt = BranchMergeReceipt(
                    outcome="no_change",
                    provenance=branch_merge_provenance(context),
                    result_main_head=context.main_head,
                    authorized_by=context.authorized_by,
                )
                outcome.status = "noop"
                yield _sse(
                    AgentEvent(
                        event="answer",
                        text="The merge resolved conflicting branch changes to current main.",
                    )
                )
                yield _sse(AgentEvent(event="done"))
                return
            candidate_problem = BranchMergeCandidateProblem(
                "An empty merge candidate omits non-conflicting source branch changes."
            )
            candidate = None
        if candidate is not None:
            try:
                outcome.prepared = prepare_branch_merge_with_history(
                    main_history,
                    candidate,
                    expected_main_head=context.main_head,
                    context=context,
                )
            except RevisionConflict:
                if classify_refreshed_context(context, load_context()) == "unchanged":
                    outcome.status = "retryable"
                    outcome.diagnostic = (
                        "Main admission reported movement before the refreshed main head became "
                        "available; retry the merge task."
                    )
                    yield _sse(AgentEvent(event="error", text=outcome.diagnostic))
                    return
                continue
            except BranchMergeSemanticConflict as exc:
                candidate_problem = exc
            else:
                try:
                    outcome.committed = commit_branch_merge_with_history(
                        main_history,
                        candidate,
                        expected_main_head=context.main_head,
                    )
                except BranchMergeAlreadyCommitted as exc:
                    receipt = branch_merge_receipt_from_committed_patch(exc.patch)
                    outcome.prepared = None
                    outcome.status = "committed"
                    outcome.result_main_head = receipt.result_main_head
                    outcome.rebased_main_head = receipt.provenance.rebased_main_head
                    outcome.receipt = receipt
                    yield _sse(
                        AgentEvent(
                            event="answer",
                            text=(
                                "This graph branch head was already merged into main revision "
                                f"{receipt.result_main_head.revision}."
                            ),
                        )
                    )
                    yield _sse(AgentEvent(event="done"))
                    return
                except BranchMergeAlreadyResolved as exc:
                    receipt = exc.receipt
                    outcome.prepared = None
                    outcome.status = "noop"
                    outcome.result_main_head = receipt.result_main_head
                    outcome.rebased_main_head = receipt.provenance.rebased_main_head
                    outcome.receipt = receipt
                    yield _sse(
                        AgentEvent(
                            event="answer",
                            text="This graph branch head was already resolved without a main Patch.",
                        )
                    )
                    yield _sse(AgentEvent(event="done"))
                    return
                except RevisionConflict:
                    outcome.prepared = None
                    if classify_refreshed_context(context, load_context()) == "unchanged":
                        outcome.status = "retryable"
                        outcome.diagnostic = (
                            "Main moved during atomic append before the refreshed head became "
                            "available; retry the merge task."
                        )
                        yield _sse(AgentEvent(event="error", text=outcome.diagnostic))
                        return
                    continue
                except BranchMergeSemanticConflict as exc:
                    outcome.prepared = None
                    candidate_problem = exc
                else:
                    committed = outcome.committed
                    assert committed is not None
                    outcome.status = "committed"
                    outcome.result_main_head = committed.projection.head
                    outcome.receipt = branch_merge_receipt_from_committed_patch(committed.patch)
                    yield _sse(
                        AgentEvent(
                            event="answer",
                            text=(
                                "Merged graph branch "
                                f"{context.metadata.branch_id} into main revision "
                                f"{committed.projection.head.revision}."
                            ),
                        )
                    )
                    yield _sse(AgentEvent(event="done"))
                    return

        assert candidate_problem is not None
        if (
            not residue
            or session_id is None
            or outcome.correction_rounds >= PATCH_CORRECTION_MAX_ROUNDS
        ):
            outcome.status = "rejected"
            outcome.diagnostic = candidate_problem.message
            yield _sse(AgentEvent(event="error", text=candidate_problem.message))
            return

        outcome.correction_rounds += 1
        diagnostics_path = _stage_json_task_input(
            stage.local_stage,
            stage.remote_stage,
            f"task-{token}-branch-merge-correction-{outcome.correction_rounds}.json",
            {
                "kind": "branch_merge_patch",
                "context_id": context.context_id,
                "problem": candidate_problem.message,
                "conflicts": [item.model_dump(mode="json") for item in candidate_problem.conflicts],
            },
        )
        assert original_contract_path is not None
        context_path = _stage_merge_context_reference(stage, token, context, outcome.rebase_rounds)
        contract = branch_merge_correction_contract(
            original_contract_path=original_contract_path,
            context_path=context_path,
            context_id=context.context_id,
            patch_path=_patch_path(stage),
            diagnostics_path=diagnostics_path,
            validator_command=validator_command,
        )
        contract_path, prompt = _stage_task_contract(
            stage.local_stage,
            stage.remote_stage,
            f"task-{token}-branch-merge-correction-{outcome.correction_rounds}.md",
            contract,
            execution=execution,
            role=f"branch_merge_correction_{outcome.correction_rounds}",
        )
        pre_launch_digest = _existing_patch_digest(stage.workspace, stage.remote_stage)
        _record_merge_launch(
            execution,
            request,
            prompt=prompt,
            contract_path=contract_path,
            write_scope=write_scope,
            context=context,
            continuation="patch_correction",
            round_number=outcome.correction_rounds,
        )
        provider, events = _provider_turn(
            launcher,
            request,
            prompt,
            stage=stage,
            write_scope=write_scope,
            execution=execution,
            binary=binary,
            session_id=session_id,
            required_session_id=session_id,
        )
        async with aclosing(events) as frames:
            async for frame in frames:
                yield frame
        if provider.session_id:
            session_id = provider.session_id
            outcome.native_session_id = session_id
        if provider.paused:
            outcome.status = "paused"
            return
        if provider.failed or not provider.completed:
            outcome.status = "rejected"
            outcome.diagnostic = "The branch merge correction turn did not complete."
            return
        try:
            candidate_text = _read_candidate_text(stage)
            digest = hashlib.sha256(candidate_text.encode("utf-8")).hexdigest()
            if pre_launch_digest is not None and digest == pre_launch_digest:
                raise BranchMergeCandidateProblem(
                    "The correction left patch.json byte-identical; rewrite the candidate."
                )
            candidate = parse_branch_merge_candidate(
                candidate_text, context, deterministic_ops=deterministic_ops
            )
            candidate_problem = None
        except (AgentOutputProblem, BranchMergeCandidateProblem, OSError, StateUnavailable) as exc:
            candidate_problem = BranchMergeCandidateProblem(str(exc))
            candidate = None


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
    bookkeeping: frozenset[str],
) -> list[Any]:
    result: list[Any] = []
    for identity in sorted(set(before) | set(after)):
        old = before.get(identity)
        new = after.get(identity)
        if old is None:
            result.append(
                model.model_validate({"change": "created", id_field: identity, "after": new})
            )
        elif new is None:
            result.append(
                model.model_validate({"change": "removed", id_field: identity, "before": old})
            )
        elif _semantic_document(old, bookkeeping) != _semantic_document(new, bookkeeping):
            result.append(
                model.model_validate(
                    {"change": "updated", id_field: identity, "before": old, "after": new}
                )
            )
    return result


def _graph_semantic_document(state: GraphState) -> dict[str, JsonValue]:
    return {
        "nodes": {
            identity: _semantic_document(value, _SEMANTIC_NODE_BOOKKEEPING)
            for identity, value in state.nodes.items()
        },
        "edges": {
            identity: _semantic_document(value, _SEMANTIC_EDGE_BOOKKEEPING)
            for identity, value in state.edges.items()
        },
        "proposals": {
            identity: _semantic_document(value, _SEMANTIC_PROPOSAL_BOOKKEEPING)
            for identity, value in state.proposals.items()
        },
        "ambiguities": {
            identity: _semantic_document(value, _SEMANTIC_AMBIGUITY_BOOKKEEPING)
            for identity, value in state.ambiguities.items()
        },
        "glossary": {
            identity: _semantic_document(value, _SEMANTIC_GLOSSARY_BOOKKEEPING)
            for identity, value in state.glossary.items()
        },
        "project_truth_scope": _semantic_document(state.project_truth_scope, frozenset()),
        "ontology": _semantic_document(state.ontology, frozenset()),
    }


def _graph_semantic_write_paths(
    before: GraphState,
    after: GraphState,
) -> set[SemanticWritePath]:
    old = _graph_semantic_document(before)
    new = _graph_semantic_document(after)
    paths: set[SemanticWritePath] = set()
    for collection in ("nodes", "edges", "proposals", "ambiguities", "glossary"):
        old_values = old[collection]
        new_values = new[collection]
        assert isinstance(old_values, dict) and isinstance(new_values, dict)
        for identity in set(old_values) | set(new_values):
            if identity not in old_values or identity not in new_values:
                paths.add((collection, identity, "$"))
                continue
            paths.update(
                _document_write_paths(
                    old_values[identity],
                    new_values[identity],
                    prefix=(collection, identity),
                )
            )
    for field in ("project_truth_scope", "ontology"):
        old_value = old[field]
        new_value = new[field]
        if isinstance(old_value, dict) and isinstance(new_value, dict):
            paths.update(_document_write_paths(old_value, new_value, prefix=(field,)))
        elif old_value != new_value:
            paths.add((field, "$"))
    return paths


def _document_write_paths(
    before: object,
    after: object,
    *,
    prefix: SemanticWritePath,
) -> set[SemanticWritePath]:
    if before == after:
        return set()
    if isinstance(before, dict) and isinstance(after, dict):
        paths: set[SemanticWritePath] = set()
        for key in set(before) | set(after):
            if key not in before or key not in after:
                value = after[key] if key in after else before[key]
                suffix = ("$",) if isinstance(value, dict) else ()
                paths.add((*prefix, str(key), *suffix))
                continue
            paths.update(
                _document_write_paths(
                    before[key],
                    after[key],
                    prefix=(*prefix, str(key)),
                )
            )
        return paths
    return {prefix}


def _branch_conflict_paths(
    conflicts: list[BranchMergeConflict],
) -> set[SemanticWritePath]:
    paths: set[SemanticWritePath] = set()
    globals_ = {"project_truth_scope", "ontology"}
    for conflict in conflicts:
        parts = tuple(part for part in conflict.field_path.split(".") if part)
        if conflict.collection in globals_:
            if parts and parts[0] == conflict.collection:
                parts = parts[1:]
            path = (conflict.collection, *(parts or ("$",)))
        else:
            assert conflict.entity_id is not None
            path = (conflict.collection, conflict.entity_id, *(parts or ("$",)))
        if path[-1] != "$" and any(
            isinstance(value, dict) for value in (conflict.base, conflict.branch, conflict.main)
        ):
            path = (*path, "$")
        paths.add(path)
    return paths


def _declared_bookkeeping_sensitive_write_paths(operation: object) -> set[SemanticWritePath]:
    """Name declared writes whose no-op application still mutates node bookkeeping."""

    if isinstance(operation, UpdateNodesOperation):
        return {
            ("nodes", update.id, field) for update in operation.nodes for field in update.changes
        }
    if isinstance(operation, SetStandingOperation):
        return {("nodes", operation.node_id, "standing")}
    return set()


def _semantic_path_authorizes_declared_write(
    allowed: SemanticWritePath,
    declared: SemanticWritePath,
) -> bool:
    if _semantic_path_covers(allowed, declared):
        return True
    allowed_parts = allowed[:-1] if allowed and allowed[-1] == "$" else allowed
    return allowed_parts[: len(declared)] == declared


def _semantic_path_covers(rule: SemanticWritePath, path: SemanticWritePath) -> bool:
    if rule and rule[-1] == "$":
        return path[: len(rule) - 1] == rule[:-1]
    return rule == path


def _semantic_path_value(document: dict[str, JsonValue], path: SemanticWritePath) -> object:
    parts = path[:-1] if path and path[-1] == "$" else path
    value: object = document
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _render_semantic_path(path: SemanticWritePath) -> str:
    return "/".join(path[:-1] if path and path[-1] == "$" else path)


def _semantic_change_is_subsumed(
    change: str,
    before: BaseModel | None,
    after: BaseModel | None,
    main: BaseModel | None,
    bookkeeping: frozenset[str],
) -> bool:
    before_document = _optional_semantic_document(before, bookkeeping)
    after_document = _optional_semantic_document(after, bookkeeping)
    main_document = _optional_semantic_document(main, bookkeeping)
    if change == "created":
        return main_document == after_document
    if change == "removed":
        return main_document is None
    return _branch_changes_are_present(before_document, after_document, main_document)


_MISSING = object()


def _branch_changes_are_present(base: object, branch: object, main: object) -> bool:
    if base == branch:
        return True
    if branch == main:
        return True
    if isinstance(base, dict) and isinstance(branch, dict) and isinstance(main, dict):
        for key in set(base) | set(branch):
            base_value = base.get(key, _MISSING)
            branch_value = branch.get(key, _MISSING)
            if base_value == branch_value:
                continue
            if not _branch_changes_are_present(
                base_value,
                branch_value,
                main.get(key, _MISSING),
            ):
                return False
        return True
    return False


def _collection_conflicts(
    collection: str,
    base: Mapping[str, BaseModel],
    branch: Mapping[str, BaseModel],
    main: Mapping[str, BaseModel],
    bookkeeping: frozenset[str],
) -> list[BranchMergeConflict]:
    conflicts: list[BranchMergeConflict] = []
    for identity in sorted(set(base) | set(branch)):
        base_value = _optional_semantic_document(base.get(identity), bookkeeping)
        branch_value = _optional_semantic_document(branch.get(identity), bookkeeping)
        main_value = _optional_semantic_document(main.get(identity), bookkeeping)
        if base_value == branch_value:
            continue
        if base_value is None:
            if main_value is not None and main_value != branch_value:
                conflicts.append(
                    _conflict(
                        "branch_created_main_created",
                        collection,
                        identity,
                        "$",
                        base_value,
                        branch_value,
                        main_value,
                    )
                )
            continue
        if branch_value is None:
            if main_value not in (base_value, None):
                conflicts.append(
                    _conflict(
                        "branch_removed_main_changed",
                        collection,
                        identity,
                        "$",
                        base_value,
                        branch_value,
                        main_value,
                    )
                )
            continue
        if main_value is None:
            conflicts.append(
                _conflict(
                    "branch_changed_main_removed",
                    collection,
                    identity,
                    "$",
                    base_value,
                    branch_value,
                    main_value,
                )
            )
            continue
        for field_path, base_field, branch_field, main_field in _conflicting_fields(
            base_value,
            branch_value,
            main_value,
        ):
            conflicts.append(
                _conflict(
                    "both_changed",
                    collection,
                    identity,
                    field_path,
                    base_field,
                    branch_field,
                    main_field,
                )
            )
    return conflicts


def _conflicting_fields(
    base: JsonValue,
    branch: JsonValue,
    main: JsonValue,
    *,
    prefix: str = "",
) -> list[tuple[str, JsonValue, JsonValue, JsonValue]]:
    if base in (branch, main) or branch == main:
        return []
    if isinstance(base, dict) and isinstance(branch, dict) and isinstance(main, dict):
        conflicts: list[tuple[str, JsonValue, JsonValue, JsonValue]] = []
        for key in sorted(set(base) | set(branch) | set(main)):
            path = f"{prefix}.{key}" if prefix else key
            conflicts.extend(
                _conflicting_fields(
                    base.get(key),
                    branch.get(key),
                    main.get(key),
                    prefix=path,
                )
            )
        return conflicts
    return [(prefix or "$", base, branch, main)]


def _conflict(
    code: str,
    collection: str,
    identity: str,
    field_path: str,
    base: JsonValue,
    branch: JsonValue,
    main: JsonValue,
) -> BranchMergeConflict:
    return BranchMergeConflict.model_validate(
        {
            "code": code,
            "collection": collection,
            "entity_id": identity,
            "field_path": field_path,
            "base": base,
            "branch": branch,
            "main": main,
            "message": (
                f"Branch and main changed {collection}/{identity} at {field_path} "
                "differently from the base."
            ),
        }
    )


def _semantic_document(value: object, bookkeeping: frozenset[str]) -> JsonValue:
    if isinstance(value, BaseModel):
        document: JsonValue = value.model_dump(mode="json", exclude=bookkeeping)
    else:
        document = _jsonable(value)
    return document


def _optional_semantic_document(
    value: BaseModel | None,
    bookkeeping: frozenset[str],
) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    document = _semantic_document(value, bookkeeping)
    if not isinstance(document, dict):  # pragma: no cover - every collection item is a model
        raise TypeError("semantic graph collection item is not an object")
    return document


def _jsonable(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=_json_default))


def _json_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def _context_id(payload: object) -> str:
    return _canonical_sha256({"kind": "branch_merge_context", "payload": payload})


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validation_diagnostic(
    report: object,
) -> tuple[str, list[TransitionConflictDetail]]:
    messages = getattr(report, "messages", ())
    rendered: list[str] = []
    conflicts: list[TransitionConflictDetail] = []
    for item in messages:
        message = getattr(item, "message", None)
        if isinstance(message, str) and message.strip():
            rendered.append(" ".join(message.split()))
        if getattr(item, "code", None) == "transition-conflict":
            with suppress(ValueError):
                conflicts.append(
                    TransitionConflictDetail(
                        operation_index=getattr(item, "operation_index", None),
                        rule_id=getattr(item, "rule_id", None),
                        cause_chain=getattr(item, "cause_chain", ()),
                        affected_ids=[
                            *getattr(item, "related_node_ids", ()),
                            *getattr(item, "related_edge_ids", ()),
                        ],
                        invariant=getattr(item, "failed_invariant", None)
                        or "graph transition preparation",
                        message=message or "Graph transition preparation failed.",
                    )
                )
    if not rendered:
        return (
            "Branch merge candidate was rejected by canonical graph validation.",
            conflicts,
        )
    return " ".join(dict.fromkeys(rendered))[:4_000], conflicts


def _require_prepared_provenance(
    prepared: Patch,
    candidate: Patch,
    expected_main_head: GraphHeadRef,
) -> None:
    if prepared.branch_merge != candidate.branch_merge:
        raise RuntimeError("branch merge preparation changed its exact provenance")
    if prepared.authorized_by != candidate.authorized_by:
        raise RuntimeError("branch merge preparation changed its authorizer snapshot")
    if prepared.profile != "orchestrator" or prepared.task_id != candidate.task_id:
        raise RuntimeError("branch merge preparation changed its task attribution")
    if prepared.transition is None:
        raise RuntimeError("branch merge was prepared without a transition trace")
    if prepared.transition.pre_head != expected_main_head:
        raise RevisionConflict("branch merge preparation used a different main head")
    if prepared.revision != expected_main_head.revision + 1:
        raise RuntimeError("branch merge preparation did not target exactly one next revision")


def _patch_path(stage: BranchMergeStage) -> str:
    if stage.remote_stage is not None:
        return str(stage.remote_stage.workspace / "patch.json")
    return str(stage.workspace / "patch.json")


def _stage_merge_plan(
    stage: BranchMergeStage,
    token: str,
    ops: list[dict[str, Any]],
    *,
    round_number: int,
) -> str:
    return _stage_json_task_input(
        stage.local_stage,
        stage.remote_stage,
        f"task-{token}-branch-merge-plan-{round_number}.json",
        {"ops": ops},
    )


def _stage_merge_context(
    stage: BranchMergeStage,
    token: str,
    context: BranchMergeContext,
    *,
    round_number: int,
) -> str:
    return _stage_json_task_input(
        stage.local_stage,
        stage.remote_stage,
        f"task-{token}-branch-merge-context-{round_number}-{context.context_id[:16]}.json",
        context.model_dump(mode="json"),
    )


def _stage_merge_context_reference(
    stage: BranchMergeStage,
    token: str,
    context: BranchMergeContext,
    round_number: int,
) -> str:
    label = f"task-{token}-branch-merge-context-{round_number}-{context.context_id[:16]}.json"
    if stage.remote_stage is not None:
        assert stage.remote_stage.root is not None
        return str(stage.remote_stage.root / "inputs" / label)
    assert stage.local_stage is not None
    target = stage.local_stage / "inputs" / label
    if not target.is_file():
        raise ValueError("branch merge correction lost its immutable merge context")
    return str(target)


def _clear_patch_candidates(stage: BranchMergeStage) -> None:
    if stage.remote_stage is not None:
        names = stage.remote_stage.list_workspace_entries()
        for name in names:
            if not _is_agent_json_output(name):
                continue
            stage.remote_stage.remove_workspace_file(name)
        return
    for item in stage.workspace.iterdir():
        if not _is_agent_json_output(item.name):
            continue
        if item.is_symlink() or item.is_file():
            item.unlink()
            continue
        raise ValueError(f"branch merge workspace JSON output is not a regular file: {item.name}")


def _is_agent_json_output(name: str) -> bool:
    """Keep RCP-owned command credentials while clearing inherited agent output."""

    folded = name.casefold()
    return folded.endswith(".json") and not folded.startswith(
        ("rcp-command-", ".rcp-command-", ".rcp-mailbox-")
    )


def _read_candidate_text(stage: BranchMergeStage) -> str:
    text, _name = _collect_patch_text(stage.workspace, stage.remote_stage)
    return text


def _provider_turn(
    launcher: AgentLauncher,
    request: RunRequest,
    prompt: str,
    *,
    stage: BranchMergeStage,
    write_scope: ProjectWriteScope,
    execution: Any | None,
    binary: str | None,
    session_id: str | None,
    required_session_id: str | None,
) -> tuple[_ProviderOutcome, AsyncIterator[str]]:
    provider_outcome = _ProviderOutcome(session_id=session_id)
    inputs = (
        Path(str(stage.remote_stage.root / "inputs"))
        if stage.remote_stage is not None and stage.remote_stage.root is not None
        else stage.local_stage / "inputs"  # type: ignore[operator]
    )
    stream = _stream_agent_events(
        launcher,
        request,
        prompt,
        workspace=stage.workspace,
        session_id=session_id,
        read_dirs=[inputs],
        # The provider API treats write_dirs as admitted repository roots.
        # The scratch workspace is already the cwd/workspace root carried by
        # ProjectWriteScope, while a graph-only merge admits no repositories.
        write_dirs=[],
        write_scope=write_scope,
        execution_host=write_scope.execution_host,
        execution=execution,
        remote_stage=stage.remote_stage,
        capability="orchestrate",
        outcome=provider_outcome,
        binary=binary,
        required_session_id=required_session_id,
    )
    return provider_outcome, stream


def _record_merge_launch(
    execution: Any | None,
    request: RunRequest,
    *,
    prompt: str,
    contract_path: str,
    write_scope: ProjectWriteScope,
    context: BranchMergeContext,
    continuation: str,
    round_number: int,
) -> None:
    _record_agent_launch_receipt(
        execution,
        request,
        prompt=prompt,
        contract_path=contract_path,
        remote=bool(write_scope.execution_host),
        resumed=continuation != "initial",
        write_scope=write_scope,
        continuation=continuation,
        extra={
            "surface": "branch_merge",
            "mode": "branch_merge",
            "capability": "orchestrate",
            "network_access": True,
            "launch_kind": continuation,
            "round": round_number,
            "merge_context_id": context.context_id,
            "merge_id": branch_merge_id(context.metadata),
            "branch_id": context.metadata.branch_id,
            "branch_head": context.metadata.head.model_dump(mode="json"),
            "main_head": context.main_head.model_dump(mode="json"),
            "write_directory_count": 1,
            "repository_write_root_count": 0,
        },
    )
