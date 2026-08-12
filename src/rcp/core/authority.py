from __future__ import annotations

from hashlib import sha256
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.core.models import (
    AuthorizedHuman,
    Decision,
    GraphState,
    Hypothesis,
    Patch,
    ResearchQuestion,
)
from rcp.providers import AgentCapability

AGENT_GRAPH_AUTHORITY_POLICY_VERSION = "s115-v1"

AgentProfile = Literal["ordinary", "orchestrator"]
DispatchPatchKind = Literal["seed", "refresh", "work", "experiment_loop"]


class AgentDispatchScope(BaseModel):
    """The normalized concrete scope captured before an agent task can run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_truth_scope: list[str] = Field(default_factory=list)
    campaign_id: str | None = Field(default=None, min_length=1)
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
    authorized_by: AuthorizedHuman | None
    dispatch_authority: AgentDispatchAuthority | None
    campaign_id: str | None = Field(default=None, min_length=1)


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
) -> None:
    contract = authority.task_contract
    scope = authority.scope
    if contract == "orchestrate":
        if (
            scope.campaign_id is None
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
                f"Authority refused action {action!r}: orchestrate requires an exact campaign, "
                "project-wide run scope, Work Patch kind, and no chat or control scope."
            )
        return
    if contract in {"discuss", "work_auto"}:
        if contract == "work_auto" and scope.campaign_id is not None:
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
                    f"Authority refused action {action!r}: a campaign worker requires an exact "
                    "campaign, project-wide run scope, Work Patch kind, and no chat or control "
                    "scope."
                )
            return
        if scope.campaign_id is not None:
            raise ValueError(
                f"Authority refused action {action!r}: {contract} cannot carry campaign scope."
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
            for value in (scope.campaign_id, scope.chat_scope, scope.chat_id, scope.node_id)
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
            scope.campaign_id,
            scope.patch_kind,
            scope.control_node_id,
            scope.control_episode_id,
        )
    ):
        raise ValueError(
            f"Authority refused action {action!r}: paper_readonly cannot carry chat, Patch, or "
            "control scope."
        )


def require_dispatch(authority: AgentDispatchAuthority) -> AgentDispatchAuthority:
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
    _require_scope_shape(authority, action="dispatch")
    return authority


def require_apply(authority: AgentTaskAuthority, patch: Patch) -> AgentDispatchAuthority:
    """Require a live task binding to expose this exact graph Patch at Apply."""

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
    _require_scope_shape(dispatch, action="apply")
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
EVIDENCE_RELATIONS = frozenset({"supports", "weakens", "refutes", "inconclusive", "contradicts"})
PROTECTED_EPISTEMIC_RELATIONS = frozenset(
    {"has_subquestion", "has_hypothesis", "supersedes", "duplicate_of"}
)

CONTENT_CHANGE_INTENT = "content_change"
REMOVAL_INTENT = "removal"
SUPERSEDE_INTENT = "supersede"
MERGE_INTENT = "merge"
PROTECTED_RELATION_CHANGE_INTENT = "protected_relation_change"
STATUS_CHANGE_INTENT = "status_change"
ProposalIntent = Literal[
    "content_change",
    "removal",
    "supersede",
    "merge",
    "protected_relation_change",
    "status_change",
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

_BASE_ACTION_BY_OPERATION: dict[str, GraphAction] = {
    "create_nodes": CREATE_NODE,
    "update_nodes": UPDATE_NODE,
    "create_edges": CREATE_EDGE,
    "remove_edges": REMOVE_EDGE,
    "remove_nodes": REMOVE_NODE,
    "supersede_nodes": SUPERSEDE_NODE,
    "merge_nodes": MERGE_NODE,
    "create_ambiguities": CREATE_AMBIGUITY,
    "resolve_ambiguities": RESOLVE_AMBIGUITY,
    "create_proposals": CREATE_PROPOSAL,
    "resolve_proposals": RESOLVE_PROPOSAL,
    "withdraw_proposals": WITHDRAW_PROPOSAL,
    "upsert_glossary": UPSERT_GLOSSARY,
    "set_coverage": SET_COVERAGE,
    "set_standing": SET_STANDING,
    "set_project_truth_scope": SET_PROJECT_TRUTH_SCOPE,
    "set_ontology": SET_ONTOLOGY,
}

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

_AGENT_GRAPH_AUTHORITY_BODY = """Assert directly:
- Ordinary legal graph structure and content are assertions, not Proposals, outside the protected
  changes below. Agents may create legal nodes, edit same-Patch nodes, and edit ordinary nodes.
- Editing accepted ordinary-node content resets that node to asserted standing for review. Removing
  an asserted or contested ordinary node also removes its edges; never directly remove an accepted
  node or an Experiment with an active bounded-loop attempt.
- Agents may create a Decision as `open` or `ready`, and may queue an existing Decision as `open`,
  `ready`, or `revisit`. Agents never write `selected_option` or set `status="decided"`; every
  agent-created Hypothesis starts `status="proposed"`.
