from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
from rcp.config import AgentSurface, AgentSurfaceConfig, MachineConfig, Manifest
from rcp.control import derive_experiment_control_state
from rcp.core.models import (
    ACTIVE_EXPERIMENT_ATTEMPT_STATUSES,
    HUMAN_EDITABLE_NODE_FIELDS,
    ExperimentDecisionPin,
    GraphState,
    OntologyState,
    Patch,
    ProjectNode,
    Proposal,
    Standing,
)
from rcp.core.validation.proposals import proposal_is_stale
from rcp.history import HistoryManager
from rcp.limits import (
    CHAT_PAGE_DEFAULT_LIMIT,
    CHAT_PAGE_MAX_LIMIT,
    CHAT_PREVIEW_MAX_CHARS,
    CHAT_TITLE_MAX_CHARS,
)
from rcp.paper import PaperService, PaperSnapshot
from rcp.providers import PROVIDER_IDS, ProviderId
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
    preflight_provider_roots,
)
from rcp.transport import repository_access as build_repository_access

_SETTINGS_SURFACES: tuple[AgentSurface, ...] = (
    "seed",
    "refresh",
    "node_chat",
    "project_chat",
    "paper_coach",
)


ConversationMode = Literal["discuss", "work"]
TaskTrigger = Literal["human", "experiment_run", "watcher"]
GraphPatchKind = Literal["work", "experiment_loop"]


class GraphUpdateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["none", "applied", "rejected"]
    applied_revision: int | None = Field(default=None, ge=0)
    change_summary: list[str] = Field(default_factory=list)
    proposal_ids: list[str] = Field(default_factory=list)
    validation_messages: list[str] = Field(default_factory=list)
    correction_rounds: int = Field(default=0, ge=0)
    repairable: bool = False


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    operation_id: str | None = None
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
    trigger: TaskTrigger = "human"


class ChatSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str
    kind: Literal["node_chat", "project_chat"]
    node_id: str | None
    title: str
    updated_at: str
    message_count: int = Field(ge=1)
    last_message_preview: str


class ChatSummaryPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ChatSummary]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=CHAT_PAGE_MAX_LIMIT)


class ChatTranscript(ChatSummary):
    messages: list[ChatMessage] = Field(min_length=1)


@dataclass(frozen=True)
class _ChatSummaryCacheEntry:
    fingerprint: tuple[int, int, int, int, int]
    summary: ChatSummary | None


class _StoredChatRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    session_id: str = Field(alias="sessionId")
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
    trigger: TaskTrigger = "human"


class ReviewRequest(BaseModel):
    standing: Literal["asserted", "accepted", "contested"]


class NodeEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_updated_rev: int = Field(ge=0)
    changes: dict[str, Any] = Field(min_length=1)


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


class GraphSyncAmbiguityResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ambiguity_id: str
    status: Literal["resolved", "dismissed"]


class GraphSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    nodes: list[GraphSyncNodeChange] = Field(default_factory=list)
    proposals: list[GraphSyncProposalDecision] = Field(default_factory=list)
    ambiguities: list[GraphSyncAmbiguityResolution] = Field(default_factory=list)
    ontology: OntologyState | None = None
    custom_nodes: list[ProjectNode] = Field(default_factory=list)
    removed_node_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_unique_targets(self) -> GraphSyncRequest:
        for label, values in (
            ("node", [item.node_id for item in self.nodes]),
            ("proposal", [item.proposal_id for item in self.proposals]),
            ("ambiguity", [item.ambiguity_id for item in self.ambiguities]),
            ("custom node", [item.id for item in self.custom_nodes]),
            ("removed node", self.removed_node_ids),
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
        return self


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

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
    mode: ConversationMode = "discuss"
    trigger: TaskTrigger = "human"
    patch_kind: GraphPatchKind = "work"
    control_node_id: str | None = None
    control_revision: int | None = Field(default=None, ge=0)
    control_decision_bundle: list[ExperimentDecisionPin] = Field(default_factory=list)
    control_completion_criteria: list[str] = Field(default_factory=list)
    watcher_ids: list[str] = Field(default_factory=list)
    workflow_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    invoked_workflow_ids: list[str] = Field(default_factory=list)
    invoked_skill_ids: list[str] = Field(default_factory=list)
    resolved_skill_packages: list[SkillReference] | None = None


class CoachRequest(BaseModel):
    message: str
    provider: ProviderId | None = None
    model: str | None = None
    reasoning: str | None = None
    run_on: str | None = None
    session_id: str | None = None
    workflow_ids: list[str] | None = None
    skill_ids: list[str] | None = None
    invoked_workflow_ids: list[str] = Field(default_factory=list)
    invoked_skill_ids: list[str] = Field(default_factory=list)
    resolved_skill_packages: list[SkillReference] | None = None


class AgentProfileSettings(BaseModel):
    provider: ProviderId
    model: str = ""
    reasoning: str = "medium"
    run_on: str = Field(min_length=1)


class ProjectSettingsRequest(BaseModel):
    default_run_truth_scope: list[str] = Field(min_length=1)
    agent_profiles: dict[AgentSurface, AgentProfileSettings]
    skill_defaults: SkillDefaults = Field(default_factory=SkillDefaults)
    # Partial by machine and provider. Omission preserves every recorded path;
    # an empty string explicitly clears one provider's record.
    machine_provider_paths: dict[str, dict[ProviderId, str]] | None = None

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


class ProjectService:
    def __init__(
        self,
        manifest: Manifest,
        history: HistoryManager,
        paper: PaperService,
        launcher: AgentLauncher | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.history = history
        self.paper = paper
        self.launcher = launcher or AgentLauncher()
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
        self.indexer = ConversationIndexer(
            manifest,
            (data_dir / "source-cache") if data_dir else None,
            app_chat_origin=app_chat_origin,
        )
        self._index_lock = threading.Lock()
        self._indexes: dict[str, ConversationIndex] = {}
        self._chat_summary_lock = threading.Lock()
        self._chat_summary_cache: dict[Path, _ChatSummaryCacheEntry] = {}

    @property
    def manifest(self) -> Manifest:
        return self.history.manifest

    def chat_path(
        self,
        chat_id: str,
        *,
        chat_scope: Literal["node", "project"],
        node_id: str | None,
    ) -> Path:
        target = node_id if chat_scope == "node" else "project"
        if target is None:
            raise ValueError("Node chat requires a node_id")
        safe_target = re.sub(r"[^A-Za-z0-9._-]+", "_", target).strip("._") or "node"
        return self.history.workspace.root / "chat" / f"{safe_target}-{chat_id}.jsonl"

    def chat_summaries(
        self,
        *,
        offset: int = 0,
        limit: int = CHAT_PAGE_DEFAULT_LIMIT,
    ) -> ChatSummaryPage:
        if offset < 0:
            raise ValueError("chat offset must be non-negative")
        if limit < 1 or limit > CHAT_PAGE_MAX_LIMIT:
            raise ValueError(f"chat limit must be between 1 and {CHAT_PAGE_MAX_LIMIT}")
        chats = sorted(
            self._canonical_chat_summaries(),
            key=lambda item: (datetime.fromisoformat(item.updated_at), item.chat_id),
            reverse=True,
        )
        return ChatSummaryPage(
            items=chats[offset : offset + limit],
            total=len(chats),
            offset=offset,
            limit=limit,
        )

    def chat_transcript(self, chat_id: str) -> ChatTranscript | None:
        try:
            normalized = str(uuid.UUID(chat_id))
        except ValueError as exc:
            raise ValueError("chat_id must be a UUID") from exc
        if normalized != chat_id:
            raise ValueError("chat_id must be a canonical UUID")
        suffix = f"-{chat_id}.jsonl"
        with self.history.workspace.snapshot_lock:
            transcripts = [
                transcript
                for path, _ in self._canonical_chat_files()
                if path.name.endswith(suffix)
                and (transcript := self._read_chat_transcript(path)) is not None
                and transcript.chat_id == chat_id
            ]
        # The same UUID under two canonical node/project paths is ambiguous.
        return transcripts[0] if len(transcripts) == 1 else None

    def _canonical_chat_summaries(self) -> list[ChatSummary]:
        with self.history.workspace.snapshot_lock:
            files = self._canonical_chat_files()
            live_paths = {path for path, _ in files}
            summaries: dict[str, ChatSummary | None] = {}
            with self._chat_summary_lock:
                for stale in self._chat_summary_cache.keys() - live_paths:
                    del self._chat_summary_cache[stale]
                for path, fingerprint in files:
                    cached = self._chat_summary_cache.get(path)
                    if cached is None or cached.fingerprint != fingerprint:
                        transcript = self._read_chat_transcript(path)
                        summary = (
                            ChatSummary.model_validate(transcript.model_dump(exclude={"messages"}))
                            if transcript is not None
                            else None
                        )
                        cached = _ChatSummaryCacheEntry(fingerprint, summary)
                        self._chat_summary_cache[path] = cached
                    if cached.summary is None:
                        continue
                    chat_id = cached.summary.chat_id
                    if chat_id in summaries:
                        # One conversation has exactly one canonical file. Ambiguous
                        # duplicates are safer hidden than selected by filesystem order.
                        summaries[chat_id] = None
                    else:
                        summaries[chat_id] = cached.summary
            return [summary for summary in summaries.values() if summary is not None]

    def _canonical_chat_files(
        self,
    ) -> list[tuple[Path, tuple[int, int, int, int, int]]]:
        """Reconcile a stale mirror, then return safe canonical file candidates."""

        workspace = self.history.workspace
        if workspace.remote:
            workspace.refresh_if_stale()
        chat_dir = workspace.root / "chat"
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

    def _read_chat_transcript(self, path: Path) -> ChatTranscript | None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    return None
                with os.fdopen(descriptor, encoding="utf-8") as handle:
                    descriptor = -1
                    lines = handle.read().splitlines()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            raw_records = [json.loads(line) for line in lines if line.strip()]
            records = [_StoredChatRecord.model_validate(record) for record in raw_records]
        except (OSError, TypeError, ValueError):
            return None
        if not records:
            return None

        first = records[0]
        try:
            chat_id = str(uuid.UUID(first.session_id))
            if chat_id != first.session_id:
                return None
            timestamps = [datetime.fromisoformat(record.timestamp) for record in records]
            if any(timestamp.tzinfo is None for timestamp in timestamps):
                return None
            for record in records:
                if str(uuid.UUID(record.uuid)) != record.uuid:
                    return None
        except ValueError:
            return None
        if any(
            record.session_id != chat_id
            or record.chat_scope != first.chat_scope
            or record.node_id != first.node_id
            or record.type != record.role
            for record in records
        ):
            return None
        if first.chat_scope == "node":
            if not first.node_id:
                return None
            kind: Literal["node_chat", "project_chat"] = "node_chat"
        else:
            if first.node_id is not None:
                return None
            kind = "project_chat"
        if path != self.chat_path(
            chat_id,
            chat_scope=first.chat_scope,
            node_id=first.node_id,
        ):
            return None

        messages = [
            ChatMessage(
                message_id=record.uuid,
                operation_id=record.operation_id,
                role=record.role,
                text=record.text,
                timestamp=record.timestamp,
                native_session_id=record.native_session_id,
                provider=record.provider,
                model=record.model,
                reasoning=record.reasoning,
                execution_machine=record.execution_machine,
                applied_revision=record.applied_revision,
                mode=record.mode,
                graph_update=record.graph_update,
                trigger=record.trigger,
            )
            for record in records
        ]
        first_user = next((message.text for message in messages if message.role == "user"), "")
        title = " ".join(first_user.split())[:CHAT_TITLE_MAX_CHARS]
        if not title:
            title = first.node_id or "Project chat"
        preview_source = next(
            (message.text for message in reversed(messages) if message.text.strip()),
            "",
        )
        preview = " ".join(preview_source.split())[:CHAT_PREVIEW_MAX_CHARS]
        updated_at = records[max(range(len(records)), key=timestamps.__getitem__)].timestamp
        return ChatTranscript(
            chat_id=chat_id,
            kind=kind,
            node_id=first.node_id,
            title=title,
            updated_at=updated_at,
            message_count=len(messages),
            last_message_preview=preview,
            messages=messages,
        )

    def project_snapshot(
        self,
        *,
        state: GraphState | None = None,
        paper: PaperSnapshot | None = None,
    ) -> dict[str, object]:
        if state is None:
            state = self.history.state()
        if paper is None:
            paper = self.paper.snapshot()
        primary = self._primary_question(state)
        pending = [item for item in state.proposals.values() if item.status == "pending"]
        open_ambiguities = [item for item in state.ambiguities.values() if item.status == "open"]
        blockers = [
            item
            for item in state.nodes.values()
            if item.type == "blocker" and item.status == "open"
        ]
        refresh_profile = self.manifest.agent_profile("refresh")
        profiles = {
            surface: self.manifest.agent_profile(surface).model_dump(mode="json")
            for surface in ("seed", "refresh", "node_chat", "project_chat", "paper_coach")
        }
        experiment_control = {
            node.id: derive_experiment_control_state(state, node.id).model_dump(mode="json")
            for node in state.nodes.values()
            if node.type == "experiment"
        }
        return {
            "name": self.manifest.name,
            "revision": state.revision,
            "state_repository": self.manifest.state.repository,
            "canonical_state": self.history.workspace.status().model_dump(mode="json"),
            "run_on": refresh_profile.run_on,
            "project_truth_scope": state.project_truth_scope,
            "default_run_truth_scope": self.manifest.agent.default_run_truth_scope,
            "repositories": [repository.model_dump() for repository in self.manifest.repositories],
            "machines": [machine.model_dump() for machine in self.manifest.machines],
            "primary_question": primary,
            "last_refresh_at": state.last_refresh_at,
            "experiment_control": experiment_control,
            "counts": {
                "pending_proposals": len(pending),
                "open_ambiguities": len(open_ambiguities),
                "open_blockers": len(blockers),
                "asserted": sum(
                    node.standing == Standing.ASSERTED for node in state.nodes.values()
                ),
                "accepted": sum(
                    node.standing == Standing.ACCEPTED for node in state.nodes.values()
                ),
                "contested": sum(
                    node.standing == Standing.CONTESTED for node in state.nodes.values()
                ),
            },
            "coverage": state.coverage.model_dump(mode="json"),
            "graph": state.model_dump(mode="json"),
            "paper": paper.model_dump(mode="json"),
            "paper_coach": self.manifest.coach.model_dump(mode="json"),
            "agent_profiles": profiles,
            "skill_catalog": official_registry().catalog(),
            "skill_defaults": self.manifest.agent.skill_defaults.model_dump(mode="json"),
            "provider_readiness": {},
            "providers": {},
            "cache_metrics": self.indexer.cache_metrics().model_dump(mode="json"),
            "validation_messages": [
                item.model_dump(mode="json") for item in state.validation_messages
            ],
        }

    def readiness_snapshot(self, *, refresh: bool = False) -> dict[str, object]:
        return self.readiness_for(self.manifest, self.launcher, refresh=refresh)

    @staticmethod
    def readiness_for(
        manifest: Manifest,
        launcher: AgentLauncher,
        *,
        refresh: bool = False,
    ) -> dict[str, object]:
        """Probe providers without reading or replaying canonical project history."""

        targets = [
            (
                machine.alias,
                machine.host,
                provider,
                machine.provider_paths.get(provider),
            )
            for machine in manifest.machines
            for provider in PROVIDER_IDS
        ]

        def inspect(
            host: str,
            provider: ProviderId,
            binary: str | None,
        ) -> dict[str, object]:
            kwargs: dict[str, object] = {"host": host}
            if binary is not None:
                kwargs["binary"] = binary
            if refresh:
                kwargs["refresh"] = True
            return launcher.readiness(provider, **kwargs).model_dump(mode="json")

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

    def update_settings(self, request: ProjectSettingsRequest) -> None:
        profiles = {
            surface: AgentSurfaceConfig(
                provider=request.agent_profiles[surface].provider,
                model=request.agent_profiles[surface].model,
                reasoning=request.agent_profiles[surface].reasoning,
                run_on=request.agent_profiles[surface].run_on,
            )
            for surface in _SETTINGS_SURFACES
        }
        provider_path_updates = self._validate_provider_path_updates(request.machine_provider_paths)
        prior_paths = {
            (alias, provider): self.manifest.machine_map[alias].provider_paths.get(provider)
            for alias, updates in (provider_path_updates or {}).items()
            for provider in updates
        }
        self.history.update_agent_settings(
            request.default_run_truth_scope,
            profiles,
            provider_path_updates,
            request.skill_defaults,
        )
        for (alias, provider), prior_path in prior_paths.items():
            machine = self.manifest.machine_map[alias]
            current_path = machine.provider_paths.get(provider)
            if prior_path == current_path:
                continue
            if prior_path is not None:
                self.launcher.invalidate_readiness(
                    provider,
                    host=machine.host,
                    binary=prior_path,
                )
            if current_path is not None:
                self.launcher.invalidate_readiness(
                    provider,
                    host=machine.host,
                    binary=current_path,
                )
        self.paper.manifest = self.manifest
        self.invalidate_source_index()

    def resolve_provider_path(
        self,
        machine_alias: str,
        provider: ProviderId,
    ) -> ProviderReadiness:
        try:
            machine = self.manifest.machine_map[machine_alias]
        except KeyError:
            raise ValueError(f"unknown execution machine: {machine_alias}") from None
        discovered = self.launcher.readiness(
            provider,
            host=machine.host,
            refresh=True,
        )
        if discovered.path_state == "unreachable":
            raise ValueError(discovered.reason or f"{machine_alias} is unreachable")
        if not discovered.installed or not discovered.binary_path:
            raise ValueError(
                discovered.reason or f"No {provider} executable was found on {machine_alias}."
            )
        self.history.update_machine_provider_paths(
            {machine_alias: {provider: discovered.binary_path}}
        )
        self.paper.manifest = self.manifest
        return self.launcher.readiness(
            provider,
            host=machine.host,
            binary=discovered.binary_path,
            refresh=True,
        )

    def _validate_provider_path_updates(
        self,
        updates: dict[str, dict[ProviderId, str]] | None,
    ) -> dict[str, dict[ProviderId, str]] | None:
        if updates is None:
            return None
        unknown = set(updates) - set(self.manifest.machine_map)
        if unknown:
            raise ValueError(f"provider paths use unknown machines: {sorted(unknown)}")
        validated: dict[str, dict[ProviderId, str]] = {}
        for alias, provider_updates in updates.items():
            machine = self.manifest.machine_map[alias]
            merged = dict(machine.provider_paths)
            merged.update(provider_updates)
            candidate = MachineConfig(
                alias=machine.alias,
                host=machine.host,
                provider_paths=merged,
            )
            validated[alias] = {
                provider: candidate.provider_paths.get(provider, "")
                for provider in provider_updates
            }
        return validated

    def review_node(self, node_id: str, request: ReviewRequest) -> GraphState:
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
        _, result = self.history.append(patch)
        return result.state

    def sync_graph(
        self,
        request: GraphSyncRequest,
        *,
        active_control_node_ids: set[str],
    ) -> GraphState:
        """Commit one project-wide human draft in one canonical transaction."""

        has_staged_work = (
            any(
                (
                    request.nodes,
                    request.proposals,
                    request.ambiguities,
                    request.custom_nodes,
                    request.removed_node_ids,
                )
            )
            or request.ontology is not None
        )
        if not has_staged_work:
            return self.history.current_materialization().state
        try:
            _, result = self.history.append_batch_from_state(
                lambda state: self._build_sync_patches(
                    request,
                    state,
                    active_control_node_ids=active_control_node_ids,
                ),
                expected_revision=request.base_revision,
            )
        except ValueError as exc:
            if "graph changed after this draft began" in str(exc):
                raise NodeEditConflict(str(exc)) from exc
            raise
        return result.state

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
                    request.ambiguities,
                    request.custom_nodes,
                    request.removed_node_ids,
                )
            )
            and not ontology_changed
        ):
            return []

        patches: list[Patch] = []

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
            if extension_type is None:
                raise ValueError(
                    "Human-created graph nodes must use an active custom ontology type; "
                    "base-node authoring is not available."
                )
            definition = active_types.get(extension_type)
            if definition is None:
                raise ValueError(
                    f"Custom node {node.id} uses inactive or unknown ontology type "
                    f"{extension_type!r}."
                )
            if node.type != definition.base_type:
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
                    ops=[{"op": "create_nodes", "nodes": [prepared.model_dump(mode="json")]}],
                    change_summary=[
                        f"Created “{node.title}” as a {extension_type.replace('_', ' ')}."
                    ],
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
                if node.type == "experiment":
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

        removed_node_ids = set(request.removed_node_ids)
        for staged in request.proposals:
            proposal = state.proposals.get(staged.proposal_id)
            if proposal is None:
                raise KeyError(staged.proposal_id)
            if proposal.status != "pending":
                raise NodeEditConflict(f"Proposal {proposal.id} is no longer pending.")
            stale_from_removal = bool(removed_node_ids.intersection(proposal.related_node_ids))
            if self._proposal_is_stale(state, proposal) or stale_from_removal:
                reason = (
                    f"The proposal “{proposal.title}” became stale because a related research "
                    "concept was removed in this Sync."
                    if stale_from_removal
                    else f"The proposal “{proposal.title}” was stale and was withdrawn without "
                    "applying changes."
                )
                patches.append(
                    Patch(
                        kind="approval",
                        author="human",
                        summary=f"Withdrew stale proposal “{proposal.title}”.",
                        ops=[
                            {
                                "op": "resolve_proposals",
                                "resolutions": [{"id": proposal.id, "status": "withdrawn"}],
                            }
                        ],
                        change_summary=[reason],
                    )
                )
                continue
            standing = "accepted" if staged.decision == "approved" else "contested"
            semantic_ops = proposal.ops if staged.decision == "approved" else []
            standing_ops = [
                {"op": "set_standing", "node_id": node_id, "standing": standing}
                for node_id in proposal.related_node_ids
            ]
            patches.append(
                Patch(
                    kind="approval",
                    author="human",
                    summary=f"{staged.decision.title()} proposal “{proposal.title}”.",
                    ops=[
                        *semantic_ops,
                        {
                            "op": "resolve_proposals",
                            "resolutions": [
                                {
                                    "id": proposal.id,
                                    "status": staged.decision,
                                    "reason": staged.reason,
                                }
                            ],
                        },
                        *standing_ops,
                    ],
                    change_summary=[f"The proposal “{proposal.title}” was {staged.decision}."],
                )
            )

        for staged in request.ambiguities:
            ambiguity = state.ambiguities.get(staged.ambiguity_id)
            if ambiguity is None:
                raise KeyError(staged.ambiguity_id)
            if ambiguity.status != "open":
                raise NodeEditConflict(f"Ambiguity {ambiguity.id} is no longer open.")
            patches.append(
                Patch(
                    kind="approval",
                    author="human",
                    summary=f"Marked the open question “{ambiguity.question}” {staged.status}.",
                    ops=[
                        {
                            "op": "resolve_ambiguities",
                            "resolutions": [{"id": ambiguity.id, "status": staged.status}],
                        }
                    ],
                    change_summary=[
                        f"The open question “{ambiguity.question}” was {staged.status}."
                    ],
                )
            )

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
            display_title = str(staged.changes.get("title", node.title))
            if staged.changes:
                allowed = set(HUMAN_EDITABLE_NODE_FIELDS[node.type])
                if "extension_fields" in staged.changes:
                    self._validate_human_extension_fields(state, node, staged.changes)
                    allowed.add("extension_fields")
                disallowed = sorted(set(staged.changes) - allowed)
                if disallowed:
                    raise ValueError(
                        f"Direct edits to {node.id} cannot change: {', '.join(disallowed)}."
                    )
                current = node.model_dump(mode="python")
                if any(current[field] != value for field, value in staged.changes.items()):
                    candidate = {**current, **staged.changes}
                    try:
                        type(node).model_validate(candidate)
                    except ValueError as exc:
                        raise ValueError(f"Invalid wording for {node.id}: {exc}") from exc
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
                    change_summary.append(f"Updated wording for “{display_title}”.")
            if staged.cancel_attempt_ids:
                ops.append(
                    {
                        "op": "update_nodes",
                        "nodes": [
                            {
                                "id": node.id,
                                "base_updated_rev": staged.base_updated_rev,
                                "changes": {
                                    "attempts": self._cancelled_attempts(
                                        node, staged.cancel_attempt_ids
                                    )
                                },
                            }
                        ],
                    }
                )
                change_summary.append(f"Released open experiment attempts for “{display_title}”.")
            if staged.standing is not None and staged.standing != node.standing:
                ops.append(
                    {
                        "op": "set_standing",
                        "node_id": node.id,
                        "standing": staged.standing,
                    }
                )
                change_summary.append(f"“{display_title}” is now {staged.standing}.")
            if ops:
                patches.append(
                    Patch(
                        kind="approval",
                        author="human",
                        summary=f"Synced staged changes for “{display_title}”.",
                        ops=ops,
                        change_summary=change_summary,
                    )
                )

        return patches

    @staticmethod
    def _cancelled_attempts(node: ProjectNode, attempt_ids: list[str]) -> list[dict[str, Any]]:
        """Close the named open attempts, leaving every other attempt untouched.

        The human releases an attempt whose watcher can no longer answer. Only an
        open attempt can be released — this never rewrites a finished record.
        """

        from rcp.core.models import Experiment, utc_now

        if not isinstance(node, Experiment):
            raise ValueError(f"{node.id} has no attempts to release.")
        open_ids = {
            attempt.id
            for attempt in node.attempts
            if attempt.status in ACTIVE_EXPERIMENT_ATTEMPT_STATUSES
        }
        unknown = sorted(set(attempt_ids) - open_ids)
        if unknown:
            raise ValueError(f"{node.id} has no open attempt named: {', '.join(unknown)}.")
        finished_at = utc_now()
        return [
            (
                attempt.model_copy(
                    update={
                        "status": "cancelled",
                        "finished_at": finished_at,
                        "failure_reason": "Released by the human.",
                    }
                )
                if attempt.id in set(attempt_ids)
                else attempt
            ).model_dump(mode="json")
            for attempt in node.attempts
        ]

    def edit_node(self, node_id: str, request: NodeEditRequest) -> GraphState:
        state = self.history.state()
        self.history.require_writable(state)
        node = state.nodes.get(node_id)
        if node is None:
            raise KeyError(node_id)
        if request.base_updated_rev != node.updated_rev:
            raise NodeEditConflict(
                f"{node_id} changed after this editor opened; reload it before saving."
            )
        disallowed = sorted(
            set(request.changes)
            - (
                set(HUMAN_EDITABLE_NODE_FIELDS[node.type])
                | ({"extension_fields"} if "extension_fields" in request.changes else set())
            )
        )
        if "extension_fields" in request.changes:
            self._validate_human_extension_fields(state, node, request.changes)
        if disallowed:
            raise ValueError(f"Direct edits to {node_id} cannot change: {', '.join(disallowed)}.")
        current = node.model_dump(mode="python")
        if all(current[field] == value for field, value in request.changes.items()):
            raise ValueError("The submitted node wording is unchanged.")
        candidate = {**current, **request.changes}
        try:
            type(node).model_validate(candidate)
        except ValueError as exc:
            raise ValueError(f"Invalid wording for {node_id}: {exc}") from exc
        patch = Patch(
            kind="approval",
            author="human",
            summary=f"Edited wording for “{request.changes.get('title', node.title)}”.",
            ops=[
                {
                    "op": "update_nodes",
                    "nodes": [
                        {
                            "id": node_id,
                            "base_updated_rev": request.base_updated_rev,
                            "changes": request.changes,
                        }
                    ],
                }
            ],
            change_summary=[
                f"Updated human-authored wording for “{request.changes.get('title', node.title)}”."
            ],
        )
        _, result = self.history.append(patch)
        return result.state

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

    def decide_proposal(self, proposal_id: str, request: ProposalDecisionRequest) -> GraphState:
        state = self.history.state()
        self.history.require_writable(state)
        proposal = state.proposals.get(proposal_id)
        if proposal is None:
            raise KeyError(proposal_id)
        if proposal.status != "pending":
            raise ValueError("proposal is not pending")
        if self._proposal_is_stale(state, proposal):
            patch = Patch(
                kind="approval",
                author="human",
                summary=f"Withdrew stale proposal “{proposal.title}”.",
                ops=[
                    {
                        "op": "resolve_proposals",
                        "resolutions": [{"id": proposal_id, "status": "withdrawn"}],
                    }
                ],
                change_summary=[
                    f"The proposal “{proposal.title}” was stale and was withdrawn without "
                    "applying changes."
                ],
            )
        else:
            standing = "accepted" if request.decision == "approved" else "contested"
            semantic_ops = proposal.ops if request.decision == "approved" else []
            standing_ops = [
                {"op": "set_standing", "node_id": node_id, "standing": standing}
                for node_id in proposal.related_node_ids
            ]
            patch = Patch(
                kind="approval",
                author="human",
                summary=f"{request.decision.title()} proposal “{proposal.title}”.",
                ops=[
                    *semantic_ops,
                    {
                        "op": "resolve_proposals",
                        "resolutions": [
                            {
                                "id": proposal_id,
                                "status": request.decision,
                                "reason": request.reason,
                            }
                        ],
                    },
                    *standing_ops,
                ],
                change_summary=[f"The proposal “{proposal.title}” was {request.decision}."],
            )
        _, result = self.history.append(patch)
        return result.state

    def resolve_agent_profile(
        self,
        surface: AgentSurface,
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
        if model is not None:
            updates["model"] = model
        if reasoning is not None:
            updates["reasoning"] = reasoning
        if run_on is not None:
            if run_on not in self.manifest.machine_map:
                raise ValueError(f"unknown execution machine: {run_on}")
            state_machine = self.manifest.repository_map[self.manifest.state.repository].machine
            if surface != "paper_coach" and run_on != state_machine:
                raise ValueError(
                    f"{surface.replace('_', ' ')} must run on canonical state machine "
                    f"{state_machine!r}"
                )
            updates["run_on"] = run_on
        return base.model_copy(update=updates)

    def assemble_run(
        self,
        request: RunRequest,
        surface: AgentSurface = "refresh",
    ) -> RunContext:
        materialization = self.history.current_materialization()
        state = self.history.require_writable(materialization.state)
        selected = request.run_truth_scope or self.manifest.agent.default_run_truth_scope
        selected_set = set(selected)
        project_scope = set(state.project_truth_scope or self.manifest.project.truth_scope)
        if not selected_set or not selected_set.issubset(project_scope):
            raise ValueError("run truth scope must be a non-empty subset of project truth scope")
        profile = self.resolve_agent_profile(
            surface,
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
        execution_machine = self.manifest.machine_map[profile.run_on]
        repository_access = {
            alias: build_repository_access(
                self.manifest.repository_map[alias],
                self.manifest.machine_map[self.manifest.repository_map[alias].machine],
            )
            for alias in selected
            if alias in self.manifest.repository_map
        }
        assembler = ContextAssembler(self.manifest)
        source_roots = assembler.source_roots(execution_machine.alias)
        source_errors = preflight_provider_roots(source_roots, execution_machine)
        context = assembler.assemble(
            state,
            request.run_truth_scope,
            repository_access=repository_access,
            refresh_delta=(
                self.history.refresh_delta(materialization) if surface == "refresh" else None
            ),
            source_roots=source_roots,
            source_errors=source_errors,
        )
        return context

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
        )

    def assemble_chat(self, request: RunRequest) -> ChatContext:
        """Chat context: the graph and live pointers, never the ingest corpus."""

        state = self.history.state()
        selected = request.run_truth_scope or self.manifest.agent.default_run_truth_scope
        repository_access = {
            alias: build_repository_access(
                self.manifest.repository_map[alias],
                self.manifest.machine_map[self.manifest.repository_map[alias].machine],
            )
            for alias in selected
            if alias in self.manifest.repository_map
        }
        return ContextAssembler(self.manifest).chat_context(
            state,
            node_id=request.node_id if request.chat_scope == "node" else None,
            run_truth_scope=request.run_truth_scope,
            repository_access=repository_access,
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
        return request.model_copy(
            update={
                "workflow_ids": selection.workflow_ids,
                "skill_ids": selection.skill_ids,
                "invoked_workflow_ids": invoked_workflow_ids,
                "invoked_skill_ids": invoked_skill_ids,
                "resolved_skill_packages": selection.resolved_skill_packages,
            }
        )

    def coach_context(
        self, request: CoachRequest, draft_path: Path | None = None
    ) -> tuple[dict[str, object], list[Path]]:
        # Resolved for its validation of the requested provider/model/run_on.
        self.resolve_agent_profile(
            "paper_coach",
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
        repository_access = {
            alias: build_repository_access(
                self.manifest.repository_map[alias],
                self.manifest.machine_map[self.manifest.repository_map[alias].machine],
            )
            for alias in self.manifest.project.truth_scope
        }
        pointers = ContextAssembler(self.manifest).paper_pointers(draft_path, repository_access)
        read_dirs = [
            Path(access.path)
            for access in repository_access.values()
            if not access.host and Path(access.path).exists()
        ]
        return pointers, read_dirs

    def pointer_hashes(self) -> tuple[str, int, str]:
        snapshot = self.paper.snapshot()
        intro_hash = (
            snapshot.canonical_hash or hashlib.sha256(snapshot.content.encode("utf-8")).hexdigest()
        )
        state = self.history.state()
        research_path = self.manifest.research_dir / "research.md"
        research = research_path.read_bytes() if research_path.exists() else b""
        return intro_hash, state.revision, hashlib.sha256(research).hexdigest()

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

    @staticmethod
    def _proposal_is_stale(state: GraphState, proposal: Proposal) -> bool:
        return proposal_is_stale(state, proposal)

    @staticmethod
    def _primary_question(state: GraphState):
        questions = [node for node in state.nodes.values() if node.type == "research_question"]
        questions.sort(
            key=lambda node: (
                {Standing.ACCEPTED: 0, Standing.ASSERTED: 1, Standing.CONTESTED: 2}[node.standing],
                node.id,
            )
        )
        return questions[0].model_dump(mode="json") if questions else None


def now_utc() -> datetime:
    return datetime.now(UTC)
