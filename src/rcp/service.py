from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import stat
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from rcp.agents import (
    AgentLauncher,
    AgentPatch,
    ChatContext,
    ContextAssembler,
    PromptFactory,
    ProviderReadiness,
    RunContext,
    parse_agent_patch_json,
)
from rcp.agents.context import CoachContext
from rcp.agents.provider_accounts import LoginRefusal, ProviderAccounts
from rcp.agents.write_scope import RegisteredRepositoryRoot, registered_repository_roots
from rcp.attachments import ChatAttachmentDescriptor, ProjectReferenceSelector
from rcp.compute import machine_compute_context
from rcp.config import (
    AGENT_EXECUTION_PROFILES,
    AgentExecutionProfile,
    AgentSurface,
    AgentSurfaceConfig,
    MachineConfig,
    Manifest,
    launches_work_like,
    load_manifest,
    require_base_machine,
)
from rcp.control import derive_experiment_control_state, released_attempts
from rcp.core.attention import (
    project_counts,
    project_graph_attention,
    project_graph_mutation_availability,
    project_primary_question,
)
from rcp.core.authority import (
    AgentDispatchAuthority,
    AgentDispatchScope,
)
from rcp.core.materialize import MaterializationResult, apply_valid_patch
from rcp.core.models import (
    HUMAN_EDITABLE_NODE_FIELDS,
    AuthorizedHuman,
    ExperimentDecisionPin,
    GraphState,
    OntologyState,
    Patch,
    ProjectNode,
    Proposal,
    Standing,
)
from rcp.core.operations import (
    NewEdge,
    ProposalResolution,
    RepositoryDescriptor,
    ResolveProposalsOperation,
    SetProjectTruthScopeOperation,
    UpdateNodesOperation,
)
from rcp.core.project_types import project_type_of
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.core.transitions import (
    CommittedTransition,
    PreparedTransition,
    project_transition_projection,
)
from rcp.core.validation.approval import checked_human_decision_choice, is_decision_choice
from rcp.core.validation.proposals import (
    normalized_decision_proposal_ops,
    proposal_approval_operations,
    proposal_is_stale,
    proposal_updates_node,
)
from rcp.history import HistoryManager
from rcp.history.branches import BranchHistoryManager
from rcp.history.queries import HistoryEngine
from rcp.limits import (
    ARTIFACT_CONTEXT_MAX_SELECTIONS,
    BACKUP_INVENTORY_MAX_ENTRIES,
    CHAT_ATTACHMENT_MAX_COUNT,
    CHAT_PAGE_DEFAULT_LIMIT,
    CHAT_PAGE_MAX_LIMIT,
    CHAT_PREVIEW_MAX_CHARS,
    REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
)
from rcp.operation_ids import canonical_operation_uuid
from rcp.paper import PaperLive, PaperService
from rcp.paper.changes import PaperChanges
from rcp.paper.projection import publication_badge
from rcp.provider_login_rows import missing_own_login_row, provider_login_row
from rcp.provider_skills import ProviderSkillInventoryManager
from rcp.providers import (
    PROVIDER_IDS,
    ModelChoice,
    ProviderId,
    ProviderSkillReference,
    configured_runtime,
    profile_for,
)
from rcp.providers.browser_grant import BrowserTurnStatus
from rcp.providers.live_status import AgentStatus
from rcp.provisioning_repositories import team_repository_sources
from rcp.runs.auto_research import AutoResearchRunRequest
from rcp.runs.provider_login import parked_provider_work, provider_login_host
from rcp.skill_registry import (
    SkillDefaults,
    SkillReference,
    SkillSelection,
    official_registry,
)
from rcp.sources import (
    AppChatOrigin,
    ConversationIndex,
    ConversationIndexer,
    ImportedProviderSourceInventory,
    ImportedProviderSourceStore,
    preflight_provider_roots,
    project_cache_roots,
)
from rcp.storage import AppStore
from rcp.storage.chat_display import chat_agent_setup
from rcp.storage.chat_forks import ChatForkOrigin
from rcp.storage.models import MACHINE_LOGIN_ID, LoginKey
from rcp.transport.state_transfer import diagnostics as state_transfer_diagnostics
from rcp.turn_outcomes import TurnOutcome

if TYPE_CHECKING:
    from rcp.runs.provider_sign_in import ProviderSignInRunner

_SETTINGS_SURFACES: tuple[AgentExecutionProfile, ...] = (
    "seed",
    "refresh",
    "node_chat",
    "project_chat",
    "paper_coach",
    "orchestrator",
)


logger = logging.getLogger(__name__)


class _ProjectSnapshotDraft:
    """Opaque graph-only project snapshot data for the display boundary."""

    __slots__ = ("_payload",)

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __getitem__(self, key: str) -> object:
        return self._payload[key]

    def __setitem__(self, key: str, value: object) -> None:
        self._payload[key] = value

    def _as_dict(self) -> dict[str, object]:
        return dict(self._payload)


ConversationMode = Literal["discuss", "work"]
TaskTrigger = Literal["human", "orchestrator", "experiment_run", "watcher", "schedule"]
GraphPatchKind = Literal["work", "experiment_loop"]


def _chooses_decision_option(state: GraphState, node_id: str, changes: dict[str, Any]) -> bool:
    node = state.nodes.get(node_id)
    return (
        node is not None
        and project_type_of(state).is_chooser(node.type)
        and is_decision_choice(changes)
    )


def _proposal_applies_decision_choice(state: GraphState, proposal: Proposal) -> bool:
    for operation in normalized_decision_proposal_ops(state, proposal):
        if not isinstance(operation, UpdateNodesOperation):
            continue
        for update in operation.nodes:
            if _chooses_decision_option(state, update.id, update.changes):
                return True
    return False


def _proposal_judgment_patch(
    state: GraphState,
    proposal: Proposal,
    *,
    decision: Literal["approved", "rejected"],
    reason: str | None,
    stale_reason: str | None = None,
) -> Patch:
    if proposal_is_stale(state, proposal):
        withdrawal_reason = (
            stale_reason
            or f"The proposal “{proposal.title}” was stale and was withdrawn without applying "
            "changes."
        )
        return Patch(
            kind="approval",
            author="human",
            summary=f"Withdrew stale proposal “{proposal.title}”.",
            ops=[
                {
                    "op": "resolve_proposals",
                    "resolutions": [
                        {
                            "id": proposal.id,
                            "status": "withdrawn",
                            "reason": withdrawal_reason,
                        }
                    ],
                }
            ],
            change_summary=[withdrawal_reason],
        )

    return Patch(
        kind="approval",
        author="human",
        producer="human",
        summary=f"{decision.title()} proposal “{proposal.title}”.",
        ops=(
            proposal_approval_operations(state, proposal, reason=reason)
            if decision == "approved"
            else [
                ResolveProposalsOperation(
                    op="resolve_proposals",
                    resolutions=[
                        ProposalResolution(id=proposal.id, status=decision, reason=reason)
                    ],
                )
            ]
        ),
        change_summary=[f"The proposal “{proposal.title}” was {decision}."],
        human_action=(
            "decision_choice"
            if decision == "approved" and _proposal_applies_decision_choice(state, proposal)
            else None
        ),
    )


def _stage_sync_patch(state: GraphState, patch: Patch) -> GraphState:
    """Advance the in-memory Sync draft exactly as append_batch will."""

    return apply_valid_patch(
        state,
        patch.model_copy(update={"revision": state.revision + 1}),
    )


class GraphUpdateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "unavailable": Apply could not reach canonical state. The rules never
    # judged the Patch, and `commit_status` says what the commit point showed.
    status: Literal["none", "applied", "rejected", "unavailable"]
    applied_revision: int | None = Field(default=None, ge=0)
    change_summary: list[str] = Field(default_factory=list)
    proposal_ids: list[str] = Field(default_factory=list)
    validation_messages: list[str] = Field(default_factory=list)
    correction_rounds: int = Field(default=0, ge=0)
    repairable: bool = False
    commit_status: Literal["absent", "present", "unknown"] | None = None


class SteeringReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1)
    turn_id: str = Field(min_length=1)
    status: Literal["delivered", "refused", "unknown"]
    label: str
    reason: str | None = None


class ChatArtifactQuote(BaseModel):
    """What one numbered comment was drawn on, in the order the message numbers them."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["text", "box"]
    #: The selected text or the boxed elements' names; empty for an area of an image.
    quote: str
    comment: str


class ChatArtifactComments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    #: None once the artifact is gone.
    name: str | None
    quotes: list[ChatArtifactQuote]


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    operation_id: str | None = None
    browser_status: BrowserTurnStatus = Field(
        default_factory=lambda: BrowserTurnStatus(status="not_requested")
    )
    #: The turn's live agent status, joined by the chat route; display only.
    agent_status: AgentStatus | None = None
    #: The artifact comments a human turn carries, joined by the chat route; display only.
    artifact_comments: ChatArtifactComments | None = None
    role: Literal["user", "assistant"]
    text: str
    timestamp: str
    native_session_id: str | None = None
    provider: str | None = None
    model: str | None = None
    reasoning: str | None = None
    execution_machine: str | None = None
    applied_revision: int | None = Field(default=None, ge=0)
    mode: ConversationMode | None = None
    graph_update: GraphUpdateResult | None = None
    turn_outcome: TurnOutcome | None = None
    trigger: TaskTrigger = "human"
    steering: SteeringReceipt | None = None
    attachments: list[ChatAttachmentDescriptor] = Field(default_factory=list)


class ChatOriginView(ChatForkOrigin):
    # False once restore detached the fork; its next message starts a fresh session.
    pending: bool


class ChatAgentSetup(BaseModel):
    """The provider, model, effort, and machine of a chat's newest turn, and when it started."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str | None = None
    reasoning: str | None = None
    run_on: str | None = None
    selected_at: str


class ChatSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str
    graph_target: GraphTargetRef = Field(default_factory=GraphTargetRef)
    kind: Literal["node_chat", "project_chat"]
    node_id: str | None
    title: str
    updated_at: str
    message_count: int = Field(ge=0)
    last_message_preview: str
    conversation_kind: Literal["chat", "episode", "auto_research_child", "auto_research"] = "chat"
    orchestrator_episode_id: str | None = None
    human_turn_refusal: dict[str, str] | None = None
    graph_title: str = "Main"
    origin: ChatOriginView | None = None
    # Every listed agent shows its own setup, not only those whose turns are loaded.
    agent_setup: ChatAgentSetup | None = None


class ChatSummaryPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ChatSummary]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=CHAT_PAGE_MAX_LIMIT)


class ChatTranscript(ChatSummary):
    messages: list[ChatMessage] = Field(default_factory=list)


@dataclass(frozen=True)
class _ChatSummaryCacheEntry:
    fingerprint: tuple[int, int, int, int, int]
    summary: ChatSummary | None


class _StoredChatRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    graph_target: GraphTargetRef | None = Field(default=None, alias="graphTarget")
    native_session_id: str | None = Field(default=None, alias="nativeSessionId")
    node_id: str | None = Field(default=None, alias="nodeId")
    chat_scope: Literal["node", "project"] = Field(alias="chatScope")
    provider: str | None = None
    model: str | None = None
    reasoning: str | None = None
    execution_machine: str | None = Field(default=None, alias="executionMachine")
    timestamp: str
    uuid: str
    operation_id: str | None = Field(default=None, alias="operationId")
    type: Literal["user", "assistant"]
    role: Literal["user", "assistant"]
    text: str
    applied_revision: int | None = Field(default=None, alias="appliedRevision", ge=0)
    mode: ConversationMode | None = None
    graph_update: GraphUpdateResult | None = Field(default=None, alias="graphUpdate")
    turn_outcome: TurnOutcome | None = Field(default=None, alias="turnOutcome")
    trigger: TaskTrigger = "human"
    steering: SteeringReceipt | None = None
    attachments: list[ChatAttachmentDescriptor] = Field(default_factory=list)


