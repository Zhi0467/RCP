from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rcp.agents import AgentEvent, AgentLauncher, PromptFactory
from rcp.agents.command_mailbox import StagedCommandMailbox
from rcp.agents.continuation_prompt import (
    LaunchPhase,
    MasterRef,
    PromptNode,
    changed_since_master,
    classify,
    compose,
    master_key,
)
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.agents.prompts import (
    GraphReadPrompt,
    _attachment_items,
    _invoked_package_section,
    graph_read_contract,
    invoked_package_pointers,
    invoked_provider_skill_section,
)
from rcp.attachments import stage_turn_attachments
from rcp.background import AgentTaskExecution
from rcp.config import AgentSurfaceConfig
from rcp.core.models import HiddenReadScope
from rcp.limits import (
    COMMAND_MAILBOX_TIMEOUT_SECONDS,
    PAPER_COACH_MAX_REFRESH_ROUNDS,
)
from rcp.paper import PaperLive, PaperService, PaperSnapshot, WritingSession
from rcp.paper.changes import InvalidChanges, get_paper_changes, read_paper_changes
from rcp.providers import ProviderSkillReference
from rcp.runs.chat import _validated_local_chat_resume_stage, open_remote_conversation_stage
from rcp.runs.patch_validator import (
    stage_patch_validation_mailbox,
)
from rcp.runs.recorded_settlement import (
    absorb_recorded_events,
    attach_retained_stage,
    note_stage_unreachable,
    provider_turn_request,
    refuse_recorded_session_mismatch,
)
from rcp.runs.recorded_turn import decode_recorded_turn
from rcp.runs.session_master import (
    commit_prepared_graph_observation,
    continuation_session_master,
    prepare_graph_observation,
    prepared_graph_observation_delta,
    record_inline_prompt,
    record_session_master,
)
from rcp.runs.shared import (
    _pinned_to_profile,
    _prepare_hidden_read_scope,
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _remove_local_tree,
    _safe_stage_name,
    _sse,
    _stage_context_paths,
    _stage_json_task_input,
    _stage_task_contract,
    _stage_task_input,
    _stream_agent_events,
    _swept_stage_root,
    _task_token,
    note_link_lost_before_provider,
)
from rcp.runs.tasks.work_turn_runtime import (
    WorkValidatorMailboxLifecycle,
    load_work_mailbox_context,
    restore_work_validator_mailbox,
    start_work_validator_mailbox,
)
from rcp.service import CoachRequest, ProjectService
from rcp.skills.staging import stage_skill_selection
from rcp.storage.agent_tasks import native_session_profile_reason
from rcp.transport import RemoteRunStage, StateUnavailable, StateUnreachable

if TYPE_CHECKING:
    from rcp.agents.context import CoachContext
    from rcp.paper.changes import PaperChanges
    from rcp.runs.recorded_turn import RecordedProviderTurn
    from rcp.storage import AppStore


def _coach_request_on_base(service: ProjectService, request: CoachRequest) -> CoachRequest:
    """Migrate retained requests before resolving a new launch, leaving admission untouched."""
    base_machine = service.manifest.base_machine
    if request.run_on is not None and request.run_on != base_machine:
        return request.model_copy(update={"run_on": base_machine, "session_id": None})
    return request


def _resolve_coach_launch(
    service: ProjectService,
    request: CoachRequest,
    *,
    reusing_checkpoint: bool = False,
) -> tuple[CoachRequest, AgentSurfaceConfig]:
    existing = None
    base_machine = service.manifest.base_machine
    if request.session_id:
        existing = next(
            (
                session
                for session in service.paper.sessions()
                if session.native_session_id == request.session_id
            ),
            None,
        )
        if existing is None and not reusing_checkpoint:
            raise ValueError("That native session was not created by this Paper workspace.")
        if (
            existing is not None
            and existing.execution_machine == base_machine
            and request.run_on is not None
            and request.run_on != existing.execution_machine
        ):
            raise ValueError("A resumed session cannot change execution machine.")
    previous = existing if existing and request.provider in {None, existing.provider} else None
    profile = service.resolve_agent_profile(
        "paper_coach",
        provider=request.provider or (existing.provider if existing else None),
        model=(
            request.model
            if request.model is not None
            else ("" if previous.model == "provider-default" else previous.model)
            if previous
            else None
        ),
        reasoning=request.reasoning or (previous.reasoning if previous else None),
        run_on=base_machine,
    )
    resolved = request.model_copy(
        update={
            "provider": profile.provider,
            "model": profile.model or None,
            "reasoning": profile.reasoning,
            "run_on": profile.run_on,
            "session_id": (
                None
                if existing is not None
                and native_session_profile_reason(
                    {"provider": existing.provider, "run_on": existing.execution_machine},
                    {"provider": profile.provider, "run_on": profile.run_on},
                )
                != "current_session"
                else request.session_id
            ),
        }
    )
    result = service.resolve_skill_request(resolved)
    assert isinstance(result, CoachRequest)
    return result, profile


def _resolved_coach_request(service: ProjectService, request: CoachRequest) -> CoachRequest:
    resolved, _profile = _resolve_coach_launch(service, request)
    return resolved


