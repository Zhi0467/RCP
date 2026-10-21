from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from contextlib import aclosing, suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.agents import AgentEvent, AgentLauncher
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import MergeAnswersArguments, MergeAnswersCommandRequest
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.agents.write_scope import resolve_project_write_scope
from rcp.core.models import BranchMergeReceipt
from rcp.core.transition_models import GraphTargetRef
from rcp.history import RevisionConflict
from rcp.history.branches import BranchHistoryManager
from rcp.limits import PATCH_SELF_CHECK_MAX_REQUEST_BYTES, PATCH_SELF_CHECK_TIMEOUT_SECONDS
from rcp.runs import merge_contract, merge_plan
from rcp.runs.branch_merge import (
    MERGE_FINALIZATION_PASS_PREFIX,
    BranchMergeContext,
    BranchMergeRunOutcome,
    BranchMergeStage,
    MergeFinalizationPass,
    branch_merge_id,
    classify_refreshed_context,
    merge_planning_context,
    settle_branch_merge,
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
from rcp.runs.patch_validator import (
    MergeAnswersCheck,
    cleanup_patch_validation_mailbox,
    serve_patch_validation_mailbox,
    stage_patch_validation_mailbox,
)
from rcp.runs.recorded_settlement import (
    attach_retained_stage,
    note_stage_unreachable,
    open_recorded_pass,
)
from rcp.runs.recorded_turn import RecordedProviderTurn
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
from rcp.runs.tasks.work_turn_runtime import (
    WorkValidatorMailboxLifecycle,
    restore_work_validator_mailbox,
    start_work_validator_mailbox,
)
from rcp.service import ProjectService, RunRequest
from rcp.storage import AgentTaskRecord, EpisodeRecord
from rcp.transport import RemoteRunStage, RunStageMailbox, StateUnavailable

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution


BRANCH_MERGE_FINALIZATION_ROLE = "branch_merge_finalization_context"


def _retained_merge_pass(execution: AgentTaskExecution) -> MergeFinalizationPass:
    passes = [
        contract
        for contract in execution.store.agent_task_contracts(execution.operation_id)
        if contract.role.startswith(MERGE_FINALIZATION_PASS_PREFIX)
    ]
    if not passes:
        raise ValueError("The merge turn has no retained provider pass.")
    # An unresolved pass prevents any subsequent launch on this operation.
    return MergeFinalizationPass.model_validate_json(passes[-1].content)


def resume_branch_merge_command_mailbox(
    service: Callable[[], ProjectService], execution: AgentTaskExecution
) -> WorkValidatorMailboxLifecycle | None:
    retained = _retained_merge_pass(execution)
    if retained.plan is None or retained.turn_id is None:
        return None
    plan = retained.plan
    checks = MergeAnswersCheck(
        plan=plan,
        history=merge_history(service(), plan.destination_head.target),
        task_id=execution.operation_id,
        turn_id=retained.turn_id,
        execution=execution,
    )
    if retained.context is None:
        raise ValueError("The merge mailbox lost its graph context.")
    scope = GraphReadScope.resolve(retained.context.metadata.project_id, checks.history)

    def handle(command, identity):
        if isinstance(command, MergeAnswersCommandRequest):
            return checks(command, identity)
        return handle_graph_history_command(service(), scope, command)

    return restore_work_validator_mailbox(execution, validate=None, command_handler=handle)


async def finalize_recorded_branch_merge_result(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    recorded: RecordedProviderTurn,
) -> AsyncIterator[str]:
    """Settle the retained merge plan without preparing or launching another turn."""
    del launcher, data_dir
    content = execution.store.agent_task_contract(
        execution.operation_id, BRANCH_MERGE_FINALIZATION_ROLE
    )
    if content is None:
        raise ValueError("The merge turn has no retained finalization context.")
    retained = _retained_merge_pass(execution)
    task = execution.store.agent_task(execution.operation_id)
    if (
        task is None
        or task.kind != "branch_merge"
        or task.authorized_by is None
        or task.request != request.model_dump(mode="json")
        or json.loads(content) != task.request
        or retained.write_scope.fingerprint != execution.write_scope_fingerprint
        or retained.write_scope.workspace_root != retained.workspace
    ):
        raise ValueError("The retained merge request or write scope changed after launch.")
    authorized_by = task.authorized_by
    binding = request.merge_binding if isinstance(request, BranchMergeRunRequest) else None
    episode = execution.store.episode(task.episode_id) if task.episode_id else None
    if binding is None and (episode is None or episode.graph_target != task.graph_target):
        raise ValueError("The recorded merge task lost its episode binding.")
    try:
        local, remote, workspace = attach_retained_stage(
            execution,
            owner="branch merge",
            stage_host=retained.stage_host,
            stage_root=retained.stage_root,
            workspace=retained.workspace,
        )
        stage = BranchMergeStage(local_stage=local, remote_stage=remote, workspace=workspace)
        provider = _ProviderOutcome(session_id=retained.session_id)
        for frame in open_recorded_pass(
            recorded,
            execution=execution,
            workspace=workspace,
            remote_stage=remote,
            outcome=provider,
            capability="orchestrate",
            write_scope=retained.write_scope,
            required_session_id=retained.session_id,
        ):
            if _event(frame).event != "done":
                yield frame
        if provider.failed or provider.paused or not provider.completed:
            raise ValueError("The recorded merge provider did not complete its turn.")

        def verify_delivery() -> None:
            if binding:
                execution.store.agent_task_authority(task.project_id, task.operation_id)
            else:
                verify_episode_merge_code(execution.store, episode)

        if retained.context is None:
            if task.graph_target.kind != "main" or retained.plan is not None:
                raise ValueError("The recorded code merge lost its graph binding.")
            verify_delivery()
            complete_graph_merge(service, execution.store, episode)
            _cleanup_merge_stage(stage, execution)
            yield _sse(AgentEvent(event="done"))
            return

        context, plan = retained.context, retained.plan
        if plan is None or plan.context_id != context.context_id:
            raise ValueError("The recorded merge plan lost its exact context.")
        history = merge_history(service, plan.destination_head.target)
        branch = merge_history(service, task.graph_target)
        assert isinstance(branch, BranchHistoryManager)

        def load_context() -> BranchMergeContext:
            fresh = load_branch_merge_context(
                service,
                execution.store,
                episode,
                operation_id=task.operation_id,
                authorized_by=authorized_by,
                merge_binding=binding,
            )
            if fresh.run_truth_scope != request.run_truth_scope:
                raise ValueError("Project truth membership changed after merge dispatch.")
            return fresh

        outcome = BranchMergeRunOutcome(native_session_id=provider.session_id)
        existing = branch.reconcile_merge_receipt(branch_merge_id(context.metadata))
        if existing is not None:
            outcome.receipt = existing
        else:
            if classify_refreshed_context(context, load_context()) != "unchanged":
                raise RevisionConflict("The destination moved; retry the merge task.")
            answers = merge_contract.MergeAnswers(answers={}, cards={})
            if retained.turn_id is not None:
                raw = RunStageMailbox.for_stage(
                    local_stage=workspace if remote is None else None, remote_stage=remote
                ).read_text("answers.json", max_bytes=PATCH_SELF_CHECK_MAX_REQUEST_BYTES)
                checks = MergeAnswersCheck(
                    plan=plan,
                    history=history,
                    task_id=task.operation_id,
                    turn_id=retained.turn_id,
                    execution=execution,
                )
                # Re-run the same check against these exact recorded bytes. A new
                # checker with no successful receipt can never admit an answer.
                checks(
                    MergeAnswersCommandRequest(
                        request_id=hashlib.sha256(raw.encode()).hexdigest()[:32],
                        mailbox_id="0" * 32,
                        credential="0" * 64,
                        verb="merge_answers",
                        arguments=MergeAnswersArguments(action="check", answers=raw),
                    ),
                    CommandTurnIdentity(
                        episode_id=None,
                        task_id=task.operation_id,
                        turn_id=retained.turn_id,
                        authority="broker",
                    ),
                )
                if checks.destination_moved:
                    raise RevisionConflict("The destination moved; retry the merge task.")
                checks.require_receipt(raw.encode("utf-8"))
                answers = merge_contract.MergeAnswers.model_validate_json(raw)
            settle_branch_merge(
                context=context,
                plan=plan,
                answers=answers,
                main_history=history,
                load_context=load_context,
                outcome=outcome,
                before_commit=verify_delivery,
                reconcile_receipt=lambda: branch.reconcile_merge_receipt(
                    branch_merge_id(context.metadata)
                ),
                publish_receipt=branch.write_merge_receipt,
            )
        for frame in _finish_branch_merge_task(service, execution, task, episode, request, outcome):
            yield frame
        _cleanup_merge_stage(stage, execution)
    except (RevisionConflict, merge_contract.MergeHiddenClash) as exc:
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "branch_merge_retryable",
            {"reason": str(exc), "retry_required": True},
        )
        yield _sse(AgentEvent(event="error", text=f"{exc} Retry the merge task."))
    except (OSError, StateUnavailable, ValueError) as exc:
        note_stage_unreachable(execution, exc)
        yield _sse(AgentEvent(event="error", text=str(exc)))


