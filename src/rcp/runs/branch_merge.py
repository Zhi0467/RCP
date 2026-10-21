"""Task orchestration and canonical publication for a planned graph branch merge.

RCP constructs operations through the merge contract. Agents supply only checked
conflict answers and review-card prose; history owns preparation and publication.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from rcp.agents import AgentEvent, AgentLauncher
from rcp.agents.branch_merge_prompt import (
    BRANCH_MERGE_POLICY_VERSION,
    branch_merge_rebase_parts,
    branch_merge_task_contract,
    code_merge_task_contract,
)
from rcp.agents.context import _has_ontology_extensions
from rcp.agents.continuation_prompt import (
    LaunchPhase,
    MasterRef,
    changed_since_master,
    classify,
    compose,
    graph_head_delta,
    master_key,
)
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.agents.graph_rules import graph_rules
from rcp.agents.invocation_broker import ProviderInvocationGate
from rcp.agents.prompts import graph_read_contract
from rcp.agents.write_scope import ProjectWriteScope
from rcp.core.authority import (
    WriteAuthority,
    capture_change_authority,
)
from rcp.core.models import (
    AuthorizedHuman,
    BranchMergeProvenance,
    BranchMergeReceipt,
    GraphBranchMetadata,
    GraphState,
    HiddenReadScope,
    Patch,
    ValidationMessage,
)
from rcp.core.operations import (
    HumanEditCause,
    ProposalStandingChangeOperation,
    ProposalStatusChangeOperation,
    SetStandingOperation,
    UpdateNodesOperation,
)
from rcp.core.project_types import project_type_of
from rcp.core.transition_models import (
    GraphHeadRef,
    TransitionConflictDetail,
)
from rcp.core.transitions import (
    CommittedTransition,
    PreparedTransition,
    project_transition_projection,
)
from rcp.history import (
    BranchMergeAlreadyCommitted,
    BranchMergeAlreadyResolved,
    HistoryManager,
    PatchRejected,
    RevisionConflict,
)
from rcp.history.branches import BranchHistoryManager
from rcp.history.queries import StepRecord
from rcp.limits import MAX_BRANCH_MERGE_REBASE_ROUNDS, PATCH_SELF_CHECK_MAX_REQUEST_BYTES
from rcp.runs import merge_contract, merge_patch, merge_plan
from rcp.runs.merge_contract import (
    MergePlanningContext,
    MergePreparationContract,
    PriorReview,
    SourceHumanFact,
)
from rcp.runs.patch_validator import MergeAnswersCheck
from rcp.runs.session_master import (
    prepare_graph_observation,
    record_inline_prompt,
    record_session_master,
)
from rcp.runs.shared import (
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _sse,
    _stage_json_task_input,
    _stage_or_reuse_task_input,
    _stage_task_contract,
    _stream_agent_events,
    _task_token,
)
from rcp.service import ProjectService, RunRequest
from rcp.transport import RemoteRunStage, RunStageMailbox, StateUnavailable


class _StrictMergeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BranchPatchSummary(_StrictMergeModel):
    revision: int = Field(ge=0)
    transition_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    summary: str
    change_summary: list[str] = Field(default_factory=list)
    task_id: str | None = None
    profile: Literal["ordinary", "orchestrator"] | None = None


class BranchMergeEligibility(_StrictMergeModel):
    """Caller-supplied durable proof that no branch writer may race the merge."""

    branch_head: GraphHeadRef
    active_branch_writer_task_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_quiescence(self) -> BranchMergeEligibility:
        if self.branch_head.target.kind != "branch":
            raise ValueError("merge eligibility must name one exact branch head")
        if self.active_branch_writer_task_ids:
            raise ValueError("a graph branch with an active writer is not merge eligible")
        return self


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
    branch_patch_summaries: list[BranchPatchSummary] = Field(default_factory=list)
    transition_contract: MergePreparationContract
    human_review_changes: list[SourceHumanFact] = Field(default_factory=list)
    prior_reviews: tuple[PriorReview, ...] = ()
    previous_merge_receipt: BranchMergeReceipt | None = None
    previous_branch_graph: GraphState | None = None
    dispatcher_episode_id: str | None = None
    # A member's merge runs with no episode, even out of an episode-owned fork.
    member_merge: bool = False

    @property
    def review_base_head(self) -> GraphHeadRef:
        if self.previous_merge_receipt is not None:
            return self.previous_merge_receipt.provenance.branch_head
        return self.metadata.effective_parent_head

    @model_validator(mode="after")
    def require_exact_snapshots(self) -> BranchMergeContext:
        if self.run_truth_scope != sorted(set(self.run_truth_scope)):
            raise ValueError("branch merge run truth scope must be sorted and unique")
        if self.metadata.effective_parent_head.revision != self.base_graph.revision:
            raise ValueError("branch merge base graph does not match the exact base head")
        if self.metadata.head.revision != self.branch_graph.revision:
            raise ValueError("branch merge graph does not match the exact branch head")
        if (
            self.main_head.target != self.metadata.effective_parent_head.target
            or self.main_head.revision != self.main_graph.revision
        ):
            raise ValueError("branch merge main graph does not match one exact main head")
        if self.eligibility.branch_head != self.metadata.head:
            raise ValueError("merge eligibility was proved for a different branch head")
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
                or receipt.provenance.branch_base_head != self.metadata.effective_parent_head
                or receipt.provenance.branch_head.target != self.metadata.head.target
                or receipt.provenance.branch_head.revision != self.previous_branch_graph.revision
                or self.previous_branch_graph.revision > self.branch_graph.revision
                or receipt.result_main_head.target != self.main_head.target
                or receipt.result_main_head.revision > self.main_graph.revision
            ):
                raise ValueError(
                    "previous merge receipt does not match this source lineage and main"
                )
        merge_base = self.previous_branch_graph or self.base_graph
        revisions = [item.revision for item in self.branch_patch_summaries]
        if revisions != sorted(set(revisions)):
            raise ValueError("branch Patch summaries must be ordered by unique revision")
        if any(
            revision <= self.metadata.effective_parent_head.revision
            or revision > self.metadata.head.revision
            for revision in revisions
        ):
            raise ValueError("branch Patch summary lies outside the exact branch lineage")
        if self.transition_contract != MergePreparationContract.current():
            raise ValueError("branch merge context uses a stale transition-manager contract")
        for change in self.human_review_changes:
            if (
                not self.metadata.effective_parent_head.revision
                < change.revision
                <= self.metadata.head.revision
            ):
                raise ValueError("human review change lies outside the exact branch lineage")
            _require_source_human_change(merge_base, self.branch_graph, change.operation)
        excluded = {"context_id"}
        if self.dispatcher_episode_id is None:
            excluded.add("dispatcher_episode_id")
        if not self.member_merge:
            excluded.add("member_merge")
        expected_id = _context_id(self.model_dump(mode="json", exclude=excluded))
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
        human_review_changes: list[SourceHumanFact] | None = None,
        prior_reviews: tuple[PriorReview, ...] = (),
        previous_merge_receipt: BranchMergeReceipt | None = None,
        previous_branch_graph: GraphState | None = None,
        dispatcher_episode_id: str | None = None,
        member_merge: bool = False,
    ) -> BranchMergeContext:
        if run_truth_scope != sorted(set(run_truth_scope)) or not run_truth_scope:
            raise ValueError("branch merge run truth scope must be non-empty, sorted, and unique")
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
            "branch_patch_summaries": list(branch_patch_summaries or ()),
            "transition_contract": MergePreparationContract.current(),
            "human_review_changes": list(human_review_changes or ()),
            "prior_reviews": prior_reviews,
            "previous_merge_receipt": previous_merge_receipt,
            "previous_branch_graph": previous_branch_graph,
        }
        if dispatcher_episode_id is not None:
            payload["dispatcher_episode_id"] = dispatcher_episode_id
        if member_merge:
            payload["member_merge"] = True
        encoded = _jsonable(payload)
        return cls.model_validate({**payload, "context_id": _context_id(encoded)})


class BranchMergeSemanticConflict(ValueError):
    """Canonical preparation rejection, retaining its structured core details."""

    def __init__(
        self,
        message: str,
        *,
        conflicts: list[TransitionConflictDetail] | None = None,
        diagnostics=(),
    ):
        self.message = " ".join(message.split())
        self.conflicts = list(conflicts or ())
        self.diagnostics: tuple[ValidationMessage, ...] = tuple(diagnostics)
        super().__init__(self.message)


class BranchMergeSourceChanged(ValueError):
    """The supposedly quiescent source branch changed during its merge task."""


class _BranchMergeHistory(Protocol):
    def append(
        self, patch: Patch, *, discard_on_reject: bool, expected_revision: int
    ) -> tuple[Patch, Any]: ...


@dataclass
class BranchMergeRunOutcome:
    status: Literal[
        "pending",
        "noop",
        "committed",
        "rejected",
        "paused",
        "retryable",
        "awaiting_remote_result",
    ] = "pending"
    merge_id: str | None = None
    native_session_id: str | None = None
    source_branch_head: GraphHeadRef | None = None
    rebased_main_head: GraphHeadRef | None = None
    result_main_head: GraphHeadRef | None = None
    prepared: PreparedTransition | None = None
    committed: CommittedTransition | None = None
    receipt: BranchMergeReceipt | None = None
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


def branch_human_review_changes(
    patches: Sequence[StepRecord], base: GraphState, branch: GraphState
) -> list[SourceHumanFact]:
    """Retain exact human initiating writes, never generated actions or summary claims."""

    latest: dict[tuple[str, str], SourceHumanFact] = {}
    for step in patches:
        patch = step.patch
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
                    node = branch.nodes.get(update.id)
                    if (
                        "status" in update.changes
                        and node is not None
                        and project_type_of(branch).is_belief(node.type)
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
                latest[(node_id, field)] = SourceHumanFact(
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
    if after is None or getattr(after, field, None) != value:
        raise ValueError("human review fact does not match the final source branch value")
    if before is not None and getattr(before, field, None) == value:
        raise ValueError("human review fact is not a change from the branch base")


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
            "branch_base_head": metadata.effective_parent_head.model_dump(mode="json"),
            "branch_head": metadata.head.model_dump(mode="json"),
        }
    )


def branch_merge_provenance(context: BranchMergeContext) -> BranchMergeProvenance:
    return BranchMergeProvenance(
        merge_id=branch_merge_id(context.metadata),
        branch_id=context.metadata.branch_id,
        episode_id=context.metadata.episode_id,
        branch_base_head=context.metadata.effective_parent_head,
        branch_head=context.metadata.head,
        rebased_main_head=context.main_head,
        merge_task_id=context.merge_task_id,
    )


def merge_planning_context(
    context: BranchMergeContext,
    history: HistoryManager | BranchHistoryManager,
    *,
    created_at: datetime,
    authority: WriteAuthority | None = None,
) -> MergePlanningContext:
    """Capture live dispatch authority for exact loaded snapshots and a fixed timestamp.

    The task supplies one timestamp across checks and rebases. Canonical prior-review
    provenance is a loader input; this adapter never infers it from Proposal ids.
    """

    provenance = branch_merge_provenance(context)
    template = Patch(
        kind="work",
        author="agent",
        producer="agent",
        created_at=created_at,
        summary="Merge graph branch",
        ops=[],
        revision=context.main_head.revision + 1,
        run_truth_scope=context.run_truth_scope,
        source_operation_id=context.merge_task_id,
        authorized_by=context.authorized_by,
        profile="orchestrator",
        task_id=context.merge_task_id,
        episode_id=None
        if context.member_merge
        else context.dispatcher_episode_id or context.metadata.episode_id,
        branch_merge=provenance,
    )
    authority_owner = history.parent if isinstance(history, BranchHistoryManager) else history
    captured = capture_change_authority(
        context.main_graph,
        template,
        authority=authority or authority_owner.write_authority,
        project_id=authority_owner.project_id,
        apply_target=context.main_head.target,
    )
    return MergePlanningContext(
        context_id=context.context_id,
        captured_authority=captured,
        merge_task_id=context.merge_task_id,
        authorized_by=context.authorized_by,
        provenance=provenance,
        base_head=context.metadata.effective_parent_head,
        source_head=context.metadata.head,
        destination_head=context.main_head,
        base_graph=context.base_graph,
        source_graph=context.branch_graph,
        destination_graph=context.main_graph,
        run_truth_scope=tuple(context.run_truth_scope),
        source_human_facts=tuple(context.human_review_changes),
        previous_merge_receipt=context.previous_merge_receipt,
        previous_source_graph=context.previous_branch_graph,
        prior_reviews=context.prior_reviews,
        preparation_contract=context.transition_contract,
    )


def commit_branch_merge_with_history(
    history: _BranchMergeHistory,
    candidate: Patch,
    *,
    expected_main_head: GraphHeadRef,
) -> CommittedTransition:
    """Append one prepared semantic merge at an exact main revision or append nothing."""

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
            target=patch.branch_merge.rebased_main_head.target,
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
    code_roots: list[str] | None = None,
) -> None:
    """Fail closed unless the provider writes only its scratch and, with code, those roots."""

    if scope.capability != "orchestrate":
        raise ValueError("branch merge requires orchestrate capability")
    if scope.project_id != context.metadata.project_id:
        raise ValueError("branch merge write scope belongs to a different project")
    if sorted(scope.repository_roots) != sorted(code_roots or []):
        raise ValueError("branch merge repository roots must be exactly its code merge roots")
    if scope.workspace_root != str(stage.workspace):
        raise ValueError("branch merge write scope does not name its exact workspace")
    if not code_roots and scope.writable_roots != [str(stage.workspace)]:
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
        and previous.dispatcher_episode_id == current.dispatcher_episode_id
        and previous.member_merge == current.member_merge
        and previous.main_head.target == current.main_head.target
        and previous.metadata == current.metadata
        and previous.eligibility == current.eligibility
        and previous.base_graph == current.base_graph
        and previous.branch_graph == current.branch_graph
        and previous.branch_patch_summaries == current.branch_patch_summaries
        and previous.transition_contract == current.transition_contract
        and previous.human_review_changes == current.human_review_changes
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
    main_history: HistoryManager | BranchHistoryManager,
    stage: BranchMergeStage,
    write_scope: ProjectWriteScope,
    checks: MergeAnswersCheck,
    outcome: BranchMergeRunOutcome,
    graph_read_scope: GraphReadScope,
    history_client: str,
    start_check_turn: Callable[
        [merge_contract.MergePlan], Awaitable[tuple[str, str, ProviderInvocationGate | None]]
    ],
    invocation_gate: ProviderInvocationGate | None = None,
    service: ProjectService | None = None,
    execution: Any | None = None,
    binary: str | None = None,
    max_main_rebases: int = MAX_BRANCH_MERGE_REBASE_ROUNDS,
    before_commit: Callable[[], None] | None = None,
    reconcile_receipt: Callable[[], BranchMergeReceipt | None] | None = None,
    publish_receipt: Callable[[BranchMergeReceipt], BranchMergeReceipt] | None = None,
    code_block: str = "",
    code_roots: list[str] | None = None,
    code_landed: bool = False,
    hidden_read_scope: HiddenReadScope | None = None,
    recorded_answers: merge_contract.MergeAnswers | None = None,
) -> AsyncGenerator[str, None]:
    """Collect checked answers, rebase unchanged decisions, and publish once.

    `recorded_answers` settles a turn its host finished while RCP was away: the
    answers it left are prepared and committed like checked ones, and nothing
    is launched again.

    The check and commit use the same preparation contract. Only destination
    movement and newly promoted hidden clashes can require continuation; an
    unchecked or rejected final answer ends the task, never a correction launch.
    """
    if request.provider is None or request.run_on is None:
        raise ValueError("branch merge requires a pinned provider and execution machine")
    if max_main_rebases < 1:
        raise ValueError("branch merge requires a positive rebase bound")
    if execution is not None and service is None:
        raise ValueError("A tracked branch merge requires its project history service.")

    context = load_context()
    require_graph_only_merge_scope(write_scope, context=context, stage=stage, code_roots=code_roots)
    plan = checks.plan
    created_at = plan.captured_authority.patch_template.created_at
    if plan.context_id != context.context_id:
        if (
            plan.source_head != context.metadata.head
            or plan.base_head != context.metadata.effective_parent_head
            or plan.source_graph != context.branch_graph
            or plan.base_graph != context.base_graph
            or plan.authorized_by != context.authorized_by
            or plan.run_truth_scope != tuple(context.run_truth_scope)
            or plan.destination_head.target != context.main_head.target
            or plan.destination_head == context.main_head
        ):
            raise BranchMergeSourceChanged("The staged merge plan lost its exact source context.")
        plan = merge_plan.plan_merge(
            merge_planning_context(context, main_history, created_at=created_at)
        )
        checks.set_plan(plan, turn_id=checks.turn_id)
    outcome.merge_id = plan.provenance.merge_id
    outcome.source_branch_head = plan.source_head
    outcome.rebased_main_head = plan.destination_head
    answers = recorded_answers or merge_contract.MergeAnswers(answers={}, cards={})
    token = _task_token(execution)
    session_id: str | None = None
    session_master: tuple[str, str, bool] | None = None
    master_values: dict[str, object] | None = None
    delivered_head: GraphHeadRef | None = None
    previous_plan_id = plan.plan_id
    launch_number = 0
    continuation_cause: Literal["promotion", "destination_moved"] = "destination_moved"
    promotions: list[tuple[merge_contract.NodeGroupPromotion, merge_contract.MergePlan]] = []
    needs_code = bool(code_block) and not code_landed

    while True:
        try:
            if not merge_answers_complete(plan, answers) or needs_code:
                if recorded_answers is not None:
                    raise ValueError("The recorded merge turn left its answers incomplete.")
                # A fresh receipt is required for every launched continuation.
                validator_command, history_client, invocation_gate = await start_check_turn(plan)
                _answers_mailbox(stage).remove("answers.json")
                plan_path, schema_path = _stage_answers_plan(stage, token, plan)
                values = _merge_master_values(
                    plan,
                    plan_path=plan_path,
                    answers_schema_path=schema_path,
                    answers_path=str(stage.workspace / "answers.json"),
                    validator_command=validator_command,
                    retained_answers=answers,
                    history_client=history_client,
                )
                staged_head = plan.destination_head
                if service is not None:
                    staged_head = prepare_graph_observation(
                        execution,
                        service,
                        owner_id=context.metadata.episode_id or context.merge_task_id,
                        target=graph_read_scope.target,
                        revision=plan.destination_head.revision,
                        native_session_id=session_id,
                        previous_head=delivered_head,
                    )
                if session_master is None:
                    contract = graph_read_contract(
                        branch_merge_task_contract(
                            plan=plan,
                            plan_path=plan_path,
                            answers_path=str(stage.workspace / "answers.json"),
                            answers_schema_path=schema_path,
                            validator_command=validator_command,
                            retained_answers=answers,
                            code_block=code_block,
                        ),
                        graph_read_scope,
                        history_client,
                    )
                    label = f"task-{token}-branch-merge.md"
                    contract_path, prompt = _stage_task_contract(
                        stage.local_stage,
                        stage.remote_stage,
                        label,
                        contract,
                        execution=execution,
                        role="branch_merge",
                    )
                    master_values = values
                    ontology = _has_ontology_extensions(context.main_graph)
                    session_master = (label, contract, ontology)
                    if execution is not None:
                        record_session_master(
                            execution.store,
                            execution.operation_id,
                            contract,
                            master_key(BRANCH_MERGE_POLICY_VERSION, ontology_extensions=ontology),
                            values,
                        )
                    continuation = "initial"
                else:
                    if session_id is None:
                        raise ValueError("The merge continuation lost its native session.")
                    master = _restaged_merge_master(stage, session_master, master_values)
                    rules, session_master = _changed_graph_rules(context, session_master)
                    prompt = compose(
                        classify(LaunchPhase(session_id=session_id, phase="turn")),
                        parts=branch_merge_rebase_parts(
                            cause=continuation_cause,
                            previous_plan_id=previous_plan_id,
                            plan_id=plan.plan_id,
                            code=bool(code_block),
                        )
                        + rules,
                        master=master,
                        delta={
                            **(changed_since_master(master, values) or {}),
                            **graph_head_delta(delivered_head, staged_head),
                        },
                    )
                    contract_path = record_inline_prompt(
                        execution,
                        local_stage=stage.local_stage,
                        remote_stage=stage.remote_stage,
                        label=f"task-{token}-branch-merge-rebase-{launch_number}.md",
                        role="branch_merge_rebase",
                        prompt=prompt,
                    )
                    continuation = "merge_rebase"
                _record_merge_launch(
                    execution,
                    request,
                    prompt=prompt,
                    contract_path=contract_path,
                    write_scope=write_scope,
                    context=context,
                    continuation=continuation,
                    round_number=launch_number,
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
                    hidden_read_scope=hidden_read_scope,
                    invocation_gate=invocation_gate,
                )
                launch_number += 1
                async with aclosing(events) as frames:
                    async for frame in frames:
                        # The enclosing task publishes done only after canonical settlement.
                        event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                        if event.event != "done":
                            yield frame
                session_id = provider.session_id
                outcome.native_session_id = session_id
                if provider.remote_result_pending:
                    outcome.status = "awaiting_remote_result"
                    return
                if provider.paused:
                    outcome.status = "paused"
                    return
                if provider.failed or not provider.completed:
                    raise ValueError("The branch merge provider did not complete its turn.")
                delivered_head = staged_head
                needs_code = False
                if checks.internal_error is not None:
                    raise ValueError(checks.internal_error)
                if checks.destination_moved:
                    if classify_refreshed_context(context, load_context()) == "unchanged":
                        raise StateUnavailable(
                            "Destination movement is not yet readable; retry the merge task."
                        )
                    answers = checks.latest_answers or merge_contract.MergeAnswers(
                        answers={}, cards={}
                    )
                else:
                    try:
                        raw = (
                            _answers_mailbox(stage)
                            .read_text("answers.json", max_bytes=PATCH_SELF_CHECK_MAX_REQUEST_BYTES)
                            .encode("utf-8")
                        )
                    except FileNotFoundError as exc:
                        raise ValueError("The merge turn did not produce answers.json.") from exc
                    checks.require_receipt(raw)
                    answers = merge_contract.MergeAnswers.model_validate_json(raw)

            fresh = load_context()
            if classify_refreshed_context(context, fresh) == "main_moved":
                if outcome.rebase_rounds >= max_main_rebases:
                    raise StateUnavailable("The destination kept moving; retry the merge task.")
                new_plan = merge_plan.plan_merge(
                    merge_planning_context(fresh, main_history, created_at=created_at)
                )
                for promotion, promoted_plan in promotions:
                    if _promotion_still_applies(promoted_plan, new_plan, promotion):
                        new_plan = merge_plan.promote_node_group(new_plan, promotion)
                answers = reusable_merge_answers(plan, new_plan, answers)
                continuation_cause = "destination_moved"
                previous_plan_id = plan.plan_id
                plan, context = new_plan, fresh
                checks.set_plan(plan, turn_id=checks.turn_id)
                outcome.rebase_rounds += 1
                outcome.rebased_main_head = plan.destination_head
                continue

            outcome.prepared = merge_patch.prepare_merge_candidate(plan, answers, main_history)
            if before_commit is not None:
                before_commit()
            if not outcome.prepared.patch.ops:
                if classify_refreshed_context(context, load_context()) == "main_moved":
                    raise RevisionConflict("Destination moved before no-change settlement.")
                outcome.receipt = BranchMergeReceipt(
                    outcome="no_change",
                    provenance=plan.provenance,
                    result_main_head=plan.destination_head,
                    authorized_by=plan.authorized_by,
                )
                outcome.status = "noop"
            else:
                try:
                    outcome.committed = commit_branch_merge_with_history(
                        main_history,
                        merge_patch.build_merge_patch(plan, answers),
                        expected_main_head=plan.destination_head,
                    )
                    outcome.receipt = branch_merge_receipt_from_committed_patch(
                        outcome.committed.patch
                    )
                except BranchMergeAlreadyCommitted as exc:
                    outcome.receipt = branch_merge_receipt_from_committed_patch(exc.patch)
                except BranchMergeAlreadyResolved as exc:
                    outcome.receipt = exc.receipt
                except (OSError, StateUnavailable):
                    # Publication can have succeeded before its acknowledgment was lost.
                    outcome.receipt = reconcile_receipt() if reconcile_receipt is not None else None
                    if outcome.receipt is None:
                        raise
                outcome.status = "committed" if outcome.receipt.outcome == "committed" else "noop"
            if publish_receipt is not None:
                try:
                    outcome.receipt = publish_receipt(outcome.receipt)
                except (OSError, StateUnavailable):
                    outcome.receipt = reconcile_receipt() if reconcile_receipt is not None else None
                    if outcome.receipt is None:
                        raise
            outcome.status = "committed" if outcome.receipt.outcome == "committed" else "noop"
            outcome.result_main_head = outcome.receipt.result_main_head
            yield _sse(AgentEvent(event="done"))
            return
        except merge_contract.MergeHiddenClash as exc:
            try:
                new_plan = merge_plan.promote_node_group(plan, exc.promotion)
            except merge_contract.MergeRefused as refused:
                outcome.status = "rejected"
                outcome.diagnostic = str(refused)
                yield _sse(AgentEvent(event="error", text=outcome.diagnostic))
                return
            promotions.append((exc.promotion, new_plan))
            continuation_cause = "promotion"
            answers = reusable_merge_answers(plan, new_plan, answers)
            previous_plan_id, plan = plan.plan_id, new_plan
            checks.set_plan(plan, turn_id=checks.turn_id)
        except RevisionConflict:
            outcome.prepared = None
            outcome.receipt = None
            outcome.status = "pending"
            if classify_refreshed_context(context, load_context()) == "unchanged":
                outcome.status = "retryable"
                outcome.diagnostic = (
                    "Destination movement is not yet readable; retry the merge task."
                )
                yield _sse(AgentEvent(event="error", text=outcome.diagnostic))
                return
            # The next iteration rebuilds the plan before preparing or appending.
        except (OSError, StateUnavailable, TimeoutError, ValueError) as exc:
            outcome.receipt = None
            outcome.status = "rejected" if isinstance(exc, ValueError) else "retryable"
            outcome.diagnostic = str(exc)
            yield _sse(AgentEvent(event="error", text=outcome.diagnostic))
            return


def _promotion_still_applies(
    previous: merge_contract.MergePlan,
    current: merge_contract.MergePlan,
    promotion: merge_contract.NodeGroupPromotion,
) -> bool:
    """Keep a promoted question while its nodes and their changed fields survive.

    Use the planner's field descriptors, including destination-only fields that
    are absent from direct changes. The planner reconstructs the actual group.
    """
    fields = [
        field
        for conflict in previous.conflicts
        if isinstance(conflict, merge_contract.NodeGroupConflict)
        and set(conflict.node_ids) == set(promotion.node_ids)
        for field in conflict.fields
    ]
    missing = object()
    for node_id in promotion.node_ids:
        source = current.source_graph.nodes.get(node_id)
        destination = current.destination_graph.nodes.get(node_id)
        if source is None or destination is None:
            return False
        base = current.base_graph.nodes.get(node_id)
        if not any(
            getattr(base, field.field_name, missing) != getattr(source, field.field_name, missing)
            or getattr(base, field.field_name, missing)
            != getattr(destination, field.field_name, missing)
            for field in fields
            if field.entity_id == node_id
        ):
            return False
    return True


def reusable_merge_answers(
    previous: merge_contract.MergePlan,
    current: merge_contract.MergePlan,
    answers: merge_contract.MergeAnswers,
) -> merge_contract.MergeAnswers:
    """Reuse decisions only while their complete conflict or effect identity holds."""
    conflicts = {item.conflict_id: item for item in previous.conflicts}
    groups = {item.subject_key: item.effect_digest for item in previous.review_groups}
    return merge_contract.MergeAnswers(
        answers={
            item.conflict_id: answers.answers[item.conflict_id]
            for item in current.conflicts
            if item.conflict_id in answers.answers and conflicts.get(item.conflict_id) == item
        },
        cards={
            item.subject_key: answers.cards[item.subject_key]
            for item in current.review_groups
            if item.subject_key in answers.cards
            and groups.get(item.subject_key) == item.effect_digest
        },
    )


def merge_answers_complete(
    plan: merge_contract.MergePlan, answers: merge_contract.MergeAnswers
) -> bool:
    return {item.conflict_id for item in plan.conflicts} == answers.answers.keys() and {
        item.subject_key for item in plan.review_groups
    } == answers.cards.keys()


async def stream_code_merge_run(
    request: RunRequest,
    launcher: AgentLauncher,
    *,
    stage: BranchMergeStage,
    write_scope: ProjectWriteScope,
    code_block: str,
    verify: Callable[[], None],
    execution: Any | None = None,
    binary: str | None = None,
    hidden_read_scope: HiddenReadScope | None = None,
) -> AsyncGenerator[str, None]:
    """Run one code merge turn with no graph questions, then RCP verifies the landing."""

    if request.provider is None or request.run_on is None:
        raise ValueError("branch merge request must have a pinned provider and execution machine")
    contract = code_merge_task_contract(code_block=code_block)
    contract_path, prompt = _stage_task_contract(
        stage.local_stage,
        stage.remote_stage,
        f"task-{_task_token(execution)}-code-merge.md",
        contract,
        execution=execution,
        role="branch_merge",
    )
    _record_agent_launch_receipt(
        execution,
        request,
        prompt=prompt,
        contract_path=contract_path,
        remote=bool(write_scope.execution_host),
        resumed=False,
        write_scope=write_scope,
        continuation="initial",
        extra={
            "surface": "branch_merge",
            "mode": "code_merge",
            "capability": "orchestrate",
            "network_access": True,
            "launch_kind": "initial",
            "round": 0,
            "write_directory_count": 1 + len(write_scope.repository_roots),
            "repository_write_root_count": len(write_scope.repository_roots),
        },
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
        hidden_read_scope=hidden_read_scope,
    )
    async with aclosing(events) as frames:
        async for frame in frames:
            yield frame
    if provider.remote_result_pending:
        return
    if provider.paused or provider.failed or not provider.completed:
        raise ValueError("The code merge provider did not complete its turn.")
    verify()


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


def _answers_mailbox(stage: BranchMergeStage) -> RunStageMailbox:
    return RunStageMailbox.for_stage(
        local_stage=stage.workspace if stage.remote_stage is None else None,
        remote_stage=stage.remote_stage,
    )


def _stage_answers_plan(
    stage: BranchMergeStage,
    token: str,
    plan: merge_contract.MergePlan,
) -> tuple[str, str]:
    plan_path = _stage_json_task_input(
        stage.local_stage,
        stage.remote_stage,
        f"task-{token}-merge-plan-{plan.plan_id}.json",
        plan.model_dump(mode="json"),
    )
    schema_path = _stage_json_task_input(
        stage.local_stage,
        stage.remote_stage,
        f"task-{token}-merge-answers-schema-{plan.plan_id}.json",
        merge_contract.MergeAnswers.model_json_schema(),
    )
    return plan_path, schema_path


def _restaged_merge_master(
    stage: BranchMergeStage,
    session_master: tuple[str, str, bool] | None,
    values: dict[str, object] | None,
) -> MasterRef:
    """Point a merge continuation at the start contract this session completed a turn with."""

    if session_master is None:
        raise ValueError("A branch merge continuation has no start contract in its session.")
    label, content, _ = session_master
    return MasterRef(
        path=_stage_or_reuse_task_input(stage.local_stage, stage.remote_stage, label, content),
        bootstrap=False,
        values=values,
    )


def _merge_master_values(
    plan: merge_contract.MergePlan,
    *,
    plan_path: str,
    answers_schema_path: str,
    answers_path: str,
    validator_command: str,
    retained_answers: merge_contract.MergeAnswers,
    history_client: str,
) -> dict[str, object]:
    return {
        "merge_plan": plan_path,
        "merge_plan_id": plan.plan_id,
        "answers_schema": answers_schema_path,
        "answers_output": answers_path,
        "validator_command": validator_command,
        "retained_answers": retained_answers.model_dump(mode="json"),
        "history_client": history_client,
    }


def _changed_graph_rules(
    context: BranchMergeContext, session_master: tuple[str, str, bool] | None
) -> tuple[list[str], tuple[str, str, bool] | None]:
    """The current graph rules, only when main's ontology extensions differ from the last sent.

    The session record comes back updated to the mode now sent, so a later rebase that
    returns main to the master's mode sends the rules again.
    """

    current = _has_ontology_extensions(context.main_graph)
    if session_master is None or session_master[2] == current:
        return [], session_master
    return [graph_rules(edits=True, ontology_extensions=current)], (
        session_master[0],
        session_master[1],
        current,
    )


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
    hidden_read_scope: HiddenReadScope | None,
    invocation_gate: ProviderInvocationGate | None = None,
) -> tuple[_ProviderOutcome, AsyncGenerator[str, None]]:
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
        # The provider API treats write_dirs as admitted repository roots. The
        # scratch workspace is already the cwd/workspace root carried by
        # ProjectWriteScope; only a code merge admits repository roots.
        write_dirs=[Path(item) for item in write_scope.repository_roots],
        write_scope=write_scope,
        execution_host=write_scope.execution_host,
        execution=execution,
        remote_stage=stage.remote_stage,
        capability="orchestrate",
        invocation_gate=invocation_gate,
        outcome=provider_outcome,
        binary=binary,
        required_session_id=required_session_id,
        hidden_read_scope=hidden_read_scope,
        supervise_remote=stage.remote_stage is not None,
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
            "write_directory_count": 1 + len(write_scope.repository_roots),
            "repository_write_root_count": len(write_scope.repository_roots),
        },
    )