async def stream_coach(
    service: ProjectService,
    launcher: AgentLauncher,
    paper: PaperService,
    request: CoachRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
) -> AsyncGenerator[str, None]:
    snapshots: list[Path] = []
    try:
        async with aclosing(
            _stream_coach(
                service, launcher, paper, request, data_dir, execution, snapshots=snapshots
            )
        ) as stream:
            async for frame in stream:
                yield frame
    finally:
        for path in snapshots:
            _remove_local_tree(path, path.parent)


def _stage_saved_paper(
    live: PaperLive,
    snapshot: PaperSnapshot,
    stage: Path,
    snapshots: list[Path],
    round_number: int,
) -> Path:
    path = (stage / f"paper-base-{round_number}").resolve()
    # An interrupted process may have left its old read-only materialization.
    _remove_local_tree(path, stage.resolve())
    snapshots.append(path)
    live.materialize(snapshot, path)
    for child in path.rglob("*"):
        child.chmod(0o500 if child.is_dir() else 0o400)
    path.chmod(0o500)
    return path


def _stage_coach_round(
    live: PaperLive,
    snapshot: PaperSnapshot,
    local_stage: Path,
    snapshots: list[Path],
    round_number: int,
    remote_stage: RemoteRunStage | None,
    token: str,
) -> tuple[Path, Path]:
    snapshot_path = _stage_saved_paper(live, snapshot, local_stage, snapshots, round_number)
    if remote_stage is None:
        return snapshot_path, Path(tempfile.mkdtemp(prefix="changes-", dir=local_stage)).resolve()
    snapshot_path = Path(
        remote_stage.put_directory(
            snapshot_path,
            f"{token}-paper-base-{round_number}",
            reuse=True,
        )
    )
    remote_stage.finalize_inputs()
    changes_path = remote_stage.prepare_artifact_directory(f"{token}-{round_number}", reuse=False)
    return snapshot_path, Path(str(changes_path))


PAPER_COACH_FINALIZATION_ROLE = "paper_coach_finalization"
_COACH_ROUND_ROLE = "paper_coach_round:"


class _CoachStage(BaseModel):
    """The exact remote stage every round of one coach turn runs in."""

    stage_host: str
    stage_root: str
    workspace: str


class _CoachRound(BaseModel):
    """What collecting one remote round needs once the link to its host is gone."""

    snapshot: PaperSnapshot | None = Field(default=None, exclude=True)
    paper_path: Path | None = Field(default=None, exclude=True)
    changes: str
    revision: int
    file_ids: list[str]
    replace_group_ids: list[str] | None
    replace_source_task_id: str | None


def _coach_round_contracts(store: AppStore, operation_id: str) -> list[str]:
    return [
        record.content
        for record in store.agent_task_contracts(operation_id)
        if record.role.startswith(_COACH_ROUND_ROLE)
    ]


def _record_coach_round(
    execution: AgentTaskExecution, workspace: Path, current: _CoachRound
) -> None:
    """Write down this round before its provider starts, so a dropped link can settle it."""

    store = execution.store
    stage = _CoachStage(
        stage_host=execution.stage_host or "",
        stage_root=execution.stage_root or "",
        workspace=str(workspace),
    ).model_dump_json()
    store.record_agent_task_contract(
        execution.operation_id,
        PAPER_COACH_FINALIZATION_ROLE,
        stage,
        hashlib.sha256(stage.encode()).hexdigest(),
    )
    prior = _coach_round_contracts(store, execution.operation_id)
    content = current.model_dump_json()
    if not prior or prior[-1] != content:
        store.record_agent_task_contract(
            execution.operation_id,
            f"{_COACH_ROUND_ROLE}{len(prior)}",
            content,
            hashlib.sha256(content.encode()).hexdigest(),
        )


async def collect_round(
    service: ProjectService,
    execution: AgentTaskExecution,
    request: CoachRequest,
    current: _CoachRound,
    workspace: Path,
    remote_stage: RemoteRunStage | None,
) -> AgentEvent | None:
    """Collect the same recorded round on the live and recovered paths."""
    token = _task_token(execution)
    snapshot = current.snapshot or _only_files(
        await service.paper_live.snapshot(service.paper.project_id, current.revision),
        set(current.file_ids),
    )
    try:
        tree = await asyncio.to_thread(
            read_paper_changes, workspace / current.changes, remote_stage=remote_stage
        )
        await asyncio.to_thread(
            get_paper_changes(service).collect,
            service.paper.project_id,
            token,
            snapshot,
            tree,
            mode=request.mode,
            replace_group_ids=set(current.replace_group_ids)
            if current.replace_group_ids is not None
            else None,
            replace_source_task_id=current.replace_source_task_id,
            receipt_key=f"paper:{token}:collect:{current.changes}" if remote_stage else None,
        )
    except StateUnreachable as exc:
        note_stage_unreachable(execution, exc)
        raise
    except (InvalidChanges, ValueError) as exc:
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "paper_changes_invalid",
            {"diagnostics": exc.diagnostics if isinstance(exc, InvalidChanges) else [str(exc)]},
        )
        return AgentEvent(event="error", text=str(exc))
    return None