def _fold_chat_receipts(records: list[_StoredChatRecord]) -> list[_StoredChatRecord]:
    """Fold append-only steering snapshots without changing the human message."""
    result: list[_StoredChatRecord] = []
    steering_positions: dict[str, int] = {}
    for record in records:
        if record.steering is None:
            result.append(record)
            continue
        position = steering_positions.get(record.uuid)
        if position is None:
            steering_positions[record.uuid] = len(result)
            result.append(record)
            continue
        previous = result[position]
        assert previous.steering is not None
        if (
            previous.model_dump(exclude={"steering"}) != record.model_dump(exclude={"steering"})
            or previous.steering.attempt != record.steering.attempt
            or previous.steering.turn_id != record.steering.turn_id
            or previous.steering.status != "unknown"
        ):
            raise ValueError("A steering receipt cannot change its human message or final outcome.")
        result[position] = record
    return result


@dataclass(frozen=True)
class CanonicalChatBackupSource:
    """One safe append-only chat and the exact byte boundary backup may read."""

    path: Path
    observed_bytes: int
    device: int
    inode: int


def _canonical_chat_path(
    root: Path,
    chat_id: str,
    *,
    chat_scope: Literal["node", "project"],
    node_id: str | None,
) -> Path:
    target = node_id if chat_scope == "node" else "project"
    if target is None:
        raise ValueError("Node chat requires a node_id")
    safe_target = re.sub(r"[^A-Za-z0-9._-]+", "_", target).strip("._") or "node"
    return root / "chat" / f"{safe_target}-{chat_id}.jsonl"


