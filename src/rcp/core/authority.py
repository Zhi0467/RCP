from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.core.models import (
    RELATION_SPEC,
    AuthorizedHuman,
    BranchMergeReceipt,
    GraphState,
    Patch,
    ValidationMessage,
)
from rcp.core.operations import (
    CreateAmbiguitiesOperation,
    CreateEdgesOperation,
    CreateNodesOperation,
    CreateProposalsOperation,
    GraphOperation,
    MergeNodesOperation,
    ProposalOperation,
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
from rcp.core.project_types import ProjectType, project_type_of
from rcp.core.research_rules import queues_decision
from rcp.core.transition_models import GraphTargetRef
from rcp.providers import AgentCapability

if TYPE_CHECKING:
    from rcp.core.validation.report import ValidationReport
    from rcp.core.validation.settings import ValidationContext


AGENT_GRAPH_AUTHORITY_POLICY_VERSION = "s115-v2"

AgentProfile = Literal["ordinary", "orchestrator"]
DispatchPatchKind = Literal["seed", "refresh", "work", "experiment_loop"]

#: Whether ``user_id`` is currently a member of ``project_id``. Membership is
#: operational state in SQLite, so the graph core takes it as a callable rather
#: than reaching for a store: replay never asks the question at all.
ProjectMembershipCheck = Callable[[str, str], bool]


class AgentAuthorizerDeparted(ValueError):
    """The live authorizer no longer holds project membership."""

    code = "agent_authorizer_departed"


class AgentDispatchScope(BaseModel):
    """The normalized concrete scope captured before an agent task can run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_truth_scope: list[str] = Field(default_factory=list)
    episode_id: str | None = Field(default=None, min_length=1)
    chat_scope: Literal["node", "project"] | None = None
    chat_id: str | None = Field(default=None, min_length=1)
    node_id: str | None = Field(default=None, min_length=1)
    patch_kind: DispatchPatchKind | None = None
    control_node_id: str | None = Field(default=None, min_length=1)
    control_episode_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def require_normalized_scope(self) -> AgentDispatchScope:
        if any(not item for item in self.run_truth_scope):
            raise ValueError("dispatch run_truth_scope entries must be non-empty")
        if self.run_truth_scope != sorted(set(self.run_truth_scope)):
            raise ValueError("dispatch run_truth_scope must be sorted and unique")
        if self.chat_scope is None and any(
            value is not None for value in (self.chat_id, self.node_id)
        ):
            raise ValueError("dispatch chat identity requires a chat scope")
        if self.chat_scope == "project" and self.node_id is not None:
            raise ValueError("project chat dispatch scope cannot name a node")
        controls = (self.control_node_id, self.control_episode_id)
        if self.patch_kind == "experiment_loop":
            if any(value is None for value in controls):
                raise ValueError(
                    "experiment-loop dispatch scope requires its control node and episode"
                )
        elif any(value is not None for value in controls):
            raise ValueError("only experiment-loop dispatch scope may name control state")
        return self


class AgentDispatchAuthority(BaseModel):
    """The immutable semantic/task binding admitted before provider execution."""

    model_config = ConfigDict(extra="forbid", strict=True)

    profile: AgentProfile
    task_contract: AgentCapability
    scope: AgentDispatchScope


class AgentTaskAuthority(BaseModel):
    """One project-scoped operational task binding resolved for live Apply."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    apply_target: GraphTargetRef
    authorized_by: AuthorizedHuman | None
    dispatch_authority: AgentDispatchAuthority | None
    episode_id: str | None = Field(default=None, min_length=1)
    merge_source: GraphTargetRef | None = None
    merge_dispatcher: Literal["member", "orchestrator"] | None = None

    @property
    def member_merge_source(self) -> GraphTargetRef | None:
        return self.merge_source if self.merge_dispatcher == "member" else None


ORDINARY_AGENT_TASK_CONTRACTS: frozenset[AgentCapability] = frozenset(
    {"discuss", "work_auto", "scratch_patch", "paper_readonly"}
)
ORCHESTRATOR_AGENT_TASK_CONTRACTS: frozenset[AgentCapability] = frozenset({"orchestrate"})
APPLY_CAPABLE_TASK_CONTRACTS: frozenset[AgentCapability] = frozenset(
    {"work_auto", "scratch_patch", "orchestrate"}
)


def _require_scope_shape(
    authority: AgentDispatchAuthority,
    *,
    action: Literal["dispatch", "apply"],
    member_merge_source: GraphTargetRef | None = None,
) -> None:
    contract = authority.task_contract
    scope = authority.scope
    if member_merge_source is not None and (
        member_merge_source.kind != "branch"
        or authority.profile != "orchestrator"
        or contract != "orchestrate"
    ):
        raise ValueError("member merge authority requires an exact branch source and orchestrator")
    if contract == "orchestrate":
        if (
            (scope.episode_id is None and member_merge_source is None)
            or not scope.run_truth_scope
            or scope.patch_kind != "work"
            or any(
                value is not None
                for value in (
                    scope.chat_scope,
                    scope.chat_id,
                    scope.node_id,
                    scope.control_node_id,
                    scope.control_episode_id,
                )
            )
        ):
            raise ValueError(
                f"Authority refused action {action!r}: orchestrate requires an exact episode, "
                "project-wide run scope, Work Patch kind, and no chat or control scope."
            )
        return
    if contract in {"discuss", "work_auto"}:
        if contract == "work_auto" and scope.episode_id is not None:
            if (
                not scope.run_truth_scope
                or scope.patch_kind != "work"
                or any(
                    value is not None
                    for value in (
                        scope.chat_scope,
                        scope.chat_id,
                        scope.node_id,
                        scope.control_node_id,
                        scope.control_episode_id,
                    )
                )
            ):
                raise ValueError(
                    f"Authority refused action {action!r}: an episode worker requires an exact "
                    "episode, project-wide run scope, Work Patch kind, and no chat or control "
                    "scope."
                )
            return
        if scope.episode_id is not None:
            raise ValueError(
                f"Authority refused action {action!r}: {contract} cannot carry episode scope."
            )
        if scope.chat_scope is None or scope.chat_id is None:
            raise ValueError(
                f"Authority refused action {action!r}: {contract} requires an exact chat scope "
                "and chat id."
            )
        if scope.chat_scope == "node" and scope.node_id is None:
            raise ValueError(
                f"Authority refused action {action!r}: node chat scope requires an exact node id."
            )
        if contract == "discuss" and scope.patch_kind is not None:
            raise ValueError(
                f"Authority refused action {action!r}: discuss cannot expose a graph Patch kind."
            )
        if contract == "work_auto" and scope.patch_kind not in {"work", "experiment_loop"}:
            raise ValueError(
                f"Authority refused action {action!r}: work_auto requires Work or Experiment scope."
            )
        return
    if contract == "scratch_patch":
        if scope.patch_kind not in {"seed", "refresh"}:
            raise ValueError(
                f"Authority refused action {action!r}: scratch_patch requires seed or refresh "
                "scope."
            )
        if any(
            value is not None
            for value in (scope.episode_id, scope.chat_scope, scope.chat_id, scope.node_id)
        ):
            raise ValueError(
                f"Authority refused action {action!r}: scratch_patch cannot carry chat identity."
            )
        return
    if contract == "paper_readonly" and any(
        value is not None
        for value in (
            scope.chat_scope,
            scope.chat_id,
            scope.node_id,
            scope.episode_id,
            scope.patch_kind,
            scope.control_node_id,
            scope.control_episode_id,
        )
    ):
        raise ValueError(
            f"Authority refused action {action!r}: paper_readonly cannot carry chat, Patch, or "
            "control scope."
        )


def require_dispatch(
    authority: AgentDispatchAuthority, *, member_merge_source: GraphTargetRef | None = None
) -> AgentDispatchAuthority:
    """Require one closed profile/task-contract pair for this exact dispatch."""

    compatible = (
        authority.profile == "ordinary" and authority.task_contract in ORDINARY_AGENT_TASK_CONTRACTS
    ) or (
        authority.profile == "orchestrator"
        and authority.task_contract in ORCHESTRATOR_AGENT_TASK_CONTRACTS
    )
    if not compatible:
        raise ValueError(
            "Authority refused action 'dispatch': "
            f"profile {authority.profile!r} does not permit task contract "
            f"{authority.task_contract!r}."
        )
    _require_scope_shape(authority, action="dispatch", member_merge_source=member_merge_source)
    return authority


def require_apply(
    authority: AgentTaskAuthority,
    patch: Patch,
    *,
    is_project_member: ProjectMembershipCheck,
) -> AgentDispatchAuthority:
    """Require a live task binding to expose this exact graph Patch at Apply.

    ``is_project_member`` is read live, under the canonical append lock, rather
    than snapshotted at dispatch: membership may be given up while a task runs.
    It is a required argument so no caller can reach Apply without it.
    """

    dispatch = authority.dispatch_authority
    if dispatch is None:
        raise ValueError(
            f"Authority refused action 'apply': agent task {authority.operation_id!r} has no "
            "dispatch authority binding."
        )
    if authority.authorized_by is None:
        raise ValueError(
            f"Authority refused action 'apply': agent task {authority.operation_id!r} has no "
            "authorizer snapshot."
        )
    if not is_project_member(authority.project_id, authority.authorized_by.user_id):
        raise AgentAuthorizerDeparted(
            f"Authority refused action 'apply': the authorizer of agent task "
            f"{authority.operation_id!r} is not a member of project "
            f"{authority.project_id!r}."
        )
    compatible = (
        dispatch.profile == "ordinary" and dispatch.task_contract in ORDINARY_AGENT_TASK_CONTRACTS
    ) or (
        dispatch.profile == "orchestrator"
        and dispatch.task_contract in ORCHESTRATOR_AGENT_TASK_CONTRACTS
    )
    if not compatible:
        raise ValueError(
            "Authority refused action 'apply': "
            f"task contract {dispatch.task_contract!r} exposes no graph Patch channel."
        )
    if patch.profile is not None and patch.profile != dispatch.profile:
        raise ValueError(
            "Authority refused action 'apply': Patch profile does not match the dispatch binding."
        )
    if patch.agent_action is not None and dispatch.profile != "orchestrator":
        raise ValueError(
            "Authority refused action 'apply': only the orchestrator dispatch binding may carry "
            "an agent authority action."
        )
    _require_scope_shape(
        dispatch, action="apply", member_merge_source=authority.member_merge_source
    )
    if dispatch.task_contract not in APPLY_CAPABLE_TASK_CONTRACTS:
        raise ValueError(
            "Authority refused action 'apply': "
            f"task contract {dispatch.task_contract!r} exposes no graph Patch channel."
        )
    scope = dispatch.scope
    compatible_scope = (
        dispatch.task_contract == "scratch_patch" and scope.patch_kind in {"seed", "refresh"}
    ) or (dispatch.task_contract == "work_auto" and scope.patch_kind in {"work", "experiment_loop"})
    compatible_scope = compatible_scope or (
        dispatch.task_contract == "orchestrate" and scope.patch_kind == "work"
    )
    if not compatible_scope:
        raise ValueError(
            "Authority refused action 'apply': task contract and Patch scope are incompatible."
        )
    if patch.kind != scope.patch_kind:
        raise ValueError(
            "Authority refused action 'apply': Patch kind does not match the dispatch scope."
        )
    if sorted(set(patch.run_truth_scope)) != scope.run_truth_scope:
        raise ValueError(
            "Authority refused action 'apply': Patch run_truth_scope does not match the "
            "dispatch scope."
        )
    if patch.kind == "experiment_loop" and (
        patch.experiment_control_node_id != scope.control_node_id
    ):
        raise ValueError(
            "Authority refused action 'apply': Experiment control node does not match the "
            "dispatch scope."
        )
    return dispatch


HYPOTHESIS_PROPOSAL_FIELDS = frozenset({"status"})
EVIDENCE_EDGE_CAUSE_KIND = "evidence_edge"
# Supersede and merge restructure any graph, so they are protected beside the
# project type's own structural relations.
_LIFECYCLE_RELATIONS = frozenset({"supersedes", "duplicate_of"})


def protected_epistemic_relations(state: GraphState | None = None) -> frozenset[str]:
    """Relations that restructure an existing protected belief."""

    return project_type_of(state).protected_relations | _LIFECYCLE_RELATIONS


PROTECTED_EPISTEMIC_RELATIONS = protected_epistemic_relations()

CONTENT_CHANGE_INTENT = "content_change"
REMOVAL_INTENT = "removal"
SUPERSEDE_INTENT = "supersede"
MERGE_INTENT = "merge"
PROTECTED_RELATION_CHANGE_INTENT = "protected_relation_change"
STATUS_CHANGE_INTENT = "status_change"
STANDING_CHANGE_INTENT = "standing_change"
ProposalIntent = Literal[
    "content_change",
    "removal",
    "supersede",
    "merge",
    "protected_relation_change",
    "status_change",
    "standing_change",
]
PROPOSAL_INTENTS = frozenset(get_args(ProposalIntent))

CREATE_NODE = "create_node"
UPDATE_NODE = "update_node"
UPDATE_PROTECTED_EPISTEMIC = "update_protected_epistemic"
DECIDE_DECISION = "decide_decision"
QUEUE_DECISION = "queue_decision"
REMOVE_NODE = "remove_node"
REMOVE_PROTECTED_EPISTEMIC = "remove_protected_epistemic"
SUPERSEDE_NODE = "supersede_node"
SUPERSEDE_PROTECTED_EPISTEMIC = "supersede_protected_epistemic"
MERGE_NODE = "merge_node"
MERGE_PROTECTED_EPISTEMIC = "merge_protected_epistemic"
CREATE_EDGE = "create_edge"
REMOVE_EDGE = "remove_edge"
RESTRUCTURE_PROTECTED_EPISTEMIC = "restructure_protected_epistemic"
SET_STANDING = "set_standing"
CREATE_PROPOSAL = "create_proposal"
RESOLVE_PROPOSAL = "resolve_proposal"
WITHDRAW_PROPOSAL = "withdraw_proposal"
UPSERT_GLOSSARY = "upsert_glossary"
SET_COVERAGE = "set_coverage"
SET_PROJECT_TRUTH_SCOPE = "set_project_truth_scope"
SET_ONTOLOGY = "set_ontology"
CREATE_AMBIGUITY = "create_ambiguity"
RESOLVE_AMBIGUITY = "resolve_ambiguity"

GraphAction = Literal[
    "create_node",
    "update_node",
    "update_protected_epistemic",
    "decide_decision",
    "queue_decision",
    "remove_node",
    "remove_protected_epistemic",
    "supersede_node",
    "supersede_protected_epistemic",
    "merge_node",
    "merge_protected_epistemic",
    "create_edge",
    "remove_edge",
    "restructure_protected_epistemic",
    "set_standing",
    "create_proposal",
    "resolve_proposal",
    "withdraw_proposal",
    "upsert_glossary",
    "set_coverage",
    "set_project_truth_scope",
    "set_ontology",
    "create_ambiguity",
    "resolve_ambiguity",
]

GRAPH_ACTIONS = frozenset(get_args(GraphAction))

ORDINARY_AGENT_GRAPH_ACTIONS: frozenset[GraphAction] = frozenset(
    {
        CREATE_NODE,
        UPDATE_NODE,
        QUEUE_DECISION,
        REMOVE_NODE,
        SUPERSEDE_NODE,
        MERGE_NODE,
        CREATE_EDGE,
        REMOVE_EDGE,
        CREATE_PROPOSAL,
        WITHDRAW_PROPOSAL,
        UPSERT_GLOSSARY,
    }
)

ORCHESTRATOR_AGENT_GRAPH_ACTIONS: frozenset[GraphAction] = frozenset(
    {*ORDINARY_AGENT_GRAPH_ACTIONS, DECIDE_DECISION, SET_STANDING}
)

HUMAN_GRAPH_ACTIONS = GRAPH_ACTIONS - {
    CREATE_PROPOSAL,
    UPSERT_GLOSSARY,
    SET_COVERAGE,
    CREATE_AMBIGUITY,
    RESOLVE_AMBIGUITY,
}


def _code_list(names: Iterable[str]) -> str:
    words = [f"`{name}`" for name in names]
    if len(words) <= 2:
        return " or ".join(words)
    return f"{', '.join(words[:-1])}, or {words[-1]}"


def _render_agent_graph_authority_body(project_type: ProjectType) -> str:
    """Render the authority rules with the type names enforcement resolves."""

    def names(node_types: Iterable[str]) -> str:
        return project_type.label_list(node_types)

    def one(node_types: Iterable[str]) -> str:
        label = names(node_types)
        return f"{'an' if label[:1].lower() in 'aeiou' else 'a'} {label}"

    chooser = names(project_type.chooser_types)
    belief = names(project_type.belief_types)
    outcome = names(project_type.outcome_types)
    control = names(project_type.control_node_types)
    a_chooser = one(project_type.chooser_types)
    a_control = one(project_type.control_node_types)
    protected = names(project_type.protected_belief_types)
    content_lifecycle = names(project_type.protected_belief_types - project_type.belief_types)
    protected_relations = project_type.protected_relations | _LIFECYCLE_RELATIONS
    relations = _code_list(
        relation for relation in RELATION_SPEC if relation in protected_relations
    )
    return f"""Assert directly:
- Ordinary legal graph structure and content are assertions, not Proposals, outside the protected
  changes below. Agents may create legal nodes, edit same-Patch nodes, and edit ordinary nodes.
- Editing accepted ordinary-node content resets that node to asserted standing for review. Removing
  an asserted or contested ordinary node also removes its edges; never directly remove an accepted
  node or {a_control} with an active bounded-loop attempt.
- Agents may create {a_chooser} as `open` or `ready`, and may queue an existing {chooser} as `open`,
  `ready`, or `revisit`. Agents never write `selected_option` or set `status="decided"`; every
  agent-created {belief} starts `status="proposed"`.
- Legal edges are direct except {relations}
  restructuring an existing belief. Same-Patch nodes and {outcome}/{control} relations stay direct.
- Agents may remove, supersede, or merge ordinary nodes.
- Add or revise thin project-wide definitions with `upsert_glossary`; terms explain prose, not claims.
Proposal-only changes:
- Any edit, removal, supersede, merge, or protected relation change involving an existing
  {protected} waits for a human. Put exactly one semantic operation in the
  Proposal and declare its `intent` as `content_change`, `removal`, `supersede`, `merge`,
  `protected_relation_change`, or `status_change`; never bundle separate changes.
- `status_change` updates exactly one {belief}'s `status`, including one created earlier in the
  same outer Patch, and requires `kind="evidence_edge"` naming a valid {outcome} -> {belief}
  epistemic edge. A {content_lifecycle} lifecycle change uses `content_change`, like its other
  human-held fields, and carries no evidence cause. Other intents likewise carry their reasoning in
  the Proposal card and do not carry a cause.
Human-only authority:
- Agents never set `standing`, approve, or reject Proposals; they may withdraw any pending Proposal
  with `withdraw_proposals` when obsolete or duplicated. Withdrawal applies no semantic operations.
  Agents may not change project configuration or the ontology, and may neither apply nor propose
  `set_ontology`. Agents may not authorize {a_control} **Run**. Approval never
  launches or resumes {a_control}. Only the human pressing **Run** grants RCP permission to
  launch. A human request cannot delegate these actions."""


_AGENT_GRAPH_AUTHORITY_BODY = _render_agent_graph_authority_body(project_type_of())

AGENT_GRAPH_AUTHORITY_POLICY_DIGEST = sha256(
    _AGENT_GRAPH_AUTHORITY_BODY.encode("utf-8")
).hexdigest()[:16]


def render_agent_graph_authority_contract() -> str:
    """Return the one model-facing authority block shared by graph-capable tasks."""

    return (
        "Agent graph authority contract:\n"
        f"- Policy version: `{AGENT_GRAPH_AUTHORITY_POLICY_VERSION}`\n"
        f"- Policy digest: `{AGENT_GRAPH_AUTHORITY_POLICY_DIGEST}`\n"
        f"{_AGENT_GRAPH_AUTHORITY_BODY}"
    )


def operation_actions(
    state: GraphState,
    patch: Patch,
    operation: GraphOperation,
) -> frozenset[GraphAction]:
    """Derive the closed authority action set for one operation.

    ``state`` is the graph from before the outer Patch. The whole Patch is also
    supplied so a node created in that Patch is never mistaken for an existing
    protected belief. A batch may require more than one action when its targets
    cross action boundaries.
    """

    if isinstance(operation, CreateNodesOperation):
        return frozenset({CREATE_NODE})
    if isinstance(operation, UpdateNodesOperation):
        return _update_actions(state, patch, operation)
    if isinstance(operation, CreateEdgesOperation):
        return _create_edge_actions(state, patch, operation)
    if isinstance(operation, RemoveEdgesOperation):
        return _remove_edge_actions(state, patch, operation)
    if isinstance(operation, RemoveNodesOperation):
        return _node_target_actions(
            state,
            patch,
            operation.node_ids,
            REMOVE_NODE,
            REMOVE_PROTECTED_EPISTEMIC,
        )
    if isinstance(operation, SupersedeNodesOperation):
        return _node_lifecycle_actions(
            state,
            patch,
            (item.id for item in operation.nodes),
            (
                (item.id, item.superseded_by)
                for item in operation.nodes
                if item.superseded_by is not None
            ),
            "supersedes",
            SUPERSEDE_NODE,
            SUPERSEDE_PROTECTED_EPISTEMIC,
        )
    if isinstance(operation, MergeNodesOperation):
        return _node_lifecycle_actions(
            state,
            patch,
            (item.duplicate for item in operation.merges),
            ((item.duplicate, item.canonical) for item in operation.merges),
            "duplicate_of",
            MERGE_NODE,
            MERGE_PROTECTED_EPISTEMIC,
        )
    if isinstance(operation, SetStandingOperation):
        if is_existing_protected_node(state, operation.node_id) or project_type_of(
            state
        ).is_protected_belief(_created_node_type(patch, operation.node_id)):
            return frozenset({UPDATE_PROTECTED_EPISTEMIC})
        return frozenset({SET_STANDING})
    if isinstance(operation, CreateAmbiguitiesOperation):
        return frozenset({CREATE_AMBIGUITY})
    if isinstance(operation, ResolveAmbiguitiesOperation):
        return frozenset({RESOLVE_AMBIGUITY})
    if isinstance(operation, CreateProposalsOperation):
        return frozenset({CREATE_PROPOSAL})
    if isinstance(operation, ResolveProposalsOperation):
        return frozenset({RESOLVE_PROPOSAL})
    if isinstance(operation, WithdrawProposalsOperation):
        return frozenset({WITHDRAW_PROPOSAL})
    if isinstance(operation, UpsertGlossaryOperation):
        return frozenset({UPSERT_GLOSSARY})
    if isinstance(operation, SetCoverageOperation):
        return frozenset({SET_COVERAGE})
    if isinstance(operation, SetProjectTruthScopeOperation):
        return frozenset({SET_PROJECT_TRUTH_SCOPE})
    if isinstance(operation, SetOntologyOperation):
        return frozenset({SET_ONTOLOGY})
    raise ValueError(f"Unknown graph operation {getattr(operation, 'op', None)!r}.")


def is_existing_protected_node(state: GraphState, node_id: str) -> bool:
    """Whether ``node_id`` names a pre-Patch protected belief."""

    node = state.nodes.get(node_id)
    return node is not None and project_type_of(state).is_protected_belief(node.type)


def permits(patch: Patch, action: GraphAction) -> bool:
    """Whether the Patch producer may perform ``action`` at admission time."""

    if action not in GRAPH_ACTIONS:
        raise ValueError(f"Unknown graph action {action!r}.")
    if patch.author == "human":
        return action in HUMAN_GRAPH_ACTIONS
    if patch.author != "agent" or patch.profile not in {None, "ordinary", "orchestrator"}:
        return False
    if patch.profile == "orchestrator":
        return action in ORCHESTRATOR_AGENT_GRAPH_ACTIONS
    return action in ORDINARY_AGENT_GRAPH_ACTIONS


def _update_actions(
    state: GraphState,
    patch: Patch,
    operation: UpdateNodesOperation,
) -> frozenset[GraphAction]:
    project_type = project_type_of(state)
    actions: set[GraphAction] = set()
    for update in operation.nodes:
        node_id = update.id
        if is_existing_protected_node(state, node_id):
            actions.add(UPDATE_PROTECTED_EPISTEMIC)
            continue
        node = state.nodes.get(node_id)
        existing_chooser = node is not None and project_type.is_chooser(node.type)
        is_chooser = existing_chooser or project_type.is_chooser(_created_node_type(patch, node_id))
        if is_chooser and (
            patch.human_action == "decision_choice" or patch.agent_action == "decision_choice"
        ):
            actions.add(DECIDE_DECISION)
        elif existing_chooser and queues_decision(update.changes):
            actions.add(QUEUE_DECISION)
        else:
            actions.add(UPDATE_NODE)
    return frozenset(actions or {UPDATE_NODE})


def _created_node_type(patch: Patch, node_id: str) -> str | None:
    for operation in patch.ops:
        if not isinstance(operation, CreateNodesOperation):
            continue
        for node in operation.nodes:
            if node.id == node_id:
                return node.type
    return None


def _create_edge_actions(
    state: GraphState,
    patch: Patch,
    operation: CreateEdgesOperation,
) -> frozenset[GraphAction]:
    actions: set[GraphAction] = set()
    new_node_ids = created_node_ids(patch) - set(state.nodes)
    for edge in operation.edges:
        endpoints = (edge.source, edge.target)
        restructures = _restructures_protected_relation(
            state,
            patch,
            edge.relation,
            endpoints,
            new_node_ids=new_node_ids,
        )
        actions.add(RESTRUCTURE_PROTECTED_EPISTEMIC if restructures else CREATE_EDGE)
    return frozenset(actions or {CREATE_EDGE})


def _remove_edge_actions(
    state: GraphState,
    patch: Patch,
    operation: RemoveEdgesOperation,
) -> frozenset[GraphAction]:
    actions: set[GraphAction] = set()
    new_edge_ids = created_edge_ids(patch) - set(state.edges)
    for edge_id in operation.edge_ids:
        edge = state.edges.get(edge_id)
        restructures = (
            edge is not None
            and edge_id not in new_edge_ids
            and edge.relation in protected_epistemic_relations(state)
            and any(
                is_existing_protected_node(state, node_id) for node_id in (edge.source, edge.target)
            )
        )
        actions.add(RESTRUCTURE_PROTECTED_EPISTEMIC if restructures else REMOVE_EDGE)
    return frozenset(actions or {REMOVE_EDGE})


def _node_target_actions(
    state: GraphState,
    patch: Patch,
    node_ids: Iterable[str],
    base: GraphAction,
    protected: GraphAction,
) -> frozenset[GraphAction]:
    actions = {
        protected if is_existing_protected_node(state, node_id) else base for node_id in node_ids
    }
    return frozenset(actions or {base})


def _node_lifecycle_actions(
    state: GraphState,
    patch: Patch,
    node_ids: Iterable[str],
    generated_edge_endpoints: Iterable[tuple[str, str]],
    generated_relation: str,
    base: GraphAction,
    protected: GraphAction,
) -> frozenset[GraphAction]:
    """Include both lifecycle and implicit protected-relation authority."""

    actions = set(_node_target_actions(state, patch, node_ids, base, protected))
    new_node_ids = created_node_ids(patch) - set(state.nodes)
    for endpoints in generated_edge_endpoints:
        if _restructures_protected_relation(
            state,
            patch,
            generated_relation,
            endpoints,
            new_node_ids=new_node_ids,
        ):
            actions.add(RESTRUCTURE_PROTECTED_EPISTEMIC)
    return frozenset(actions)


def _restructures_protected_relation(
    state: GraphState,
    patch: Patch,
    relation: str,
    endpoints: tuple[str, str],
    *,
    new_node_ids: set[str],
) -> bool:
    return (
        len(endpoints) == 2
        and relation in protected_epistemic_relations(state)
        and not new_node_ids.intersection(endpoints)
        and any(is_existing_protected_node(state, node_id) for node_id in endpoints)
    )


def created_node_ids(patch: Patch) -> set[str]:
    return {
        node.id
        for operation in patch.ops
        if isinstance(operation, CreateNodesOperation)
        for node in operation.nodes
    }


def created_edge_ids(patch: Patch) -> set[str]:
    edge_ids: set[str] = set()
    for operation in patch.ops:
        if not isinstance(operation, CreateEdgesOperation):
            continue
        for edge in operation.edges:
            edge_ids.add(edge.id or f"{edge.source}::{edge.relation}::{edge.target}")
    return edge_ids


@dataclass(frozen=True)
class Writer:
    """Resolved role used by staged rules; no live identity or task lookups."""

    author: Literal["human", "agent"] | None
    profile: AgentProfile | None
    human_action: str | None = None
    agent_action: str | None = None

    @classmethod
    def from_patch(cls, patch: Patch) -> Writer:
        return cls(patch.author, patch.profile, patch.human_action, patch.agent_action)


def validate_authorship(patch: Patch, report: ValidationReport) -> None:
    """Persisted producer/kind consistency, shared by admission and replay."""
    revision = patch.revision or None
    if patch.kind == "identity":
        if patch.producer != "system":
            report.reject(
                "wrong-producer",
                "Identity patches must be produced by RCP's system producer.",
                revision,
            )
        if patch.author is not None:
            report.reject(
                "wrong-author",
                "Identity patches have no human or agent author.",
                revision,
            )
        return

    expected_author = "human" if patch.kind == "approval" else "agent"
    if patch.author != expected_author:
        report.reject(
            "wrong-author",
            f"{patch.kind} patches must be authored by {expected_author}.",
            revision,
        )
    if patch.producer == "system":
        report.reject(
            "system-producer-forbidden",
            "The system producer is reserved for identity patches.",
            revision,
        )
    elif patch.producer != patch.author:
        report.reject(
            "producer-author-mismatch",
            "Human and agent patches must retain the same producer and legacy author role.",
            revision,
        )


def validate_operation_authority(state: GraphState, patch: Patch, report: ValidationReport) -> None:
    """Profile-only feedback, evaluated against the pre-Patch graph."""

    from rcp.core.validation.registry import OP_RULES

    for operation in patch.ops:
        # Historical-only operations have no live channel. Gate 2 retains their
        # legacy-only-operation shape rejection before any graph rule runs.
        if OP_RULES[operation.op].legacy_only:
            continue
        for action in sorted(operation_actions(state, patch, operation)):
            if not permits(patch, action):
                report.reject(
                    "graph-action-refused",
                    f"Action {action!r} is not permitted for this Patch producer.",
                    patch.revision or None,
                )


def validate_writer_actions(
    state: GraphState, patch: Patch, report: ValidationReport, *, replay: bool = False
) -> None:
    """Check action declarations that depend only on the writer's role.

    Replay skips the two checks that were always admission-only, so older
    history that predates them still materializes.
    """

    revision = patch.revision or None
    if patch.human_action is not None and (patch.kind != "approval" or patch.author != "human"):
        report.reject(
            "invalid-human-action",
            "Only a human approval Patch may declare a human authority action.",
            revision,
        )
    if patch.agent_action is not None and (
        patch.author != "agent" or patch.profile != "orchestrator"
    ):
        report.reject(
            "invalid-agent-action",
            "Only the orchestrator profile may declare an agent authority action.",
            revision,
        )

    # These checks depend on the writer, never on written-order staging.
    for operation in patch.ops:
        if operation.op == "resolve_proposals" and patch.kind != "approval" and not replay:
            report.reject(
                "agent-resolved-proposal",
                "Only a human approval patch may resolve or withdraw a Proposal.",
                revision,
            )
        elif operation.op == "withdraw_proposals" and (
            patch.author != "agent" or patch.kind == "approval"
        ):
            report.reject(
                "human-only-proposal-withdrawal",
                "Only an agent patch may withdraw a Proposal.",
                revision,
            )
        elif operation.op == "set_standing" and not replay and not permits(patch, "set_standing"):
            report.reject(
                "agent-set-standing", "This Patch producer may not set standing.", revision
            )
        elif operation.op == "set_project_truth_scope" and patch.kind != "approval":
            report.reject(
                "agent-set-project-scope",
                "Project truth-scope membership requires human approval.",
                revision,
            )
        elif operation.op == "set_ontology" and patch.kind != "approval":
            report.reject(
                "agent-set-ontology",
                "Only the human Settings and Sync paths may change ontology.",
                revision,
            )


def _authorship_rejected(patch: Patch) -> bool:
    from rcp.core.validation.report import ValidationReport

    report = ValidationReport()
    validate_authorship(patch, report)
    return report.rejected


#: A refused authorizer never enters canonical history, like a departed agent
#: authorizer, whatever the caller's discard choice.
HUMAN_AUTHORIZER_NOT_MEMBER = "human-authorizer-not-member"


def refused_authorizer(report: ValidationReport) -> bool:
    """Whether admission refused the human authorizer, so nothing may be recorded."""

    return any(item.code == HUMAN_AUTHORIZER_NOT_MEMBER for item in report.messages)


class WriteAuthority:
    """Live admission authority. Without resolvers, only system/history use is allowed."""

    def __init__(
        self,
        *,
        agent_authority_resolver: Callable[[str, str], AgentTaskAuthority] | None = None,
        project_membership_check: ProjectMembershipCheck | None = None,
    ) -> None:
        self.agent_authority_resolver = agent_authority_resolver
        self.project_membership_check = project_membership_check

    def validate_permissions(self, state: GraphState, patch: Patch) -> ValidationReport:
        from rcp.core.validation.report import ValidationReport

        report = ValidationReport()
        try:
            validate_operation_authority(state, patch, report)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            report.reject(
                "malformed-operation",
                f"Patch operations are malformed: {exc}.",
                patch.revision or None,
            )
        return report

    def admit(
        self,
        state: GraphState,
        patch: Patch,
        *,
        project_id: str | None,
        authorized_by: AuthorizedHuman | None,
        apply_target: GraphTargetRef,
    ) -> tuple[Patch, Writer, ValidationReport]:
        from rcp.core.validation.report import ValidationReport

        report = ValidationReport()
        # A Patch whose author does not match its kind is never stamped; Gate 2
        # reports the persisted-shape rejection.
        if _authorship_rejected(patch):
            return patch, Writer.from_patch(patch), report
        patch = self.stamp_attribution(
            patch, project_id=project_id, authorized_by=authorized_by, apply_target=apply_target
        )
        if patch.kind == "approval" and (
            project_id is None
            or self.project_membership_check is None
            or patch.authorized_by is None
            or not self.project_membership_check(project_id, patch.authorized_by.user_id)
        ):
            report.reject(
                HUMAN_AUTHORIZER_NOT_MEMBER,
                "The authorizing human is not a current project member.",
                patch.revision or None,
            )
            return patch, Writer.from_patch(patch), report
        report = self.validate_permissions(state, patch)
        return patch, Writer.from_patch(patch), report

    def stamp_attribution(
        self,
        patch: Patch,
        *,
        project_id: str | None,
        authorized_by: AuthorizedHuman | None,
        apply_target: GraphTargetRef,
    ) -> Patch:
        if patch.kind == "identity":
            if authorized_by is not None or any(
                value is not None
                for value in (
                    patch.authorized_by,
                    patch.profile,
                    patch.task_id,
                    patch.episode_id,
                )
            ):
                raise ValueError("identity patches are system-owned and cannot carry attribution")
            return patch

        if patch.kind == "approval":
            if authorized_by is None:
                raise ValueError(
                    "human approval patches require an explicit authorized_by snapshot"
                )
            authorizer = self._canonical_authorizer(authorized_by)
            return patch.model_copy(
                update={
                    "authorized_by": authorizer,
                    "profile": None,
                    "task_id": None,
                    "episode_id": None,
                }
            )

        if authorized_by is not None:
            raise ValueError("explicit authorized_by is only valid for human approval patches")
        operation_id = patch.source_operation_id
        if not operation_id or not operation_id.strip():
            raise ValueError("agent patches require a non-empty direct source_operation_id")
        if (
            project_id is None
            or self.agent_authority_resolver is None
            or self.project_membership_check is None
        ):
            raise ValueError(
                "agent attribution requires a canonical project-scoped agent_authority_resolver "
                "and project_membership_check"
            )
        try:
            resolved = self.agent_authority_resolver(project_id, operation_id)
        except KeyError as exc:
            raise ValueError(f"unknown agent task {operation_id!r}") from exc
        if resolved.operation_id != operation_id or resolved.project_id != project_id:
            raise ValueError("agent authority resolver returned another task or project")
        if resolved.apply_target != apply_target:
            raise ValueError(
                f"agent task {operation_id!r} is authorized to Apply to "
                f"{resolved.apply_target.key}, not {apply_target.key}"
            )
        dispatch = require_apply(resolved, patch, is_project_member=self.project_membership_check)
        if (
            apply_target != GraphTargetRef()
            and patch.kind == "experiment_loop"
            and resolved.episode_id is None
        ):
            raise ValueError("Experiment-loop patches require canonical episode attribution")
        if (
            dispatch.profile == "orchestrator"
            and resolved.episode_id is None
            and resolved.member_merge_source is None
        ):
            raise ValueError("orchestrator agent tasks require a canonical episode_id")
        assert resolved.authorized_by is not None
        authorizer = self._canonical_authorizer(resolved.authorized_by)
        canonical = (authorizer, dispatch.profile, operation_id, resolved.episode_id)
        supplied = (patch.authorized_by, patch.profile, patch.task_id, patch.episode_id)
        if any(value is not None for value in supplied) and supplied != canonical:
            raise ValueError(
                "agent patch attribution does not match the canonical task attribution"
            )
        return patch.model_copy(
            update={
                "authorized_by": authorizer,
                "profile": dispatch.profile,
                "task_id": operation_id,
                "episode_id": resolved.episode_id,
            }
        )

    @staticmethod
    def _canonical_authorizer(authorizer: AuthorizedHuman) -> AuthorizedHuman:
        try:
            snapshot = AuthorizedHuman.model_validate(authorizer.model_dump(mode="python"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("authorized_by must be a valid authorizer snapshot") from exc
        if not snapshot.display_name.strip():
            raise ValueError("authorized_by must name the authorizing human")
        return snapshot

    def require_no_change_merge(
        self, receipt: BranchMergeReceipt, *, project_id: str | None
    ) -> None:
        """Apply the live task and membership gate before admitting a no-change receipt."""

        operation_id = receipt.provenance.merge_task_id
        if (
            project_id is None
            or self.agent_authority_resolver is None
            or self.project_membership_check is None
        ):
            raise ValueError(
                "branch merge receipt attribution requires a canonical project-scoped "
                "agent_authority_resolver and project_membership_check"
            )
        try:
            resolved = self.agent_authority_resolver(project_id, operation_id)
        except KeyError as exc:
            raise ValueError(f"unknown agent task {operation_id!r}") from exc
        if resolved.operation_id != operation_id or resolved.project_id != project_id:
            raise ValueError("agent authority resolver returned another task or project")
        if resolved.apply_target != receipt.provenance.rebased_main_head.target:
            raise ValueError("a branch merge receipt requires its recorded destination authority")
        if resolved.merge_source is None:
            if resolved.episode_id is None or resolved.episode_id != receipt.provenance.episode_id:
                raise ValueError("a legacy branch merge receipt requires its recorded episode")
        elif resolved.merge_source != GraphTargetRef(
            kind="branch", branch_id=receipt.provenance.branch_id
        ):
            raise ValueError("a branch merge receipt requires its recorded source authority")
        dispatch = resolved.dispatch_authority
        if dispatch is None:
            raise ValueError("branch merge receipt task has no dispatch authority binding")
        probe = Patch(
            kind="work",
            author="agent",
            producer="agent",
            summary="Validate no-change branch merge receipt authority.",
            source_operation_id=operation_id,
            run_truth_scope=list(dispatch.scope.run_truth_scope),
            repositories_read=[],
            profile="orchestrator",
            task_id=operation_id,
            episode_id=resolved.episode_id,
            authorized_by=receipt.authorized_by,
            ops=[],
        )
        bound = require_apply(
            resolved,
            probe,
            is_project_member=self.project_membership_check,
        )
        if bound.profile != "orchestrator":
            raise ValueError("a branch merge receipt requires orchestrator authority")
        if resolved.authorized_by is None:
            raise ValueError("branch merge receipt task has no authorizer snapshot")
        if self._canonical_authorizer(resolved.authorized_by) != receipt.authorized_by:
            raise ValueError("branch merge receipt authorizer does not match its task authority")


# These are the only Gate-1 refusals which can be represented as pending review.
# Configuration, task, membership, and every other action remain hard refusals.
_REVIEWABLE_GRAPH_ACTIONS: frozenset[GraphAction] = frozenset(
    {
        UPDATE_PROTECTED_EPISTEMIC,
        REMOVE_PROTECTED_EPISTEMIC,
        SUPERSEDE_PROTECTED_EPISTEMIC,
        MERGE_PROTECTED_EPISTEMIC,
        RESTRUCTURE_PROTECTED_EPISTEMIC,
    }
)


@dataclass(frozen=True)
class ChangeClassification:
    """One atomic direct change and the exact result of the core gates.

    ``review_operations`` is populated only after the supported pending Proposal
    form passes validation. It never authorizes applying that effect directly.
    Diagnostics retain their codes and related resources; flags are not refusals.
    """

    operation_indices: tuple[int, ...]
    disposition: Literal["admitted", "reviewable", "refused"]
    review_operations: tuple[ProposalOperation, ...] = ()
    diagnostics: tuple[ValidationMessage, ...] = ()


class CapturedChangeAuthority(BaseModel):
    """Serializable preflight authority for pure planning, never publication.

    ``capture_change_authority`` obtains this snapshot from live admission. The
    admitted Patch template has no operations. Consumers may replace only ops;
    its complete attribution, scope, target provenance, and action declaration
    stay bound. Preparation and publication must check live authority again.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str | None
    apply_target: GraphTargetRef
    patch_template: Patch

    @model_validator(mode="after")
    def require_empty_template(self) -> CapturedChangeAuthority:
        if self.patch_template.ops:
            raise ValueError("captured authority requires an empty-operation Patch template")
        return self


def capture_change_authority(
    state: GraphState,
    patch_template: Patch,
    *,
    authority: WriteAuthority,
    project_id: str | None,
    apply_target: GraphTargetRef,
    authorized_by: AuthorizedHuman | None = None,
) -> CapturedChangeAuthority:
    """Run live admission once before passing an immutable envelope to a planner.

    Refusals propagate; no failed task, membership, or authorship check produces
    a capture. This checks authority only: the planner's later operations still
    need both permission and legality classification, and final live preparation.
    """
    if patch_template.ops:
        raise ValueError("authority capture requires an empty-operation Patch template")
    admitted, _, report = authority.admit(
        state,
        patch_template,
        project_id=project_id,
        authorized_by=authorized_by,
        apply_target=apply_target,
    )
    validate_authorship(admitted, report)
    if report.rejected:
        raise ValueError(
            "; ".join(item.message for item in report.messages if item.level == "reject")
        )
    return CapturedChangeAuthority(
        project_id=project_id,
        apply_target=apply_target,
        patch_template=admitted,
    )


def classify_graph_changes(
    state: GraphState,
    patch: Patch,
    *,
    authority: WriteAuthority | CapturedChangeAuthority,
    project_id: str | None,
    apply_target: GraphTargetRef,
    context: ValidationContext,
    authorized_by: AuthorizedHuman | None = None,
    operation_groups: tuple[tuple[int, ...], ...] | None = None,
) -> tuple[ChangeClassification, ...]:
    """Classify coherent direct operation groups without applying any effect.

    A live WriteAuthority resolves task/profile/authorizer authority first.
    A CapturedChangeAuthority instead verifies the exact preflight envelope for
    pure planning; it never substitutes for live preparation. Failure of either
    binding refuses every group, before any Proposal conversion. By default
    the entire Patch is one atomic change: creation plus connecting edges, or an
    edge removal plus replacement, must not be judged as isolated operations.
    Explicit groups must partition the operations in written order. Earlier
    admitted groups provide staged context; pending review effects do not.

    Both gates judge direct changes. Only protected action refusals and codes
    explicitly owned as reviewable by operation rules are eligible for a
    supported Proposal form, which must itself pass both gates. Malformed data,
    missing dependencies, and invalid proposed effects remain exact structured
    refusals. This function does not repair invalid encodings or invent causes;
    the builder must supply the correct ordered operations and truthful cause.
    A final assembled Patch still requires authoritative transition preparation.
    """
    from rcp.core.materialize import apply_valid_patch, prepare_patch_bookkeeping
    from rcp.core.models import GatedCard, Proposal
    from rcp.core.validation import validate_patch
    from rcp.core.validation.ops import REVIEWABLE_OPERATION_REFUSALS

    groups = operation_groups if operation_groups is not None else (tuple(range(len(patch.ops))),)
    if operation_groups is not None and any(not group for group in groups):
        raise ValueError("operation_groups cannot contain empty groups")
    if tuple(index for group in groups for index in group) != tuple(range(len(patch.ops))):
        raise ValueError("operation_groups must partition Patch operations in written order")
    if context.mode != "admission":
        raise ValueError("change classification requires admission validation")
    try:
        if isinstance(authority, CapturedChangeAuthority):
            if (
                project_id != authority.project_id
                or apply_target != authority.apply_target
                or patch.model_dump(exclude={"ops"})
                != authority.patch_template.model_dump(exclude={"ops"})
                or (authorized_by is not None and authorized_by != patch.authorized_by)
            ):
                raise ValueError("candidate Patch does not match captured planning authority")
            permission_gate = WriteAuthority()
            writer = Writer.from_patch(patch)
            admission = permission_gate.validate_permissions(state, patch)
        else:
            permission_gate = authority
            patch, writer, admission = authority.admit(
                state,
                patch,
                project_id=project_id,
                authorized_by=authorized_by,
                apply_target=apply_target,
            )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        diagnostic = ValidationMessage(
            level="reject",
            code=exc.code
            if isinstance(exc, AgentAuthorizerDeparted)
            else "write-authority-refused",
            message=str(exc),
            patch_revision=patch.revision or None,
        )
        return tuple(
            ChangeClassification(group, "refused", diagnostics=(diagnostic,)) for group in groups
        )
    # Operation refusals are classified below; any independent admission refusal
    # (for example lost human membership) can never become a pending Proposal.
    admission_refusals = tuple(
        item
        for item in admission.messages
        if item.level == "reject" and item.code != "graph-action-refused"
    )
    if admission_refusals:
        return tuple(
            ChangeClassification(group, "refused", diagnostics=admission_refusals)
            for group in groups
        )

    admitted_ops: list[GraphOperation] = []
    admitted_indices: list[int] = []
    staged = state
    results: list[ChangeClassification] = []
    for group in groups:
        operations = [patch.ops[index] for index in group]
        candidate_indices = (*admitted_indices, *group)
        candidate = patch.model_copy(update={"ops": [*admitted_ops, *operations]})
        permission = permission_gate.validate_permissions(state, candidate)
        refused_actions = {
            action
            for operation in operations
            for action in operation_actions(state, candidate, operation)
            if not permits(candidate, action)
        }
        if permission.rejected:
            diagnostics = _classification_diagnostics(permission.messages, candidate_indices)
            reviewable = bool(refused_actions) and refused_actions <= _REVIEWABLE_GRAPH_ACTIONS
        else:
            report = validate_patch(state, candidate, context, writer=writer)
            diagnostics = _classification_diagnostics(report.messages, candidate_indices)
            if not report.rejected:
                results.append(ChangeClassification(group, "admitted", diagnostics=diagnostics))
                admitted_ops.extend(operations)
                admitted_indices.extend(group)
                staged = apply_valid_patch(state, candidate)
                continue
            reviewable = all(
                item.code in REVIEWABLE_OPERATION_REFUSALS
                for item in report.messages
                if item.level == "reject"
            )
        if not reviewable:
            results.append(ChangeClassification(group, "refused", diagnostics=diagnostics))
            continue
        try:
            review_ops = _review_operations(staged, operations)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            diagnostic = ValidationMessage(
                level="reject",
                code="malformed-operation",
                message=f"Patch operations are malformed: {exc}.",
                patch_revision=patch.revision or None,
            )
            results.append(ChangeClassification(group, "refused", diagnostics=(diagnostic,)))
            continue
        if review_ops is None:
            results.append(ChangeClassification(group, "refused", diagnostics=diagnostics))
            continue
        # A stable private probe id avoids collisions with actual pending cards.
        probe_id = "proposal/core-change-classification"
        while probe_id in state.proposals:
            probe_id += "-probe"
        proposal = Proposal(
            id=probe_id, title="Review change", card=GatedCard(), ops=list(review_ops)
        )
        review_candidate = prepare_patch_bookkeeping(
            state,
            patch.model_copy(
                update={
                    "ops": [
                        *admitted_ops,
                        CreateProposalsOperation(op="create_proposals", proposals=[proposal]),
                    ]
                }
            ),
        )
        review_report = permission_gate.validate_permissions(state, review_candidate)
        if not review_report.rejected:
            review_report = validate_patch(state, review_candidate, context, writer=writer)
        if review_report.rejected:
            results.append(
                ChangeClassification(
                    group,
                    "refused",
                    diagnostics=_classification_diagnostics(
                        review_report.messages, (*admitted_indices, group[0])
                    ),
                )
            )
        else:
            results.append(ChangeClassification(group, "reviewable", review_ops, diagnostics))
    return tuple(results)


def _classification_diagnostics(
    messages: Iterable[ValidationMessage], indices: tuple[int, ...]
) -> tuple[ValidationMessage, ...]:
    """Retain original Patch positions when earlier reviews are omitted."""
    return tuple(
        message.model_copy(update={"operation_index": indices[message.operation_index]})
        if message.operation_index is not None and message.operation_index < len(indices)
        else message
        for message in messages
    )


def _review_operations(
    state: GraphState, operations: list[GraphOperation]
) -> tuple[ProposalOperation, ...] | None:
    """Encode supported review intents; the Proposal validator owns their legality."""
    from pydantic import TypeAdapter

    adapter = TypeAdapter(ProposalOperation)
    values: list[dict[str, object]] = []
    for operation in operations:
        raw = operation.model_dump(mode="python", exclude_unset=True)
        if isinstance(operation, UpdateNodesOperation):
            for update in operation.nodes:
                node = state.nodes.get(update.id)
                changes = dict(update.changes)
                if (
                    node is not None
                    and project_type_of(state).is_belief(node.type)
                    and "status" in changes
                ):
                    status = update.model_dump(mode="python", exclude_unset=True)
                    status["changes"] = {"status": changes.pop("status")}
                    values.append(
                        {"op": "update_nodes", "intent": "status_change", "nodes": [status]}
                    )
                    if changes:
                        values.append(
                            {
                                "op": "update_nodes",
                                "intent": "content_change",
                                "nodes": [{"id": update.id, "changes": changes}],
                            }
                        )
                else:
                    values.append(
                        {
                            "op": "update_nodes",
                            "intent": "content_change",
                            "nodes": [update.model_dump(mode="python", exclude_unset=True)],
                        }
                    )
        elif isinstance(operation, RemoveNodesOperation):
            values.extend(
                {"op": "remove_nodes", "intent": "removal", "node_ids": [node_id]}
                for node_id in operation.node_ids
            )
        elif isinstance(operation, SupersedeNodesOperation):
            values.extend(
                {"op": "supersede_nodes", "intent": "supersede", "nodes": [item]}
                for item in raw["nodes"]
            )
        elif isinstance(operation, MergeNodesOperation):
            values.extend(
                {"op": "merge_nodes", "intent": "merge", "merges": [item]} for item in raw["merges"]
            )
        elif isinstance(operation, (CreateEdgesOperation, RemoveEdgesOperation)):
            field = "edges" if isinstance(operation, CreateEdgesOperation) else "edge_ids"
            values.extend(
                {"op": operation.op, "intent": "protected_relation_change", field: [item]}
                for item in raw[field]
            )
        elif isinstance(operation, SetStandingOperation):
            values.append({**raw, "intent": "standing_change"})
        else:
            return None
    return tuple(adapter.validate_python(value) for value in values)