async def _finish_coach_turn(service: ProjectService, request: CoachRequest, token: str) -> None:
    """After the final round, apply directly when the turn asked for it."""
    if request.mode == "apply_directly":
        # PaperChanges holds the live write-admission fence around each apply.
        await asyncio.to_thread(
            get_paper_changes(service).accept_all,
            service.paper.project_id,
            token,
            "Coach, applied directly",
            directly=True,
        )


async def _record_writing_session(
    service: ProjectService,
    paper: PaperService,
    request: CoachRequest,
    *,
    runtime_id: str,
    native_session_id: str,
    execution_machine: str,
) -> None:
    assert request.provider is not None  # Admission resolves the provider.
    existing = next(
        (item for item in paper.sessions() if item.native_session_id == native_session_id), None
    )
    intro_hash, graph_revision, research_hash = await asyncio.to_thread(service.pointer_hashes)
    now = datetime.now(UTC)
    paper.record_session(
        WritingSession(
            provider=request.provider,
            runtime_id=runtime_id,
            native_session_id=native_session_id,
            execution_machine=execution_machine,
            project_id=paper.project_id,
            title=existing.title if existing else request.message[:72],
            model=request.model or "provider-default",
            reasoning=request.reasoning,
            created_at=existing.created_at if existing else now,
            last_resumed_at=now,
            introduction_hash_examined=intro_hash,
            graph_revision_examined=graph_revision,
            research_md_hash_examined=research_hash,
        )
    )


def resume_coach_command_mailbox(
    service: Callable[[], ProjectService], execution: AgentTaskExecution
) -> WorkValidatorMailboxLifecycle | None:
    saved = load_work_mailbox_context(execution)
    if saved is None:
        return None
    from rcp.runs.graph_history_queries import handle_graph_history_command

    scope = GraphReadScope.model_validate(saved["graph_read_scope"])
    reader = service()
    return restore_work_validator_mailbox(
        execution,
        validate=None,
        command_handler=lambda command, _identity: handle_graph_history_command(
            reader, scope, command
        ),
    )


async def finalize_recorded_coach_result(
    service: ProjectService,
    launcher: AgentLauncher,
    request: CoachRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    recorded: RecordedProviderTurn,
) -> AsyncIterator[str]:
    """Collect the last remote round from the pass its host recorded.

    Collection only: refresh rounds a watched turn could add are not started, so
    cards that went stale meanwhile stay outdated for the human to refresh.
    """

    del launcher, data_dir  # Recovery launches nothing and stages nothing.
    stage = execution.store.agent_task_contract(
        execution.operation_id, PAPER_COACH_FINALIZATION_ROLE
    )
    rounds = _coach_round_contracts(execution.store, execution.operation_id)
    if stage is None or not rounds:
        raise ValueError("The coach turn's finalization binding is unavailable.")
    retained = _CoachStage.model_validate_json(stage)
    last = _CoachRound.model_validate_json(rounds[-1])
    _local, remote_stage, workspace = attach_retained_stage(
        execution,
        owner="Paper coach",
        stage_host=retained.stage_host,
        stage_root=retained.stage_root,
        workspace=retained.workspace,
    )
    outcome = _ProviderOutcome(session_id=request.session_id)
    verdict = decode_recorded_turn(
        recorded,
        replace(
            provider_turn_request(workspace, recorded, capability="paper_suggest"),
            write_dirs=[workspace / last.changes],
        ),
    )
    refusal = refuse_recorded_session_mismatch(execution, outcome, verdict, request.session_id)
    if refusal:
        for frame in refusal:
            yield frame
        return
    for frame in absorb_recorded_events(outcome, verdict):
        yield frame
    for answer in outcome.answers:
        yield _sse(AgentEvent(event="answer", text=answer))
    if outcome.session_id:
        # A controller that died mid-turn never reached the live stream's session record.
        await _record_writing_session(
            service,
            service.paper,
            request,
            runtime_id=recorded.runtime_id,
            native_session_id=outcome.session_id,
            execution_machine=request.run_on or "",
        )
    if outcome.failed:
        return
    if not outcome.completed:
        yield _sse(AgentEvent(event="error", text=f"{request.provider} produced no result."))
        return
    event = await collect_round(service, execution, request, last, workspace, remote_stage)
    if event is None:
        await _finish_coach_turn(service, request, _task_token(execution))
        commit_prepared_graph_observation(execution, service, native_session_id=outcome.session_id)
        event = AgentEvent(event="done")
    yield _sse(event)


def _coach_stopped(execution: AgentTaskExecution) -> bool:
    return execution.control.pause_requested.is_set() or execution.control.detach_requested.is_set()


def _only_files(snapshot: PaperSnapshot, file_ids: set[str]) -> PaperSnapshot:
    return snapshot.model_copy(
        update={"files": [file for file in snapshot.files if file.file_id in file_ids]}
    )


