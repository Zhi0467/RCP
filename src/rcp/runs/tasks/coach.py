from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path

from rcp.agents import AgentEvent, AgentLauncher, PromptFactory
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
    _invoked_package_section,
    graph_read_contract,
    invoked_package_pointers,
    invoked_provider_skill_section,
)
from rcp.background import AgentTaskExecution
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import COMMAND_MAILBOX_TIMEOUT_SECONDS, PAPER_COACH_MAX_REFRESH_ROUNDS
from rcp.paper import PaperLive, PaperService, PaperSnapshot, WritingSession
from rcp.paper.changes import InvalidChanges, get_paper_changes
from rcp.providers import ProviderSkillReference, configured_runtime_id
from rcp.runs.patch_validator import (
    cleanup_patch_validation_mailbox,
    serve_patch_validation_mailbox,
    stage_patch_validation_mailbox,
)
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
    _pinned_to_profile,
    _prepare_hidden_read_scope,
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _record_provider_exit,
    _remove_local_tree,
    _sse,
    _stage_json_task_input,
    _stage_or_reuse_task_input,
    _stage_task_contract,
    _stage_task_input,
    _swept_stage_root,
    _task_token,
)
from rcp.service import CoachRequest, ProjectService
from rcp.skills.staging import stage_skill_selection
from rcp.transport import StateUnavailable


