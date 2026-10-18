"""Shared, non-persisted contracts for planning, building, and checking graph merges.

These models are task artifacts, never replacements for canonical Patch/receipt
models. Collections and fields use model names, not JSON pointers. The planner
compares content; core alone decides legality, reviewability, and approval effects.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from rcp.core.authority import CapturedChangeAuthority
from rcp.core.models import (
    AuthorizedHuman,
    BranchMergeProvenance,
    BranchMergeReceipt,
    GraphState,
    Patch,
    Proposal,
)
from rcp.core.operations import ProposalStandingChangeOperation, ProposalStatusChangeOperation
from rcp.core.transition_models import GraphHeadRef
from rcp.core.validation.proposals import ProposalDependenciesAndEffects

if TYPE_CHECKING:
    from rcp.core.transitions import PreparedTransition
    from rcp.history import HistoryManager

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Collection = Literal[
    "nodes", "edges", "proposals", "ambiguities", "glossary", "project_truth_scope", "ontology"
]
Side = Literal["branch", "destination"]
FieldAnswerKind = Literal["branch", "destination", "merged"]


class _MergeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MissingValue(_MergeModel):
    """Entity/field absence; deliberately different from PresentValue(value=None)."""

    present: Literal[False] = False


class PresentValue(_MergeModel):
    present: Literal[True] = True
    value: JsonValue


MergeValue = Annotated[MissingValue | PresentValue, Field(discriminator="present")]


class MergeChange(_MergeModel):
    """One whole-field or whole-entity change, before operation construction.

    field_name=None means entity presence; global collections use entity_id=None.
    change_id is stable for collection/entity/field, independent of graph heads.
    Values retain all three snapshots so the builder can preserve destination
    work. The planner records supersede/merge-node retirement as its ordinary
    field and edge changes; only the builder reconstructs the semantic
    supersede/merge operation from them (no separate semantic descriptor).
    """

    change_id: str = Field(min_length=1)
    collection: Collection
    entity_id: str | None
    field_name: str | None
    base: MergeValue
    branch: MergeValue
    destination: MergeValue


class FieldConflict(_MergeModel):
    kind: Literal["field"] = "field"
    conflict_id: str = Field(min_length=1)
    collection: Collection
    entity_id: str | None
    field_name: str = Field(min_length=1)
    base: MergeValue
    branch: MergeValue
    destination: MergeValue
    allowed_answer_kinds: tuple[FieldAnswerKind, ...]

    @model_validator(mode="after")
    def require_field_answers(self) -> Self:
        expected = ("branch", "destination")
        if self.collection != "nodes" or self.field_name not in {"status", "standing"}:
            expected += ("merged",)
        if self.allowed_answer_kinds != expected:
            raise ValueError("field answer kinds must match the field's judgment boundary")
        return self


class PresenceConflict(_MergeModel):
    kind: Literal["presence"] = "presence"
    conflict_id: str = Field(min_length=1)
    collection: Collection
    entity_id: str = Field(min_length=1)
    base: MergeValue
    branch: MergeValue
    destination: MergeValue
    removed_on: Side
    allowed_answer_kinds: tuple[Literal["keep-removal"], Literal["keep-node"]] = (
        "keep-removal",
        "keep-node",
    )


class IncidentEdgeConflict(_MergeModel):
    """Node removal versus added/edited incident edges on the other side.

    Values are complete node values; incident_edges records the competing whole
    edge changes. keep-removal removes the node and its incident edges together;
    keep-node retains the surviving node and the competing edge work.
    """

    kind: Literal["incident_edge"] = "incident_edge"
    conflict_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    base: MergeValue
    branch: MergeValue
    destination: MergeValue
    removed_on: Side
    incident_edges: tuple[MergeChange, ...] = Field(min_length=1)
    allowed_answer_kinds: tuple[Literal["keep-removal"], Literal["keep-node"]] = (
        "keep-removal",
        "keep-node",
    )


class NodeGroupConflict(_MergeModel):
    """A once-promoted group from core diagnostics naming related_node_ids.

    fields contains the union of branch/destination changed fields on those nodes,
    including one-sided changes. branch/destination chooses that side for the
    entire group. merged supplies a node-id -> field-name -> FieldAnswer object;
    each nested field obeys its allowed kinds (status/standing still pick a side).
    Nested field conflict ids are descriptors, not additional top-level answers.
    A second preparation failure of this group returns the exact diagnostic.
    """

    kind: Literal["node_group"] = "node_group"
    conflict_id: str = Field(min_length=1)
    node_ids: tuple[str, ...] = Field(min_length=1)
    fields: tuple[FieldConflict, ...] = Field(min_length=1)
    base: dict[str, MergeValue]
    branch: dict[str, MergeValue]
    destination: dict[str, MergeValue]
    diagnostic_codes: tuple[str, ...] = Field(min_length=1)
    allowed_answer_kinds: tuple[Literal["branch"], Literal["destination"], Literal["merged"]] = (
        "branch",
        "destination",
        "merged",
    )


MergeConflict = Annotated[
    FieldConflict | PresenceConflict | IncidentEdgeConflict | NodeGroupConflict,
    Field(discriminator="kind"),
]


class ReviewGroup(_MergeModel):
    """One connected approval group; read/read overlap alone never connects it.

    Join groups when either core-reported effects intersect the other's
    dependencies. subject_key identifies the sorted subjects, independent of
    Proposal ids, heads, and card prose. effect_digest binds the exact proposed
    effect, including outstanding pending effects on re-merge. The planner computes
    it as merge_contract_digest([c.model_dump(mode="json") for c in changes] +
    [outstanding prior-review effects]). The run loop reuses card prose on rebase
    only when the new group's digest equals the old one. Recompute footprints after
    answers change effects; never infer dependencies in the merge layer. Unfiled footprint drafts
    explicitly use CURRENT_PROPOSAL_DEPENDENCY_RULE; Proposal's default retains
    historical rule 1 and must not be used for a new review.
    """

    subject_key: str = Field(min_length=1)
    changes: tuple[MergeChange, ...] = Field(min_length=1)
    footprint: ProposalDependenciesAndEffects
    effect_digest: Digest
    replaces_proposal_ids: tuple[str, ...] = ()


class SourceHumanFact(_MergeModel):
    """Exact canonical source human judgment, never agent-manufactured proof."""

    revision: int = Field(ge=1)
    operation_index: int = Field(ge=0)
    operation: ProposalStandingChangeOperation | ProposalStatusChangeOperation


class PriorReview(_MergeModel):
    """Canonical earlier merge filing, scoped by source/destination/subject.

    proposal retains its current resolution and outstanding ops. Only reviews
    filed by provenance for this source/destination may be replaced; carried
    source Proposals and another branch's reviews are never replacement inputs.
    Pending effects are retained in a replacement, rejected effects suppressed,
    and approved effects give way to later destination judgments.
    """

    subject_key: str = Field(min_length=1)
    proposal: Proposal
    provenance: BranchMergeProvenance
    effect_digest: Digest


class MergePreparationContract(_MergeModel):
    """Exact preparation rules and schemas, shared by check and commit."""

    ruleset_tag: str = Field(min_length=1)
    trigger_manifest: dict[str, JsonValue]
    transition_trace_schema: dict[str, JsonValue]
    orchestrator_patch_schema: dict[str, JsonValue]


class MergePlanningContext(_MergeModel):
    """Loader's closed input to pure planning, replacing old derived delta/residue data.

    The loader captures live authority with capture_change_authority using an empty
    Patch template, exact source/destination heads and immutable snapshots. Both
    planner and builder classify candidates made by replacing template.ops and
    setting agent_action to core's derived_agent_action(state, candidate);
    no synthetic membership resolver is permitted. The capture is preflight
    evidence, never a substitute for live admission at preparation/commit.
    Source human facts and prior reviews come only from canonical history.
    """

    schema_generation: Literal[1] = 1
    context_id: Digest
    captured_authority: CapturedChangeAuthority
    merge_task_id: str = Field(min_length=1)
    authorized_by: AuthorizedHuman
    profile: Literal["orchestrator"] = "orchestrator"
    provenance: BranchMergeProvenance
    base_head: GraphHeadRef
    source_head: GraphHeadRef
    destination_head: GraphHeadRef
    base_graph: GraphState
    source_graph: GraphState
    destination_graph: GraphState
    run_truth_scope: tuple[str, ...] = Field(min_length=1)
    source_human_facts: tuple[SourceHumanFact, ...] = ()
    previous_merge_receipt: BranchMergeReceipt | None = None
    previous_source_graph: GraphState | None = None
    prior_reviews: tuple[PriorReview, ...] = ()
    preparation_contract: MergePreparationContract

    @model_validator(mode="after")
    def require_bound_inputs(self) -> Self:
        for head, graph in (
            (self.base_head, self.base_graph),
            (self.source_head, self.source_graph),
            (self.destination_head, self.destination_graph),
        ):
            if head.revision != graph.revision:
                raise ValueError("merge head does not match its snapshot revision")
        if (
            self.source_head != self.provenance.branch_head
            or self.destination_head != self.provenance.rebased_main_head
            or self.base_head != self.provenance.branch_base_head
            or self.merge_task_id != self.provenance.merge_task_id
        ):
            raise ValueError("merge plan does not match its canonical provenance")
        if (self.previous_merge_receipt is None) != (self.previous_source_graph is None):
            raise ValueError("previous delivery requires both receipt and source snapshot")
        if self.previous_merge_receipt is not None:
            previous = self.previous_merge_receipt
            assert self.previous_source_graph is not None
            if (
                previous.provenance.branch_id != self.provenance.branch_id
                or previous.provenance.branch_base_head != self.base_head
                or previous.provenance.branch_head.revision != self.previous_source_graph.revision
                or previous.provenance.branch_head.revision > self.source_head.revision
                or previous.result_main_head.target != self.destination_head.target
                or previous.result_main_head.revision > self.destination_head.revision
            ):
                raise ValueError("previous delivery does not match this source and destination")
        for review in self.prior_reviews:
            if (
                review.provenance.branch_id != self.provenance.branch_id
                or review.provenance.rebased_main_head.target != self.destination_head.target
            ):
                raise ValueError("prior review belongs to another source or destination")
        if self.run_truth_scope != tuple(sorted(set(self.run_truth_scope))):
            raise ValueError("merge truth scope must be sorted and unique")
        if (
            self.captured_authority.apply_target != self.destination_head.target
            or self.captured_authority.patch_template.authorized_by != self.authorized_by
            or self.captured_authority.patch_template.branch_merge != self.provenance
            or self.captured_authority.patch_template.profile != self.profile
            or self.captured_authority.patch_template.task_id != self.merge_task_id
            or tuple(self.captured_authority.patch_template.run_truth_scope) != self.run_truth_scope
        ):
            raise ValueError("merge inputs do not match captured dispatch authority")
        return self


class MergePlan(MergePlanningContext):
    """Closed pure-planning output consumed unchanged by builder and check.

    plan_id is SHA256 of UTF-8 JSON for model_dump(mode='json') excluding plan_id.
    Omit plan_id to compute it; supplying it verifies the binding. Serialize
    with sorted keys, separators (',', ':'), ensure_ascii=True, allow_nan=False.
    All sequences use deterministic order. Conflict/subject ids use semantic
    coordinates; plan identity also binds values, exact heads, authority, source
    facts, prior reviews, and preparation contract. On rebase, reuse an answer
    only if its id AND complete base/branch/destination descriptors are unchanged.
    Snapshots are private values: consumers must not mutate nested graph models.
    direct_changes and review-group changes contain only branch-side changes the
    destination must receive.
    """

    plan_id: Digest = ""
    direct_changes: tuple[MergeChange, ...] = ()
    conflicts: tuple[MergeConflict, ...] = ()
    review_groups: tuple[ReviewGroup, ...] = ()

    @model_validator(mode="after")
    def require_bound_plan(self) -> Self:
        ids = [conflict.conflict_id for conflict in self.conflicts]
        keys = [group.subject_key for group in self.review_groups]
        if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
            raise ValueError("conflict ids and review subject keys must be unique")
        expected_id = merge_contract_digest(self.model_dump(mode="json", exclude={"plan_id"}))
        if self.plan_id and self.plan_id != expected_id:
            raise ValueError("merge plan identity does not match its content")
        object.__setattr__(self, "plan_id", expected_id)
        return self


class SideAnswer(_MergeModel):
    kind: Side


class MergedAnswer(_MergeModel):
    kind: Literal["merged"] = "merged"
    value: JsonValue


class PresenceAnswer(_MergeModel):
    kind: Literal["keep-removal", "keep-node"]


FieldAnswer = Annotated[SideAnswer | MergedAnswer, Field(discriminator="kind")]
ConflictAnswer = Annotated[SideAnswer | MergedAnswer | PresenceAnswer, Field(discriminator="kind")]


class ReviewCard(_MergeModel):
    situation_cold: str = Field(min_length=1)
    why_human_now: str = Field(min_length=1)
    consequences: str = Field(min_length=1)
    decision_needed: str = Field(min_length=1)


class MergeAnswers(_MergeModel):
    """The only agent output: exact conflict keys and stable review-subject keys.

    Keys match exactly plan.conflicts and plan.review_groups. A planned group
    eliminated by answers uses no card and files no Proposal; its supplied card
    is harmless. Changed grouping/effect identity requires a new plan/card.
    The builder rejects missing/unknown keys, disallowed kinds and invalid typed
    values using core diagnostics; it does not coerce a wrong field type. A
    node-group merged value is validated as node-id -> field-name -> FieldAnswer.
    No ops, Proposal ids, authority claims, or Patch fallback are accepted.
    """

    answers: dict[str, ConflictAnswer]
    cards: dict[str, ReviewCard]


class MergeCheckReceipt(_MergeModel):
    """Successful dry preparation bound to the exact collected answers bytes.

    answers_digest is SHA256 of file bytes (not parsed/re-serialized JSON).
    Timeout produces no success receipt and remains unavailable. Publication
    uncertainty is reconciled using canonical merge receipts, never this receipt.
    """

    task_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    plan_id: Digest
    answers_digest: Digest
    source_head: GraphHeadRef
    destination_head: GraphHeadRef
    preparation_contract: MergePreparationContract


class MergeDiagnostic(_MergeModel):
    """One structured core diagnostic; code and related ids, never parsed prose."""

    code: str = Field(min_length=1)
    message: str
    related_node_ids: tuple[str, ...] = ()


class NodeGroupPromotion(_MergeModel):
    """Core rejected the built Patch naming nodes that carry no open conflict."""

    node_ids: tuple[str, ...] = Field(min_length=1)
    diagnostics: tuple[MergeDiagnostic, ...] = Field(min_length=1)


class MergeAnswersRejected(ValueError):
    """Agent-fixable: bad keys/kinds/types, or a core rejection naming nodes that
    already sit under an open conflict (including an already-promoted group).
    The check returns these diagnostics; the agent edits answers and rechecks."""

    def __init__(self, diagnostics: tuple[MergeDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__("; ".join(item.message for item in diagnostics))


class MergeHiddenClash(ValueError):
    """Core rejected the built Patch naming nodes with no open conflict on them.
    The run loop calls promote_node_group once and asks the agent again."""

    def __init__(self, promotion: NodeGroupPromotion) -> None:
        self.promotion = promotion
        super().__init__("; ".join(item.message for item in promotion.diagnostics))


class MergeRefused(ValueError):
    """Hard refusal (lost authority, malformed data, missing dependency, forbidden
    configuration, invalid proposed effect, or a rejection naming no node).
    The merge task fails with these diagnostics; no Proposal fallback."""

    def __init__(self, diagnostics: tuple[MergeDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__("; ".join(item.message for item in diagnostics))


def merge_contract_digest(value: object) -> str:
    """Canonical identity for JSON-valued plan/effect descriptors, not file bytes."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def plan_merge(context: MergePlanningContext) -> MergePlan:
    """Slice 2: compare immutable source base, delivered baseline and destination.

    Return whole-content-field changes, presence/incident-edge conflicts, core-
    routed review groups and exact source/prior-review facts. One-sided edits win;
    equal edits agree. Missing differs from null. Use the previous delivered source
    snapshot for new work, retaining outstanding pending effects only from scoped
    canonical earlier reviews. Do not resurrect rejected work or overwrite later
    destination judgments after approval. Core owns routing/dependencies; hard
    refusals propagate intact, never becoming review cards. Hidden clashes may
    promote each diagnostic-named node group once after dry preparation. This
    function performs no history writes, provider calls, or agent op parsing.
    """
    raise NotImplementedError