def _validate_paper_continuation(
    execution: AgentTaskExecution,
    request: CoachRequest,
    project_id: str,
) -> None:
    source_id = request.refresh_source_task_id
    if not source_id:
        if request.refresh_group_ids:
            raise ValueError("Paper refresh requires its source task")
        return
    source = execution.store.agent_task(source_id)
    fresh_on_base = (
        source is not None
        and source.request.get("run_on") != request.run_on
        and request.session_id is None
    )
    if (
        source is None
        or source.project_id != project_id
        or source.kind != "paper_coach"
        or not source.native_session_id
        or (not fresh_on_base and source.native_session_id != request.session_id)
        or source.request.get("provider") != request.provider
        or (not fresh_on_base and source.request.get("run_on") != request.run_on)
        or source.request.get("mode", "review") != request.mode
    ):
        raise ValueError("Paper continuation must retain its task, session, machine, and mode")


def _paper_retry_diagnostics(execution: AgentTaskExecution) -> list[str]:
    task = execution.store.agent_task(execution.operation_id)
    source_task_id = task.parent_operation_id if task is not None else None
    if source_task_id:
        receipts = execution.store.agent_task_receipts(source_task_id)
        for receipt in reversed(receipts):
            if receipt.category == "paper_changes_invalid":
                diagnostics = receipt.payload.get("diagnostics")
                if isinstance(diagnostics, list) and all(isinstance(x, str) for x in diagnostics):
                    return diagnostics
    return list(execution.retry_feedback)


@dataclass(frozen=True)
class _CoachStages:
    local: Path
    workspace: Path
    remote: RemoteRunStage | None

    @property
    def inputs(self) -> Path | None:
        return None if self.remote else self.local

    @property
    def local_workspace(self) -> Path | None:
        return None if self.remote else self.workspace

    @property
    def inputs_dir(self) -> Path:
        if self.remote and self.remote.root:
            return Path(str(self.remote.root / "inputs"))
        return self.local / "inputs"


@dataclass
class _CoachLaunch:
    request: CoachRequest
    profile: AgentSurfaceConfig
    stages: _CoachStages
    read_dirs: list[Path]
    prompt: str
    master: MasterRef
    current: _CoachRound
    open_round: Callable[[int, PaperSnapshot, set[str] | None], Awaitable[_CoachRound]]
    master_values: Callable[[_CoachRound], dict[str, object]]
    history: StagedCommandMailbox
    graph_read_scope: GraphReadScope
    provider_outcome: _ProviderOutcome


class _CoachResult(Enum):
    DONE = "done"
    PAUSED = "paused"
    FAILED = "failed"
    PENDING = "pending"


_LAUNCH_KINDS = {"retry": "retry", "handoff": "retry", "resume": "resume"}


def _coach_repository_dirs(context: CoachContext, *, remote: bool) -> list[Path]:
    """Readable local repository folders; on a remote stage, every non-host repository."""
    return [
        Path(item.path)
        for item in context.repositories
        if not item.host and (remote or Path(item.path).exists())
    ]


def _keeps_checkpoint(
    reusing: bool,
    *,
    previous_machine: str | None,
    previous_session: str | None,
    request: CoachRequest,
    profile: AgentSurfaceConfig,
    retrying: bool,
) -> bool:
    """A native checkpoint survives only on the same machine and session it was saved for."""
    return bool(
        reusing
        and previous_machine in {None, profile.run_on}
        and previous_session == request.session_id
        and not (retrying and not request.session_id)
    )


def _coach_stages(
    service: ProjectService,
    paper: PaperService,
    request: CoachRequest,
    profile: AgentSurfaceConfig,
    data_dir: Path,
    execution: AgentTaskExecution,
    *,
    token: str,
    reusing_checkpoint: bool,
) -> _CoachStages:
    execution_host = service.manifest.machine_map[profile.run_on].host
    stage_root = _swept_stage_root(data_dir, store=execution.store)
    if reusing_checkpoint and not execution_host:
        saved_name = Path(execution.stage_root or "").name
        local_stage = _validated_local_chat_resume_stage(execution, stage_root / saved_name)
    else:
        local_stage = stage_root / f"paper-{token}"
        local_stage.mkdir(parents=True, exist_ok=bool(execution_host and reusing_checkpoint))
        if not execution_host:
            execution.checkpoint_stage("", str(local_stage))
    # OpenCode and Claude find a native session by its working folder, so a session's
    # turns all run in the folder of the turn that started it. One session runs one
    # turn at a time, and its command mailbox owns the whole folder.
    origin = (
        execution.store.native_session_origin(paper.project_id, "paper_coach", request.session_id)
        if request.session_id
        else None
    )
    stage_name = f"paper-coach-{_safe_stage_name(origin) if origin else token}"
    if not execution_host:
        workspace = stage_root / stage_name
        workspace.mkdir(mode=0o700, exist_ok=True)
        os.utime(workspace)
        return _CoachStages(local_stage, workspace, None)
    remote_stage = open_remote_conversation_stage(
        execution_host, stage_name, execution, saved_stage=reusing_checkpoint
    )
    return _CoachStages(local_stage, Path(str(remote_stage.workspace)), remote_stage)


