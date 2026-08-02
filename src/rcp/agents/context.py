from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.config import Manifest
from rcp.core.models import CoverageBoundary, GraphState, Patch
from rcp.history.delta import RefreshDelta
from rcp.limits import (
    RUN_INLINE_SESSION_BYTES,
    RUN_INLINE_SESSION_LIMIT,
)
from rcp.sources import ConversationIndex, ConversationIndexer, ConversationSession
from rcp.transport import RepositoryAccess


class RepositoryPointer(BaseModel):
    alias: str
    machine: str
    host: str = ""
    path: str


class SessionPointer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    provider: str
    machine: str
    path: str
    cursor: str | None = None
    cursor_note: str | None = None
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    last_uuid: str | None = None
    record_count: int = 0
    slice_record_count: int = 0
    slice_sha256: str = ""
    thread_source: str | None = None
    parent_session_id: str | None = None
    originator: str | None = None
    source_kind: str | None = None


class SessionRoutingIndex(BaseModel):
    """The complete immutable routing table for one ingest invocation."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    session_count: int = Field(ge=0)
    sessions: list[SessionPointer]

    @model_validator(mode="after")
    def count_matches_rows(self) -> SessionRoutingIndex:
        if self.session_count != len(self.sessions):
            raise ValueError("session_count must equal the number of routing rows")
        return self


class SessionRoutingIndexPointer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_count: int = Field(ge=0)


class RunContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_name: str
    run_truth_scope: list[str]
    repositories: list[RepositoryPointer]
    # Kept in memory for cursor/evidence validation and remote staging. Prompt
    # serialization uses the bounded inline list plus the immutable index below.
    sessions: list[SessionPointer]
    sessions_inline: list[SessionPointer] = Field(default_factory=list)
    sessions_omitted: int = Field(default=0, ge=0)
    session_routing_index: SessionRoutingIndexPointer | None = None
    refresh_delta: RefreshDelta | None = None
    graph_revision: int = Field(ge=0)
    graph_path: str
    research_md_path: str
    introduction_path: str | None
    glossary_path: str
    coverage_path: str
    facts_dir: str
    state_repository: str
    source_errors: list[str]
    source_roots: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def routing_metadata_matches_internal_sessions(self) -> RunContext:
        if self.sessions_omitted != len(self.sessions) - len(self.sessions_inline):
            raise ValueError("sessions_omitted does not match the bounded inline rows")
        if self.sessions_inline != self.sessions[: len(self.sessions_inline)]:
            raise ValueError("inline sessions must be the leading routing rows")
        if (
            self.session_routing_index is not None
            and self.session_routing_index.session_count != len(self.sessions)
        ):
            raise ValueError("session routing index count does not match internal sessions")
        return self

    def prompt_payload(self) -> dict[str, Any]:
        """Return the bounded, evidence-complete representation shown to an agent."""

        payload = self.model_dump(mode="json", exclude={"sessions", "sessions_inline"})
        payload["sessions"] = [session.model_dump(mode="json") for session in self.sessions_inline]
        return payload


class ChatRelation(BaseModel):
    relation: str
    direction: Literal["outgoing", "incoming"]
    other_node_id: str
    other_node_type: str
    other_node_title: str
    explanation: str = ""


class ChatContext(BaseModel):
    """Graph and exact repository context for one conversation turn."""

    project_name: str
    run_truth_scope: list[str]
    repositories: list[RepositoryPointer]
    graph_path: str
    research_md_path: str
    introduction_path: str | None
    glossary_path: str
    coverage_path: str
    facts_dir: str
    state_repository: str
    graph_revision: int
    node: dict[str, Any] | None
    relations: list[ChatRelation]


class ContextAssembler:
    def __init__(
        self,
        manifest: Manifest,
        indexer: ConversationIndexer | None = None,
        session_routing_root: Path | None = None,
    ) -> None:
        self.manifest = manifest
        self.indexer = indexer or ConversationIndexer(manifest)
        self.session_routing_root = session_routing_root

    def assemble(
        self,
        state: GraphState,
        index: ConversationIndex,
        run_truth_scope: list[str] | None = None,
        repository_access: dict[str, RepositoryAccess] | None = None,
        refresh_delta: RefreshDelta | None = None,
        pin_artifact: Callable[[Path], None] | None = None,
        source_roots: dict[str, str] | None = None,
    ) -> RunContext:
        selected = run_truth_scope or self.manifest.agent.default_run_truth_scope
        selected_set = set(selected)
        project_scope = set(state.project_truth_scope or self.manifest.project.truth_scope)
        if not selected_set or not selected_set.issubset(project_scope):
            raise ValueError("run truth scope must be a non-empty subset of project truth scope")

        repositories = []
        for item in self.manifest.repositories:
            if item.alias not in selected_set:
                continue
            access = (repository_access or {}).get(item.alias)
            repositories.append(
                RepositoryPointer(
                    alias=item.alias,
                    machine=item.machine,
                    host=access.host if access else self.manifest.machine_map[item.machine].host,
                    path=item.path,
                )
            )
        source_errors = list(index.source_errors)
        try:
            cursors = self._load_cursors()
        except Exception as exc:
            cursors = {}
            source_errors.append(f"cursor state unavailable: {type(exc).__name__}: {exc}")
        sessions = []
        active_slice_paths: list[str] = []
        slice_errors: list[str] = []
        for item in sorted(index.for_scope(selected), key=_session_priority, reverse=True):
            cursor, cursor_note = _usable_cursor(cursors.get(item.key))
            try:
                evidence_slice = self.indexer.materialize_slice(
                    item,
                    from_uuid=cursor,
                    active_paths=active_slice_paths,
                    pin_artifact=pin_artifact,
                )
            except Exception as exc:
                # One unreadable session must not cost the run every other one.
                # It is dropped from the context and reported; the provider gets
                # the source-root fallback so the gap is never silent.
                slice_errors.append(f"{item.key}: {exc}")
                continue
            active_slice_paths.append(evidence_slice.path)
            repaired = self.indexer.cursor_repairs.pop(item.key, None)
            if repaired is not None:
                cursor, cursor_note = (
                    repaired,
                    f"Stored cursor {cursor!r} no longer exists because the source file was "
                    f"rewritten; the same record was re-resolved by content as {repaired!r}.",
                )
            sessions.append(
                SessionPointer(
                    key=item.key,
                    provider=item.provider,
                    machine=item.source_machine,
                    path=evidence_slice.path,
                    cursor=cursor,
                    cursor_note=cursor_note,
                    first_timestamp=item.first_timestamp,
                    last_timestamp=item.last_timestamp,
                    last_uuid=item.last_uuid,
                    record_count=item.record_count,
                    slice_record_count=evidence_slice.record_count,
                    slice_sha256=evidence_slice.content_sha256,
                    thread_source=item.thread_source,
                    parent_session_id=item.parent_session_id,
                    originator=item.originator,
                    source_kind=item.source_kind,
                )
            )
        root = self.manifest.research_dir
        introduction = root / "paper" / "introduction.md"
        routing_root = self.session_routing_root or _default_session_routing_root(
            self.manifest, self.indexer
        )
        try:
            routing_index = write_session_routing_index(
                sessions,
                routing_root,
                pin_artifact=pin_artifact,
            )
            self.indexer.register_session_artifact(
                routing_index.path,
                active_paths=active_slice_paths,
            )
        except Exception as exc:
            routing_index = None
            source_errors.append(f"session routing index unavailable: {type(exc).__name__}: {exc}")
        sessions_inline, sessions_omitted = bounded_session_metadata(sessions)
        return RunContext(
            project_name=self.manifest.name,
            run_truth_scope=selected,
            repositories=repositories,
            sessions=sessions,
            sessions_inline=sessions_inline,
            sessions_omitted=sessions_omitted,
            session_routing_index=routing_index,
            refresh_delta=refresh_delta,
            graph_revision=state.revision,
            graph_path=str(root / "graph.json"),
            research_md_path=str(root / "research.md"),
            introduction_path=str(introduction) if introduction.exists() else None,
            glossary_path=str(root / "glossary.json"),
            coverage_path=str(root / "coverage.json"),
            facts_dir=str(root / "facts"),
            state_repository=self.manifest.state.repository,
            source_errors=[*source_errors, *slice_errors],
            source_roots=source_roots or {},
        )

    def chat_context(
        self,
        state: GraphState,
        *,
        node_id: str | None = None,
        run_truth_scope: list[str] | None = None,
        repository_access: dict[str, RepositoryAccess] | None = None,
    ) -> ChatContext:
        selected = run_truth_scope or self.manifest.agent.default_run_truth_scope
        selected_set = set(selected)
        project_scope = set(state.project_truth_scope or self.manifest.project.truth_scope)
        if not selected_set or not selected_set.issubset(project_scope):
            raise ValueError("run truth scope must be a non-empty subset of project truth scope")
        if node_id is not None and node_id not in state.nodes:
            raise ValueError(f"unknown node: {node_id}")

        repositories = [
            RepositoryPointer(
                alias=item.alias,
                machine=item.machine,
                host=(
                    (repository_access or {}).get(item.alias).host
                    if (repository_access or {}).get(item.alias)
                    else self.manifest.machine_map[item.machine].host
                ),
                path=item.path,
            )
            for item in self.manifest.repositories
            if item.alias in selected_set
        ]
        root = self.manifest.research_dir
        introduction = root / "paper" / "introduction.md"
        return ChatContext(
            project_name=self.manifest.name,
            run_truth_scope=selected,
            repositories=repositories,
            graph_path=str(root / "graph.json"),
            research_md_path=str(root / "research.md"),
            introduction_path=str(introduction) if introduction.exists() else None,
            glossary_path=str(root / "glossary.json"),
            coverage_path=str(root / "coverage.json"),
            facts_dir=str(root / "facts"),
            state_repository=self.manifest.state.repository,
            graph_revision=state.revision,
            node=state.nodes[node_id].model_dump(mode="json") if node_id else None,
            relations=_one_hop_relations(state, node_id) if node_id else [],
        )

    def source_roots(self, execution_machine: str | None) -> dict[str, str]:
        """Name provider source roots for a degraded Seed/Refresh run."""
        machine = self.manifest.machine_map.get(execution_machine or "")
        remote = bool(machine and machine.host)
        claude = (
            self.manifest.sources.remote_claude_roots
            if remote
            else self.manifest.sources.claude_roots
        )
        codex = (
            self.manifest.sources.remote_codex_roots
            if remote
            else self.manifest.sources.codex_roots
        )

        def display(values: list[str]) -> str:
            return "; ".join(
                str(Path(value).expanduser()) if not remote else value for value in values
            )

        return {"claude": display(claude), "codex": display(codex)}

    def paper_pointers(
        self,
        introduction_override: Path | None = None,
        repository_access: dict[str, RepositoryAccess] | None = None,
    ) -> dict[str, object]:
        root = self.manifest.research_dir
        introduction = introduction_override or root / "paper" / "introduction.md"
        return {
            "introduction": str(introduction),
            "graph": str(root / "graph.json"),
            "research_md": str(root / "research.md"),
            "truth_repositories": [
                {
                    "alias": item.alias,
                    "machine": item.machine,
                    "host": (
                        repository_access[item.alias].host
                        if repository_access and item.alias in repository_access
                        else self.manifest.machine_map[item.machine].host
                    ),
                    "path": item.path,
                }
                for item in self.manifest.repositories
                if item.alias in self.manifest.project.truth_scope
            ],
        }

    def _load_cursors(self) -> dict[str, str]:
        path = self.manifest.research_dir / "cursors.json"
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return {str(key): str(cursor) for key, cursor in value.items()}


def bounded_session_metadata(
    sessions: list[SessionPointer],
    *,
    limit: int = RUN_INLINE_SESSION_LIMIT,
    byte_limit: int = RUN_INLINE_SESSION_BYTES,
) -> tuple[list[SessionPointer], int]:
    """Keep the highest-priority routing rows inline under deterministic limits."""

    selected: list[SessionPointer] = []
    for session in sessions[:limit]:
        candidate = [*selected, session]
        encoded = json.dumps(
            [item.model_dump(mode="json") for item in candidate],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > byte_limit:
            break
        selected.append(session)
    return selected, len(sessions) - len(selected)


def write_session_routing_index(
    sessions: list[SessionPointer],
    root: Path,
    *,
    exposed_path: str | None = None,
    pin_artifact: Callable[[Path], None] | None = None,
) -> SessionRoutingIndexPointer:
    """Write one content-addressed routing index outside canonical history.

    The file is safe to stage beside remote session slices. ``exposed_path`` lets
    the staging layer replace the local cache path with the path visible to the
    remote agent without changing the bytes or digest.
    """

    value = SessionRoutingIndex(session_count=len(sessions), sessions=sessions)
    content = (
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    destination_root = root / f"routing-{digest}"
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / "routing.json"
    if pin_artifact is not None:
        pin_artifact(destination)
    if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".routing-",
            suffix=".tmp",
            dir=destination_root,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o444)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    destination.chmod(0o444)
    return SessionRoutingIndexPointer(
        path=exposed_path or str(destination),
        sha256=digest,
        session_count=len(sessions),
    )


def with_session_routing(
    context: RunContext,
    sessions: list[SessionPointer],
    root: Path,
    *,
    exposed_path: str | None = None,
) -> RunContext:
    """Rebind a context after a staging layer rewrites its session paths."""

    pointer = write_session_routing_index(sessions, root, exposed_path=exposed_path)
    return with_session_routing_pointer(context, sessions, pointer)


def with_session_routing_pointer(
    context: RunContext,
    sessions: list[SessionPointer],
    pointer: SessionRoutingIndexPointer,
) -> RunContext:
    """Attach an index pointer already copied to its agent-visible location."""

    if pointer.session_count != len(sessions):
        raise ValueError("session routing pointer count does not match staged sessions")
    inline, omitted = bounded_session_metadata(sessions)
    return context.model_copy(
        update={
            "sessions": sessions,
            "sessions_inline": inline,
            "sessions_omitted": omitted,
            "session_routing_index": pointer,
        }
    )


def _default_session_routing_root(manifest: Manifest, indexer: ConversationIndexer) -> Path:
    del manifest
    return indexer.session_artifact_root()


def validate_work_patch(patch: Patch) -> None:
    """Work may reflect graph changes, but it never advances ingest state."""

    if patch.processed_cursors:
        raise ValueError(
            "A Work patch must not claim processed_cursors; only seed and refresh read "
            "conversations forward from a cursor."
        )
    if any(operation.get("op") == "set_coverage" for operation in patch.ops):
        raise ValueError(
            "A Work patch must not set coverage; only seed and refresh move the coverage boundary."
        )


def validate_processed_cursors(context: RunContext, cursors: dict[str, str]) -> None:
    expected = {session.key: session.last_uuid for session in context.sessions}
    unknown = sorted(set(cursors) - set(expected))
    if unknown:
        raise ValueError(f"processed_cursors contains sessions outside this run context: {unknown}")
    for key, cursor in cursors.items():
        terminal_record = expected[key]
        if terminal_record is None:
            raise ValueError(f"Session {key!r} has no indexed terminal record.")
        if cursor != terminal_record:
            raise ValueError(
                f"processed_cursors[{key!r}] must be the indexed terminal record id "
                f"{terminal_record!r}, not {cursor!r}."
            )


def normalize_processed_cursors(
    context: RunContext,
    patch: Patch,
    previous_coverage: CoverageBoundary,
) -> Patch:
    """Derive immutable terminal cursors from coverage instead of model strings."""

    context_sessions = {session.key: session for session in context.sessions}
    unknown = sorted(set(patch.processed_cursors) - set(context_sessions))
    if unknown:
        raise ValueError(f"processed_cursors contains sessions outside this run context: {unknown}")

    final_read = set(previous_coverage.sessions_read)
    final_skipped = set(previous_coverage.sessions_skipped)
    for operation in patch.ops:
        if operation.get("op") != "set_coverage":
            continue
        coverage = operation.get("coverage")
        if not isinstance(coverage, dict):
            continue
        if "sessions_read" in coverage:
            final_read = {str(key) for key in coverage["sessions_read"]}
        if "sessions_skipped" in coverage:
            final_skipped = {str(key) for key in coverage["sessions_skipped"]}

    canonical = {
        key: context_sessions[key].last_uuid
        for key in sorted((final_read | final_skipped) & set(context_sessions))
        if context_sessions[key].last_uuid is not None
    }
    return patch.model_copy(update={"processed_cursors": canonical})


def validate_session_evidence(
    context: RunContext,
    patch: Patch,
    previous_coverage: CoverageBoundary,
) -> None:
    validate_processed_cursors(context, patch.processed_cursors)
    previous_read = set(previous_coverage.sessions_read)
    previous_skipped = set(previous_coverage.sessions_skipped)
    final_read = set(previous_read)
    final_skipped = set(previous_skipped)
    declared_read: set[str] | None = None
    declared_skipped: set[str] | None = None

    for operation in patch.ops:
        if operation.get("op") != "set_coverage":
            continue
        coverage = operation.get("coverage")
        if not isinstance(coverage, dict):
            continue
        if "sessions_read" in coverage:
            declared_read = {str(key) for key in coverage["sessions_read"]}
            final_read = set(declared_read)
        if "sessions_skipped" in coverage:
            declared_skipped = {str(key) for key in coverage["sessions_skipped"]}
            final_skipped = set(declared_skipped)

    overlap = sorted(final_read & final_skipped)
    if overlap:
        raise ValueError(
            "coverage.sessions_read and coverage.sessions_skipped must be disjoint; "
            f"overlap={overlap}."
        )

    lost_read = sorted(previous_read - final_read)
    if lost_read:
        raise ValueError(
            "coverage.sessions_read cannot silently remove or downgrade previously read "
            f"sessions: {lost_read}."
        )
    lost_skipped = sorted(previous_skipped - final_read - final_skipped)
    if lost_skipped:
        raise ValueError(
            f"Coverage cannot silently remove previously accounted sessions: {lost_skipped}."
        )

    context_sessions = {session.key: session for session in context.sessions}
    previous_sessions = previous_read | previous_skipped
    promoted_outside_context = sorted((previous_skipped & final_read) - set(context_sessions))
    if promoted_outside_context:
        raise ValueError(
            "Previously skipped sessions can be promoted to read only when their immutable "
            f"slice is present in this run context: {promoted_outside_context}."
        )
    newly_claimed_outside_context = sorted(
        (final_read | final_skipped) - previous_sessions - set(context_sessions)
    )
    if newly_claimed_outside_context:
        raise ValueError(
            "Coverage introduced sessions outside this run context: "
            f"{newly_claimed_outside_context}."
        )

    accounted = final_read | final_skipped
    expected_cursors = {
        key: context_sessions[key].last_uuid
        for key in sorted(accounted & set(context_sessions))
        if context_sessions[key].last_uuid is not None
    }
    if patch.processed_cursors != expected_cursors:
        raise ValueError(
            "processed_cursors must be derived from the sessions explicitly accounted for "
            "by this patch's coverage update; "
            f"expected {expected_cursors}, got {patch.processed_cursors}."
        )


def _one_hop_relations(state: GraphState, node_id: str) -> list[ChatRelation]:
    relations = []
    for edge in state.edges.values():
        if edge.source == node_id:
            other_id, direction = edge.target, "outgoing"
        elif edge.target == node_id:
            other_id, direction = edge.source, "incoming"
        else:
            continue
        other = state.nodes.get(other_id)
        if other is None:
            continue
        relations.append(
            ChatRelation(
                relation=edge.relation,
                direction=direction,
                other_node_id=other_id,
                other_node_type=other.type,
                other_node_title=other.title,
                explanation=edge.explanation,
            )
        )
    return sorted(relations, key=lambda item: (item.direction, item.relation, item.other_node_id))


def _session_priority(session: ConversationSession) -> tuple[int, int, float, int, str]:
    interactive_root = int(
        (session.thread_source == "user" or session.provider in {"claude", "app_chat"})
        and session.parent_session_id is None
        and session.originator != "codex_exec"
    )
    root_session = int(session.parent_session_id is None)
    timestamp = session.last_timestamp or session.first_timestamp
    if timestamp is None:
        recency = float("-inf")
    else:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        recency = timestamp.timestamp()
    return interactive_root, root_session, recency, session.record_count, session.key


def _usable_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    if not cursor:
        return None, None
    try:
        datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    except ValueError:
        return cursor, None
    return None, "Legacy timestamp cursor ignored; read this session from its beginning."