- Legal edges are direct except `has_subquestion`, `has_hypothesis`, `supersedes`, or `duplicate_of`
  restructuring an existing belief. Same-Patch nodes and Evidence/Experiment relations stay direct.
- Agents may remove, supersede, or merge ordinary nodes.
Proposal-only changes:
- Any edit, removal, supersede, merge, or protected relation change involving an existing
  ResearchQuestion or Hypothesis waits for a human. Put exactly one semantic operation in the
  Proposal and declare its `intent` as `content_change`, `removal`, `supersede`, `merge`,
  `protected_relation_change`, or `status_change`; never bundle separate changes.
- `status_change` updates exactly one Hypothesis's `status`, including one created earlier in the
  same outer Patch, and requires `kind="evidence_edge"` naming a valid Evidence -> Hypothesis
  epistemic edge. A ResearchQuestion lifecycle change uses `content_change`, like its other
  human-held fields, and carries no evidence cause. Other intents likewise carry their reasoning in
  the Proposal card and do not carry a cause.
Human-only authority:
- Agents never set `standing`, approve, or reject Proposals; they may withdraw any pending Proposal
  with `withdraw_proposals` when obsolete or duplicated. Withdrawal applies no semantic operations.
  Agents may not change project configuration or authorize an Experiment **Run**. Approval never
  launches or resumes an Experiment. Only the human pressing **Run** grants RCP permission to
  launch. A human request cannot delegate these actions."""

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
    operation: dict[str, Any],
) -> frozenset[GraphAction]:
    """Derive the closed authority action set for one operation.

    ``state`` is the graph from before the outer Patch. The whole Patch is also
    supplied so a node created in that Patch is never mistaken for an existing
    protected belief. A batch may require more than one action when its targets
    cross action boundaries.
    """

    name = operation.get("op")
    if not isinstance(name, str) or name not in _BASE_ACTION_BY_OPERATION:
        raise ValueError(f"Unknown graph operation {name!r}.")
    base = _BASE_ACTION_BY_OPERATION[name]

    if name == "update_nodes":
        return _update_actions(state, patch, operation, base)
    if name == "create_edges":
        return _create_edge_actions(state, patch, operation, base)
    if name == "remove_edges":
        return _remove_edge_actions(state, patch, operation, base)
    if name == "remove_nodes":
        return _node_target_actions(
            state,
            patch,
            operation.get("node_ids", []),
            base,
            REMOVE_PROTECTED_EPISTEMIC,
        )
    if name == "supersede_nodes":
        items = [item for item in _list(operation.get("nodes")) if isinstance(item, dict)]
        return _node_lifecycle_actions(
            state,
            patch,
            (item.get("id") for item in items),
            ((item.get("id"), item.get("superseded_by")) for item in items),
            "supersedes",
            base,
            SUPERSEDE_PROTECTED_EPISTEMIC,
        )
    if name == "merge_nodes":
        items = [item for item in _list(operation.get("merges")) if isinstance(item, dict)]
        return _node_lifecycle_actions(
            state,
            patch,
            (item.get("duplicate") for item in items),
            ((item.get("duplicate"), item.get("canonical")) for item in items),
            "duplicate_of",
            base,
            MERGE_PROTECTED_EPISTEMIC,
        )
    if name == "set_standing":
        node_id = operation.get("node_id")
        if is_existing_protected_node(state, patch, node_id) or _created_node_type(
            patch, node_id
        ) in {"research_question", "hypothesis"}:
            return frozenset({UPDATE_PROTECTED_EPISTEMIC})
    return frozenset({base})


def is_existing_protected_node(state: GraphState, patch: Patch, node_id: Any) -> bool:
    """Whether ``node_id`` names a pre-Patch ResearchQuestion or Hypothesis."""

    if not isinstance(node_id, str):
        return False
    return isinstance(state.nodes.get(node_id), (ResearchQuestion, Hypothesis))


def permits(patch: Patch, action: GraphAction) -> bool:
    """Whether the Patch producer may perform ``action`` at admission time."""

    if action not in GRAPH_ACTIONS:
        raise ValueError(f"Unknown graph action {action!r}.")
    if patch.author == "human":
        return action in HUMAN_GRAPH_ACTIONS
    if patch.author != "agent" or patch.profile not in {None, "ordinary", "orchestrator"}:
        return False
    if action == SET_COVERAGE:
        return patch.kind in {"seed", "refresh"}
    if patch.profile == "orchestrator":
        return action in ORCHESTRATOR_AGENT_GRAPH_ACTIONS
    return action in ORDINARY_AGENT_GRAPH_ACTIONS


def _update_actions(
    state: GraphState,
    patch: Patch,
    operation: dict[str, Any],
    base: GraphAction,
) -> frozenset[GraphAction]:
    actions: set[GraphAction] = set()
    for update in _list(operation.get("nodes")):
        if not isinstance(update, dict):
            continue
        node_id = update.get("id")
        if is_existing_protected_node(state, patch, node_id):
            actions.add(UPDATE_PROTECTED_EPISTEMIC)
            continue
        node = state.nodes.get(node_id) if isinstance(node_id, str) else None
        is_decision = isinstance(node, Decision) or _created_node_type(patch, node_id) == "decision"
        changes = update.get("changes")
        if is_decision and (
            patch.human_action == "decision_choice" or patch.agent_action == "decision_choice"
        ):
            actions.add(DECIDE_DECISION)
        elif (
            isinstance(node, Decision)
            and isinstance(changes, dict)
            and changes.get("status") in {"open", "ready", "revisit"}
        ):
            actions.add(QUEUE_DECISION)
        else:
            actions.add(base)
    return frozenset(actions or {base})


def _created_node_type(patch: Patch, node_id: Any) -> str | None:
    if not isinstance(node_id, str):
        return None
    for operation in patch.ops:
        if operation.get("op") != "create_nodes":
            continue
        for raw in _list(operation.get("nodes")):
            if isinstance(raw, dict) and raw.get("id") == node_id:
                node_type = raw.get("type")
                return node_type if isinstance(node_type, str) else None
    return None


def _create_edge_actions(
    state: GraphState,
    patch: Patch,
    operation: dict[str, Any],
    base: GraphAction,
) -> frozenset[GraphAction]:
    actions: set[GraphAction] = set()
    new_node_ids = created_node_ids(patch) - set(state.nodes)
    for edge in _list(operation.get("edges")):
        if not isinstance(edge, dict):
            continue
        endpoints = tuple(
            node_id
            for node_id in (edge.get("source"), edge.get("target"))
            if isinstance(node_id, str)
        )
        restructures = _restructures_protected_relation(
            state,
            patch,
            edge.get("relation"),
            endpoints,
            new_node_ids=new_node_ids,
        )
        actions.add(RESTRUCTURE_PROTECTED_EPISTEMIC if restructures else base)
    return frozenset(actions or {base})


def _remove_edge_actions(
    state: GraphState,
    patch: Patch,
    operation: dict[str, Any],
    base: GraphAction,
) -> frozenset[GraphAction]:
    actions: set[GraphAction] = set()
    new_edge_ids = created_edge_ids(patch) - set(state.edges)
    for edge_id in _list(operation.get("edge_ids")):
        edge = state.edges.get(edge_id) if isinstance(edge_id, str) else None
        restructures = (
            edge is not None
            and edge_id not in new_edge_ids
            and edge.relation in PROTECTED_EPISTEMIC_RELATIONS
            and any(
                is_existing_protected_node(state, patch, node_id)
                for node_id in (edge.source, edge.target)
            )
        )
        actions.add(RESTRUCTURE_PROTECTED_EPISTEMIC if restructures else base)
    return frozenset(actions or {base})


def _node_target_actions(
    state: GraphState,
    patch: Patch,
    node_ids: Any,
    base: GraphAction,
    protected: GraphAction,
) -> frozenset[GraphAction]:
    if not isinstance(node_ids, (list, tuple, set, frozenset)) and not hasattr(
        node_ids, "__next__"
    ):
        return frozenset({base})
    actions = {
        protected if is_existing_protected_node(state, patch, node_id) else base
        for node_id in node_ids
    }
    return frozenset(actions or {base})


def _node_lifecycle_actions(
    state: GraphState,
    patch: Patch,
    node_ids: Any,
    generated_edge_endpoints: Any,
    generated_relation: str,
    base: GraphAction,
    protected: GraphAction,
) -> frozenset[GraphAction]:
    """Include both lifecycle and implicit protected-relation authority."""

    actions = set(_node_target_actions(state, patch, node_ids, base, protected))
    new_node_ids = created_node_ids(patch) - set(state.nodes)
    for raw_endpoints in generated_edge_endpoints:
        endpoints = tuple(node_id for node_id in raw_endpoints if isinstance(node_id, str))
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
    relation: Any,
    endpoints: tuple[str, ...],
    *,
    new_node_ids: set[str],
) -> bool:
    return (
        len(endpoints) == 2
        and relation in PROTECTED_EPISTEMIC_RELATIONS
        and not new_node_ids.intersection(endpoints)
        and any(is_existing_protected_node(state, patch, node_id) for node_id in endpoints)
    )


def created_node_ids(patch: Patch) -> set[str]:
    return {
        node_id
        for operation in patch.ops
        if operation.get("op") == "create_nodes"
        for raw in _list(operation.get("nodes"))
        if isinstance(raw, dict) and isinstance((node_id := raw.get("id")), str)
    }


def created_edge_ids(patch: Patch) -> set[str]:
    edge_ids: set[str] = set()
    for operation in patch.ops:
        if operation.get("op") != "create_edges":
            continue
        for raw in _list(operation.get("edges")):
            if not isinstance(raw, dict):
                continue
            edge_id = raw.get("id")
            if not isinstance(edge_id, str):
                source = raw.get("source")
                relation = raw.get("relation")
                target = raw.get("target")
                if all(isinstance(value, str) for value in (source, relation, target)):
                    edge_id = f"{source}::{relation}::{target}"
            if isinstance(edge_id, str):
                edge_ids.add(edge_id)
    return edge_ids


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