async def _refresh_file_ids(
    changes: PaperChanges, project_id: str, request: CoachRequest
) -> set[str] | None:
    """The files a refresh re-reads: those its still-undecided source groups touch."""
    if not request.refresh_source_task_id:
        return None
    wanted_groups = set(request.refresh_group_ids)
    selected = [
        group
        for group in await asyncio.to_thread(
            changes.groups, project_id, request.refresh_source_task_id
        )
        if group.group_id in wanted_groups
    ]
    if (
        len(selected) != len(wanted_groups)
        or not selected
        or any(group.state not in {"pending", "outdated"} for group in selected)
    ):
        raise ValueError("Only undecided source groups can be refreshed")
    return await asyncio.to_thread(changes.affected_file_ids, project_id, wanted_groups)


@dataclass(frozen=True)
class _RoundOpener:
    """Stages one round's snapshot and records the revision it starts from."""

    live: PaperLive
    changes: PaperChanges
    stages: _CoachStages
    snapshots: list[Path]
    token: str
    project_id: str
    refresh_source_task_id: str | None
    execution: AgentTaskExecution

    async def __call__(
        self, n: int, snapshot: PaperSnapshot, selected_files: set[str] | None
    ) -> _CoachRound:
        remote_stage = self.stages.remote
        paper_path, changes_path = _stage_coach_round(
            self.live, snapshot, self.stages.local, self.snapshots, n, remote_stage, self.token
        )
        source = self.refresh_source_task_id if n == 0 else None
        replacing = (
            await asyncio.to_thread(
                self.changes.undecided_group_ids,
                self.project_id,
                source or self.token,
                selected_files,
            )
            if selected_files is not None and (source or n)
            else None
        )
        current = _CoachRound(
            snapshot=snapshot,
            paper_path=paper_path,
            changes=changes_path.relative_to(self.stages.workspace).as_posix()
            if remote_stage
            else str(changes_path),
            revision=snapshot.revision,
            file_ids=[file.file_id for file in snapshot.files],
            replace_group_ids=sorted(replacing) if replacing is not None else None,
            replace_source_task_id=source,
        )
        self.execution.store.record_agent_task_receipt(
            self.execution.operation_id, "paper_turn_base", {"revision": snapshot.revision}
        )
        return current


@dataclass(frozen=True)
class _CoachTurnInputs:
    """The staged files one coach turn points its agent at."""

    human_request_path: str
    retry_diagnostics_path: str | None
    attachment_pointers: list[dict[str, object]]
    skill_pointers: list[dict[str, object]]
    invoked_skill_pointers: list[dict[str, object]]


def _coach_prompt(
    execution: AgentTaskExecution,
    request: CoachRequest,
    stages: _CoachStages,
    inputs: _CoachTurnInputs,
    *,
    token: str,
    master_values: dict[str, object],
    render_contract: Callable[[], str],
) -> tuple[MasterRef, str, str]:
    """The master, the prompt and its recorded path, for a new session or a continued one."""
    continuation = execution.continuation
    retry_attempt = continuation in {"retry", "handoff"}
    node = classify(
        LaunchPhase(
            session_id=request.session_id,
            phase="turn" if continuation == "fresh" else "recovery",
        )
    )
    # The coach reads the graph under the base rules only, whatever the ontology.
    key = master_key(PromptFactory.PAPER_COACH_POLICY_VERSION, ontology_extensions=False)
    if node == "session_start":
        contract = render_contract()
        contract_path, prompt = _stage_task_contract(
            stages.inputs,
            stages.remote,
            f"task-{token}-{'base' if retry_attempt else 'initial'}.md",
            contract,
            execution=execution,
            role="paper_coach_retry_base" if retry_attempt else "paper_coach",
        )
        record_session_master(execution.store, execution.operation_id, contract, key, master_values)
        return (
            MasterRef(path=contract_path, bootstrap=False, values=master_values),
            prompt,
            contract_path,
        )
    assert request.session_id is not None
    master = continuation_session_master(
        execution,
        local_stage=stages.inputs,
        native_session_id=request.session_id,
        remote_stage=stages.remote,
        label_prefix="paper-coach-master",
        key=key,
        render=render_contract,
        values=master_values,
    )
    prompt = compose(
        node,
        parts=_coach_continuation_parts(
            node,
            refresh=request.refresh_source_task_id is not None,
            master_opens_now=master.bootstrap,
            human_request_path=inputs.human_request_path,
            human_message=request.message,
            attachments=inputs.attachment_pointers,
            retry_diagnostics_path=inputs.retry_diagnostics_path,
            invoked_skill_pointers=inputs.invoked_skill_pointers,
            invoked_provider_skills=request.resolved_provider_skills,
        ),
        master=master,
        delta={
            **(changed_since_master(master, master_values) or {}),
            **prepared_graph_observation_delta(execution, request.session_id),
        },
    )
    recovery_mode = {"resume": "resume", "retry": "retry"}.get(continuation)
    contract_path = record_inline_prompt(
        execution,
        local_stage=stages.inputs,
        remote_stage=stages.remote,
        label=f"task-{token}-{recovery_mode or 'initial'}.md",
        role=f"paper_coach_{recovery_mode}" if recovery_mode else "paper_coach",
        prompt=prompt,
    )
    return master, prompt, contract_path


