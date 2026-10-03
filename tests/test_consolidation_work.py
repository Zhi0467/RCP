from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from rcp.agents import AgentProcessControl
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import ApplyArguments, ApplyCommandRequest
from rcp.background import AgentTaskExecution
from rcp.runs.consolidation import consolidation_apply_handler, settlement_source_effect_id
from rcp.runs.questions import work_command_handler
from rcp.runs.tasks.work import _apply_work_patch, stream_work_run
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord
from rcp.transport.workspace_mailbox import RunStageMailbox

from .helpers import (
    agent_patch_json,
    append_fixture_patch,
    create_named_app,
    refresh_patch,
    seed_patch,
)
from .test_api import ScriptedLauncher, _named_test_authorizer


def _admit(app, *, chat_id=None):
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
    request = RunRequest(
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
        status="queued",
        status_message="queued",
        request=request.model_dump(mode="json"),
        authorized_by=authorizer,
        dispatch_authority=resolve_dispatch_authority("project_chat", request),
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    run = store.claim_consolidation_occurrence(
        schedule,
        occurrence_date=now.date().isoformat(),
        next_due_at=(now + timedelta(days=1)).isoformat(),
        input_head=app.state.service.history.state().revision,
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
            source_effect_id=settlement_source_effect_id(service, execution, text),
        )
        assert failure is None
        assert result.status == "applied"
    assert service.history.state().revision == initial_revision + 3


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
            lambda *_: (_ for _ in ()).throw(RuntimeError("crash")),
        )
        with pytest.raises(RuntimeError, match="crash"):
            apply("commit")
    assert app.state.service.history.state().revision == initial_revision + 1
    assert (tmp_path / "patch.json").exists()
    response = apply("commit")
    assert response.status == "ok"
    assert response.result["revision"] == initial_revision + 1
    assert app.state.service.history.state().revision == initial_revision + 1
    assert execution.store.list_consolidation_apply_receipts(execution.operation_id)[0]["result"]


def test_consolidation_apply_live_membership_is_required_by_transition(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    _, execution = _admit(app)
    task = execution.store.agent_task(execution.operation_id)
    initial_revision = app.state.service.history.state().revision
    with execution.store.connection() as conn:
        conn.execute(
            "DELETE FROM project_members WHERE project_id=? AND user_id=?",
            (task.project_id, task.authorized_by.user_id),
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
        source_effect_id=str(uuid.uuid4()),
    )
    assert applied is None
    assert failure is not None
    assert app.state.service.history.state().revision == initial_revision
    assert (tmp_path / "patch.json").exists()


@pytest.mark.parametrize("change", ["delete", "renew", "expire", "leave"])
def test_consolidation_queued_launch_rechecks_authorization(
    manifest, tmp_path, monkeypatch, change
):
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
    else:
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
    assert (
        refused.error.startswith("consolidation_authorization_")
        or refused.error == "consolidation_authorizer_departed"
    )


@pytest.mark.asyncio
async def test_consolidation_bound_owner_pins_workflow_and_refuses_watchers(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    request, execution = _admit(app)
    assert work_command_handler(execution, None).allowed_verbs == {"validate", "apply", "lesson"}
    launcher = ScriptedLauncher([{"watch.json": "{}"}], message="Consolidation finished.")
    async for _ in stream_work_run(
        app.state.service, launcher, request, tmp_path / "data", execution=execution
    ):
        pass
    assert launcher.calls == 1
    assert launcher.resumed_sessions == [None]
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
    for method in (manager.resume, manager.retry, manager.repair_graph_update):
        with pytest.raises(ValueError, match="consolidation_continuation_forbidden"):
            method(execution.operation_id)
    monkeypatch.setattr(
        manager, "_transport_retry_attempt", lambda *_: pytest.fail("scheduled a retry")
    )
    manager._auto_retry_transport_loss(execution.store.agent_task(execution.operation_id))


@pytest.mark.asyncio
async def test_consolidation_admission_keeps_stage_but_drops_native_session(manifest, tmp_path):
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
    _, execution = _admit(app, chat_id=request.chat_id)
    task = execution.store.agent_task(execution.operation_id)
    assert task.native_session_id is None
    assert task.request["session_id"] is None
    assert task.stage_root == previous.stage_root


@pytest.mark.asyncio
async def test_consolidation_mailbox_apply_is_not_reapplied_at_turn_settlement(
    manifest, tmp_path, monkeypatch
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
                    response = handlers[-1](
                        _apply_request("during-turn"),
                        CommandTurnIdentity(None, execution.operation_id, "turn", "broker"),
                    )
                    assert response.status == "ok"
                    # Simulate a handoff surviving consumption (including crash-after-append).
                    (self.workspaces[-1] / "patch.json").write_text(text)
                yield event

    launcher = ApplyingLauncher([{"patch.json": text}], message="Applied the graph changes")
    frames = [
        frame
        async for frame in stream_work_run(
            app.state.service, launcher, request, tmp_path / "data", execution=execution
        )
    ]
    assert app.state.service.history.state().revision == initial_revision + 1
    assert launcher.calls == 1
    assert execution.store.list_consolidation_apply_receipts(execution.operation_id)[0]["result"]
    assert frames


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