def canonical_chat_backup_sources(root: Path) -> tuple[CanonicalChatBackupSource, ...]:
    """Inventory every canonical chat without refreshing or taking its append lock."""

    chat_dir = root / "chat"
    try:
        directory = chat_dir.lstat()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise ValueError("The canonical chat directory is unavailable.") from exc
    if not stat.S_ISDIR(directory.st_mode):
        raise ValueError("The canonical chat path is not a safe directory.")
    try:
        candidates = sorted(chat_dir.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ValueError("The canonical chat directory cannot be enumerated.") from exc
    if len(candidates) > BACKUP_INVENTORY_MAX_ENTRIES:
        raise ValueError("The canonical chat inventory exceeds its entry bound.")
    sources: list[CanonicalChatBackupSource] = []
    for path in candidates:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ValueError("A canonical chat file cannot be inspected.") from exc
        if path.parent != chat_dir or path.suffix != ".jsonl" or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("The canonical chat directory contains an unsafe entry.")
        sources.append(
            CanonicalChatBackupSource(
                path=path,
                observed_bytes=metadata.st_size,
                device=metadata.st_dev,
                inode=metadata.st_ino,
            )
        )
    return tuple(sources)


def iter_canonical_chat_backup_prefix(
    source: CanonicalChatBackupSource,
    *,
    project_id: str,
    operation_projects: Mapping[str, str],
) -> Iterator[bytes]:
    """Yield the typed complete prefix whose operations exist in the DB snapshot."""

    descriptor = os.open(source.path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or (initial.st_dev, initial.st_ino) != (source.device, source.inode)
            or initial.st_size < source.observed_bytes
        ):
            raise ValueError("The canonical chat changed before its backup read.")
        first: _StoredChatRecord | None = None
        remaining = source.observed_bytes
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while remaining:
                line = handle.readline(remaining)
                remaining -= len(line)
                if not line or not line.endswith(b"\n"):
                    break
                try:
                    raw = json.loads(line)
                    record = _StoredChatRecord.model_validate(raw)
                    _validate_stored_chat_record(
                        record,
                        first=first,
                        root=source.path.parent.parent,
                        path=source.path,
                    )
                except (TypeError, UnicodeError, ValueError) as exc:
                    raise ValueError("A canonical chat record is malformed.") from exc
                if first is None:
                    first = record
                if record.operation_id is not None:
                    operation_id = canonical_operation_uuid(
                        record.operation_id, label="canonical chat operation identity"
                    )
                    owner = operation_projects.get(operation_id)
                    if owner is None:
                        break
                    if owner != project_id:
                        raise ValueError("A canonical chat references another project's task.")
                yield line
        final = os.fstat(descriptor)
        try:
            current = source.path.lstat()
        except OSError as exc:
            raise ValueError("The canonical chat changed during its backup read.") from exc
        if (
            (final.st_dev, final.st_ino) != (source.device, source.inode)
            or (current.st_dev, current.st_ino) != (source.device, source.inode)
            or final.st_size < source.observed_bytes
        ):
            raise ValueError("The canonical chat changed during its backup read.")
    finally:
        os.close(descriptor)


def iter_canonical_chat_transfer(
    source: CanonicalChatBackupSource,
    *,
    operation_id_map: Mapping[str, str],
) -> Iterator[bytes]:
    """Yield one typed chat with source-native and unmapped task bindings removed."""

    descriptor = os.open(source.path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or (initial.st_dev, initial.st_ino) != (source.device, source.inode)
            or initial.st_size < source.observed_bytes
        ):
            raise ValueError("The canonical chat changed before its transfer read.")
        first: _StoredChatRecord | None = None
        remaining = source.observed_bytes
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while remaining:
                line = handle.readline(remaining)
                remaining -= len(line)
                if not line or not line.endswith(b"\n"):
                    raise ValueError("A canonical chat record is incomplete.")
                try:
                    raw = json.loads(line)
                    record = _StoredChatRecord.model_validate(raw)
                    _validate_stored_chat_record(
                        record,
                        first=first,
                        root=source.path.parent.parent,
                        path=source.path,
                    )
                except (TypeError, UnicodeError, ValueError) as exc:
                    raise ValueError("A canonical chat record is malformed.") from exc
                if first is None:
                    first = record
                mapped_operation_id = None
                if record.operation_id is not None:
                    source_operation_id = canonical_operation_uuid(
                        record.operation_id, label="canonical chat operation identity"
                    )
                    mapped_operation_id = operation_id_map.get(source_operation_id)
                    if mapped_operation_id is not None:
                        canonical_operation_uuid(
                            mapped_operation_id, label="mapped canonical chat operation identity"
                        )
                transferred = record.model_copy(
                    update={
                        "native_session_id": None,
                        "execution_machine": None,
                        "operation_id": mapped_operation_id,
                    }
                )
                yield (
                    json.dumps(
                        transferred.model_dump(mode="json", by_alias=True, exclude_none=True),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                        allow_nan=False,
                    ).encode("utf-8")
                    + b"\n"
                )
        if first is None:
            raise ValueError("A canonical chat cannot transfer without messages.")
        final = os.fstat(descriptor)
        try:
            current = source.path.lstat()
        except OSError as exc:
            raise ValueError("The canonical chat changed during its transfer read.") from exc
        if (
            (final.st_dev, final.st_ino) != (source.device, source.inode)
            or (current.st_dev, current.st_ino) != (source.device, source.inode)
            or final.st_size < source.observed_bytes
            or current.st_size < source.observed_bytes
        ):
            raise ValueError("The canonical chat changed during its transfer read.")
    finally:
        os.close(descriptor)


def _validate_stored_chat_record(
    record: _StoredChatRecord,
    *,
    first: _StoredChatRecord | None,
    root: Path,
    path: Path,
) -> None:
    chat_id = str(uuid.UUID(record.session_id))
    message_id = str(uuid.UUID(record.uuid))
    timestamp = datetime.fromisoformat(record.timestamp)
    if (
        chat_id != record.session_id
        or message_id != record.uuid
        or timestamp.tzinfo is None
        or record.type != record.role
    ):
        raise ValueError("canonical chat record identity is invalid")
    if record.steering is not None and (record.role != "user" or record.trigger != "human"):
        raise ValueError("A steering receipt must belong to a human message.")
    anchor = first or record
    if (
        record.session_id != anchor.session_id
        or record.chat_scope != anchor.chat_scope
        or record.node_id != anchor.node_id
        or (
            record.graph_target is not None
            and anchor.graph_target is not None
            and record.graph_target != anchor.graph_target
        )
    ):
        raise ValueError("canonical chat record context changed")
    if (anchor.chat_scope == "node" and not anchor.node_id) or (
        anchor.chat_scope == "project" and anchor.node_id is not None
    ):
        raise ValueError("canonical chat scope is invalid")
    if path != _canonical_chat_path(
        root,
        anchor.session_id,
        chat_scope=anchor.chat_scope,
        node_id=anchor.node_id,
    ):
        raise ValueError("canonical chat path does not match its records")


class ReviewRequest(BaseModel):
    standing: Literal["asserted", "accepted", "contested"]


class NodeEditConflict(ValueError):
    pass


class ProposalDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str | None = None


class GraphSyncNodeChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    base_updated_rev: int = Field(ge=0)
    changes: dict[str, Any] = Field(default_factory=dict)
    standing: Literal["asserted", "accepted", "contested"] | None = None
    cancel_attempt_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_change(self) -> GraphSyncNodeChange:
        if not self.changes and self.standing is None and not self.cancel_attempt_ids:
            raise ValueError("a staged node must change wording, standing, or an open attempt")
        if len(self.cancel_attempt_ids) != len(set(self.cancel_attempt_ids)):
            raise ValueError("a staged node cannot cancel the same attempt twice")
        return self


class GraphSyncProposalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    decision: Literal["approved", "rejected"]
    reason: str | None = None


class GraphSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    nodes: list[GraphSyncNodeChange] = Field(default_factory=list)
    proposals: list[GraphSyncProposalDecision] = Field(default_factory=list)
    ontology: OntologyState | None = None
    custom_nodes: list[ProjectNode] = Field(default_factory=list)
    removed_node_ids: list[str] = Field(default_factory=list)
    added_edges: list[NewEdge] = Field(default_factory=list)
    removed_edge_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_unique_targets(self) -> GraphSyncRequest:
        for label, values in (
            ("node", [item.node_id for item in self.nodes]),
            ("proposal", [item.proposal_id for item in self.proposals]),
            ("custom node", [item.id for item in self.custom_nodes]),
            ("removed node", self.removed_node_ids),
            (
                "added edge",
                [
                    item.id or f"{item.source}::{item.relation}::{item.target}"
                    for item in self.added_edges
                ],
            ),
            ("removed edge", self.removed_edge_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"a graph sync cannot contain duplicate {label} targets")
        staged_node_ids = {item.node_id for item in self.nodes}
        removed_node_ids = set(self.removed_node_ids)
        conflicting_node_ids = sorted(staged_node_ids & removed_node_ids)
        if conflicting_node_ids:
            raise ValueError(
                "a graph sync cannot both change and remove the same node: "
                f"{', '.join(conflicting_node_ids)}"
            )
        created_node_ids = {item.id for item in self.custom_nodes}
        if created_node_ids & (staged_node_ids | removed_node_ids):
            raise ValueError("a graph sync cannot create and change or remove the same node")
        if any(
            edge.source in removed_node_ids or edge.target in removed_node_ids
            for edge in self.added_edges
        ):
            raise ValueError("a graph sync cannot connect a node it removes")
        return self


class ArtifactSelectionRect(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def stays_inside_viewport(self) -> ArtifactSelectionRect:
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("artifact selection rectangle must stay inside its viewport")
        return self


class ArtifactViewport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    width: int = Field(ge=1, le=32768)
    height: int = Field(ge=1, le=32768)


class ArtifactTextSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["text"]
    text: str = Field(min_length=1, max_length=4096)
    surrounding_text: str = Field(default="", max_length=6144)
    comment: str = Field(default="", max_length=2048)


class ArtifactBoxElement(BaseModel):
    """One HTML element a box covers, named so the agent can find it in the source."""

    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=512)
    label: str = Field(default="", max_length=256)
    text: str = Field(default="", max_length=512)
    # Where the box lies within this element, when the box sits inside it.
    region: ArtifactSelectionRect | None = None


class ArtifactBoxSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["box"]
    # A fraction of the image for an image artifact, of the visible frame for HTML.
    rect: ArtifactSelectionRect
    viewport: ArtifactViewport
    # None marks a box from the viewer before elements were named. Its rect is a
    # fraction of the viewer area, not of an image, so it is never cropped.
    elements: list[ArtifactBoxElement] | None = Field(default=None, max_length=8)
    # That older viewer's sampled nearby text, kept so its stored requests still render.
    labels: str = Field(default="", max_length=4096)
    comment: str = Field(default="", max_length=2048)


ArtifactSelection = Annotated[
    ArtifactTextSelection | ArtifactBoxSelection,
    Field(discriminator="kind"),
]


class ArtifactContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: Literal["task", "episode_report"] = "task"
    operation_id: str = Field(min_length=1)
    artifact_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    episode_id: str | None = None
    fresh_session: bool = False
    # Set by the viewer's Edit now: the one prompt branch asking for the edit this turn.
    edit_now: bool = False
    # The version the selections were drawn on; admission refuses a newer one.
    base_version: str | None = None
    selections: list[ArtifactSelection] = Field(
        default_factory=list, max_length=ARTIFACT_CONTEXT_MAX_SELECTIONS
    )

    @model_validator(mode="after")
    def source_identity_is_coherent(self) -> ArtifactContextRequest:
        if (self.source == "episode_report") != (self.episode_id is not None):
            raise ValueError("episode report context requires exactly one episode_id")
        return self


class ArtifactEditAdmission(BaseModel):
    """Server-owned edit binding, frozen before reserving the provider session."""

    model_config = ConfigDict(extra="forbid", strict=True)

    artifact_id: str
    base_version: str
    source_name: str
    media_type: str
    operation_id: str
    # Recorded by edits that staged a separate copy; the tracked file replaced it.
    staged_scope_id: str | None = None
    origin_operation_id: str
    launch_kind: Literal["discuss", "revoking"]
    master_operation_id: str | None = None
    master_sha256: str | None = None
    master_path: str | None = None
    stage_host: str | None = None
    stage_root: str | None = None
    reply_episode_id: str | None = None
    episode_id: str | None = None
    fresh_session: bool = False
    # An edit resumes the origin's session. False only on older composer comments,
    # which ran as ordinary chat turns; a comment now carries no edit at all.
    origin_bound: bool = True


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    browser_requested: bool = False

    provider: ProviderId | None = None
    run_truth_scope: list[str] | None = None
    model: str | None = None
    reasoning: str | None = None
    run_on: str | None = None
    chat_scope: Literal["node", "project"] = "node"
    node_id: str | None = None
    message: str | None = None
    chat_id: str | None = None
    session_id: str | None = None
    # Resolved from the immutable chat origin, never accepted from HTTP clients.
    fork_session_id: str | None = None
    fork_source_cwd: str | None = None
    fork_source_host: str | None = None
    # The source chat's latest turn when the first message forked; no launch reads it.
    fork_source_operation_id: str | None = None
    # The Work turn that started this Experiment episode; never accepted from HTTP clients.
    started_from_operation_id: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    mode: ConversationMode = "discuss"
    isolation_owner_episode_id: str | None = None
    code_worktree: bool = False
    graph_isolation: bool = False
    branch_name: str | None = None
    worktree: bool = Field(default=False, exclude_if=lambda value: not value)
    worktree_integration: Literal["pull_request", "starting_branch", "default_branch"] | None = (
        Field(default=None, exclude_if=lambda value: value is None)
    )
    # Server-resolved integration branch, never accepted from HTTP clients.
    worktree_integration_target: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    artifact_context: ArtifactContextRequest | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    artifact_edit: ArtifactEditAdmission | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    trigger: TaskTrigger = "human"
    patch_kind: GraphPatchKind = "work"
    control_node_id: str | None = None
    control_revision: int | None = Field(default=None, ge=0)
    control_episode_id: str | None = None
    control_invocation: int | None = Field(default=None, ge=1)
    control_invocation_ceiling: int | None = Field(default=None, ge=1)
    control_decision_bundle: list[ExperimentDecisionPin] = Field(default_factory=list)
    control_completion_criteria: list[str] = Field(default_factory=list)
    watcher_ids: list[str] = Field(default_factory=list)
    workflow_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    invoked_workflow_ids: list[str] = Field(default_factory=list)
    invoked_skill_ids: list[str] = Field(default_factory=list)
    invoked_provider_skill_names: list[str] = Field(default_factory=list)
    resolved_provider_skills: list[ProviderSkillReference] = Field(default_factory=list)
    resolved_skill_packages: list[SkillReference] | None = None
    # The set/client fields are short-lived ingress capabilities accepted only on a
    # human chat request. RCP replaces them with the claimed batch and metadata
    # before the task record is created.
    attachment_set_id: str | None = None
    attachment_client_id: str | None = None
    attachment_batch_id: str | None = None
    attachments: list[ChatAttachmentDescriptor] = Field(default_factory=list)
    # Human selectors, accepted only on a human chat request. Admission copies each
    # source into the claimed attachment batch and clears this list.
    references: list[ProjectReferenceSelector] = Field(
        default_factory=list, max_length=CHAT_ATTACHMENT_MAX_COUNT
    )


class CoachRequest(BaseModel):
    message: str
    mode: Literal["review", "apply_directly"] = "review"
    refresh_source_task_id: str | None = None
    refresh_group_ids: list[str] = Field(default_factory=list)
    provider: ProviderId | None = None
    model: str | None = None
    reasoning: str | None = None
    run_on: str | None = None
    session_id: str | None = None
    workflow_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    invoked_workflow_ids: list[str] = Field(default_factory=list)
    invoked_skill_ids: list[str] = Field(default_factory=list)
    invoked_provider_skill_names: list[str] = Field(default_factory=list)
    resolved_provider_skills: list[ProviderSkillReference] = Field(default_factory=list)
    resolved_skill_packages: list[SkillReference] | None = None

    attachment_scope_id: str | None = None
    attachment_set_id: str | None = None
    attachment_client_id: str | None = None
    attachment_batch_id: str | None = None
    attachments: list[ChatAttachmentDescriptor] = Field(default_factory=list)
    references: list[ProjectReferenceSelector] = Field(
        default_factory=list, max_length=CHAT_ATTACHMENT_MAX_COUNT
    )

    @model_validator(mode="before")
    @classmethod
    def apply_directly_mode(cls, value: object) -> object:
        if not isinstance(value, dict) or "paper_apply_directly" not in value:
            return value
        direct = value["paper_apply_directly"]
        if not isinstance(direct, bool):
            raise ValueError("paper_apply_directly must be a boolean")
        mode = "apply_directly" if direct else "review"
        if "mode" in value and value["mode"] != mode:
            raise ValueError("Paper mode and paper_apply_directly disagree")
        return {key: item for key, item in value.items() if key != "paper_apply_directly"} | {
            "mode": mode
        }


def resolve_dispatch_authority(
    kind: str,
    request: object,
) -> AgentDispatchAuthority | None:
    """Resolve one profile binding from the server-owned task shape."""

    if kind == "auto_research":
        if not isinstance(request, AutoResearchRunRequest):
            raise TypeError("auto_research dispatch requires an AutoResearchRunRequest")
        return AgentDispatchAuthority(
            profile="orchestrator" if request.role == "orchestrator" else "ordinary",
            task_contract="orchestrate" if request.role == "orchestrator" else "work_auto",
            scope=AgentDispatchScope(
                run_truth_scope=sorted(set(request.run_truth_scope or ())),
                episode_id=request.episode_id,
                patch_kind="work",
            ),
        )
    if kind == "paper_coach":
        if not isinstance(request, CoachRequest):
            raise TypeError("paper_coach dispatch requires a CoachRequest")
        return AgentDispatchAuthority(
            profile="ordinary",
            task_contract="paper_suggest",
            scope=AgentDispatchScope(),
        )
    if not isinstance(request, RunRequest):
        raise TypeError(f"{kind} dispatch requires a RunRequest")

    run_truth_scope = sorted(set(request.run_truth_scope or ()))
    if kind in {"seed", "refresh"}:
        return AgentDispatchAuthority(
            profile="ordinary",
            task_contract="scratch_patch",
            scope=AgentDispatchScope(
                run_truth_scope=run_truth_scope,
                patch_kind=kind,
            ),
        )
    if kind not in {"node_chat", "project_chat"}:
        raise ValueError(f"Authority refused action 'dispatch': unknown task kind {kind!r}.")

    chat_scope: Literal["node", "project"] = "node" if kind == "node_chat" else "project"
    if request.chat_scope != chat_scope:
        raise ValueError(
            "Authority refused action 'dispatch': task kind and chat scope do not match."
        )
    work = request.mode == "work"
    patch_kind = request.patch_kind if work else None
    experiment = patch_kind == "experiment_loop"
    return AgentDispatchAuthority(
        profile="ordinary",
        task_contract="work_auto" if work else "discuss",
        scope=AgentDispatchScope(
            run_truth_scope=run_truth_scope,
            chat_scope=chat_scope,
            chat_id=request.chat_id,
            node_id=request.node_id if chat_scope == "node" else None,
            patch_kind=patch_kind,
            control_node_id=request.control_node_id if experiment else None,
            control_episode_id=request.control_episode_id if experiment else None,
        ),
    )


class AgentProfileSettings(BaseModel):
    provider: ProviderId
    runtime: str = ""
    model: str = ""
    reasoning: str = "medium"
    run_on: str = Field(min_length=1)


class ProjectSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_auto_research_invocation_ceiling: int | None = Field(
        default=None,
        ge=1,
        description="Operational invocations per newly authorized episode.",
    )
    agent_profiles: dict[AgentExecutionProfile, AgentProfileSettings]
    skill_defaults: SkillDefaults = Field(default_factory=SkillDefaults)

    @model_validator(mode="after")
    def require_every_surface(self) -> ProjectSettingsRequest:
        expected = set(_SETTINGS_SURFACES)
        actual = set(self.agent_profiles)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(
                f"agent profiles must contain every surface; missing={missing}, extra={extra}"
            )
        return self


def _imported_source_store(
    data_dir: Path | None,
    project_id: str | None,
) -> ImportedProviderSourceStore | None:
    if data_dir is None or project_id is None:
        return None
    try:
        parsed = uuid.UUID(project_id)
    except ValueError:
        return None
    if parsed.version != 4 or str(parsed) != project_id:
        return None
    return ImportedProviderSourceStore(data_dir, project_id)


def project_repository_descriptors(
    repositories: list[dict[str, Any]], *, store: AppStore, project_id: str
) -> list[dict[str, Any]]:
    """Overlay team provisioning provenance on the Settings repository inventory.

    Personal projects and team repositories without resolvable evidence get no
    source fields, so nothing claims or offers a server-only state it cannot prove.
    """
    sources = team_repository_sources(store, project_id)
    if sources is None:
        return repositories
    result = []
    for repository in repositories:
        record = sources.get(repository["alias"])
        if record is None:
            result.append(repository)
            continue
        source = record.repository
        result.append(
            {
                **repository,
                "source": "github" if source is not None else "server_only",
                "github_identity": source.identity if source is not None else None,
                "can_connect": source is None and record.existing_path is None,
            }
        )
    return result


def agent_login_accounts(
    manifest: Manifest,
    store: AppStore | None,
    project_id: str | None,
    *,
    viewer_id: str | None = None,
) -> set[LoginKey]:
    """Resolve visible launch targets and retain parked Codex session bindings."""
    pairs = {
        (profile.provider, provider_login_host(manifest, profile.run_on))
        for profile in map(manifest.agent_profile, AGENT_EXECUTION_PROFILES)
    }
    if store is None:
        return {LoginKey(provider, host, MACHINE_LOGIN_ID) for provider, host in pairs}
    resolver = ProviderAccounts.for_store(store)
    accounts: set[LoginKey] = set()
    if project_id is not None:
        task_ids = {
            task.operation_id
            for task in store.agent_tasks(project_id)
            if task.status == "queued"
            or (
                task.request.get("provider") == "codex"
                and task.failure_kind == "provider_auth"
                and task.can_retry
            )
        }
        for work in parked_provider_work(store, project_id, lambda: manifest).values():
            task_ids.update(task_id for _, task_id in work.recoveries)
            task_ids.update(work.queued)
            task_ids.update(work.experiments)
            task_ids.update(group[0].origin_operation_id for group in work.watcher_groups)
        for task_id in task_ids:
            task = store.agent_task(task_id)
            if task is None or not task.request.get("provider"):
                continue
            provider = str(task.request["provider"])
            host = (
                task.stage_host or ""
                if task.stage_host is not None or task.stage_root is not None
                else provider_login_host(manifest, str(task.request.get("run_on") or "local"))
            )
            pairs.add((provider, host))
            if provider == "codex" and task.native_session_id:
                bound = resolver.bound_login(provider, host, task.native_session_id)
                if bound is not None:
                    accounts.add(bound)
    for provider, host in pairs:
        selected, _ = resolver.resolve_login(provider, host, viewer_id)
        if not isinstance(selected, LoginRefusal):
            accounts.add(selected)
        elif selected.login is not None:
            accounts.add(selected.login)
        else:
            accounts.update(
                LoginKey(provider, host, row.login_id)
                for row in store.provider_logins(provider, host)
                if (
                    row.member_id == viewer_id
                    if selected.source == "own"
                    else row.member_id is None
                )
            )
    return {key for key in accounts if not store.provider_login_state(key).removed_at}


class ProjectService:
    def __init__(
        self,
        manifest: Manifest,
        history: HistoryManager,
        paper: PaperService,
        launcher: AgentLauncher | None = None,
        data_dir: Path | None = None,
        provider_skills: ProviderSkillInventoryManager | None = None,
        project_id: str | None = None,
        repository_inventory: Callable[[], list[RegisteredRepositoryRoot]] | None = None,
        task_continuation_session: Callable[[str, str], str | None] | None = None,
        chat_graph_target: Callable[[str, str], GraphTargetRef | None] | None = None,
        project_service_resolver: Callable[[str], ProjectService] | None = None,
        paper_live: PaperLive | None = None,
        paper_admission: Callable[[str], AbstractContextManager[object]] | None = None,
    ) -> None:
        self.history = history
        self.paper = paper
        self.paper_live = paper_live or PaperLive(paper.store, lambda _project: paper.workspace)
        self._paper_admission = paper_admission
        self.paper_changes = PaperChanges(paper.store, self.paper_live, self._admit_paper_changes)
        self.launcher = launcher or AgentLauncher()
        self.provider_skills = provider_skills
        self._data_dir = data_dir
        self._project_id = project_id or history.project_id
        if data_dir is not None and self._project_id is not None:
            history.history_engine.configure_store(data_dir, self._project_id)
        self.imported_sources = _imported_source_store(data_dir, self._project_id)
        self._repository_inventory = repository_inventory
        self._task_continuation_session = task_continuation_session
        self._chat_graph_target = chat_graph_target
        self._project_service_resolver = project_service_resolver
        state_repository = manifest.repository_map[manifest.state.repository]
        state_machine = manifest.machine_map[state_repository.machine]
        app_chat_origin = AppChatOrigin(
            machine=state_repository.machine,
            host=state_machine.host if history.workspace.remote else "",
            root=(
                str(PurePosixPath(state_repository.path) / ".research" / "chat")
                if history.workspace.remote
                else str(manifest.research_dir / "chat")
            ),
        )
        canonical_project_id = project_id or history.project_id
        cache_root = None
        if data_dir is not None and canonical_project_id is not None:
            cache_root = project_cache_roots(data_dir, canonical_project_id)[0]
        self.indexer = ConversationIndexer(
            manifest,
            cache_root,
            app_chat_origin=app_chat_origin,
            login_store=paper.store,
        )
        self._index_lock = threading.Lock()
        self._indexes: dict[str, ConversationIndex] = {}
        self._chat_summary_lock = threading.Lock()
        self._chat_summary_cache: dict[Path, _ChatSummaryCacheEntry] = {}

    @contextmanager
    def _admit_paper_changes(self, project_id: str) -> Iterator[None]:
        with self._paper_admission(project_id) if self._paper_admission else nullcontext():
            self.paper.store.require_project_accepts_new_work(project_id)
            yield

    @property
    def history_engine(self) -> HistoryEngine:
        """The canonical project owner, shared by all target wrappers."""
        engine = self.history.history_engine
        if self._data_dir is not None and self._project_id is not None:
            engine.configure_store(self._data_dir, self._project_id)
        return engine

    @property
    def manifest(self) -> Manifest:
        return self.history.manifest

    def repository_ownership_inventory(
        self,
        *,
        project_id: str,
    ) -> list[RegisteredRepositoryRoot]:
        if self._repository_inventory is not None:
            return self._repository_inventory()
        return registered_repository_roots(self.manifest, project_id=project_id)

    def for_graph_target(
        self,
        target: GraphTargetRef,
        *,
        expected_episode_id: str | None = None,
        initialize: bool = True,
        refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
    ) -> ProjectService:
        """Return a service whose graph reads and writes stay on one exact target."""

        main = (
            self.history.parent if isinstance(self.history, BranchHistoryManager) else self.history
        )
        if target.kind == "main":
            if expected_episode_id is not None:
                raise ValueError("an episode-target service requires a graph branch")
            if main is self.history:
                return self
            history = main
        else:
            assert target.branch_id is not None
            history = main.branch(
                target.branch_id,
                expected_episode_id=expected_episode_id,
                expected_project_id=self._project_id,
                initialize=initialize,
                refresh_max_age_seconds=refresh_max_age_seconds,
            )
        return ProjectService(
            history.manifest,
            history,
            self.paper,
            self.launcher,
            data_dir=self._data_dir,
            provider_skills=self.provider_skills,
            project_id=self._project_id,
            repository_inventory=self._repository_inventory,
            task_continuation_session=self._task_continuation_session,
            chat_graph_target=self._chat_graph_target,
            project_service_resolver=self._project_service_resolver,
            paper_live=self.paper_live,
            paper_admission=self._paper_admission,
        )

    def resolve_project_service(self, project_id: str) -> ProjectService:
        """Open a canonical project; callers enforce their own read authority first."""
        if project_id == self._project_id:
            return self
        if self._project_service_resolver is None:
            raise KeyError(project_id)
        return self._project_service_resolver(project_id)

    def chat_path(
        self,
        chat_id: str,
        *,
        chat_scope: Literal["node", "project"],
        node_id: str | None,
    ) -> Path:
        return _canonical_chat_path(
            self.history.workspace.root,
            chat_id,
            chat_scope=chat_scope,
            node_id=node_id,
        )

    def restore_canonical_chat(
        self,
        source: Path,
        *,
        expected_sha256: str,
        expected_size: int,
        operation_projects: Mapping[str, str],
    ) -> Path:
        """Validate and publish one archived canonical chat without appending to it."""

        try:
            metadata = source.lstat()
        except OSError as exc:
            raise ValueError("The archived canonical chat is unavailable.") from exc
        if not stat.S_ISREG(metadata.st_mode) or expected_size <= 0:
            raise ValueError("The archived canonical chat is not a nonempty regular file.")
        descriptor = CanonicalChatBackupSource(
            path=source,
            observed_bytes=expected_size,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
        digest = hashlib.sha256()
        observed = 0
        for line in iter_canonical_chat_backup_prefix(
            descriptor,
            project_id=self._project_id,
            operation_projects=operation_projects,
        ):
            digest.update(line)
            observed += len(line)
        if observed != expected_size or digest.hexdigest() != expected_sha256:
            raise ValueError("The archived canonical chat differs from its manifest proof.")
        relative = Path("chat") / source.name
        self.history.workspace.restore_exact_file(
            relative,
            source,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
        return self.history.workspace.root / relative

    def chat_summaries(
        self,
        *,
        offset: int = 0,
        limit: int = CHAT_PAGE_DEFAULT_LIMIT,
        inventory: bool = False,
        task_summaries: list[ChatSummary] | None = None,
    ) -> ChatSummaryPage:
        """Page canonical chats, optionally including every target and task-only chat."""
        if offset < 0:
            raise ValueError("chat offset must be non-negative")
        if limit < 1 or limit > CHAT_PAGE_MAX_LIMIT:
            raise ValueError(f"chat limit must be between 1 and {CHAT_PAGE_MAX_LIMIT}")
        summaries = {
            summary.chat_id: summary.model_copy(
                update={
                    "graph_title": "Main"
                    if summary.graph_target.kind == "main"
                    else (summary.graph_target.branch_id or "")[:8],
                }
            )
            for summary in self._canonical_chat_summaries()
        }
        for task in task_summaries or []:
            canonical = summaries.get(task.chat_id)
            if canonical is None:
                # A dormant fork has a readable empty transcript. Other task-only
                # conversations remain inventory-only until their transcript exists.
                if inventory or task.origin is not None:
                    summaries[task.chat_id] = task
            elif canonical.graph_target == task.graph_target:
                summaries[task.chat_id] = canonical.model_copy(
                    update={
                        "origin": task.origin,
                        "conversation_kind": task.conversation_kind,
                        "orchestrator_episode_id": task.orchestrator_episode_id,
                        "human_turn_refusal": task.human_turn_refusal,
                        "graph_title": task.graph_title,
                        # Task records know which turns were artifact edits; history does not.
                        "agent_setup": task.agent_setup or canonical.agent_setup,
                        # Human turns after an Auto-research chain ends join its one card.
                        **(
                            {
                                "message_count": canonical.message_count + task.message_count,
                                "updated_at": max(
                                    canonical.updated_at,
                                    task.updated_at,
                                    key=datetime.fromisoformat,
                                ),
                            }
                            if task.conversation_kind == "auto_research"
                            else {}
                        ),
                    }
                )
        chats = sorted(
            (
                summary
                for summary in summaries.values()
                if inventory or summary.graph_target == self.history.graph_target
            ),
            key=lambda item: (datetime.fromisoformat(item.updated_at), item.chat_id),
            reverse=True,
        )
        # The project inventory is one consistent snapshot: paging a list sorted by
        # mutable recency would skip a conversation updated between requests.
        return ChatSummaryPage(
            items=chats if inventory else chats[offset : offset + limit],
            total=len(chats),
            offset=0 if inventory else offset,
            limit=limit,
        )

    def chat_transcript(self, chat_id: str) -> ChatTranscript | None:
        return self.chat_transcripts([chat_id]).get(chat_id)

    def chat_transcripts(self, chat_ids: list[str]) -> dict[str, ChatTranscript]:
        """Read selected canonical chats in one scan of this exact graph target."""
        for chat_id in chat_ids:
            try:
                normalized = str(uuid.UUID(chat_id))
            except ValueError as exc:
                raise ValueError("chat_id must be a UUID") from exc
            if normalized != chat_id:
                raise ValueError("chat_id must be a canonical UUID")
        requested = set(chat_ids)
        if not requested:
            return {}
        suffixes = tuple(f"-{chat_id}.jsonl" for chat_id in requested)
        transcripts: dict[str, ChatTranscript | None] = {}
        with self.history.workspace.read_snapshot() as root:
            for path, _ in self._canonical_chat_files(root):
                if (
                    path.name.endswith(suffixes)
                    and (transcript := self._read_chat_transcript(path, root)) is not None
                    and transcript.chat_id in requested
                    and transcript.graph_target == self.history.graph_target
                ):
                    transcripts[transcript.chat_id] = (
                        None if transcript.chat_id in transcripts else transcript
                    )
        # The same UUID under two canonical node/project paths is ambiguous.
        return {chat_id: transcript for chat_id, transcript in transcripts.items() if transcript}

    def _chat_project_id(self) -> str:
        """Conversation names are project-scoped; only a bound project has chats."""
        if self._project_id is None:
            raise ValueError("Chat names require a bound project.")
        return self._project_id

    def _canonical_chat_summaries(self) -> list[ChatSummary]:
        with self.history.workspace.read_snapshot() as root:
            files = self._canonical_chat_files(root)
            # Keyed inside the root: each display generation is a new directory.
            live_paths = {path.relative_to(root) for path, _ in files}
            summaries: dict[str, ChatSummary | None] = {}
            with self._chat_summary_lock:
                for stale in self._chat_summary_cache.keys() - live_paths:
                    del self._chat_summary_cache[stale]
                for path, fingerprint in files:
                    key = path.relative_to(root)
                    cached = self._chat_summary_cache.get(key)
                    if cached is None or cached.fingerprint != fingerprint:
                        transcript = self._read_chat_transcript(path, root)
                        summary = (
                            ChatSummary.model_validate(transcript.model_dump(exclude={"messages"}))
                            if transcript is not None
                            else None
                        )
                        cached = _ChatSummaryCacheEntry(fingerprint, summary)
                        self._chat_summary_cache[key] = cached
                    if cached.summary is None:
                        continue
                    chat_id = cached.summary.chat_id
                    if chat_id in summaries:
                        # One conversation has exactly one canonical file. Ambiguous
                        # duplicates are safer hidden than selected by filesystem order.
                        summaries[chat_id] = None
                    else:
                        summaries[chat_id] = cached.summary
            return [
                summary.model_copy(
                    update={
                        "title": self.paper.store.chat_name(
                            self._chat_project_id(), summary.chat_id, summary.title
                        )
                    }
                )
                for summary in summaries.values()
                if summary is not None
            ]

    def _canonical_chat_files(
        self,
        root: Path,
    ) -> list[tuple[Path, tuple[int, int, int, int, int]]]:
        """Return safe canonical file candidates from the pinned read root."""

        chat_dir = root / "chat"
        try:
            directory_stat = chat_dir.lstat()
        except OSError:
            return []
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
            return []

        files: list[tuple[Path, tuple[int, int, int, int, int]]] = []
        try:
            candidates = sorted(chat_dir.iterdir(), key=lambda path: path.name)
        except OSError:
            return []
        for path in candidates:
            if path.parent != chat_dir or path.suffix != ".jsonl":
                continue
            try:
                file_stat = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
                continue
            files.append(
                (
                    path,
                    (
                        file_stat.st_dev,
                        file_stat.st_ino,
                        file_stat.st_size,
                        file_stat.st_mtime_ns,
                        file_stat.st_ctime_ns,
                    ),
                )
            )
        return files

    def _read_chat_transcript(self, path: Path, root: Path) -> ChatTranscript | None:
        """Read one chat file under `root`, the pinned state root it was listed from."""
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    return None
                with os.fdopen(descriptor, encoding="utf-8") as handle:
                    descriptor = -1
                    lines = handle.read().split("\n")
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            raw_records = [json.loads(line) for line in lines if line.strip()]
            records = [_StoredChatRecord.model_validate(record) for record in raw_records]
            if not records:
                return None
            targets = {
                record.graph_target.key: record.graph_target
                for record in records
                if record.graph_target is not None
            }
            if self._chat_graph_target is not None:
                bound_target = self._chat_graph_target(self._project_id, records[0].session_id)
                if bound_target is not None:
                    targets[bound_target.key] = bound_target
                elif not targets and any(record.operation_id for record in records):
                    # A task-backed legacy transcript needs a durable target
                    # binding; missing operational history cannot imply main.
                    return None
            if len(targets) > 1:
                return None
            graph_target = next(iter(targets.values()), GraphTargetRef())
            records = _fold_chat_receipts(
                [record.model_copy(update={"graph_target": graph_target}) for record in records]
            )
        except (OSError, TypeError, ValueError):
            return None
        if not records:
            return None

        first = records[0]
        try:
            timestamps = [datetime.fromisoformat(record.timestamp) for record in records]
            for record in records:
                _validate_stored_chat_record(
                    record,
                    first=first,
                    root=root,
                    path=path,
                )
        except ValueError:
            return None
        chat_id = first.session_id
        if first.chat_scope == "node":
            kind: Literal["node_chat", "project_chat"] = "node_chat"
        else:
            kind = "project_chat"

        messages: list[ChatMessage] = []
        for record in records:
            native_session_id = record.native_session_id
            if self._task_continuation_session is not None:
                native_session_id = (
                    self._task_continuation_session(self._project_id, record.operation_id)
                    if record.operation_id is not None
                    else None
                )
            messages.append(
                ChatMessage(
                    message_id=record.uuid,
                    operation_id=record.operation_id,
                    role=record.role,
                    text=record.text,
                    timestamp=record.timestamp,
                    native_session_id=native_session_id,
                    provider=record.provider,
                    model=record.model,
                    reasoning=record.reasoning,
                    execution_machine=record.execution_machine,
                    applied_revision=record.applied_revision,
                    mode=record.mode,
                    graph_update=record.graph_update,
                    turn_outcome=(
                        record.turn_outcome
                        if record.role == "assistant" and record.mode == "work"
                        else None
                    ),
                    trigger=record.trigger,
                    steering=record.steering,
                    attachments=record.attachments,
                )
            )
        first_message = next((message.text for message in messages if message.role == "user"), "")
        preview_source = next(
            (message.text for message in reversed(messages) if message.text.strip()),
            "",
        )
        preview = " ".join(preview_source.split())[:CHAT_PREVIEW_MAX_CHARS]
        updated_at = records[max(range(len(records)), key=timestamps.__getitem__)].timestamp
        # Only for chats without task records (older or transferred ones): history
        # cannot tell an artifact edit, which may run an older agent, from a turn.
        newest = max(
            (index for index, record in enumerate(records) if record.provider),
            key=lambda index: (timestamps[index], index),
            default=None,
        )
        setup = (
            chat_agent_setup(
                {
                    "provider": records[newest].provider,
                    "model": records[newest].model,
                    "reasoning": records[newest].reasoning,
                    "run_on": records[newest].execution_machine,
                },
                records[newest].timestamp,
            )
            if newest is not None
            else None
        )
        return ChatTranscript(
            chat_id=chat_id,
            graph_target=graph_target,
            kind=kind,
            node_id=first.node_id,
            # Names belong to operational storage, never immutable transcript text.
            title=self.paper.store.chat_name(
                self._chat_project_id(),
                chat_id,
                first_message if first_message.strip() else first.node_id or "Chat",
            ),
            updated_at=updated_at,
            message_count=len(messages),
            last_message_preview=preview,
            agent_setup=ChatAgentSetup.model_validate(setup) if setup else None,
            messages=messages,
        )

    def project_snapshot(
        self,
        *,
        materialization: MaterializationResult | None = None,
        head: GraphHeadRef | None = None,
    ) -> _ProjectSnapshotDraft:
        with self.history.workspace.read_snapshot() as root:
            reader = self
            if self.history.workspace.remote:
                reader = copy.copy(self)
                reader.history = copy.copy(self.history)
                reader.history.manifest = load_manifest(
                    root / "manifest.toml", project_root=self.manifest.path.parent.parent
                )
                reader.history.manifest._path = self.manifest.path
            return reader._project_snapshot(materialization=materialization, head=head)

    def _project_snapshot(
        self,
        *,
        materialization: MaterializationResult | None,
        head: GraphHeadRef | None,
    ) -> _ProjectSnapshotDraft:
        if materialization is None:
            materialization = self.history.current_materialization()
        state = materialization.state
        if head is None:
            # Previews and Sync report the exact head, transition id included; a
            # display head without it reads as a different head at the same
            # revision and restarts the browser's staged preview on every poll.
            # A halted replay has no transition chain to name.
            head = (
                self.history.head_ref(materialization)
                if state.replay_status == "complete"
                else GraphHeadRef(target=self.history.graph_target, revision=state.revision)
            )
        if head.target != self.history.graph_target or head.revision != state.revision:
            raise ValueError(
                "The graph snapshot and its head must describe the same target revision."
            )
        primary = project_primary_question(state)
        attention = project_graph_attention(state)
        counts = project_counts(state, attention)
        refresh_profile = self.manifest.agent_profile("refresh")
        profiles = self.effective_profiles(self.manifest, self.launcher)
        return _ProjectSnapshotDraft(
            {
                "name": self.manifest.name,
                "revision": state.revision,
                "graph_target": head.target.model_dump(mode="json"),
                "graph_head": head.model_dump(mode="json"),
                "graph_changes": None,
                "state_repository": self.manifest.state.repository,
                "canonical_state": self.history.workspace.status().model_dump(mode="json"),
                "run_on": refresh_profile.run_on,
                "project_truth_scope": state.project_truth_scope,
                "default_run_truth_scope": self.manifest.agent.default_run_truth_scope,
                "default_auto_research_invocation_ceiling": (
                    self.manifest.agent.default_auto_research_invocation_ceiling
                ),
                "repositories": [
                    repository.model_dump() for repository in self.manifest.repositories
                ],
                "machines": [
                    machine.model_dump(include={"alias", "host", "os_account"})
                    for machine in self.manifest.machines
                ],
                "primary_question": (
                    primary.model_dump(mode="json") if primary is not None else None
                ),
                "last_refresh_at": state.last_refresh_at,
                "attention": attention.model_dump(mode="json"),
                "counts": counts.model_dump(mode="json"),
                "graph_mutation": project_graph_mutation_availability(state).model_dump(
                    mode="json"
                ),
                "graph": state.model_dump(mode="json"),
                "paper": publication_badge(self.paper.store, self.paper.project_id),
                "paper_coach": self.manifest.coach.model_dump(mode="json"),
                "agent_profiles": profiles,
                "skill_catalog": official_registry().catalog(),
                "skill_defaults": self.manifest.agent.skill_defaults.model_dump(mode="json"),
                # Undeclared fields follow the running release; complete_snapshot refills them.
                "skill_defaults_declared": sorted(
                    self.manifest.agent.skill_defaults.model_fields_set
                ),
                "provider_logins": self.provider_logins_for(
                    self.manifest, self.launcher, self._project_id
                ),
                "provider_readiness": {},
                "provider_skill_inventories": self.provider_skill_inventory_snapshot(),
                "providers": {},
                "cache_metrics": self.indexer.cache_metrics().model_dump(mode="json"),
                "validation_messages": [
                    item.model_dump(mode="json") for item in state.validation_messages
                ],
            }
        )

    def readiness_snapshot(self, *, refresh: bool = False) -> dict[str, object]:
        snapshot = self.readiness_for(
            self.manifest, self.launcher, refresh=refresh, provider_skills=self.provider_skills
        )
        snapshot["agent_profiles"] = self.effective_profiles(self.manifest, self.launcher)
        self.wait_for_provider_skill_inventories()
        snapshot["provider_skill_inventories"] = self.provider_skill_inventory_snapshot()
        return snapshot

    def wait_for_provider_skill_inventories(self) -> None:
        """Wait only for an already-scheduled startup refresh; never start one."""

        self.wait_for_provider_skill_inventories_for(
            self.manifest, self.launcher, self.provider_skills
        )

    @staticmethod
    def wait_for_provider_skill_inventories_for(
        manifest: Manifest,
        launcher: AgentLauncher,
        provider_skills: ProviderSkillInventoryManager | None,
    ) -> None:
        if provider_skills is None:
            return
        for machine in manifest.machines:
            for provider in PROVIDER_IDS:
                provider_skills.wait(
                    provider,
                    machine.host,
                    launcher.provider_binary(provider, machine.host),
                )

    def provider_skill_inventory_snapshot(self) -> dict[str, dict[ProviderId, object]]:
        """Return startup-cached provider skills without probing a provider."""

        return self.provider_skill_inventories_for(
            self.manifest, self.launcher, self.provider_skills
        )

    @staticmethod
    def provider_skill_inventories_for(
        manifest: Manifest,
        launcher: AgentLauncher,
        provider_skills: ProviderSkillInventoryManager | None,
    ) -> dict[str, dict[ProviderId, object]]:
        """Project provider inventories mapped through its machine aliases."""

        if provider_skills is None:
            return {
                machine.alias: {provider: None for provider in PROVIDER_IDS}
                for machine in manifest.machines
            }
        return {
            machine.alias: {
                provider: provider_skills.snapshot(
                    provider,
                    machine.host,
                    launcher.provider_binary(provider, machine.host),
                    machine.alias,
                ).model_dump(mode="json")
                for provider in PROVIDER_IDS
            }
            for machine in manifest.machines
        }

    @staticmethod
    def provider_logins_for(
        manifest: Manifest,
        launcher: AgentLauncher,
        project_id: str | None,
        *,
        viewer_id: str | None = None,
        sign_ins: ProviderSignInRunner | None = None,
    ) -> list[dict[str, object]]:
        """Project notices use the same viewer-filtered rows as Settings."""
        if launcher.accounts is None:
            return []
        store = launcher.accounts.store
        accounts = agent_login_accounts(manifest, store, project_id, viewer_id=viewer_id)
        rows = [
            provider_login_row(
                store,
                launcher.accounts.credentials,
                sign_ins,
                key,
                viewer_id=viewer_id,
                machines=sorted(
                    machine.name for machine in store.space_machines() if machine.host == key.host
                ),
                provider_path=store.provider_binary(key.provider, key.host),
                agent_used=True,
            )
            for key in sorted(accounts)
            if profile_for(key.provider).authentication.manages_login
        ]
        if viewer_id is not None:
            pairs = {
                (profile.provider, provider_login_host(manifest, profile.run_on))
                for profile in map(manifest.agent_profile, AGENT_EXECUTION_PROFILES)
            }
            for provider, host in sorted(pairs):
                selected, _ = launcher.accounts.resolve_login(provider, host, viewer_id)
                if (
                    isinstance(selected, LoginRefusal)
                    and selected.login is None
                    and selected.source == "own"
                    and not any(
                        row.member_id == viewer_id for row in store.provider_logins(provider, host)
                    )
                ):
                    rows.append(missing_own_login_row(provider, host, viewer_id))
        return rows

    @staticmethod
    def viewer_readiness(
        snapshot: dict[str, object],
        manifest: Manifest,
        launcher: AgentLauncher,
        viewer_id: str,
        *,
        refresh: bool = False,
    ) -> dict[str, object]:
        """Readiness is cached per project; the login a launch would use is the viewer's.

        For a provider RCP signs in, the entry describes the login the viewer's
        setting resolves on that machine: an added login is probed itself (its
        models can differ from the machine sign-in's), and a refusal reads as
        not authenticated.
        """
        machines = snapshot.get("provider_readiness")
        accounts = launcher.accounts
        if accounts is None or not isinstance(machines, dict):
            return snapshot

        def entry(host: str, provider: str, readiness: object) -> object:
            if not isinstance(readiness, dict) or not (
                profile_for(provider).authentication.manages_login
            ):
                return readiness
            return launcher.member_readiness(
                provider,
                host=host,
                binary=launcher.provider_binary(provider, host),
                refresh=refresh,
                member_id=viewer_id,
                machine_readiness=ProviderReadiness.model_validate(
                    {"provider": provider, **readiness}
                ),
            ).model_dump(mode="json")

        resolved: dict[str, object] = {}
        for alias, providers in machines.items():
            machine = manifest.machine_map.get(alias)
            resolved[alias] = (
                {
                    provider: entry(machine.host or "", provider, readiness)
                    for provider, readiness in providers.items()
                }
                if machine is not None and isinstance(providers, dict)
                else providers
            )
        return {**snapshot, "provider_readiness": resolved}

    @staticmethod
    def readiness_for(
        manifest: Manifest,
        launcher: AgentLauncher,
        *,
        refresh: bool = False,
        provider_skills: ProviderSkillInventoryManager | None = None,
    ) -> dict[str, object]:
        """Probe providers without reading or replaying canonical project history.

        An explicit refresh is the one product path that re-probes provider-native
        skills without reusing the stored inventory, so edits to a skill become
        visible while the executable, its version, and the probe command are
        unchanged. Implicit reads never start a skill probe.
        """

        targets = [
            (
                machine.alias,
                machine.host,
                provider,
                launcher.provider_binary(provider, machine.host),
            )
            for machine in manifest.machines
            for provider in PROVIDER_IDS
        ]

        def inspect(
            host: str,
            provider: ProviderId,
            binary: str | None,
        ) -> dict[str, object]:
            readiness = launcher.readiness(
                provider, host=host, binary=binary, refresh=refresh, login_id=MACHINE_LOGIN_ID
            )
            if refresh and provider_skills is not None:
                try:
                    provider_skills.refresh(provider, host, binary, readiness, reuse_cached=False)
                except Exception as exc:
                    # The inventory records its own probe failure; only a follower
                    # that outwaited the owner reaches here, and readiness still answers.
                    logger.warning(
                        "Provider skill refresh for %s on %s did not settle: %s",
                        provider,
                        host or "local",
                        exc,
                    )
            return readiness.model_dump(mode="json")

        readiness_by_machine: dict[str, dict[ProviderId, dict[str, object]]] = {
            machine.alias: {} for machine in manifest.machines
        }
        with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as executor:
            probes = [
                (
                    alias,
                    provider,
                    executor.submit(inspect, host, provider, binary),
                )
                for alias, host, provider, binary in targets
            ]
            for alias, provider, probe in probes:
                readiness_by_machine[alias][provider] = probe.result()
        coach_machine = manifest.agent_profile("paper_coach").run_on
        return {
            "provider_readiness": readiness_by_machine,
            "state_transfers": {
                machine.alias: state_transfer_diagnostics(machine.host)
                for machine in manifest.machines
                if machine.host
            },
            "providers": readiness_by_machine[coach_machine],
        }

    def clear_rebuildable_caches(self) -> dict[str, object]:
        with self._index_lock:
            self._indexes.clear()
            return self.indexer.clear_rebuildable_caches().model_dump(mode="json")

    def graph_snapshot(self) -> dict[str, object]:
        state = self.history.state()
        return state.model_dump(mode="json")

    def index_snapshot(
        self,
        *,
        refresh: bool = False,
        execution_machine: str | None = None,
        pin_artifact: Callable[[Path], None] | None = None,
    ) -> ConversationIndex:
        with self._index_lock:
            self.indexer.manifest = self.manifest
            key = execution_machine or "cached-local"
            if refresh or key not in self._indexes:
                self._indexes[key] = self.indexer.build(
                    execution_machine=execution_machine,
                    pin_artifact=pin_artifact,
                )
            return self._indexes[key]

    def invalidate_source_index(self) -> None:
        with self._index_lock:
            self._indexes.clear()

    @staticmethod
    def _settings_profile(
        surface: AgentExecutionProfile,
        requested: AgentProfileSettings,
    ) -> AgentSurfaceConfig:
        """One saved profile, reporting a rejection the human can act on.

        A Pydantic envelope names the model and dumps the whole input. The
        settings form shows this text verbatim, so it says which profile failed
        and nothing else.
        """

        try:
            return AgentSurfaceConfig(
                provider=requested.provider,
                runtime=requested.runtime,
                model=requested.model,
                reasoning=requested.reasoning,
                run_on=requested.run_on,
            )
        except ValidationError as exc:
            detail = exc.errors()[0]["msg"].removeprefix("Value error, ")
            raise ValueError(f"{surface.replace('_', ' ')}: {detail}") from exc

    def update_settings(self, request: ProjectSettingsRequest) -> None:
        profiles = {
            surface: self._settings_profile(surface, request.agent_profiles[surface])
            for surface in _SETTINGS_SURFACES
        }
        self.history.update_agent_settings(
            self.manifest.agent.default_run_truth_scope,
            profiles,
            request.skill_defaults,
            request.default_auto_research_invocation_ceiling,
        )
        self.paper.manifest = self.manifest
        self.invalidate_source_index()

    def add_existing_repository(
        self, alias: str, path: str, writable: bool, *, authorized_by: AuthorizedHuman | None
    ) -> None:
        """Add a Git folder on the base machine as a repository: one human approval.

        The same approval a team add publishes; the history manager mirrors the
        descriptor into the manifest.
        """

        if re.fullmatch(r"[a-z][a-z0-9-]{0,47}", alias) is None:
            raise ValueError(
                "repository names must start with a letter and use lowercase letters, "
                "numbers, or hyphens"
            )
        machine = self.manifest.base_machine
        folder = PurePosixPath(path)
        if not folder.is_absolute() or ".." in folder.parts or str(folder) != path:
            raise ValueError("Choose a folder by its full path.")
        if alias in self.manifest.repository_map:
            raise ValueError("The repository name already belongs to this project.")
        for repository in self.manifest.repositories:
            other = PurePosixPath(repository.path)
            if repository.machine == machine and (
                folder == other or folder.is_relative_to(other) or other.is_relative_to(folder)
            ):
                raise ValueError("The folder overlaps a repository of this project.")
        from rcp.setup import require_git_work_tree

        require_git_work_tree(self.manifest.machine_map[machine].host, path)

        def add(state: GraphState) -> list[Patch]:
            if alias in self.manifest.repository_map:
                raise ValueError("The repository name already belongs to this project.")
            scope = [*state.project_truth_scope, *([alias] if writable else [])]
            return [
                Patch(
                    kind="approval",
                    author="human",
                    producer="human",
                    summary=f"Add repository {alias}.",
                    ops=[
                        SetProjectTruthScopeOperation(
                            op="set_project_truth_scope",
                            truth_scope=scope,
                            repository=RepositoryDescriptor(
                                alias=alias, machine=machine, path=path
                            ),
                        )
                    ],
                    change_summary=[f"Added repository {alias}."],
                )
            ]

        self.history.append_batch_from_state(add, authorized_by=authorized_by)
        self.paper.manifest = self.manifest
        self.invalidate_source_index()

    def set_repository_writable(
        self, alias: str, writable: bool, *, authorized_by: AuthorizedHuman | None
    ) -> None:
        """Add one repository to, or drop it from, the truth scope: a human approval."""

        if alias not in self.manifest.repository_map:
            raise KeyError(alias)

        def change(state: GraphState) -> list[Patch]:
            current = list(state.project_truth_scope)
            if (alias in current) == writable:
                return []
            scope = [*current, alias] if writable else [item for item in current if item != alias]
            summary = f"Agents may write {alias}." if writable else f"Agents only read {alias}."
            return [
                Patch(
                    kind="approval",
                    author="human",
                    producer="human",
                    summary=summary,
                    ops=[
                        SetProjectTruthScopeOperation(
                            op="set_project_truth_scope", truth_scope=scope
                        )
                    ],
                    change_summary=[summary],
                )
            ]

        self.history.append_batch_from_state(change, authorized_by=authorized_by)
        self.paper.manifest = self.manifest

    def add_machine(self, alias: str, host: str, os_account: str) -> None:
        """Append one execution machine, copied from a space machine card."""

        if re.fullmatch(r"[a-z][a-z0-9-]{0,47}", alias) is None:
            raise ValueError(
                "machine names must start with a letter and use lowercase letters, "
                "numbers, or hyphens"
            )
        self.history.add_machine(MachineConfig(alias=alias, host=host, os_account=os_account))
        self.paper.manifest = self.manifest
        self.invalidate_source_index()

    def remove_machine(self, alias: str) -> None:
        """Drop one machine this project no longer uses; its space card stays."""

        self.history.remove_machine(alias)
        self.paper.manifest = self.manifest
        self.invalidate_source_index()

    def machine_prompt_profiles(self, run_on: str) -> list[dict[str, str]]:
        """Every project machine as seen from `run_on`, read from the manifest and cards now."""
        context = machine_compute_context(
            self.manifest, self.launcher.machine_cards(), execution_machine=run_on
        )
        return [profile.model_dump(mode="json") for profile in context.active]

    def review_node(
        self,
        node_id: str,
        request: ReviewRequest,
        *,
        authorized_by: AuthorizedHuman | None = None,
    ) -> GraphState:
        state = self.history.state()
        self.history.require_writable(state)
        if node_id not in state.nodes:
            raise KeyError(node_id)
        node = state.nodes[node_id]
        patch = Patch(
            kind="approval",
            author="human",
            summary=f"Marked “{node.title}” {request.standing}.",
            ops=[{"op": "set_standing", "node_id": node_id, "standing": request.standing}],
            change_summary=[f"“{node.title}” is now {request.standing}."],
        )
        _, result = self.history.append(patch, authorized_by=authorized_by)
        return result.state

    def sync_graph(
        self,
        request: GraphSyncRequest,
        *,
        active_control_node_ids: set[str],
        authorized_by: AuthorizedHuman | None = None,
    ) -> GraphState:
        """Commit one project-wide human draft in one canonical transaction."""

        transition = self.sync_graph_transition(
            request,
            active_control_node_ids=active_control_node_ids,
            authorized_by=authorized_by,
        )
        return (
            transition.projection.graph
            if transition is not None
            else self.history.current_materialization().state
        )

    def preview_sync_graph(
        self,
        request: GraphSyncRequest,
        *,
        active_control_node_ids: set[str],
        authorized_by: AuthorizedHuman | None = None,
    ) -> PreparedTransition:
        """Prepare the same complete Sync candidate without writing canonical history."""

        return self.history.preview_batch_from_state(
            lambda state: self._build_sync_patches(
                request,
                state,
                active_control_node_ids=active_control_node_ids,
            ),
            expected_revision=request.base_revision,
            authorized_by=authorized_by,
        )

    def sync_graph_transition(
        self,
        request: GraphSyncRequest,
        *,
        active_control_node_ids: set[str],
        authorized_by: AuthorizedHuman | None = None,
    ) -> CommittedTransition | None:
        """Commit one project-wide human draft as exactly one graph transition."""

        has_staged_work = (
            any(
                (
                    request.nodes,
                    request.proposals,
                    request.custom_nodes,
                    request.removed_node_ids,
                    request.added_edges,
                    request.removed_edge_ids,
                )
            )
            or request.ontology is not None
        )
        if not has_staged_work:
            return None
        try:
            prepared, result = self.history.append_batch_from_state(
                lambda state: self._build_sync_patches(
                    request,
                    state,
                    active_control_node_ids=active_control_node_ids,
                ),
                expected_revision=request.base_revision,
                authorized_by=authorized_by,
            )
        except ValueError as exc:
            if "graph changed after this draft began" in str(exc):
                raise NodeEditConflict(str(exc)) from exc
            raise
        if not prepared:
            return None
        patch = prepared[0]
        if patch.transition is None:
            raise RuntimeError("graph Sync committed without transition provenance")
        return CommittedTransition(
            patch=patch,
            projection=project_transition_projection(
                result.state,
                patch.transition,
                canonical=True,
            ),
        )

    def _build_sync_patches(
        self,
        request: GraphSyncRequest,
        state: GraphState,
        *,
        active_control_node_ids: set[str],
    ) -> list[Patch]:
        """Build one Sync from the same fresh state that history will append against."""

        ontology_changed = request.ontology is not None and request.ontology != state.ontology
        if (
            not any(
                (
                    request.nodes,
                    request.proposals,
                    request.custom_nodes,
                    request.removed_node_ids,
                    request.added_edges,
                    request.removed_edge_ids,
                )
            )
            and not ontology_changed
        ):
            return []

        patches: list[Patch] = []

        project_type = project_type_of(state)
        removed_node_ids = set(request.removed_node_ids)
        direct_choice_node_ids = {
            staged.node_id
            for staged in request.nodes
            if _chooses_decision_option(state, staged.node_id, staged.changes)
        }
        superseded_proposal_ids = {
            proposal.id
            for proposal in state.proposals.values()
            if proposal.status == "pending"
            and any(
                proposal_updates_node(proposal, decision_id)
                for decision_id in direct_choice_node_ids
            )
        }
        directly_changed_node_ids = {item.node_id for item in request.nodes}
        for judgment in request.proposals:
            if judgment.decision != "approved":
                continue
            proposal = state.proposals.get(judgment.proposal_id)
            if proposal is None or proposal.status != "pending":
                continue
            if proposal.id in superseded_proposal_ids:
                continue
            overlapping_node_ids = sorted(
                directly_changed_node_ids.intersection(proposal.related_node_ids)
            )
            if overlapping_node_ids:
                raise NodeEditConflict(
                    "A graph Sync cannot approve a Proposal and directly change its dependent "
                    "node in the same draft: "
                    f"{', '.join(overlapping_node_ids)}."
                )

        effective_ontology = request.ontology if ontology_changed else state.ontology
        if ontology_changed:
            current_types = {item.name for item in state.ontology.types}
            newly_defined_types = {
                item.name for item in effective_ontology.types if item.name not in current_types
            }
            used_new_types = sorted(
                {
                    node.extension_type
                    for node in request.custom_nodes
                    if node.extension_type in newly_defined_types
                }
            )
            if used_new_types:
                raise ValueError(
                    "This draft both defines and uses a new ontology type "
                    f"({', '.join(used_new_types)}). Sync the ontology first, then create "
                    "nodes of that type in a new draft."
                )
            patches.append(
                Patch(
                    kind="approval",
                    author="human",
                    summary="Updated the project ontology.",
                    ops=[
                        {
                            "op": "set_ontology",
                            "ontology": effective_ontology.model_dump(mode="json"),
                        }
                    ],
                    change_summary=["Updated the project ontology."],
                )
            )

        active_types = {item.name: item for item in effective_ontology.types if not item.deprecated}
        for node in request.custom_nodes:
            extension_type = node.extension_type
            definition = active_types.get(extension_type)
            if extension_type is not None and definition is None:
                raise ValueError(
                    f"Custom node {node.id} uses inactive or unknown ontology type "
                    f"{extension_type!r}."
                )
            if definition is not None and node.type != definition.base_type:
                raise ValueError(
                    f"Custom node {node.id} must use base type {definition.base_type!r} "
                    f"for ontology type {extension_type!r}."
                )
            prepared = node.model_copy(
                update={
                    "standing": Standing.ASSERTED,
                    "created_rev": 0,
                    "updated_rev": 0,
                    "source_refs": [],
                }
            )
            patches.append(
                Patch(
                    kind="approval",
                    author="human",
                    summary=f"Created “{node.title}”.",
                    ops=[
                        {
                            "op": "create_nodes",
                            "nodes": [prepared.model_dump(mode="json", exclude_unset=True)],
                        }
                    ],
                    change_summary=[
                        f"Created “{node.title}” as a {(extension_type or node.type).replace('_', ' ')}."
                    ],
                )
            )

        if request.removed_edge_ids or request.added_edges:
            for edge_id in request.removed_edge_ids:
                if edge_id not in state.edges:
                    raise KeyError(edge_id)
            edge_ops: list[dict[str, Any]] = []
            if request.removed_edge_ids:
                edge_ops.append({"op": "remove_edges", "edge_ids": request.removed_edge_ids})
            if request.added_edges:
                edge_ops.append(
                    {
                        "op": "create_edges",
                        "edges": [
                            edge.model_dump(mode="json", exclude_none=True)
                            for edge in request.added_edges
                        ],
                    }
                )
            patches.append(
                Patch(
                    kind="approval",
                    author="human",
                    summary="Edited graph connections.",
                    ops=edge_ops,
                    change_summary=["Edited graph connections."],
                )
            )

        if request.removed_node_ids:
            for node_id in request.removed_node_ids:
                node = state.nodes.get(node_id)
                if node is None:
                    raise KeyError(node_id)
                if node.standing == Standing.ACCEPTED:
                    raise NodeEditConflict(
                        f"Accepted node {node_id} cannot be removed; withdraw its acceptance "
                        "and Sync before removing it."
                    )
                if project_type.is_control_node(node.type):
                    control = derive_experiment_control_state(
                        state,
                        node_id,
                        active_control_node_ids=active_control_node_ids,
                    )
                    if control.active:
                        raise NodeEditConflict(
                            f"Experiment {node_id} cannot be removed while its bounded "
                            "experiment loop is active."
                        )
            node_ids = list(request.removed_node_ids)
            titles = ", ".join(f"“{state.nodes[node_id].title}”" for node_id in node_ids)
            patches.append(
                Patch(
                    kind="approval",
                    author="human",
                    summary=f"Removed {titles}.",
                    ops=[{"op": "remove_nodes", "node_ids": node_ids}],
                    change_summary=[f"Removed {titles}."],
                )
            )

        decision_state = state
        for patch in patches:
            decision_state = _stage_sync_patch(decision_state, patch)
        for staged in request.proposals:
            proposal = decision_state.proposals.get(staged.proposal_id)
            if proposal is None:
                raise KeyError(staged.proposal_id)
            if proposal.id in superseded_proposal_ids:
                continue
            if proposal.status != "pending":
                raise NodeEditConflict(f"Proposal {proposal.id} is no longer pending.")
            stale_from_removal = bool(removed_node_ids.intersection(proposal.related_node_ids))
            stale_reason = (
                f"The proposal “{proposal.title}” became stale because a related research "
                "concept was removed in this Sync."
                if stale_from_removal
                else None
            )
            patch = _proposal_judgment_patch(
                decision_state,
                proposal,
                decision=staged.decision,
                reason=staged.reason,
                stale_reason=stale_reason,
            )
            patches.append(patch)
            decision_state = _stage_sync_patch(decision_state, patch)

        for staged in request.nodes:
            node = state.nodes.get(staged.node_id)
            if node is None:
                raise KeyError(staged.node_id)
            if staged.base_updated_rev != node.updated_rev:
                raise NodeEditConflict(
                    f"{node.id} changed after this draft began; reload before syncing it."
                )
            ops: list[dict[str, Any]] = []
            change_summary: list[str] = []
            changed_fields: set[str] = set()
            display_title = str(staged.changes.get("title", node.title))
            is_direct_choice = project_type.is_chooser(node.type) and is_decision_choice(
                staged.changes
            )
            if staged.changes:
                allowed = set(HUMAN_EDITABLE_NODE_FIELDS[node.type])
                selected_option = checked_human_decision_choice(node, staged.changes)
                if is_direct_choice:
                    allowed.update({"selected_option", "status"})
                if "extension_fields" in staged.changes:
                    self._validate_human_extension_fields(state, node, staged.changes)
                    allowed.add("extension_fields")
                disallowed = sorted(set(staged.changes) - allowed)
                if disallowed:
                    raise ValueError(
                        f"Direct edits to {node.id} cannot change: {', '.join(disallowed)}."
                    )
                current = node.model_dump(mode="python")
                changed_fields = {
                    field for field, value in staged.changes.items() if current[field] != value
                }
                if changed_fields or is_direct_choice:
                    candidate = {**current, **staged.changes}
                    try:
                        type(node).model_validate(candidate)
                    except ValueError as exc:
                        raise ValueError(f"Invalid edit for {node.id}: {exc}") from exc
                    ops.append(
                        {
                            "op": "update_nodes",
                            "nodes": [
                                {
                                    "id": node.id,
                                    "base_updated_rev": staged.base_updated_rev,
                                    "changes": staged.changes,
                                }
                            ],
                        }
                    )
                    ordinary_changed_fields = changed_fields - {"selected_option", "status"}
                    if ordinary_changed_fields:
                        change_summary.append(f"Updated wording for “{display_title}”.")
                    if is_direct_choice:
                        change_summary.append(
                            f"Selected “{selected_option}” for “{display_title}”."
                        )
                    elif "status" in changed_fields:
                        change_summary.append(
                            f"Updated lifecycle for “{display_title}”: status is now "
                            f"{staged.changes['status']}."
                        )
            if is_direct_choice:
                withdrawals = [
                    proposal
                    for proposal in state.proposals.values()
                    if proposal.status == "pending" and proposal_updates_node(proposal, node.id)
                ]
                if withdrawals:
                    ops.append(
                        {
                            "op": "resolve_proposals",
                            "resolutions": [
                                {
                                    "id": proposal.id,
                                    "status": "withdrawn",
                                    "reason": (
                                        f"The human decided {node.id} directly, making this "
                                        "Proposal stale."
                                    ),
                                }
                                for proposal in withdrawals
                            ],
                        }
                    )
                    change_summary.extend(
                        f"Withdrew Proposal “{proposal.title}” because the human decided "
                        f"“{display_title}” directly."
                        for proposal in withdrawals
                    )
            if staged.cancel_attempt_ids:
                ops.append(
                    {
                        "op": "update_nodes",
                        "nodes": [
                            {
                                "id": node.id,
                                "base_updated_rev": staged.base_updated_rev,
                                "changes": {
                                    "attempts": released_attempts(node, staged.cancel_attempt_ids)
                                },
                            }
                        ],
                    }
                )
                change_summary.append(f"Released open experiment attempts for “{display_title}”.")
            resulting_standing = staged.standing
            if (
                changed_fields
                and not is_direct_choice
                and resulting_standing is None
                and node.standing != Standing.ASSERTED
            ):
                resulting_standing = Standing.ASSERTED
            if resulting_standing is not None and resulting_standing != node.standing:
                ops.append(
                    {
                        "op": "set_standing",
                        "node_id": node.id,
                        "standing": resulting_standing,
                    }
                )
                change_summary.append(f"“{display_title}” is now {resulting_standing}.")
            if ops:
                patches.append(
                    Patch(
                        kind="approval",
                        author="human",
                        summary=f"Synced staged changes for “{display_title}”.",
                        ops=ops,
                        change_summary=change_summary,
                        human_action="decision_choice" if is_direct_choice else None,
                    )
                )

        return patches

    @staticmethod
    def _validate_human_extension_fields(
        state: GraphState,
        node: ProjectNode,
        changes: dict[str, Any],
    ) -> None:
        values = changes.get("extension_fields")
        if not isinstance(values, dict):
            raise ValueError("Extension fields must be submitted as one complete object.")
        owner_types = {node.type}
        if node.extension_type is not None:
            owner_types.add(node.extension_type)
        definitions = {
            field.name: field for field in state.ontology.fields if field.owner_type in owner_types
        }
        missing = object()
        protected = sorted(
            name
            for name in set(node.extension_fields) | set(values)
            if (definition := definitions.get(name)) is None or definition.deprecated
            if node.extension_fields.get(name, missing) != values.get(name, missing)
        )
        if protected:
            raise ValueError(
                f"Extension fields for {node.id} are not active on its ontology type and "
                f"cannot be changed: {', '.join(protected)}."
            )

    def decide_proposal(
        self,
        proposal_id: str,
        request: ProposalDecisionRequest,
        *,
        authorized_by: AuthorizedHuman | None = None,
    ) -> GraphState:
        def build(state: GraphState) -> list[Patch]:
            proposal = state.proposals.get(proposal_id)
            if proposal is None:
                raise KeyError(proposal_id)
            if proposal.status != "pending":
                raise ValueError("proposal is not pending")
            return [
                _proposal_judgment_patch(
                    state,
                    proposal,
                    decision=request.decision,
                    reason=request.reason,
                )
            ]

        _, result = self.history.append_batch_from_state(
            build,
            authorized_by=authorized_by,
        )
        return result.state

    def resolve_agent_profile(
        self,
        surface: AgentExecutionProfile,
        *,
        provider: ProviderId | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        run_on: str | None = None,
    ) -> AgentSurfaceConfig:
        base = self.manifest.agent_profile(surface)
        updates: dict[str, object] = {}
        if provider is not None:
            updates["provider"] = provider
            if provider != base.provider and model is None:
                updates["model"] = ""
            if provider != base.provider:
                updates["runtime"] = configured_runtime(provider, None)
        if model is not None:
            updates["model"] = model
        if reasoning is not None:
            updates["reasoning"] = reasoning
        if run_on is not None:
            if run_on not in self.manifest.machine_map:
                raise ValueError(f"unknown execution machine: {run_on}")
            updates["run_on"] = require_base_machine(
                run_on, self.manifest.base_machine, label=surface.replace("_", " ")
            )
        return self._with_catalog_head(
            self.manifest, self.launcher, base.model_copy(update=updates)
        )

    @classmethod
    def effective_profiles(
        cls,
        manifest: Manifest,
        launcher: AgentLauncher,
    ) -> dict[AgentExecutionProfile, dict[str, object]]:
        """Every settings profile with the model it would run, for both projections.

        `model` stays the manifest value, so an unnamed profile is saved back
        unnamed and keeps following the catalog. `effective_model` is what runs:
        the catalog head once that provider's readiness is cached, else the
        manifest value. A readiness refresh re-exports these with the refreshed
        catalog, so no surface derives the head itself.
        """
        profiles: dict[AgentExecutionProfile, dict[str, object]] = {}
        for surface in _SETTINGS_SURFACES:
            saved = manifest.agent_profile(surface)
            exported = saved.model_dump(mode="json")
            exported["effective_model"] = cls._with_catalog_head(manifest, launcher, saved).model
            # Whether a Work-like launch precondition applies here at all, so no
            # surface has to infer it from a capability that is only the default.
            exported["work_like_capable"] = launches_work_like(surface)
            profiles[surface] = exported
        return profiles

    @staticmethod
    def _with_catalog_head(
        manifest: Manifest,
        launcher: AgentLauncher,
        profile: AgentSurfaceConfig,
    ) -> AgentSurfaceConfig:
        """Fill an unnamed model with the first model the provider CLI vendors on that machine.

        There is no "provider default" choice: a profile or request that names no
        model runs the head of the catalog the readiness probe already collected.
        Only an already-cached probe is consulted, so admission never launches a
        CLI; an unknown catalog leaves the model empty and the CLI picks its own.
        """
        if profile.model:
            return profile
        machine = manifest.machine_map.get(profile.run_on)
        if machine is None:
            return profile
        readiness = launcher.cached_readiness(
            profile.provider,
            host=machine.host,
            binary=launcher.provider_binary(profile.provider, machine.host),
        )
        if readiness is None or not readiness.models:
            return profile
        head: ModelChoice = readiness.models[0]
        filled: dict[str, object] = {"model": head.id}
        # The effort was chosen without a model; keep it only if the head accepts it,
        # and trust the catalog's default only when the head's own list contains it.
        # A head with no efforts takes none, as the model picker stores it.
        if not head.reasoning:
            filled["reasoning"] = ""
        elif profile.reasoning not in head.reasoning:
            filled["reasoning"] = (
                head.default_reasoning
                if head.default_reasoning in head.reasoning
                else head.reasoning[0]
            )
        return profile.model_copy(update=filled)

    def assemble_run(
        self,
        request: RunRequest,
        surface: AgentSurface = "refresh",
        *,
        imported_source_inventory: ImportedProviderSourceInventory | None = None,
    ) -> RunContext:
        materialization = self.history.current_materialization()
        state = self.history.require_writable(materialization.state)
        profile = self.resolve_agent_profile(
            surface,
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
        execution_machine = self.manifest.machine_map[profile.run_on]
        assembler = ContextAssembler(self.manifest, graph_root=self.history.root)
        source_roots = assembler.source_roots(execution_machine.alias)
        source_errors = preflight_provider_roots(source_roots, execution_machine)
        imported = imported_source_inventory or self.imported_source_inventory(
            surface,
            execution_machine,
        )
        imported_source_roots = (
            imported.roots(self.imported_sources.root)
            if self.imported_sources is not None and imported.files
            else {}
        )
        context = assembler.assemble(
            state,
            writable=request.run_truth_scope,
            refresh_delta=(
                self.history.refresh_delta(materialization) if surface == "refresh" else None
            ),
            source_roots=source_roots,
            imported_source_roots=imported_source_roots,
            imported_source_fingerprint=(
                imported.fingerprint if imported is not None and imported.files else None
            ),
            source_errors=source_errors,
        )
        return context

    def imported_source_inventory(
        self,
        surface: AgentSurface,
        execution_machine: MachineConfig,
    ) -> ImportedProviderSourceInventory | None:
        if self.imported_sources is None or surface not in {"seed", "refresh"}:
            return None
        return self.imported_sources.inventory()

    def validate_imported_source_context(
        self,
        context: RunContext,
        inventory: ImportedProviderSourceInventory | None,
        *,
        expected_roots: dict[str, list[str]] | None = None,
    ) -> None:
        if expected_roots is None:
            expected_roots = (
                inventory.roots(self.imported_sources.root)
                if inventory is not None and inventory.files and self.imported_sources is not None
                else {}
            )
        expected_fingerprint = (
            inventory.fingerprint if inventory is not None and inventory.files else None
        )
        if (
            context.imported_source_roots != expected_roots
            or context.imported_source_fingerprint != expected_fingerprint
        ):
            raise ValueError("imported provider history changed after this context was prepared")

    def graph_task_contract(
        self,
        kind: str,
        *,
        project_name: str,
        ontology_path: str,
        ontology_extensions: bool,
        graph_path: str | None,
        research_path: str | None,
        provider_log_roots: dict[str, list[str]],
        ingestion_watermark: datetime | str | None,
        repositories: list[dict[str, str]],
        patch_path: str,
        output_schema_path: str,
        validator_command: str,
        human_request_path: str | None = None,
        retry_diagnostics_path: str | None = None,
        source_errors: list[str] | None = None,
        skill_pointers: list[dict[str, object]] | None = None,
        compute_profiles: list[dict[str, str]] | None = None,
    ) -> str:
        return PromptFactory.graph_task_contract(
            kind,
            project_name=project_name,
            ontology_path=ontology_path,
            ontology_extensions=ontology_extensions,
            graph_path=graph_path,
            research_path=research_path,
            provider_log_roots=provider_log_roots,
            ingestion_watermark=ingestion_watermark,
            repositories=repositories,
            patch_path=patch_path,
            output_schema_path=output_schema_path,
            human_request_path=human_request_path,
            retry_diagnostics_path=retry_diagnostics_path,
            source_errors=source_errors or [],
            validator_command=validator_command,
            skill_pointers=skill_pointers,
            compute_profiles=compute_profiles,
        )

    def assemble_chat(self, request: RunRequest) -> ChatContext:
        """Chat context: the graph and live pointers, never the ingest corpus."""

        return ContextAssembler(self.manifest, graph_root=self.history.root).chat_context(
            self.history.state(),
            graph_target=self.history.graph_target,
            node_id=request.node_id if request.chat_scope == "node" else None,
            writable=request.run_truth_scope,
        )

    def resolve_skill_selection(
        self,
        request: RunRequest | CoachRequest,
    ) -> SkillSelection:
        """Resolve the official packages enabled by the project Settings defaults.

        A request's recorded `resolved_skill_packages` is deliberately ignored:
        it is the receipt of an earlier attempt, and the registry is what says
        which version this launch gets.
        """

        defaults = self.manifest.agent.skill_defaults
        return official_registry().resolve(defaults=defaults)

    def resolve_skill_request(
        self,
        request: RunRequest | CoachRequest,
    ) -> RunRequest | CoachRequest:
        selection = self.resolve_skill_selection(request)
        available = {(item.kind, item.id) for item in selection.resolved_skill_packages}
        registry = official_registry()

        def validate_invocations(
            values: list[str], kind: Literal["workflow", "skill"]
        ) -> list[str]:
            normalized: list[str] = []
            seen: set[str] = set()
            for value in values:
                registry.package(kind, value)
                if value in seen:
                    continue
                if (kind, value) not in available:
                    raise ValueError(
                        f"invoked {kind} {value!r} is not enabled in project skill defaults"
                    )
                seen.add(value)
                normalized.append(value)
            return normalized

        invoked_workflow_ids = validate_invocations(request.invoked_workflow_ids, "workflow")
        invoked_skill_ids = validate_invocations(request.invoked_skill_ids, "skill")
        resolved_provider_skills: list[ProviderSkillReference] = []
        if request.invoked_provider_skill_names:
            if self.provider_skills is None:
                raise ValueError("provider-native skill inventory is unavailable")
            if request.provider is None or request.run_on is None:
                raise ValueError(
                    "provider-native skills require a resolved provider and execution machine"
                )
            try:
                machine = self.manifest.machine_map[request.run_on]
            except KeyError:
                raise ValueError(f"unknown execution machine: {request.run_on}") from None
            resolved_provider_skills = self.provider_skills.resolve(
                request.provider,
                machine.host,
                self.launcher.provider_binary(request.provider, machine.host),
                machine.alias,
                request.invoked_provider_skill_names,
            )
        return request.model_copy(
            update={
                "workflow_ids": selection.workflow_ids,
                "skill_ids": selection.skill_ids,
                "invoked_workflow_ids": invoked_workflow_ids,
                "invoked_skill_ids": invoked_skill_ids,
                "resolved_provider_skills": resolved_provider_skills,
                "resolved_skill_packages": selection.resolved_skill_packages,
            }
        )

    def coach_context(self, paper_path: Path | None = None) -> CoachContext:
        return ContextAssembler(self.manifest).paper_pointers(paper_path)

    def pointer_hashes(self) -> tuple[str, int, str]:
        snapshot = self.paper_live.run_sync(self.paper_live.snapshot(self.paper.project_id))
        ordered = snapshot.model_copy(
            update={"files": sorted(snapshot.files, key=lambda f: f.file_id)}
        )
        paper_hash = hashlib.sha256(ordered.model_dump_json().encode("utf-8")).hexdigest()
        state = self.history.state()
        research_path = self.manifest.research_dir / "research.md"
        research = research_path.read_bytes() if research_path.exists() else b""
        return paper_hash, state.revision, hashlib.sha256(research).hexdigest()

    @staticmethod
    def parse_patch_output(chunks: list[str]) -> tuple[AgentPatch, str | None]:
        last_problem: ValueError | None = None
        for chunk in reversed(chunks):
            candidate = chunk.strip()
            try:
                return parse_agent_patch_json(candidate), None
            except ValueError as exc:
                last_problem = exc
                start = candidate.find("{")
                end = candidate.rfind("}")
                if start >= 0 and end > start:
                    try:
                        return parse_agent_patch_json(candidate[start : end + 1]), None
                    except ValueError as exc:
                        last_problem = exc
                        continue
        if last_problem is not None:
            raise last_problem
        raise ValueError("agent completed without a valid semantic Patch object")