async def _prepare_coach_launch(
    service: ProjectService,
    paper: PaperService,
    request: CoachRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    *,
    snapshots: list[Path],
) -> _CoachLaunch:
    continuation = execution.continuation
    retry_attempt = continuation in {"retry", "handoff"}
    previous_session = request.session_id
    previous_machine = request.run_on
    request = _coach_request_on_base(service, request)
    request, profile = _resolve_coach_launch(
        service, request, reusing_checkpoint=execution.reuses_native_checkpoint
    )
    reusing_checkpoint = _keeps_checkpoint(
        execution.reuses_native_checkpoint,
        previous_machine=previous_machine,
        previous_session=previous_session,
        request=request,
        profile=profile,
        retrying=continuation == "retry",
    )
    request = _pinned_to_profile(request, profile)
    token = _task_token(execution)
    stages = _coach_stages(
        service,
        paper,
        request,
        profile,
        data_dir,
        execution,
        token=token,
        reusing_checkpoint=reusing_checkpoint,
    )
    workspace, remote_stage = stages.workspace, stages.remote
    live = service.paper_live
    changes = get_paper_changes(service)
    _validate_paper_continuation(execution, request, paper.project_id)
    wanted = await _refresh_file_ids(changes, paper.project_id, request)
    snapshot = await live.snapshot(paper.project_id)
    if wanted is not None:
        snapshot = _only_files(snapshot, wanted)
    open_round = _RoundOpener(
        live,
        changes,
        stages,
        snapshots,
        token,
        paper.project_id,
        request.refresh_source_task_id,
        execution,
    )
    current = await open_round(0, snapshot, wanted)
    snapshot_path, changes_path = current.paper_path, workspace / current.changes
    assert snapshot_path is not None
    input_stage = stages.inputs
    history_staged = None
    try:
        skill_pointers = stage_skill_selection(
            service.resolve_skill_selection(request),
            local_stage=input_stage,
            remote_stage=remote_stage,
            label=f"rcp-skills-{token}",
        )
        if reusing_checkpoint and not request.session_id:
            raise ValueError(
                "The continued paper-coach task has no native agent session; retry it from a "
                "clean attempt instead."
            )
        context = service.coach_context(snapshot_path)
        if remote_stage is not None:
            context = context.model_copy(
                update=_stage_context_paths(context, service, remote_stage, profile.run_on)
            )
        read_dirs = _coach_repository_dirs(context, remote=remote_stage is not None)
        retry_diagnostics_path = (
            _stage_json_task_input(
                input_stage,
                remote_stage,
                f"task-{token}-retry-diagnostics.json",
                {"diagnostics": _paper_retry_diagnostics(execution)},
            )
            if retry_attempt
            else None
        )
        repositories = [
            {"alias": item.alias, "host": item.host, "path": item.path}
            for item in context.repositories
        ]
        attachment_pointers, attachment_dirs = stage_turn_attachments(
            data_dir,
            request.attachment_batch_id,
            request.attachments,
            local_stage=input_stage,
            remote_stage=remote_stage,
        )
        read_dirs.extend(directory for directory in attachment_dirs if directory not in read_dirs)
        inputs = _CoachTurnInputs(
            human_request_path=_stage_task_input(
                input_stage,
                remote_stage,
                f"task-{token}-human-request.txt",
                request.message,
            ),
            retry_diagnostics_path=retry_diagnostics_path,
            attachment_pointers=attachment_pointers,
            skill_pointers=skill_pointers,
            invoked_skill_pointers=invoked_package_pointers(
                skill_pointers,
                workflow_ids=request.invoked_workflow_ids,
                skill_ids=request.invoked_skill_ids,
            ),
        )

        graph_read_scope = GraphReadScope.resolve(paper.project_id, service.history)
        prepare_graph_observation(
            execution,
            service,
            owner_id="paper",
            target=graph_read_scope.target,
            revision=service.history.state().revision,
            native_session_id=request.session_id,
        )
        # The client writes requests into its working folder, so the mailbox lives there.
        history_staged = stage_patch_validation_mailbox(
            local_stage=stages.local_workspace,
            remote_stage=remote_stage,
            local_input_stage=input_stage,
            task_id=token,
            turn_id=f"{token}:paper",
            authority="broker",
            timeout_seconds=COMMAND_MAILBOX_TIMEOUT_SECONDS,
        )

        graph_prompt = GraphReadPrompt(graph_read_scope, paper.store)

        def render_contract() -> str:
            return graph_read_contract(
                PromptFactory.paper_coach_task_contract(
                    paper_path=str(snapshot_path),
                    changes_path=str(changes_path),
                    graph_path=context.graph_path,
                    research_path=context.research_md_path,
                    repositories=repositories,
                    human_request_path=inputs.human_request_path,
                    retry_diagnostics_path=inputs.retry_diagnostics_path,
                    skill_pointers=skill_pointers,
                    invoked_skill_pointers=inputs.invoked_skill_pointers,
                    invoked_provider_skills=request.resolved_provider_skills,
                    attachments=attachment_pointers,
                ),
                graph_prompt,
                history_staged.client_command(),
            )

        def current_master_values(current: _CoachRound) -> dict[str, object]:
            values = _coach_master_values(
                paper_path=str(current.paper_path),
                changes_path=str(workspace / current.changes),
                graph_path=context.graph_path,
                research_path=context.research_md_path,
                repositories=repositories,
                skill_pointers=skill_pointers,
            )
            values.update(graph_prompt.values)
            values["history_client"] = history_staged.client_command()
            return values

        master, prompt, contract_path = _coach_prompt(
            execution,
            request,
            stages,
            inputs,
            token=token,
            master_values=current_master_values(current),
            render_contract=render_contract,
        )
        read_dirs.extend([Path(context.graph_path).parent, stages.inputs_dir, snapshot_path])
    except (StateUnavailable, ValueError):
        if history_staged is not None:
            history_staged.cleanup()
        raise
    _record_agent_launch_receipt(
        execution,
        request,
        prompt=prompt,
        contract_path=contract_path,
        remote=remote_stage is not None,
        resumed=reusing_checkpoint,
        continuation=continuation,
        extra={
            "surface": "paper_coach",
            "capability": "paper_suggest",
            "network_access": True,
            "launch_kind": _LAUNCH_KINDS.get(continuation, "initial"),
        },
    )
    return _CoachLaunch(
        request,
        profile,
        stages,
        read_dirs,
        prompt,
        master,
        current,
        open_round,
        current_master_values,
        history_staged,
        graph_read_scope,
        _ProviderOutcome(session_id=request.session_id),
    )