def promote_node_group(plan: MergePlan, promotion: NodeGroupPromotion) -> MergePlan:
    """Slice 2: return a new plan where the named nodes form one NodeGroupConflict.

    The group holds the union of branch-changed and destination-changed content
    fields on those nodes (one-sided changes included); their direct changes,
    field conflicts, and review-group members move into it. diagnostic_codes come
    from promotion.diagnostics. A node already inside a NodeGroupConflict raises
    MergeRefused (promotion happens once; prepare_merge_candidate routes a repeat
    rejection on such nodes to MergeAnswersRejected instead of calling this).
    """
    raise NotImplementedError


def build_merge_patch(plan: MergePlan, answers: MergeAnswers) -> Patch:
    """Slice 3: deterministically construct the complete ordered Patch in memory.

    Validate exact answer/card keys, allowed answer kinds and field types. Encode
    semantic supersede/merge and decided-node creation with core-supported op
    sequences, then classify coherent groups using classify_graph_changes with
    plan.captured_authority and its Patch template (including its fixed timestamp). Preserve
    destination standing through implicit content-reset effects. human_edit is
    allowed only for an exact SourceHumanFact; any other carried status keeps the
    cause its source transition recorded. Connected protected effects become one pending Proposal
    per ReviewGroup; recheck footprints after answers. Carry live source Proposals
    with identity/content intact and new filing stamps, but keep source resolutions
    on source. Withdraw scoped pending predecessors and file replacements in the
    same Patch. No timestamps/random ids may make identical inputs differ. Return
    operations only through Patch; no runtime graph-equivalence proof or IO.
    """
    raise NotImplementedError


def prepare_merge_candidate(
    plan: MergePlan, answers: MergeAnswers, history: HistoryManager
) -> PreparedTransition:
    """Slice 3 adapter, used identically by slice 4 check and locked commit.

    Recheck plan identity, build the Patch and use authoritative history dry preparation with exact heads,
    live membership/task authority and the bound preparation contract. Return the
    PreparedTransition including generated effects; flags alone pass. Preserve
    structured rejection codes and related ids separately from advisories. Never
    append, consume publication receipts, or route a hard refusal into a Proposal.
    A moved destination requires a new plan and revalidation; it is not an answer
    error. Outcomes: success returns the PreparedTransition; answer problems raise
    MergeAnswersRejected; a core rejection whose related_node_ids carry no open
    conflict raises MergeHiddenClash; one naming any node under an open conflict
    raises MergeAnswersRejected; one naming no node, or any hard refusal from core
    classification, raises MergeRefused. build_merge_patch raises the same types. Caller compares the exact task/turn/file digest check receipt before
    commit and retains canonical receipt reconciliation for uncertain publication.
    """
    raise NotImplementedError
