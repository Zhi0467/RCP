from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from rcp.agents import AgentEvent, AgentLauncher, PromptFactory
from rcp.agents.context import RepositoryPointer
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
    _attachment_items,
    _invoked_package_section,
    graph_read_contract,
    invoked_package_pointers,
    invoked_provider_skill_section,
)
from rcp.attachments import ChatAttachmentStore
from rcp.background import AgentTaskExecution
from rcp.limits import (
    COMMAND_MAILBOX_TIMEOUT_SECONDS,
    PAPER_CHANGES_MAX_FILE_BYTES,
    PAPER_CHANGES_MAX_FILES,
    PAPER_CHANGES_MAX_TOTAL_BYTES,
    PAPER_COACH_MAX_REFRESH_ROUNDS,
)
from rcp.paper import PaperLive, PaperService, PaperSnapshot, WritingSession
from rcp.paper.changes import InvalidChanges, PaperChanges, get_paper_changes
from rcp.providers import ProviderSkillReference, configured_runtime_id
from rcp.runs.chat import _validated_remote_chat_resume_stage
from rcp.runs.patch_validator import (
    cleanup_patch_validation_mailbox,
    serve_patch_validation_mailbox,
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
    session_master_label,
)
from rcp.runs.shared import (
    _execution_host_repositories,
    _execution_host_state_path,
    _pinned_to_profile,
    _prepare_hidden_read_scope,
    _protected_run_stage_roots,
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _remove_local_tree,
    _safe_stage_name,
    _sse,
    _stage_json_task_input,
    _stage_or_reuse_task_input,
    _stage_task_contract,
    _stage_task_input,
    _stream_agent_events,
    _swept_stage_root,
    _task_token,
    note_link_lost_before_provider,
)
from rcp.service import CoachRequest, ProjectService
from rcp.skills.staging import stage_skill_selection
from rcp.storage.agent_tasks import native_session_profile_reason
from rcp.transport import RemoteRunStage, StateUnavailable

if TYPE_CHECKING:
    from rcp.runs.recorded_turn import RecordedProviderTurn
    from rcp.storage import AppStore