async def _run_rounds(
    service: ProjectService,
    launcher: AgentLauncher,
    launch: _CoachLaunch,
    execution: AgentTaskExecution,
    hidden_read_scope: HiddenReadScope,
) -> AsyncGenerator[str | _CoachResult, None]:
    """Stream each pass immediately, then yield its single terminal turn result."""
    request, stages = launch.request, launch.stages
    token = _task_token(execution)
    changes = get_paper_changes(service)
    execution_host = stages.remote.host if stages.remote else ""
    provider_binary = service.launcher.provider_binary(launch.profile.provider, execution_host)
    current = launch.current
    for n in range(PAPER_COACH_MAX_REFRESH_ROUNDS + 1):
        prior = launch.provider_outcome
        launch.provider_outcome = outcome = _ProviderOutcome(
            session_id=prior.session_id, runtime_id=prior.runtime_id
        )
        if _coach_stopped(execution):
            yield _CoachResult.PAUSED
            return
        if stages.remote is not None:
            _record_coach_round(execution, stages.workspace, current)
        async with aclosing(
            _stream_agent_events(
                launcher,
                request,
                launch.prompt,
                workspace=stages.workspace,
                execution_host=execution_host,
                execution=execution,
                remote_stage=stages.remote,
                session_id=outcome.session_id,
                required_session_id=outcome.session_id,
                read_dirs=launch.read_dirs,
                write_dirs=[stages.workspace / current.changes],
                write_scope=None,
                capability="paper_suggest",
                invocation_gate=launch.history.invocation_gate,
                hidden_read_scope=hidden_read_scope,
                binary=provider_binary,
                forward_labelled_events=True,
                outcome=outcome,
            )
        ) as stream:
            async for frame in stream:
                yield frame
        if outcome.remote_result_pending:
            yield _CoachResult.PENDING
            return
        if outcome.failed or not outcome.completed:
            yield _CoachResult.FAILED
            return
        if _coach_stopped(execution):
            yield _CoachResult.PAUSED
            return
        try:
            event = await collect_round(
                service, execution, request, current, stages.workspace, stages.remote
            )
        except StateUnavailable as exc:
            note_link_lost_before_provider(execution, exc)
            outcome.failed = True
            yield _sse(AgentEvent(event="error", text=str(exc)))
            yield _CoachResult.FAILED
            return
        if event is not None:
            outcome.failed = True
            yield _sse(event)
            yield _CoachResult.FAILED
            return
        stale_groups = await asyncio.to_thread(
            changes.stale_group_ids, service.paper.project_id, token
        )
        if not stale_groups or n == PAPER_COACH_MAX_REFRESH_ROUNDS or not outcome.session_id:
            break
        if _coach_stopped(execution):
            break
        newer = await service.paper_live.snapshot(service.paper.project_id)
        if newer.revision <= current.revision:
            break
        stale_files = await asyncio.to_thread(
            changes.affected_file_ids, service.paper.project_id, stale_groups
        )
        current = await launch.open_round(n + 1, _only_files(newer, stale_files), stale_files)
        assert current.paper_path is not None
        launch.read_dirs.append(current.paper_path)
        # This session already received the master, even if the first pass bootstrapped it.
        launch.master = replace(launch.master, bootstrap=False)
        launch.prompt = compose(
            "human_turn",
            master=launch.master,
            delta=changed_since_master(launch.master, launch.master_values(current)),
            parts=_coach_continuation_parts(
                "human_turn",
                refresh=True,
                master_opens_now=False,
            ),
        )
        contract_path = record_inline_prompt(
            execution,
            local_stage=stages.inputs,
            remote_stage=stages.remote,
            label=f"task-{token}-paper-refresh-{n + 1}.md",
            role=f"paper_refresh_{n + 1}",
            prompt=launch.prompt,
        )
        _record_agent_launch_receipt(
            execution,
            request,
            prompt=launch.prompt,
            contract_path=contract_path,
            remote=stages.remote is not None,
            resumed=True,
            continuation="paper_refresh",
            extra={"surface": "paper_coach", "capability": "paper_suggest"},
        )
    yield _CoachResult.PAUSED if _coach_stopped(execution) else _CoachResult.DONE