async def stream_branch_merge_task(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    *,
    episode: EpisodeRecord | None,
    task: AgentTaskRecord,
    execution: AgentTaskExecution,
) -> AsyncGenerator[str, None]:
    """Run one authorized branch merge through its contained scratch stage."""

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
    stage: BranchMergeStage | None = None
    completed = False
    remote_result_pending = False
    try:
        stage_id = f"{task.operation_id}-attempt-{task.attempt}" if binding else task.operation_id
        if machine.host:
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
        # A merge orchestrator never carries a provider session into its turn.
        execution.bind_write_scope(write_scope, resumes_native_session=False)
        if remote_stage is not None:
            snapshot = request.model_dump_json()
            execution.store.record_agent_task_contract(
                execution.operation_id,
                BRANCH_MERGE_FINALIZATION_ROLE,
                snapshot,
                hashlib.sha256(snapshot.encode()).hexdigest(),
            )
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

        code_outcome = _ProviderOutcome()

        async def code_turn(
            context: BranchMergeContext | None = None,
            plan: merge_contract.MergePlan | None = None,
        ) -> AsyncIterator[str]:
            # The agent lands the code in its own turn; RCP verifies before anything else.
            assert code is not None
            async with aclosing(
                stream_code_merge_run(
                    request,
                    launcher,
                    stage=stage,
                    write_scope=write_scope,
                    code_block=code.prompt_section(write_scope),
                    verify=lambda: verify_episode_merge_code(execution.store, episode),
                    execution=execution,
                    binary=machine.provider_paths.get(request.provider),
                    hidden_read_scope=hidden_read_scope,
                    outcome=code_outcome,
                    context=context,
                    plan=plan,
                )
            ) as stream:
                async for frame in stream:
                    if _event(frame).event != "done":
                        yield frame

        if source.kind == "main":
            # A code-only owner has no graph side: one code turn completes the merge.
            if code is None:
                raise ValueError("The code merge task has no code to merge.")
            async for frame in code_turn():
                yield frame
            if code_outcome.remote_result_pending:
                remote_result_pending = True
                return
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
            async for frame in code_turn(initial_context, plan):
                yield frame
            if code_outcome.remote_result_pending:
                remote_result_pending = True
                return
            code_block = code.landed_section(write_scope)
            code_landed = True

        graph_read_scope = GraphReadScope.resolve(task.project_id, destination_history)
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

        def handle_command(command, identity):
            if isinstance(command, MergeAnswersCommandRequest):
                return checks(command, identity)
            return handle_graph_history_command(service, graph_read_scope, command)

        validator_lifecycle: WorkValidatorMailboxLifecycle | None = None
        turn_number = 0

        async def start_check_turn(current_plan):
            nonlocal staged_validator, validator_lifecycle, turn_number
            assert staged_validator is not None
            if validator_lifecycle is not None:
                await validator_lifecycle.close()
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
            validator_lifecycle = start_work_validator_mailbox(
                staged_validator,
                execution=execution,
                validate=None,
                budget=None,
                command_handler=handle_command,
                serve=serve_patch_validation_mailbox,
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
                    binary=machine.provider_paths.get(request.provider),
                    code_block=code_block,
                    code_roots=list(write_scope.repository_roots) if code else None,
                    code_landed=code_landed,
                    hidden_read_scope=hidden_read_scope,
                )
            ) as stream:
                async for frame in stream:
                    event = _event(frame)
                    if event.event != "done":
                        yield frame
        finally:
            remote_result_pending = outcome.remote_result_pending
            if validator_lifecycle is not None:
                if remote_result_pending:
                    validator_lifecycle.detach()
                else:
                    await validator_lifecycle.close()

        if remote_result_pending:
            return

        if outcome.receipt is None:
            if outcome.status in {"rejected", "retryable"} and outcome.diagnostic:
                yield _sse(AgentEvent(event="error", text=outcome.diagnostic))
            return
        for frame in _finish_branch_merge_task(service, execution, task, episode, request, outcome):
            yield frame
        completed = True
    except (OSError, StateUnavailable, ValueError) as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
    finally:
        if (
            not remote_result_pending
            and staged_validator is not None
            and not staged_validator.credential.expired
        ):
            await asyncio.to_thread(
                cleanup_patch_validation_mailbox,
                staged=staged_validator,
                execution=execution,
            )
        if completed and stage is not None:
            _cleanup_merge_stage(stage, execution)


def _cleanup_merge_stage(stage: BranchMergeStage, execution: AgentTaskExecution) -> None:
    if stage.local_stage is not None:
        with suppress(OSError, ValueError):
            _remove_local_tree(stage.local_stage, stage.local_stage.parent)
    if stage.remote_stage is not None:
        stage.remote_stage.close()
    execution.store.clear_agent_task_stage(execution.operation_id)


def _finish_branch_merge_task(
    service: ProjectService,
    execution: AgentTaskExecution,
    task: AgentTaskRecord,
    episode: EpisodeRecord | None,
    request: RunRequest,
    outcome: BranchMergeRunOutcome,
) -> Iterator[str]:
    binding = request.merge_binding if isinstance(request, BranchMergeRunRequest) else None
    assert outcome.receipt is not None
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
    if receipt.outcome == "committed":
        yield _applied_revision_event(receipt.result_main_head.revision)
    yield _sse(AgentEvent(event="done"))


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
