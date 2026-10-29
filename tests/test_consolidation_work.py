from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from rcp.agents import AgentProcessControl
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import (
    ApplyArguments,
    ApplyCommandRequest,
    FlagArguments,
    FlagCommandRequest,
)
from rcp.background import AgentTaskExecution
from rcp.core.authority import AgentAuthorizerDeparted
from rcp.core.models import Patch
from rcp.core.transition_models import GraphTargetRef
from rcp.runs.consolidation import consolidation_apply_handler, settlement_source_effect_id
from rcp.runs.questions import work_command_handler
from rcp.runs.tasks.work import _apply_work_patch, stream_work_run
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord
from rcp.transport.workspace_mailbox import RunStageMailbox
from tests.helpers import signed_in_client

from .helpers import (
    agent_patch_json,
    append_fixture_patch,
    create_named_app,
    refresh_patch,
    seed_patch,
    wait_for_task,
)
from .test_api import ScriptedLauncher, _named_test_authorizer


def _admit(app, *, chat_id=None, target=None):
    target = target or GraphTargetRef()
    store = app.state.background_tasks.store
    now = datetime.fromisoformat(store.now())
    project_id = app.state.default_project_id
    authorizer = _named_test_authorizer(store)
    schedule = store.put_consolidation_schedule(
        project_id,
        local_time="03:00",
        timezone="UTC",
        authorized_by=authorizer,
        next_due_at=(now - timedelta(minutes=1)).isoformat(),
    )
    profile = app.state.service.resolve_agent_profile("project_chat")
    request = RunRequest(
        provider=profile.provider,
        model=profile.model,
        reasoning=profile.reasoning,
        run_on=profile.run_on,
        chat_scope="project",
        chat_id=chat_id or str(uuid.uuid4()),
        mode="work",
        trigger="schedule",
        message="Nightly consolidation",
        run_truth_scope=["repo-a"],
    )
    task = AgentTaskRecord(
        operation_id=str(uuid.uuid4()),
        project_id=project_id,
        kind="project_chat",
        graph_target=target,
        status="queued",
        status_message="queued",
        request=request.model_dump(mode="json"),
        authorized_by=authorizer,
        dispatch_authority=resolve_dispatch_authority("project_chat", request),
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    store.ensure_consolidation_target(schedule, target=target.branch_id or "main")
    run = store.claim_consolidation_occurrence(
        schedule,
        occurrence_date=now.date().isoformat(),
        next_due_at=(now + timedelta(days=1)).isoformat(),
        input_head=app.state.service.for_graph_target(target).history.state().revision,
        target=target.branch_id or "main",
        task=task,
    )
    assert run is not None
    execution = AgentTaskExecution(task.operation_id, store, AgentProcessControl())
    return request, execution


def _apply_request(key):
    return ApplyCommandRequest(
        mailbox_id="a" * 32,
        request_id=uuid.uuid4().hex,
        credential="b" * 64,
        verb="apply",
        idempotency_key=key,
        arguments=ApplyArguments(patch_file="patch.json"),
    )


def _command(app, execution, tmp_path):
    mailbox = RunStageMailbox(tmp_path)
    handler = consolidation_apply_handler(lambda: app.state.service, execution, mailbox, ["repo-a"])
    identity = CommandTurnIdentity(None, execution.operation_id, "turn", "broker")
    return mailbox, lambda key: handler(_apply_request(key), identity)


def test_consolidation_apply_keys_replay_and_settlement_matches_each_digest(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    _, execution = _admit(app)
    initial_revision = app.state.service.history.state().revision
    mailbox, apply = _command(app, execution, tmp_path)
    texts = [agent_patch_json(refresh_patch(f"rq/consolidated-{i}")) for i in range(3)]
    for index, text in enumerate(texts[:2]):
        mailbox.write_text("patch.json", text)
        response = apply(str(index))
        assert response.status == "ok"
        assert response.result["revision"] == initial_revision + index + 1
        assert not (tmp_path / "patch.json").exists()
        assert apply(str(index)).result == response.result
    mailbox.write_text("patch.json", texts[2])
    assert apply("0").status == "invalid"
    assert service.history.state().revision == initial_revision + 2
    for text in texts:
        result, failure = _apply_work_patch(
            service,
            execution,
            text,
            run_truth_scope=["repo-a"],
            read_scope=None,
            source_effect_id=settlement_source_effect_id(execution, text),
        )
        assert failure is None
        assert result.status == "applied"
    assert service.history.state().revision == initial_revision + 3


def test_consolidation_apply_holds_added_evidence_for_review(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    _, execution = _admit(app)
    mailbox, apply = _command(app, execution, tmp_path)
    evidence = {
        "id": "ev/added-overnight",
        "type": "evidence",
        "title": "Seed 3 shows no spike",
        "observation": "The seed 3 log has no step-200 spike.",
        "origin": "analytic",
    }
    patch = Patch(
        kind="work",
        author="agent",
        summary="Record the seed 3 result",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[{"op": "create_nodes", "nodes": [evidence]}],
    )
    mailbox.write_text("patch.json", agent_patch_json(patch))
    assert apply("add").status == "ok"
    # A process lost before the hold, and another writer's Evidence landing meanwhile.
    with execution.store.connection() as connection:
        connection.execute("DELETE FROM evidence_flags")
        connection.execute("DELETE FROM evidence_flag_command_receipts")
    other = {**evidence, "id": "ev/other-writer", "title": "Another writer's result"}
    append_fixture_patch(
        app.state.service,
        Patch(
            kind="seed",
            author="agent",
            summary="Another writer",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[{"op": "create_nodes", "nodes": [other]}],
        ),
    )
    assert apply("add").status == "ok"
    flags = execution.store.list_evidence_flags(app.state.default_project_id)
    assert [(item["evidence_id"], item["kind"], item["finding"]) for item in flags] == [
        ("ev/added-overnight", "consolidation_added", "Record the seed 3 result")
    ]


def test_consolidation_apply_recovers_crash_after_canonical_append(manifest, tmp_path, monkeypatch):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    _, execution = _admit(app)
    initial_revision = app.state.service.history.state().revision
    mailbox, apply = _command(app, execution, tmp_path)
    text = agent_patch_json(refresh_patch())
    mailbox.write_text("patch.json", text)
    with monkeypatch.context() as patch:
        patch.setattr(
            execution.store,
            "finish_consolidation_apply",
            lambda *_: (_ for _ in ()).throw(OSError("result unavailable")),
        )
        assert apply("commit").status == "unavailable"
    assert app.state.service.history.state().revision == initial_revision + 1
    assert (tmp_path / "patch.json").exists()
    response = apply("retry-new-key")
    assert response.status == "ok"
    assert response.result["revision"] == initial_revision + 1
    assert app.state.service.history.state().revision == initial_revision + 1
    receipts = execution.store.list_consolidation_apply_receipts(execution.operation_id)
    assert len(receipts) == 2
    assert receipts[1]["result"]
    assert receipts[0]["source_effect_id"] == receipts[1]["source_effect_id"]
    assert settlement_source_effect_id(execution, text) == receipts[1]["source_effect_id"]


@pytest.mark.parametrize("departure", ["membership", "removal_started_at", "removed_at"])
def test_consolidation_apply_live_membership_is_required_by_transition(
    manifest, tmp_path, departure
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    _, execution = _admit(app)
    task = execution.store.agent_task(execution.operation_id)
    initial_revision = app.state.service.history.state().revision
    with execution.store.connection() as conn:
        if departure == "membership":
            conn.execute(
                "DELETE FROM project_members WHERE project_id=? AND user_id=?",
                (task.project_id, task.authorized_by.user_id),
            )
        else:
            conn.execute(
                "UPDATE space_users SET removal_started_at=?, removed_at=? WHERE user_id=?",
                (
                    execution.store.now(),
                    execution.store.now() if departure == "removed_at" else None,
                    task.authorized_by.user_id,
                ),
            )
    mailbox, apply = _command(app, execution, tmp_path)
    mailbox.write_text("patch.json", agent_patch_json(refresh_patch()))
    result = apply("departed")
    assert result.status == "invalid"
    applied, failure = _apply_work_patch(
        app.state.service,
        execution,
        agent_patch_json(refresh_patch()),
        run_truth_scope=["repo-a"],
        read_scope=None,
        source_effect_id=str(uuid.uuid4()),
    )
    assert applied is None
    assert failure is not None
    assert result.message == "consolidation_authorizer_departed"
    assert failure.code == AgentAuthorizerDeparted.code
    assert app.state.service.history.state().revision == initial_revision
    assert (tmp_path / "patch.json").exists()


@pytest.mark.parametrize("change", ["delete", "renew", "expire", "leave", "archive"])
def test_consolidation_queued_launch_rechecks_authorization(
    manifest, tmp_path, monkeypatch, change
):
    if change == "archive":
        from .test_branch_chats import _app_branch

        app, _, episode, _ = _app_branch(manifest, tmp_path)
        _, execution = _admit(app, target=episode.graph_target)
        execution.store.set_branch_archived(
            app.state.default_project_id, episode.graph_target.branch_id, archived=True
        )
    else:
        app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
        _, execution = _admit(app)
    store = execution.store
    task = store.agent_task(execution.operation_id)
    if change == "delete":
        store.delete_consolidation_schedule(task.project_id)
    elif change == "renew":
        schedule = store.consolidation_schedule(task.project_id)
        store.put_consolidation_schedule(
            task.project_id,
            local_time="04:00",
            timezone="UTC",
            authorized_by=task.authorized_by,
            next_due_at=schedule.next_due_at,
        )
    elif change == "expire":
        with store.connection() as conn:
            conn.execute(
                "UPDATE consolidation_schedules SET expires_at=?",
                ((datetime.fromisoformat(store.now()) - timedelta(seconds=1)).isoformat(),),
            )
    elif change == "leave":
        with store.connection() as conn:
            conn.execute(
                "DELETE FROM project_members WHERE project_id=? AND user_id=?",
                (task.project_id, task.authorized_by.user_id),
            )
    manager = app.state.background_tasks
    monkeypatch.setattr(
        manager,
        "admit_provider_task",
        lambda *_args, **_kwargs: pytest.fail("revoked launch reached provider"),
    )
    refused = manager.launch_admitted(task.operation_id)
    assert refused.status == "failed"
    assert refused.error.startswith("consolidation_authorization_") or refused.error in {
        "consolidation_authorizer_departed",
        "consolidation_target_unavailable",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ['{"status":"done"}', None])
async def test_consolidation_bound_owner_pins_workflow_and_refuses_watchers(
    manifest, tmp_path, outcome
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    request, execution = _admit(app)
    assert work_command_handler(execution, None).allowed_verbs == {
        "validate",
        "apply",
        "lesson",
        "flag",
        "bulletin",
    }
    launcher = ScriptedLauncher(
        [{"watch.json": "{}"}], message="Consolidation finished.", outcome=outcome
    )
    frames = []
    async for frame in stream_work_run(
        app.state.service, launcher, request, tmp_path / "data", execution=execution
    ):
        frames.append(frame)
    calls = 1 if outcome else 2
    assert launcher.calls == calls
    assert launcher.resumed_sessions == [None] + [launcher.native_session_id] * (calls - 1)
    assert all(item["capability"] == "work_auto" for item in launcher.launch_kwargs)
    payloads = [json.loads(frame.removeprefix("data: ")) for frame in frames]
    assert not any(item["event"] == "error" for item in payloads)
    assert execution.turn_outcome is not None
    assert execution.turn_outcome.status == ("done" if outcome else "not_stated")
    # The task contract invokes the staged workflow, not just the request field.
    contract = Path(re.search(r"(/\S+-initial\.md)", launcher.prompts[0]).group(1))
    invoked = [
        Path(line.rstrip().rstrip("`").rsplit("`", 1)[-1])
        for line in contract.read_text().splitlines()
        if "`graph-consolidation`" in line and line.rstrip().endswith("`")
    ]
    assert invoked and (invoked[0] / "WORKFLOW.md").is_file()
    receipts = execution.store.agent_task_receipts_by_category(
        execution.operation_id, "agent_launch"
    )
    assert receipts[0].payload["workflow_ids"] == ["graph-consolidation"]
    assert any(
        item["id"] == "graph-consolidation" and item["version"]
        for item in receipts[0].payload["resolved_skill_packages"]
    )
    assert execution.store.agent_task_has_receipt(
        execution.operation_id, "watcher_handoff_rejected"
    )
    assert not execution.store.watchers(app.state.default_project_id)


def test_consolidation_empty_apply_replays_without_a_new_revision(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, execution = _admit(app)
    initial_revision = app.state.service.history.state().revision
    mailbox, apply = _command(app, execution, tmp_path)
    mailbox.write_text("patch.json", '{"summary":"Nothing to change","ops":[],"change_summary":[]}')
    first = apply("empty")
    assert first.status == "ok"
    assert first.result["revision"] == initial_revision
    assert apply("empty").result == first.result
    assert app.state.service.history.state().revision == initial_revision


def test_consolidation_continuations_cannot_escape_the_bound_operation(
    manifest, tmp_path, monkeypatch
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, execution = _admit(app)
    manager = app.state.background_tasks
    store = execution.store
    store.mark_agent_task_running(execution.operation_id)
    store.checkpoint_agent_task(execution.operation_id, native_session_id="paused-session")
    store.pause_agent_task(execution.operation_id)
    update = {"status": "unavailable", "repairable": True}
    with store.connection() as conn:
        conn.execute(
            "UPDATE graph_runs SET result_json=? WHERE operation_id=?",
            (
                json.dumps({"graph_update": update, "graph_updates": [update]}),
                execution.operation_id,
            ),
        )
    client = signed_in_client(app)
    response = client.get(
        f"/api/projects/{app.state.default_project_id}/tasks/{execution.operation_id}"
    )
    assert response.status_code == 200
    task = response.json()
    assert not task["can_resume"] and not task["can_retry"] and not task["can_apply_again"]
    assert not task["result"]["graph_update"]["repairable"]
    assert not task["result"]["graph_updates"][0]["repairable"]
    for method in (manager.resume, manager.retry, manager.repair_graph_update):
        with pytest.raises(ValueError, match="consolidation_continuation_forbidden"):
            method(execution.operation_id)
    monkeypatch.setattr(
        manager, "_transport_retry_attempt", lambda *_: pytest.fail("scheduled a retry")
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE graph_runs SET status='failed',failure_kind='transport_lost' WHERE operation_id=?",
            (execution.operation_id,),
        )
    retry_eligible = store.agent_task(execution.operation_id)
    assert retry_eligible.failure_kind == "transport_lost" and retry_eligible.can_retry
    manager._auto_retry_transport_loss(retry_eligible)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_state", ["current", "restored", "different_host"])
async def test_consolidation_admission_launches_only_with_compatible_stage(
    manifest, tmp_path, stage_state
):
    from .test_api import _chat_task_execution

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    request = RunRequest(
        chat_scope="project", chat_id=str(uuid.uuid4()), mode="work", message="Prior chat"
    )
    previous = _chat_task_execution(app, request, "prior-consolidation-chat")
    launcher = ScriptedLauncher([{}], message="Prior turn finished")
    async for _ in stream_work_run(
        app.state.service, launcher, request, tmp_path / "data", execution=previous
    ):
        pass
    previous.store.complete_agent_task(previous.operation_id, applied_revision=None, result={})
    if stage_state == "restored":
        with previous.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous.store.detach_agent_tasks_for_restore(
                conn, diagnostic="restored", now=previous.store.now()
            )
        shutil.rmtree(Path(previous.stage_root))
    elif stage_state == "different_host":
        with previous.store.connection() as conn:
            conn.execute(
                "UPDATE graph_runs SET stage_host='old-host' WHERE operation_id=?",
                (previous.operation_id,),
            )
        shutil.rmtree(Path(previous.stage_root))
    next_request, execution = _admit(app, chat_id=request.chat_id)
    task = execution.store.agent_task(execution.operation_id)
    assert task.native_session_id is None
    assert task.request["session_id"] is None
    assert task.stage_root == (previous.stage_root if stage_state == "current" else None)
    next_launcher = ScriptedLauncher([{}], message="Next turn finished")
    async for _ in stream_work_run(
        app.state.service, next_launcher, next_request, tmp_path / "data", execution=execution
    ):
        pass
    assert next_launcher.calls == 1
    assert next_launcher.resumed_sessions == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("interleaved", [False, True])
@pytest.mark.parametrize("handoff", ["consumed", "surviving", "recovered_receipt"])
async def test_consolidation_mailbox_apply_is_not_reapplied_at_turn_settlement(
    manifest, tmp_path, monkeypatch, interleaved, handoff
):
    import rcp.runs.tasks.work as work

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    request, execution = _admit(app)
    initial_revision = app.state.service.history.state().revision
    text = agent_patch_json(refresh_patch())

    handlers = []
    original = work._start_work_validator_mailbox

    def capture(*args, **kwargs):
        lifecycle = original(*args, **kwargs)
        handlers.append(lifecycle.command_handler)
        return lifecycle

    monkeypatch.setattr(work, "_start_work_validator_mailbox", capture)

    class ApplyingLauncher(ScriptedLauncher):
        async def stream(self, provider, prompt, **kwargs):
            async for event in super().stream(provider, prompt, **kwargs):
                if event.event == "answer":
                    if interleaved:
                        append_fixture_patch(app.state.service, refresh_patch("rq/teammate"))
                    response = handlers[-1](
                        _apply_request("during-turn"),
                        CommandTurnIdentity(None, execution.operation_id, "turn", "broker"),
                    )
                    assert response.status == "ok"
                    if handoff == "surviving":
                        # Simulate a handoff surviving consumption (including crash-after-append).
                        (self.workspaces[-1] / "patch.json").write_text(text)
                    elif handoff == "recovered_receipt":
                        # A restarted execution has only the durable keyed result.
                        execution.applied_revision = None
                        execution.applied_graph_state = None
                        replay = handlers[-1](
                            _apply_request("during-turn"),
                            CommandTurnIdentity(None, execution.operation_id, "turn", "broker"),
                        )
                        assert replay.result == response.result
                yield event

    launcher = ApplyingLauncher([{"patch.json": text}], message="Applied the graph changes")
    async for _ in stream_work_run(
        app.state.service, launcher, request, tmp_path / "data", execution=execution
    ):
        pass
    assert app.state.service.history.state().revision == initial_revision + 1 + int(interleaved)
    assert launcher.calls == 1
    assert execution.store.list_consolidation_apply_receipts(execution.operation_id)[0]["result"]
    seen = execution.store.graph_seen_head(
        app.state.default_project_id,
        f"chat:{request.chat_id}",
        launcher.native_session_id,
        GraphTargetRef(),
    )
    assert seen is not None
    assert seen.revision == initial_revision + (0 if interleaved else 1)


def test_consolidation_startup_preserves_the_exact_unstarted_admission(
    manifest, tmp_path, monkeypatch
):
    from rcp.background import BackgroundAgentTasks
    from rcp.storage import AppStore

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, execution = _admit(app)
    engine = BackgroundAgentTasks(AppStore(execution.store.path), lambda *args: None)
    monkeypatch.setattr(engine, "_schedule_remote_reconciliation", lambda **kwargs: None)
    engine.recover_at_startup()
    assert engine.store.agent_task(execution.operation_id).status == "queued"
    assert engine.store.pending_consolidation_operation_ids() == {execution.operation_id}


@pytest.mark.parametrize("failure_phase", ["apply", "service"])
def test_consolidation_unavailable_apply_retains_unresolved_failure(
    manifest, tmp_path, monkeypatch, failure_phase
):
    import rcp.runs.tasks.work as work
    from rcp.runs.tasks.work_turn_runtime import DeliverableFailure

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, execution = _admit(app)
    mailbox, apply = _command(app, execution, tmp_path)
    mailbox.write_text("patch.json", agent_patch_json(refresh_patch()))
    monkeypatch.setattr(
        work,
        "_apply_work_patch",
        lambda *_args, **_kwargs: (
            None,
            DeliverableFailure("canonical unavailable", correctable=False, commit_status="unknown"),
        ),
    )
    if failure_phase == "service":
        from rcp.transport import StateUnavailable

        def unavailable_service():
            raise StateUnavailable("canonical unavailable")

        handler = consolidation_apply_handler(unavailable_service, execution, mailbox, ["repo-a"])
        identity = CommandTurnIdentity(None, execution.operation_id, "turn", "broker")

        def apply(key):
            return handler(_apply_request(key), identity)

    assert apply("unavailable").status == "unavailable"
    receipt = execution.store.list_consolidation_apply_receipts(execution.operation_id)[0]
    assert receipt["last_failure"]["status"] == "unavailable"
    assert receipt["result"] is None
    assert mailbox.read_text("patch.json")


def test_branch_consolidation_work_applies_and_holds_evidence_on_its_target(
    manifest, tmp_path, monkeypatch
):
    import rcp.runs.tasks.work as work
    from rcp.agents import hidden_read

    from .test_branch_chats import _app_branch
    from .test_branch_history import _branch_patch

    host_facts = hidden_read.host_facts
    monkeypatch.setattr(
        hidden_read,
        "host_facts",
        lambda paths: {**host_facts(paths), "home": str(tmp_path / "account")},
    )
    app, main, episode, root = _app_branch(manifest, tmp_path)
    _, execution = _admit(app, target=episode.graph_target)
    handlers = []
    original = work._start_work_validator_mailbox

    def capture(*args, **kwargs):
        lifecycle = original(*args, **kwargs)
        handlers.append(lifecycle.command_handler)
        return lifecycle

    monkeypatch.setattr(work, "_start_work_validator_mailbox", capture)
    text = agent_patch_json(_branch_patch("ev/branch-consolidation"))

    class ApplyingLauncher(ScriptedLauncher):
        async def stream(self, provider, prompt, **kwargs):
            async for event in super().stream(provider, prompt, **kwargs):
                if event.event == "answer":
                    identity = CommandTurnIdentity(None, execution.operation_id, "turn", "broker")
                    first = handlers[-1](_apply_request("branch"), identity)
                    assert first.status == "ok", first.message
                    assert handlers[-1](_apply_request("branch"), identity).result == first.result
                    flag = handlers[-1](
                        FlagCommandRequest(
                            mailbox_id="a" * 32,
                            request_id=uuid.uuid4().hex,
                            credential="b" * 64,
                            verb="flag",
                            idempotency_key="verify",
                            arguments=FlagArguments(
                                evidence_id="ev/branch-consolidation",
                                kind="unverified",
                                finding="The source could not be read.",
                            ),
                        ),
                        identity,
                    )
                    assert flag.status == "ok", flag.message
                yield event

    launcher = ApplyingLauncher([{"patch.json": text}], message="Applied branch changes")
    monkeypatch.setattr(app.state.launcher, "stream", launcher.stream)
    app.state.background_tasks.launch_admitted(execution.operation_id)
    task = wait_for_task(execution.store, execution.operation_id)
    assert task.status == "succeeded", task.error
    assert task.episode_id is None
    assert task.dispatch_authority.profile == "ordinary"
    assert not execution.store.agent_task(root.operation_id).finished
    branch = main.for_graph_target(episode.graph_target)
    assert "ev/branch-consolidation" in branch.history.state().nodes
    assert "ev/branch-consolidation" not in main.history.state().nodes
    flags = execution.store.list_evidence_flags(task.project_id)
    assert {(flag["target"], flag["kind"]) for flag in flags} == {
        (episode.graph_target.branch_id, "consolidation_added"),
        (episode.graph_target.branch_id, "unverified"),
    }
    assert len(branch.history.load_patches()) == 1