def _resolved_coach_request(
    service: ProjectService,
    request: CoachRequest,
) -> CoachRequest:
    existing = None
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
        if request.provider is not None and request.provider != existing.provider:
            raise ValueError("A resumed session cannot change provider.")
        if request.model is not None and (request.model or "provider-default") != existing.model:
            raise ValueError("A resumed session cannot change model.")
        if request.reasoning is not None and request.reasoning != existing.reasoning:
            raise ValueError("A resumed session cannot change reasoning.")
        if request.run_on is not None and request.run_on != existing.execution_machine:
            raise ValueError("A resumed session cannot change execution machine.")
    profile = service.resolve_agent_profile(
        "paper_coach",
        provider=request.provider or (existing.provider if existing else None),
        model=(
            request.model
            if request.model is not None
            else ("" if existing.model == "provider-default" else existing.model)
            if existing
            else None
        ),
        reasoning=request.reasoning or (existing.reasoning if existing else None),
        run_on=request.run_on or (existing.execution_machine if existing else None),
    )
    resolved = request.model_copy(
        update={
            "provider": profile.provider,
            "model": profile.model or None,
            "reasoning": profile.reasoning,
            "run_on": profile.run_on,
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
    if (
        source is None
        or source.project_id != project_id
        or source.kind != "paper_coach"
        or not source.native_session_id
        or source.native_session_id != request.session_id
        or source.request.get("provider") != request.provider
        or source.request.get("run_on") != request.run_on
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
    reusing_checkpoint = bool(execution is not None and execution.reuses_native_checkpoint)
    resuming = continuation == "resume"
    retrying = continuation == "retry"
    retry_attempt = continuation in {"retry", "handoff"}
    existing = None
    if request.session_id:
        existing = next(
            (item for item in paper.sessions() if item.native_session_id == request.session_id),
            None,
        )
    try:
        if not (reusing_checkpoint and existing is None):
            request = _resolved_coach_request(service, request)
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
    if execution_host:
        yield _sse(
            AgentEvent(
                event="error",
                text=(
                    "Remote writing-coach sessions need saved-paper snapshot and changes staging for "
                    "native resume. Choose a local machine for this invocation."
                ),
            )
        )
        return
    stage_root = _swept_stage_root(
        data_dir,
        store=execution.store if execution is not None else None,
    )
    token = _task_token(execution)
    if reusing_checkpoint:
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
        local_stage.mkdir(parents=True, exist_ok=False)
        if execution is not None:
            execution.checkpoint_stage("", str(local_stage))
    # OpenCode and Claude find a native session by its working folder, so every coach
    # turn in a project runs in one folder; a per-turn folder strands follow-ups.
    workspace = (
        stage_root / f"paper-coach-{hashlib.sha256(paper.project_id.encode()).hexdigest()[:16]}"
    )
    workspace.mkdir(mode=0o700, exist_ok=True)
    os.utime(workspace)
    try:
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
        snapshot_path = _stage_saved_paper(live, snapshot, local_stage, snapshots, 0)
        changes_path = Path(tempfile.mkdtemp(prefix="changes-", dir=local_stage)).resolve()
        _record_paper_base(execution, snapshot)
    except (OSError, ValueError) as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    history_staged = None
    try:
        skill_pointers = stage_skill_selection(
            service.resolve_skill_selection(request),
            local_stage=local_stage,
            remote_stage=None,
            label=f"rcp-skills-{token}",
        )
        if reusing_checkpoint and not request.session_id:
            raise ValueError(
                "The continued paper-coach task has no native agent session; retry it from a "
                "clean attempt instead."
            )
        pointers, read_dirs = service.coach_context(request, snapshot_path)
        retry_diagnostics_path = (
            _stage_json_task_input(
                local_stage,
                None,
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
        human_request_path = _stage_task_input(
            local_stage,
            None,
            f"task-{token}-human-request.txt",
            request.message,
        )
        invoked_skill_pointers = invoked_package_pointers(
            skill_pointers,
            workflow_ids=request.invoked_workflow_ids,
            skill_ids=request.invoked_skill_ids,
        )

        graph_read_scope = GraphReadScope(project_id=paper.project_id, target=GraphTargetRef())
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
            local_stage=workspace,
            remote_stage=None,
            local_input_stage=local_stage,
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
                local_stage,
                None,
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
                local_stage=local_stage,
                native_session_id=request.session_id,
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
                local_stage=local_stage,
                remote_stage=None,
                label=f"task-{token}-{recovery_mode or 'initial'}.md",
                role=f"paper_coach_{recovery_mode}" if recovery_mode else "paper_coach",
                prompt=prompt,
            )
        read_dirs.extend([service.manifest.research_dir, local_stage / "inputs", snapshot_path])
    except (StateUnavailable, ValueError) as exc:
        if history_staged is not None:
            history_staged.cleanup()
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    model = (
        (None if existing.model == "provider-default" else existing.model)
        if existing
        else request.model
    )
    reasoning = existing.reasoning if existing else request.reasoning
    native_session_id = request.session_id
    actual_runtime_id = (
        execution.runtime_id
        if execution is not None
        else configured_runtime_id(profile.provider, profile.runtime)
    )
    completed = False
    provider_failed = False
    provider_outcome = _ProviderOutcome(session_id=native_session_id)
    _record_agent_launch_receipt(
        execution,
        request,
        prompt=prompt,
        contract_path=contract_path,
        remote=False,
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
        remote_stage=None,
        execution=execution,
        capability="paper_suggest",
        data_dir=data_dir,
        local_stage=local_stage,
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
        intro_hash, graph_revision, research_hash = await asyncio.to_thread(service.pointer_hashes)
        now = datetime.now(UTC)
        paper.record_session(
            WritingSession(
                provider=request.provider,
                runtime_id=actual_runtime_id,
                native_session_id=native_session_id,
                execution_machine=profile.run_on,
                project_id=paper.project_id,
                title=existing.title if existing else request.message[:72],
                model=model or "provider-default",
                reasoning=reasoning,
                created_at=existing.created_at if existing else now,
                last_resumed_at=now,
                introduction_hash_examined=intro_hash,
                graph_revision_examined=graph_revision,
                research_md_hash_examined=research_hash,
            )
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
            async with aclosing(
                launcher.stream(
                    profile.provider,
                    prompt,
                    cwd=workspace,
                    model=model,
                    reasoning=reasoning,
                    session_id=native_session_id,
                    read_dirs=read_dirs,
                    write_dirs=[changes_path],
                    capability="paper_suggest",
                    invocation_gate=history_staged.invocation_gate,
                    hidden_read_scope=hidden_read_scope,
                    control=execution.control if execution is not None else None,
                    binary=provider_binary,
                    runtime_id=(execution.runtime_id or None) if execution is not None else None,
                    operation_id=execution.operation_id if execution is not None else None,
                    git_access=execution.git_access if execution is not None else None,
                )
            ) as stream:
                async for event in stream:
                    if event.event == "provider_exit":
                        try:
                            evidence: object = json.loads(event.text)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            evidence = None
                        exit_evidence: dict[str, object] = (
                            evidence
                            if isinstance(evidence, dict)
                            else {"unparsed": event.text[:400]}
                        )
                        provider_outcome.exit_evidence = exit_evidence
                        _record_provider_exit(
                            execution,
                            provider_outcome,
                            workspace=local_stage,
                            remote_stage=None,
                        )
                        continue
                    if event.event == "runtime":
                        actual_runtime_id = event.text
                    if event.event == "session" and event.session_id:
                        if native_session_id and native_session_id != event.session_id:
                            provider_failed = True
                            yield _sse(
                                AgentEvent(
                                    event="error",
                                    text="Paper coach continuation changed its native session.",
                                )
                            )
                            break
                        native_session_id = event.session_id
                    if event.event == "error":
                        provider_failed = True
                    if event.event == "done":
                        completed = True
                        continue
                    yield _sse(event)
            if not completed or provider_failed or _coach_stopped(execution):
                break
            try:
                await asyncio.to_thread(
                    changes.collect,
                    paper.project_id,
                    token,
                    snapshot,
                    changes_path,
                    mode=request.mode,
                    replace_group_ids=replace_group_ids,
                    replace_source_task_id=replace_source_task_id,
                )
            except InvalidChanges as exc:
                if execution is not None:
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "paper_changes_invalid",
                        {"diagnostics": exc.diagnostics},
                    )
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
            snapshot_path = _stage_saved_paper(
                live, snapshot, local_stage, snapshots, refresh_round + 1
            )
            changes_path = Path(tempfile.mkdtemp(prefix="changes-", dir=local_stage)).resolve()
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
                local_stage=local_stage,
                remote_stage=None,
                label=f"task-{token}-paper-refresh-{refresh_round + 1}.md",
                role=f"paper_refresh_{refresh_round + 1}",
                prompt=prompt,
            )
            _record_agent_launch_receipt(
                execution,
                request,
                prompt=prompt,
                contract_path=contract_path,
                remote=False,
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
    return [heading, "\n".join(lines)]


_COACH_MASTER_LABEL = "paper-coach-master"


def _coach_session_master(
    execution: AgentTaskExecution | None,
    *,
    local_stage: Path,
    native_session_id: str,
    key: str,
    render: Callable[[], str],
    values: dict[str, object],
) -> MasterRef:
    """Restage the session's master into this turn's own stage, or bootstrap a new one."""

    if execution is None:
        content = render()
        path = _stage_or_reuse_task_input(
            local_stage, None, session_master_label(_COACH_MASTER_LABEL, content), content
        )
        return MasterRef(path=path, bootstrap=True, values=values)
    return continuation_session_master(
        execution,
        local_stage=local_stage,
        remote_stage=None,
        native_session_id=native_session_id,
        label_prefix=_COACH_MASTER_LABEL,
        key=key,
        render=render,
        values=values,
    )