def _resolved_coach_request(
    service: ProjectService,
    request: CoachRequest,
) -> CoachRequest:
    existing = None
    base_machine = service.manifest.repository_map[service.manifest.state.repository].machine
    if request.session_id:
        existing = next(
            (
                session
                for session in service.paper.sessions()
                if session.native_session_id == request.session_id
            ),
            None,
        )
        if existing is None:
            raise ValueError("That native session was not created by this Paper workspace.")
        if (
            existing.execution_machine == base_machine
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
    return result


async def stream_coach(
    service: ProjectService,
    launcher: AgentLauncher,
    paper: PaperService,
    request: CoachRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
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


def _record_paper_base(
    execution: AgentTaskExecution | None,
    snapshot: PaperSnapshot,
) -> None:
    if execution is not None:
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "paper_turn_base",
            {"revision": snapshot.revision},
        )


PAPER_COACH_FINALIZATION_ROLE = "paper_coach_finalization"
_COACH_ROUND_ROLE = "paper_coach_round:"


class _CoachStage(BaseModel):
    """The exact remote stage every round of one coach turn runs in."""

    stage_host: str
    stage_root: str
    workspace: str


class _CoachRound(BaseModel):
    """What collecting one remote round needs once the link to its host is gone."""

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


async def _collect_round(
    changes: PaperChanges,
    project_id: str,
    token: str,
    snapshot: PaperSnapshot,
    workspace: Path,
    changes_path: Path,
    remote_stage: RemoteRunStage | None,
    *,
    mode: Literal["review", "apply_directly"],
    replace_group_ids: set[str] | None,
    replace_source_task_id: str | None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="paper-changes-") as collected:
        collected_path = changes_path
        if remote_stage is not None:
            collected_path = Path(collected)
            await asyncio.to_thread(
                remote_stage.read_workspace_directory,
                changes_path.relative_to(workspace).as_posix(),
                collected_path,
                max_files=PAPER_CHANGES_MAX_FILES + 1,
                max_file_bytes=PAPER_CHANGES_MAX_FILE_BYTES,
                max_total_bytes=PAPER_CHANGES_MAX_TOTAL_BYTES,
            )
        await asyncio.to_thread(
            changes.collect,
            project_id,
            token,
            snapshot,
            collected_path,
            mode=mode,
            replace_group_ids=replace_group_ids,
            replace_source_task_id=replace_source_task_id,
        )


def _record_invalid_changes(
    execution: AgentTaskExecution | None, exc: InvalidChanges | ValueError
) -> None:
    if execution is not None:
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "paper_changes_invalid",
            {"diagnostics": exc.diagnostics if isinstance(exc, InvalidChanges) else [str(exc)]},
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
    # The live stream forwards the coach's reply as it comes; recovery forwards it here.
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
    project_id = service.paper.project_id
    changes = get_paper_changes(service)
    token = _task_token(execution)
    snapshot = _only_files(
        await service.paper_live.snapshot(project_id, last.revision), set(last.file_ids)
    )
    try:
        await _collect_round(
            changes,
            project_id,
            token,
            snapshot,
            workspace,
            workspace / last.changes,
            remote_stage,
            mode=request.mode,
            replace_group_ids=set(last.replace_group_ids)
            if last.replace_group_ids is not None
            else None,
            replace_source_task_id=last.replace_source_task_id,
        )
    except StateUnavailable as exc:
        # An outage, not a verdict: reconciliation retries the finalization.
        note_stage_unreachable(execution, exc)
        raise
    except (InvalidChanges, ValueError) as exc:
        _record_invalid_changes(execution, exc)
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    if request.mode == "apply_directly":
        await asyncio.to_thread(
            changes.accept_all,
            project_id,
            token,
            "Coach, applied directly",
            directly=True,
        )
    commit_prepared_graph_observation(execution, service, native_session_id=outcome.session_id)
    yield _sse(AgentEvent(event="done"))


def _coach_stopped(execution: AgentTaskExecution | None) -> bool:
    return execution is not None and (
        execution.control.pause_requested.is_set() or execution.control.detach_requested.is_set()
    )


def _only_files(snapshot: PaperSnapshot, file_ids: set[str]) -> PaperSnapshot:
    return snapshot.model_copy(
        update={"files": [file for file in snapshot.files if file.file_id in file_ids]}
    )


def _validate_paper_continuation(
    execution: AgentTaskExecution | None,
    request: CoachRequest,
    project_id: str,
) -> None:
    source_id = request.refresh_source_task_id
    if not source_id:
        if request.refresh_group_ids:
            raise ValueError("Paper refresh requires its source task")
        return
    if execution is None:
        raise ValueError("Paper continuation requires a durable task")
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


async def _stream_coach(
    service: ProjectService,
    launcher: AgentLauncher,
    paper: PaperService,
    request: CoachRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
    *,
    snapshots: list[Path],
) -> AsyncGenerator[str, None]:
    continuation = execution.continuation if execution is not None else "fresh"
    reusing_checkpoint = bool(
        execution is not None
        and execution.reuses_native_checkpoint
        and not (continuation == "retry" and not request.session_id)
    )
    resuming = continuation == "resume"
    retrying = continuation == "retry"
    retry_attempt = continuation in {"retry", "handoff"}
    existing = None
    if request.session_id:
        existing = next(
            (item for item in paper.sessions() if item.native_session_id == request.session_id),
            None,
        )
    base_machine = service.manifest.repository_map[service.manifest.state.repository].machine
    if (
        reusing_checkpoint
        and existing is None
        and request.run_on
        and request.run_on != base_machine
    ):
        request = request.model_copy(update={"session_id": None, "run_on": base_machine})
        reusing_checkpoint = False
    try:
        if not (reusing_checkpoint and existing is None):
            request = _resolved_coach_request(service, request)
            if existing is not None and request.session_id is None:
                reusing_checkpoint = False
                existing = None
        profile = service.resolve_agent_profile(
            "paper_coach",
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
    except ValueError as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    request = _pinned_to_profile(request, profile)
    execution_machine = service.manifest.machine_map[profile.run_on]
    execution_host = execution_machine.host
    provider_binary = service.launcher.provider_binary(profile.provider, execution_machine.host)
    stage_root = _swept_stage_root(
        data_dir,
        store=execution.store if execution is not None else None,
    )
    token = _task_token(execution)
    if reusing_checkpoint and not execution_host:
        if not execution.stage_root:
            yield _sse(
                AgentEvent(
                    event="error",
                    text="The interrupted paper-coach task has no staging checkpoint; retry it.",
                )
            )
            return
        local_stage = Path(execution.stage_root).resolve()
        if local_stage.parent != stage_root.resolve() or not local_stage.is_dir():
            yield _sse(
                AgentEvent(
                    event="error",
                    text="The interrupted paper-coach staging checkpoint is unavailable; retry it.",
                )
            )
            return
    else:
        local_stage = stage_root / f"paper-{token}"
        local_stage.mkdir(parents=True, exist_ok=bool(execution_host and reusing_checkpoint))
        if execution is not None and not execution_host:
            execution.checkpoint_stage("", str(local_stage))
    # OpenCode and Claude find a native session by its working folder, so a session's
    # turns all run in the folder of the turn that started it. One session runs one
    # turn at a time, and its command mailbox owns the whole folder.
    origin = (
        execution.store.native_session_origin(paper.project_id, "paper_coach", request.session_id)
        if execution is not None and request.session_id
        else None
    )
    stage_name = f"paper-coach-{_safe_stage_name(origin) if origin else token}"
    remote_stage = None
    workspace = stage_root / stage_name
    try:
        if execution_host:
            if reusing_checkpoint:
                saved_root = _validated_remote_chat_resume_stage(
                    execution, execution_host, stage_name
                )
                remote_stage = RemoteRunStage(execution_host).attach(saved_root)
            else:
                remote_stage = RemoteRunStage(execution_host).open(
                    stage_name,
                    reuse=True,
                    protected_roots=_protected_run_stage_roots(
                        execution.store if execution is not None else None,
                        execution_host,
                    ),
                )
            remote_stage.touch()
            assert remote_stage.root is not None
            if execution is not None:
                execution.checkpoint_stage(execution_host, str(remote_stage.root))
            workspace = Path(str(remote_stage.workspace))
        else:
            workspace.mkdir(mode=0o700, exist_ok=True)
            os.utime(workspace)
        live = service.paper_live
        changes = get_paper_changes(service)
        _validate_paper_continuation(execution, request, paper.project_id)
        wanted = None
        if request.refresh_source_task_id:
            selected = [
                group
                for group in await asyncio.to_thread(
                    changes.groups, paper.project_id, request.refresh_source_task_id
                )
                if group.group_id in request.refresh_group_ids
            ]
            if (
                len(selected) != len(set(request.refresh_group_ids))
                or not selected
                or any(group.state not in {"pending", "outdated"} for group in selected)
            ):
                raise ValueError("Only undecided source groups can be refreshed")
            wanted = await asyncio.to_thread(
                changes.affected_file_ids, paper.project_id, set(request.refresh_group_ids)
            )
        snapshot = await live.snapshot(paper.project_id)
        if wanted is not None:
            snapshot = _only_files(snapshot, wanted)
        snapshot_path, changes_path = _stage_coach_round(
            live, snapshot, local_stage, snapshots, 0, remote_stage, token
        )
        _record_paper_base(execution, snapshot)
    except (OSError, StateUnavailable, ValueError) as exc:
        note_link_lost_before_provider(execution, exc)
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    input_stage = None if remote_stage is not None else local_stage
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
        pointers, read_dirs = service.coach_context(request, snapshot_path)
        if remote_stage is not None:
            raw = pointers["truth_repositories"]
            assert isinstance(raw, list)
            pointers["truth_repositories"] = [
                item.model_dump()
                for item in _execution_host_repositories(
                    [RepositoryPointer.model_validate(item) for item in raw], profile.run_on
                )
            ]
            for field in ("graph", "research_md"):
                pointers[field] = _execution_host_state_path(
                    service, str(pointers[field]), profile.run_on
                )
        retry_diagnostics_path = (
            _stage_json_task_input(
                input_stage,
                remote_stage,
                f"task-{token}-retry-diagnostics.json",
                {"diagnostics": _paper_retry_diagnostics(execution)},
            )
            if execution is not None and retry_attempt
            else None
        )
        raw_repositories = pointers["truth_repositories"]
        assert isinstance(raw_repositories, list)
        repositories = [
            {
                "alias": str(item["alias"]),
                "host": str(item["host"]),
                "path": str(item["path"]),
            }
            for item in raw_repositories
            if isinstance(item, dict)
        ]
        if remote_stage is not None:
            read_dirs = [Path(item["path"]) for item in repositories if not item["host"]]
        if bool(request.attachment_batch_id) != bool(request.attachments):
            raise ValueError("The chat task has incomplete attachment batch metadata.")
        attachment_pointers = (
            ChatAttachmentStore(data_dir / "chat-attachments").stage(
                request.attachment_batch_id,
                request.attachments,
                local_stage=input_stage,
                remote_stage=remote_stage,
            )
            if request.attachment_batch_id
            else []
        )
        read_dirs.extend(
            path
            for path in dict.fromkeys(
                Path(str(item["path"])).parent for item in attachment_pointers
            )
            if path not in read_dirs
        )
        human_request_path = _stage_task_input(
            input_stage,
            remote_stage,
            f"task-{token}-human-request.txt",
            request.message,
        )
        invoked_skill_pointers = invoked_package_pointers(
            skill_pointers,
            workflow_ids=request.invoked_workflow_ids,
            skill_ids=request.invoked_skill_ids,
        )

        graph_read_scope = GraphReadScope.resolve(
            paper.project_id, service.history, store=service.paper.store
        )
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
            local_stage=workspace if remote_stage is None else None,
            remote_stage=remote_stage,
            local_input_stage=local_stage if remote_stage is None else None,
            task_id=token,
            turn_id=f"{token}:paper",
            authority="broker",
            timeout_seconds=COMMAND_MAILBOX_TIMEOUT_SECONDS,
        )

        def render_contract() -> str:
            return graph_read_contract(
                PromptFactory.paper_coach_task_contract(
                    paper_path=str(snapshot_path),
                    changes_path=str(changes_path),
                    graph_path=str(pointers["graph"]),
                    research_path=str(pointers["research_md"]),
                    repositories=repositories,
                    human_request_path=human_request_path,
                    retry_diagnostics_path=retry_diagnostics_path,
                    skill_pointers=skill_pointers,
                    invoked_skill_pointers=invoked_skill_pointers,
                    invoked_provider_skills=request.resolved_provider_skills,
                    attachments=attachment_pointers,
                ),
                graph_read_scope,
                history_staged.client_command(),
            )

        def current_master_values() -> dict[str, object]:
            values = _coach_master_values(
                paper_path=str(snapshot_path),
                changes_path=str(changes_path),
                graph_path=str(pointers["graph"]),
                research_path=str(pointers["research_md"]),
                repositories=repositories,
                skill_pointers=skill_pointers,
            )
            values["branches"] = [
                item.model_dump(mode="json") for item in graph_read_scope.branches
            ]
            values["history_client"] = history_staged.client_command()
            return values

        node = classify(
            LaunchPhase(
                session_id=request.session_id,
                phase="turn" if continuation == "fresh" else "recovery",
            )
        )
        master_values = current_master_values()
        # The coach reads the graph under the base rules only, whatever the ontology.
        key = master_key(PromptFactory.PAPER_COACH_POLICY_VERSION, ontology_extensions=False)
        if node == "session_start":
            contract = render_contract()
            contract_path, prompt = _stage_task_contract(
                input_stage,
                remote_stage,
                f"task-{token}-{'base' if retry_attempt else 'initial'}.md",
                contract,
                execution=execution,
                role="paper_coach_retry_base" if retry_attempt else "paper_coach",
            )
            if execution is not None:
                record_session_master(
                    execution.store, execution.operation_id, contract, key, master_values
                )
            master = MasterRef(path=contract_path, bootstrap=False, values=master_values)
        else:
            assert request.session_id is not None
            master = _coach_session_master(
                execution,
                local_stage=input_stage,
                native_session_id=request.session_id,
                remote_stage=remote_stage,
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
                    human_request_path=human_request_path,
                    human_message=request.message,
                    attachments=attachment_pointers,
                    retry_diagnostics_path=retry_diagnostics_path,
                    invoked_skill_pointers=invoked_skill_pointers,
                    invoked_provider_skills=request.resolved_provider_skills,
                ),
                master=master,
                delta={
                    **(changed_since_master(master, master_values) or {}),
                    **prepared_graph_observation_delta(execution, request.session_id),
                },
            )
            recovery_mode = "resume" if resuming else "retry" if retrying else None
            contract_path = record_inline_prompt(
                execution,
                local_stage=input_stage,
                remote_stage=remote_stage,
                label=f"task-{token}-{recovery_mode or 'initial'}.md",
                role=f"paper_coach_{recovery_mode}" if recovery_mode else "paper_coach",
                prompt=prompt,
            )
        inputs = (
            Path(str(remote_stage.root / "inputs"))
            if remote_stage and remote_stage.root
            else local_stage / "inputs"
        )
        read_dirs.extend([Path(str(pointers["graph"])).parent, inputs, snapshot_path])
    except (StateUnavailable, ValueError) as exc:
        note_link_lost_before_provider(execution, exc)
        if history_staged is not None:
            history_staged.cleanup()
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    native_session_id = request.session_id
    actual_runtime_id = (
        execution.runtime_id
        if execution is not None
        else configured_runtime_id(profile.provider, profile.runtime)
    )
    completed = False
    provider_failed = False
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
            "launch_kind": "retry" if retry_attempt else "resume" if resuming else "initial",
        },
    )
    hidden_read_scope = await _prepare_hidden_read_scope(
        service,
        request,
        workspace=workspace,
        remote_stage=remote_stage,
        execution=execution,
        capability="paper_suggest",
        execution_host=execution_host,
        data_dir=data_dir,
        local_stage=input_stage,
    )
    from rcp.runs.graph_history_queries import handle_graph_history_command

    history_stop = asyncio.Event()
    history_task = asyncio.create_task(
        serve_patch_validation_mailbox(
            staged=history_staged,
            execution=execution,
            validate=None,
            budget=None,
            stop=history_stop,
            command_handler=lambda command, _identity: handle_graph_history_command(
                service,
                graph_read_scope,
                command,
            ),
        )
    )

    async def _record_coach_session() -> None:
        assert native_session_id is not None
        await _record_writing_session(
            service,
            paper,
            request,
            runtime_id=actual_runtime_id,
            native_session_id=native_session_id,
            execution_machine=profile.run_on,
        )

    try:
        replace_source_task_id = request.refresh_source_task_id
        replace_group_ids = (
            await asyncio.to_thread(
                changes.undecided_group_ids, paper.project_id, replace_source_task_id, wanted
            )
            if replace_source_task_id and wanted is not None
            else None
        )
        for refresh_round in range(PAPER_COACH_MAX_REFRESH_ROUNDS + 1):
            completed = False
            if _coach_stopped(execution):
                yield _sse(AgentEvent(event="paused", text="Paper coaching stopped before launch."))
                break
            if remote_stage is not None and execution is not None:
                _record_coach_round(
                    execution,
                    workspace,
                    _CoachRound(
                        changes=changes_path.relative_to(workspace).as_posix(),
                        revision=snapshot.revision,
                        file_ids=[file.file_id for file in snapshot.files],
                        replace_group_ids=sorted(replace_group_ids)
                        if replace_group_ids is not None
                        else None,
                        replace_source_task_id=replace_source_task_id,
                    ),
                )
            provider_outcome = _ProviderOutcome(session_id=native_session_id)
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    request,
                    prompt,
                    workspace=workspace,
                    execution_host=execution_host,
                    execution=execution,
                    remote_stage=remote_stage,
                    session_id=native_session_id,
                    required_session_id=native_session_id,
                    read_dirs=read_dirs,
                    write_dirs=[changes_path],
                    write_scope=None,
                    capability="paper_suggest",
                    invocation_gate=history_staged.invocation_gate,
                    hidden_read_scope=hidden_read_scope,
                    binary=provider_binary,
                    outcome=provider_outcome,
                    forward_labelled_events=True,
                )
            ) as stream:
                async for frame in stream:
                    event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                    if event.event == "runtime":
                        actual_runtime_id = event.text
                    if event.event == "session" and event.session_id:
                        native_session_id = event.session_id
                    yield frame
            completed = provider_outcome.completed
            provider_failed = provider_outcome.failed
            if provider_outcome.remote_result_pending:
                return
            if not completed or provider_failed or _coach_stopped(execution):
                break
            try:
                await _collect_round(
                    changes,
                    paper.project_id,
                    token,
                    snapshot,
                    workspace,
                    changes_path,
                    remote_stage,
                    mode=request.mode,
                    replace_group_ids=replace_group_ids,
                    replace_source_task_id=replace_source_task_id,
                )
            except StateUnavailable as exc:
                note_link_lost_before_provider(execution, exc)
                # The read-back failed, not the coach: there is nothing to correct.
                provider_failed = True
                yield _sse(AgentEvent(event="error", text=str(exc)))
                break
            except (InvalidChanges, ValueError) as exc:
                _record_invalid_changes(execution, exc)
                provider_failed = True
                yield _sse(AgentEvent(event="error", text=str(exc)))
                break
            replace_source_task_id = None
            replace_group_ids = await asyncio.to_thread(
                changes.stale_group_ids, paper.project_id, token
            )
            if (
                not replace_group_ids
                or refresh_round == PAPER_COACH_MAX_REFRESH_ROUNDS
                or not native_session_id
            ):
                break
            if _coach_stopped(execution):
                break
            newer = await live.snapshot(paper.project_id)
            if newer.revision <= snapshot.revision:
                break
            stale_files = await asyncio.to_thread(
                changes.affected_file_ids, paper.project_id, replace_group_ids
            )
            replace_group_ids = await asyncio.to_thread(
                changes.undecided_group_ids, paper.project_id, token, stale_files
            )
            snapshot = _only_files(newer, stale_files)
            snapshot_path, changes_path = _stage_coach_round(
                live, snapshot, local_stage, snapshots, refresh_round + 1, remote_stage, token
            )
            read_dirs.append(snapshot_path)
            _record_paper_base(execution, snapshot)
            values = current_master_values()
            # This round completed in the same session; it already received this master.
            master = MasterRef(path=master.path, bootstrap=False, values=master.values)
            prompt = compose(
                "human_turn",
                master=master,
                delta=changed_since_master(master, values),
                parts=[
                    PromptFactory.inline_continuation(
                        mode="paper_refresh",
                        turn_mode="paper",
                    )
                ],
            )
            contract_path = record_inline_prompt(
                execution,
                local_stage=input_stage,
                remote_stage=remote_stage,
                label=f"task-{token}-paper-refresh-{refresh_round + 1}.md",
                role=f"paper_refresh_{refresh_round + 1}",
                prompt=prompt,
            )
            _record_agent_launch_receipt(
                execution,
                request,
                prompt=prompt,
                contract_path=contract_path,
                remote=remote_stage is not None,
                resumed=True,
                continuation="paper_refresh",
                extra={
                    "surface": "paper_coach",
                    "capability": "paper_suggest",
                },
            )
        if completed and _coach_stopped(execution):
            yield _sse(AgentEvent(event="paused", text="Paper coaching stopped before collection."))
        if completed and not provider_failed and not _coach_stopped(execution):
            if request.mode == "apply_directly":
                # PaperChanges holds the live write-admission fence around each apply.
                await asyncio.to_thread(
                    changes.accept_all,
                    paper.project_id,
                    token,
                    "Coach, applied directly",
                    directly=True,
                )
            yield _sse(AgentEvent(event="done"))
    except StateUnavailable as exc:
        completed = False
        note_link_lost_before_provider(execution, exc)
        yield _sse(AgentEvent(event="error", text=str(exc)))
    finally:
        history_stop.set()
        await history_task
        await asyncio.to_thread(
            cleanup_patch_validation_mailbox,
            staged=history_staged,
            execution=execution,
        )
        # A stopped or failed turn still created its session; the chat continues in it.
        if native_session_id:
            await _record_coach_session()
    if completed and native_session_id:
        commit_prepared_graph_observation(
            execution,
            service,
            native_session_id=native_session_id,
        )


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
    human_request_path: str,
    human_message: str,
    attachments: list[dict[str, object]],
    retry_diagnostics_path: str | None,
    invoked_skill_pointers: list[dict[str, object]] | None,
    invoked_provider_skills: list[ProviderSkillReference] | None,
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


_COACH_MASTER_LABEL = "paper-coach-master"


def _coach_session_master(
    execution: AgentTaskExecution | None,
    *,
    local_stage: Path | None,
    native_session_id: str,
    remote_stage: RemoteRunStage | None,
    key: str,
    render: Callable[[], str],
    values: dict[str, object],
) -> MasterRef:
    """Restage the session's master into this turn's own stage, or bootstrap a new one."""

    if execution is None:
        content = render()
        path = _stage_or_reuse_task_input(
            local_stage, remote_stage, session_master_label(_COACH_MASTER_LABEL, content), content
        )
        return MasterRef(path=path, bootstrap=True, values=values)
    return continuation_session_master(
        execution,
        local_stage=local_stage,
        remote_stage=remote_stage,
        native_session_id=native_session_id,
        label_prefix=_COACH_MASTER_LABEL,
        key=key,
        render=render,
        values=values,
    )
