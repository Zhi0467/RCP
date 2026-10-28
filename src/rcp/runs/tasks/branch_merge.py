from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing, suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.agents import AgentEvent, AgentLauncher
from rcp.agents.command_protocol import MergeAnswersCommandRequest
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.agents.write_scope import ProjectWriteScope, resolve_project_write_scope
from rcp.core.models import BranchMergeReceipt
from rcp.core.transition_models import GraphTargetRef
from rcp.history.branches import BranchHistoryManager
from rcp.limits import PATCH_SELF_CHECK_MAX_REQUEST_BYTES, PATCH_SELF_CHECK_TIMEOUT_SECONDS
from rcp.runs import merge_plan
from rcp.runs.branch_merge import (
    BranchMergeContext,
    BranchMergeRunOutcome,
    BranchMergeStage,
    _answers_mailbox,
    branch_merge_id,
    merge_planning_context,
    recorded_turn_plan,
    stream_branch_merge_run,
    stream_code_merge_run,
)
from rcp.runs.branch_merge_context import load_branch_merge_context, merge_history
from rcp.runs.branch_merge_request import BranchMergeRunRequest
from rcp.runs.episodes.merge import (
    complete_graph_merge,
    episode_code_merge,
    verify_episode_merge_code,
)
from rcp.runs.graph_history_queries import handle_graph_history_command
from rcp.runs.merge_contract import MergeAnswers, MergePlan, NodeGroupPromotion
from rcp.runs.patch_validator import (
    MergeAnswersCheck,
    cleanup_patch_validation_mailbox,
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
from rcp.runs.session_master import commit_prepared_graph_observation
from rcp.runs.shared import (
    _prepare_hidden_read_scope,
    _protected_run_stage_roots,
    _ProviderOutcome,
    _remove_local_tree,
    _safe_stage_name,
    _sse,
    _swept_stage_root,
)
from rcp.runs.task_policy import load_stored_request
from rcp.runs.tasks.work_turn_runtime import (
    WORK_CORRECTION_SESSION_ROLE,
    WorkValidatorMailboxLifecycle,
    load_work_mailbox_context,
    restore_work_validator_mailbox,
    start_work_validator_mailbox,
)
from rcp.service import ProjectService, RunRequest
from rcp.storage import AgentTaskRecord, EpisodeRecord
from rcp.transport import RemoteRunStage, StateUnavailable

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.runs.recorded_turn import RecordedProviderTurn


async def stream_branch_merge_task(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    *,
    episode: EpisodeRecord | None,
    task: AgentTaskRecord,
    execution: AgentTaskExecution,
    recorded: RecordedProviderTurn | None = None,
) -> AsyncGenerator[str, None]:
    """Run one authorized branch merge through its contained scratch stage.

    `recorded` settles a merge whose turn its host finished while RCP was away:
    it reattaches that turn's stage and, once the turn is admitted as the live
    stream would have admitted it, commits the answers left there.
    """

    binding = request.merge_binding if isinstance(request, BranchMergeRunRequest) else None
    if binding is None and episode is None:
        yield _sse(
            AgentEvent(event="error", text="The branch merge task lost its episode binding.")
        )
        return
    if binding is not None:
        source = binding.source
    else:
        assert episode is not None
        source = episode.graph_target
    destination = binding.destination if binding else GraphTargetRef()
    destination_history = merge_history(service, destination)
    if (
        task.kind != "branch_merge"
        or task.episode_id != (episode.episode_id if episode else None)
        or (episode is not None and task.project_id != episode.project_id)
        or task.graph_target != source
        or (
            binding is None
            and episode is not None
            and episode.graph_target.kind == "branch"
            and execution.store.branch_owner(task.project_id, episode.graph_target.branch_id or "")
            != episode.episode_id
        )
        or task.authorized_by is None
    ):
        yield _sse(
            AgentEvent(event="error", text="The branch merge task lost its episode binding.")
        )
        return
    if request.provider is None or request.run_on is None:
        yield _sse(
            AgentEvent(event="error", text="The branch merge task has no pinned provider target.")
        )
        return
    machine = service.manifest.machine_map.get(request.run_on)
    if machine is None:
        yield _sse(AgentEvent(event="error", text=f"unknown execution machine: {request.run_on}"))
        return

    local_stage: Path | None = None
    remote_stage: RemoteRunStage | None = None
    staged_validator = None
    validator: WorkValidatorMailboxLifecycle | None = None
    completed = False
    try:
        stage_id = f"{task.operation_id}-attempt-{task.attempt}" if binding else task.operation_id
        if recorded is not None:
            if execution.stage_root is None:
                raise ValueError("The recorded branch merge has no retained stage.")
            remote = execution.stage_host or ""
            local_stage, remote_stage, workspace = attach_retained_stage(
                execution,
                owner="branch merge",
                stage_host=remote,
                stage_root=execution.stage_root,
                workspace=(
                    str(RemoteRunStage(remote).attach(execution.stage_root).workspace)
                    if remote
                    else str(Path(execution.stage_root) / "workspace")
                ),
            )
            stage_root = execution.stage_root
        elif machine.host:
            remote_stage = RemoteRunStage(machine.host).open(
                stage_id,
                protected_roots=_protected_run_stage_roots(execution.store, machine.host),
            )
            assert remote_stage.root is not None
            execution.checkpoint_stage(machine.host, str(remote_stage.root))
            workspace = Path(str(remote_stage.workspace))
            stage_root = str(remote_stage.root)
        else:
            parent = _swept_stage_root(data_dir, store=execution.store)
            local_stage = parent / _safe_stage_name(stage_id)
            local_stage.mkdir(mode=0o700, parents=True, exist_ok=False)
            workspace = local_stage / "workspace"
            workspace.mkdir(mode=0o700)
            execution.checkpoint_stage("", str(local_stage))
            stage_root = str(local_stage)
        stage = BranchMergeStage(
            local_stage=local_stage,
            remote_stage=remote_stage,
            workspace=workspace,
        )
        # With code to merge, the task gets chat Integrate's local-merge scope for the
        # episode's one worktree; otherwise it admits no repository.
        code = None
        if binding is None:
            assert episode is not None
            code = episode_code_merge(service, execution.store, episode)
        write_scope = resolve_project_write_scope(
            manifest=service.manifest,
            project_id=task.project_id,
            execution_machine=request.run_on,
            capability="orchestrate",
            stage_root=stage_root,
            workspace_root=str(workspace),
            admitted_aliases=[code.binding.repository_alias] if code else [],
            repository_pointers=[code.pointer] if code else [],
            remote_stage=remote_stage,
            app_data_dir=data_dir,
            repository_inventory=service.repository_ownership_inventory(project_id=task.project_id),
            conversation_worktree=code.binding if code else None,
            include_shared_checkout=code is not None,
            machine_writable_paths=code.machine_writable_paths if code else None,
        )
        if recorded is None:
            retained = write_scope.model_dump_json()
            execution.store.record_agent_task_contract(
                task.operation_id,
                BRANCH_MERGE_FINALIZATION_ROLE,
                retained,
                hashlib.sha256(retained.encode("utf-8")).hexdigest(),
            )
        else:
            frames, admitted = _admit_recorded_turn(execution, recorded, workspace)
            for frame in frames:
                yield frame
            if not admitted:
                return
        # A merge orchestrator never carries a provider session into its turn.
        execution.bind_write_scope(write_scope)
        # Every merge turn hides the same selected secrets as other orchestration.
        hidden_read_scope = await _prepare_hidden_read_scope(
            service,
            request,
            workspace=workspace,
            remote_stage=remote_stage,
            execution=execution,
            capability="orchestrate",
            data_dir=data_dir,
            local_stage=local_stage,
        )

        code_pending = False

        async def code_turn() -> AsyncIterator[str]:
            # The agent lands the code in its own turn; RCP verifies before anything else.
            nonlocal code_pending
            assert code is not None
            if recorded is not None:
                # A recorded code turn is verified where the merge commits: before_commit.
                return
            async with aclosing(
                stream_code_merge_run(
                    request,
                    launcher,
                    stage=stage,
                    write_scope=write_scope,
                    code_block=code.prompt_section(write_scope),
                    verify=lambda: verify_episode_merge_code(execution.store, episode),
                    execution=execution,
                    binary=(
                        service.launcher.provider_binary(request.provider, machine.host)
                        if request.provider
                        else None
                    ),
                    hidden_read_scope=hidden_read_scope,
                )
            ) as stream:
                async for frame in stream:
                    code_pending = code_pending or _event(frame).event == "remote_result_pending"
                    if _event(frame).event != "done":
                        yield frame

        if source.kind == "main":
            # A code-only owner has no graph side: one code turn completes the merge.
            if code is None:
                raise ValueError("The code merge task has no code to merge.")
            async for frame in code_turn():
                yield frame
            if code_pending:
                return
            if recorded is not None:
                verify_episode_merge_code(execution.store, episode)
            complete_graph_merge(service, execution.store, episode)
            completed = True
            yield _sse(AgentEvent(event="done"))
            return

        branch = merge_history(service, source)
        assert isinstance(branch, BranchHistoryManager)

        def load_context() -> BranchMergeContext:
            context = load_branch_merge_context(
                service,
                execution.store,
                episode,
                operation_id=task.operation_id,
                authorized_by=task.authorized_by,
                merge_binding=binding,
            )
            if context.run_truth_scope != request.run_truth_scope:
                raise ValueError("Project truth membership changed after merge dispatch.")
            return context

        initial_context = load_context()
        merge_id = branch_merge_id(initial_context.metadata)
        existing_receipt = branch.reconcile_merge_receipt(merge_id)
        if existing_receipt is not None and code is None:
            _apply_receipt_to_execution(service, execution, existing_receipt)
            execution.store.record_agent_task_receipt(
                task.operation_id,
                "branch_merge_reconciled",
                existing_receipt.model_dump(mode="json"),
            )
            # A restart after a crash between the receipt and the episode's own record.
            state = (
                execution.store.episode_isolation_state(task.project_id, episode.episode_id)
                if episode is not None and binding is None
                else None
            )
            if (
                binding is None
                and state
                and state.merge_attempt
                and state.merge_attempt.graph_task_id == task.operation_id
            ):
                complete_graph_merge(service, execution.store, episode)
            completed = True
            yield _sse(
                AgentEvent(
                    event="answer",
                    text=(
                        f"This branch head was already merged into {destination.key} revision "
                        f"{existing_receipt.result_main_head.revision}."
                    ),
                )
            )
            if existing_receipt.outcome == "committed":
                yield _applied_revision_event(existing_receipt.result_main_head.revision)
            yield _sse(AgentEvent(event="done"))
            return

        plan = merge_plan.plan_merge(
            merge_planning_context(
                initial_context,
                destination_history,
                created_at=datetime.fromisoformat(execution.store.now()),
            )
        )
        code_block = code.prompt_section(write_scope) if code else ""
        code_landed = False
        if code is not None and not plan.conflicts and not plan.review_groups:
            # Only the code needs judgment, so the graph then merges with no provider
            # turn, as an agentless merge does.
            async for frame in code_turn():
                yield frame
            if code_pending:
                return
            code_block = code.landed_section(write_scope)
            code_landed = True

        recorded_promotions: tuple[NodeGroupPromotion, ...] = ()
        if recorded is not None and (turn_plan := recorded_turn_plan(execution, stage)):
            # The answers left on the stage belong to the plan that turn was given.
            plan, recorded_promotions = turn_plan
        graph_read_scope = GraphReadScope.resolve(
            task.project_id, destination_history, store=service.paper.store
        )
        staged_validator = stage_patch_validation_mailbox(
            local_stage=workspace if remote_stage is None else None,
            remote_stage=remote_stage,
            task_id=task.operation_id,
            turn_id=f"{task.operation_id}:branch-merge",
            timeout_seconds=PATCH_SELF_CHECK_TIMEOUT_SECONDS,
            authority="broker",
        )
        checks = MergeAnswersCheck(
            plan=plan,
            history=destination_history,
            task_id=task.operation_id,
            turn_id=staged_validator.credential.identity.turn_id,
            execution=execution,
        )

        handle_command = _merge_command_handler(checks, service, graph_read_scope)
        turn_number = 0

        async def start_check_turn(current_plan):
            nonlocal staged_validator, validator, turn_number
            assert staged_validator is not None
            if validator is not None:
                await validator.close()
                staged_validator = stage_patch_validation_mailbox(
                    local_stage=workspace if remote_stage is None else None,
                    remote_stage=remote_stage,
                    task_id=task.operation_id,
                    turn_id=f"{task.operation_id}:branch-merge:{turn_number}",
                    timeout_seconds=PATCH_SELF_CHECK_TIMEOUT_SECONDS,
                    authority="broker",
                )
            turn_number += 1
            checks.set_plan(current_plan, turn_id=staged_validator.credential.identity.turn_id)
            validator = start_work_validator_mailbox(
                staged_validator,
                execution=execution,
                budget=None,
                validate=None,
                command_handler=handle_command,
                resume_context={
                    "plan": current_plan.model_dump(mode="json"),
                    "turn_id": staged_validator.credential.identity.turn_id,
                },
            )
            return (
                staged_validator.client_command(
                    "merge-answers", "check", str(workspace / "answers.json")
                ),
                staged_validator.client_command(),
                staged_validator.invocation_gate,
            )

        def verify_delivery() -> None:
            if binding:
                execution.store.agent_task_authority(task.project_id, task.operation_id)
            else:
                verify_episode_merge_code(execution.store, episode)

        outcome = BranchMergeRunOutcome()
        held_done: str | None = None
        try:
            async with aclosing(
                stream_branch_merge_run(
                    request,
                    launcher,
                    load_context=load_context,
                    before_commit=verify_delivery,
                    main_history=destination_history,
                    stage=stage,
                    write_scope=write_scope,
                    checks=checks,
                    start_check_turn=start_check_turn,
                    reconcile_receipt=lambda: branch.reconcile_merge_receipt(merge_id),
                    publish_receipt=branch.write_merge_receipt,
                    outcome=outcome,
                    graph_read_scope=graph_read_scope,
                    history_client=staged_validator.client_command(),
                    invocation_gate=staged_validator.invocation_gate,
                    service=service,
                    execution=execution,
                    binary=service.launcher.provider_binary(request.provider, machine.host),
                    code_block=code_block,
                    code_roots=list(write_scope.repository_roots) if code else None,
                    code_landed=code_landed or recorded is not None,
                    hidden_read_scope=hidden_read_scope,
                    recorded_answers=_left_answers(stage) if recorded is not None else None,
                    recorded_promotions=recorded_promotions,
                )
            ) as stream:
                async for frame in stream:
                    event = _event(frame)
                    if event.event == "done":
                        held_done = frame
                    else:
                        yield frame
        except BaseException as exc:
            if validator is not None:
                await validator.close(primary_error=exc)
            raise
        if validator is not None:
            if outcome.status == "awaiting_remote_result":
                # The host's turn may still check its answers while RCP is away.
                validator.detach()
            else:
                await validator.close()

        if outcome.receipt is None:
            if outcome.status == "awaiting_remote_result":
                return
            if outcome.status in {"rejected", "retryable"} and outcome.diagnostic:
                yield _sse(AgentEvent(event="error", text=outcome.diagnostic))
            return
        receipt = outcome.receipt
        _apply_receipt_to_execution(service, execution, receipt)
        execution.store.record_agent_task_receipt(
            task.operation_id,
            "branch_merge_outcome",
            receipt.model_dump(mode="json"),
        )
        if binding is None:
            complete_graph_merge(service, execution.store, episode)
        commit_prepared_graph_observation(
            execution,
            service,
            native_session_id=outcome.native_session_id,
            applied_revision=execution.applied_revision,
        )
        completed = True
        if receipt.outcome == "committed":
            yield _applied_revision_event(receipt.result_main_head.revision)
        yield held_done or _sse(AgentEvent(event="done"))
    except (OSError, StateUnavailable, ValueError) as exc:
        # A host that went away is an outage, not a verdict on a recorded turn.
        note_stage_unreachable(execution, exc)
        yield _sse(AgentEvent(event="error", text=str(exc)))
    finally:
        if (
            staged_validator is not None
            and not staged_validator.credential.expired
            and not (validator is not None and validator.detached)
        ):
            await asyncio.to_thread(
                cleanup_patch_validation_mailbox,
                staged=staged_validator,
                execution=execution,
            )
        if completed:
            if local_stage is not None:
                with suppress(OSError, ValueError):
                    _remove_local_tree(local_stage, local_stage.parent)
            if remote_stage is not None:
                remote_stage.close()
            execution.store.clear_agent_task_stage(task.operation_id)


BRANCH_MERGE_FINALIZATION_ROLE = "branch_merge_finalization"


def _merge_command_handler(
    checks: MergeAnswersCheck, service: ProjectService, graph_read_scope: GraphReadScope
):
    def handle_command(command, identity):
        if isinstance(command, MergeAnswersCommandRequest):
            return checks(command, identity)
        return handle_graph_history_command(service, graph_read_scope, command)

    return handle_command


def resume_branch_merge_command_mailbox(
    service: Callable[[], ProjectService], execution: AgentTaskExecution
) -> WorkValidatorMailboxLifecycle | None:
    """Serve a detached merge turn's answer checks and graph reads after a relaunch."""

    saved = load_work_mailbox_context(execution)
    if saved is None:
        return None
    task = execution.store.agent_task(execution.operation_id)
    if task is None:
        raise ValueError("The detached branch merge lost its task.")
    request = load_stored_request(
        BranchMergeRunRequest, task.request, operation_id=task.operation_id
    )
    destination = request.merge_binding.destination if request.merge_binding else GraphTargetRef()
    project = service()
    history = merge_history(project, destination)
    checks = MergeAnswersCheck(
        plan=MergePlan.model_validate(saved["plan"]),
        history=history,
        task_id=task.operation_id,
        turn_id=str(saved["turn_id"]),
        execution=execution,
    )
    return restore_work_validator_mailbox(
        execution,
        validate=None,
        command_handler=_merge_command_handler(
            checks,
            project,
            GraphReadScope.resolve(task.project_id, history, store=project.paper.store),
        ),
    )


def _admit_recorded_turn(
    execution: AgentTaskExecution, recorded: RecordedProviderTurn, workspace: Path
) -> tuple[list[str], bool]:
    """Hold a recorded merge turn to what its live stream would have accepted.

    Its answers are admitted only from a turn that completed on the session its
    launch pinned, decoded under the write scope that launch enforced.
    """

    retained = execution.store.agent_task_contract(
        execution.operation_id, BRANCH_MERGE_FINALIZATION_ROLE
    )
    if retained is None:
        raise ValueError("The branch merge finalization context is unavailable.")
    verdict = decode_recorded_turn(
        recorded,
        provider_turn_request(
            workspace,
            recorded,
            capability="orchestrate",
            write_scope=ProjectWriteScope.model_validate_json(retained),
        ),
    )
    provider = _ProviderOutcome()
    refused = refuse_recorded_session_mismatch(
        execution,
        provider,
        verdict,
        execution.store.agent_task_contract(execution.operation_id, WORK_CORRECTION_SESSION_ROLE),
    )
    if refused:
        return refused, False
    frames = absorb_recorded_events(provider, verdict)
    if not provider.completed and not provider.failed:
        frames.append(
            _sse(AgentEvent(event="error", text="The recorded merge turn did not complete."))
        )
    return frames, provider.completed


def _left_answers(stage: BranchMergeStage) -> MergeAnswers:
    """The answers a recorded turn left on its stage, or none for a code-only turn."""

    try:
        raw = _answers_mailbox(stage).read_text(
            "answers.json", max_bytes=PATCH_SELF_CHECK_MAX_REQUEST_BYTES
        )
    except FileNotFoundError:
        return MergeAnswers(answers={}, cards={})
    return MergeAnswers.model_validate_json(raw)


async def finalize_recorded_branch_merge_result(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    recorded: RecordedProviderTurn,
) -> AsyncGenerator[str, None]:
    """Settle a merge turn its host recorded from what that turn left on its stage."""

    task = execution.store.agent_task(execution.operation_id)
    if task is None:
        raise ValueError("The recorded branch merge lost its task.")
    episode = execution.store.episode(task.episode_id) if task.episode_id else None
    async with aclosing(
        stream_branch_merge_task(
            service,
            launcher,
            request,
            data_dir,
            episode=episode,
            task=task,
            execution=execution,
            recorded=recorded,
        )
    ) as stream:
        async for frame in stream:
            yield frame


def _apply_receipt_to_execution(
    service: ProjectService,
    execution: AgentTaskExecution,
    receipt: BranchMergeReceipt,
) -> None:
    if receipt.outcome != "committed":
        return
    result = merge_history(service, receipt.result_main_head.target).current_materialization()
    if result.state.revision < receipt.result_main_head.revision:
        raise StateUnavailable("The committed branch merge main state is not yet coherent.")
    matching = [
        patch for patch in result.patches if patch.revision == receipt.result_main_head.revision
    ]
    if len(matching) != 1:
        raise StateUnavailable("The committed branch merge revision is not uniquely readable.")
    patch = matching[0]
    if (
        patch.admission != "accepted"
        or patch.branch_merge != receipt.provenance
        or patch.transition is None
        or patch.transition.transition_id != receipt.result_main_head.transition_id
        or patch.authorized_by != receipt.authorized_by
    ):
        raise StateUnavailable("The committed branch merge receipt disagrees with main history.")
    execution.applied_revision = receipt.result_main_head.revision
    execution.applied_graph_state = result.state


def _applied_revision_event(revision: int) -> str:
    return _sse(
        AgentEvent(
            event="message",
            text=json.dumps({"applied_revision": revision}, separators=(",", ":")),
        )
    )


def _event(frame: str) -> AgentEvent:
    return AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