async def _stream_coach(
    service: ProjectService,
    launcher: AgentLauncher,
    paper: PaperService,
    request: CoachRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    *,
    snapshots: list[Path],
) -> AsyncGenerator[str, None]:
    try:
        launch = await _prepare_coach_launch(
            service, paper, request, data_dir, execution, snapshots=snapshots
        )
    except (StateUnavailable, ValueError) as exc:
        note_link_lost_before_provider(execution, exc)
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    stages, request = launch.stages, launch.request
    hidden_read_scope = await _prepare_hidden_read_scope(
        service,
        request,
        workspace=stages.workspace,
        remote_stage=stages.remote,
        execution=execution,
        capability="paper_suggest",
        execution_host=stages.remote.host if stages.remote else "",
        data_dir=data_dir,
        local_stage=stages.inputs,
    )
    from rcp.runs.graph_history_queries import handle_graph_history_command

    history = start_work_validator_mailbox(
        launch.history,
        execution=execution,
        validate=None,
        budget=None,
        command_handler=lambda command, _identity: handle_graph_history_command(
            service, launch.graph_read_scope, command
        ),
        resume_context={"graph_read_scope": launch.graph_read_scope.model_dump(mode="json")},
    )
    result = _CoachResult.FAILED
    try:
        async with aclosing(
            _run_rounds(service, launcher, launch, execution, hidden_read_scope)
        ) as rounds:
            async for frame in rounds:
                if isinstance(frame, _CoachResult):
                    result = frame
                else:
                    yield frame
        if result == _CoachResult.DONE:
            await _finish_coach_turn(service, request, _task_token(execution))
            yield _sse(AgentEvent(event="done"))
        elif result == _CoachResult.PAUSED:
            yield _sse(
                AgentEvent(
                    event="paused",
                    text=(
                        "Paper coaching stopped before collection."
                        if launch.provider_outcome.completed
                        else "Paper coaching stopped before launch."
                    ),
                )
            )
    except StateUnavailable as exc:
        note_link_lost_before_provider(execution, exc)
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    finally:
        outcome = launch.provider_outcome
        if outcome.remote_result_pending:
            history.detach()
        else:
            await history.close()
        # Even a consumer closing on an error/paused frame retains the native session.
        if outcome.session_id:
            await _record_writing_session(
                service,
                paper,
                request,
                runtime_id=outcome.runtime_id or execution.runtime_id,
                native_session_id=outcome.session_id,
                execution_machine=launch.profile.run_on,
            )
    if result != _CoachResult.PENDING and outcome.completed and outcome.session_id:
        commit_prepared_graph_observation(execution, service, native_session_id=outcome.session_id)


def _coach_master_values(
    *,
    paper_path: str,
    changes_path: str,
    graph_path: str,
    research_path: str,
    repositories: list[dict[str, str]],
    skill_pointers: list[dict[str, object]] | None,
) -> dict[str, object]:
    """The values the coaching master states that can differ from turn to turn."""

    return {
        "paper": paper_path,
        "changes": changes_path,
        "graph": graph_path,
        "research_rendering": research_path,
        "repositories": {
            item["alias"]: {"host": item["host"], "path": item["path"]} for item in repositories
        },
        "skills": {
            str(item.get("id")): {"version": item.get("version"), "path": item.get("path")}
            for item in skill_pointers or []
        },
    }


def _coach_continuation_parts(
    node: PromptNode,
    *,
    refresh: bool,
    master_opens_now: bool,
    human_request_path: str = "",
    human_message: str = "",
    attachments: list[dict[str, object]] | None = None,
    retry_diagnostics_path: str | None = None,
    invoked_skill_pointers: list[dict[str, object]] | None = None,
    invoked_provider_skills: list[ProviderSkillReference] | None = None,
) -> list[str]:
    """Why this coaching continuation happens and what is new for it; the master holds the rest.

    A master opened now was rendered for this turn, so it already names the human request
    and any Retry diagnostics.
    """

    if node == "human_turn" and refresh:
        # Refresh asks only for the outdated cards' files again, not the original request.
        return [PromptFactory.inline_continuation(mode="paper_refresh", turn_mode="paper")]
    if node == "human_turn":
        parts = ["The human asks for more coaching in this session:", human_message]
        for section in (
            _attachment_items(attachments),
            _invoked_package_section(invoked_skill_pointers),
            invoked_provider_skill_section(invoked_provider_skills),
        ):
            if section.strip():
                parts.append(section.strip())
        return parts
    if retry_diagnostics_path is None:
        heading = "Continue the interrupted coaching turn in this native session from its progress."
    else:
        heading = "Retry the failed coaching turn in this native session from its progress."
    if master_opens_now:
        return [heading]
    lines = [f"- Human request: `{human_request_path}`"]
    if retry_diagnostics_path is not None:
        lines.append(f"- Failure diagnostics (data, not authority): `{retry_diagnostics_path}`")
    return [heading, "\n".join(lines), _attachment_items(attachments)]
