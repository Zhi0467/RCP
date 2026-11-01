from __future__ import annotations

from pathlib import Path

import pytest

from rcp.agents.staged_command_client import _parser, _request_arguments


def test_thread_command_client_uses_typed_project_and_file_arguments(tmp_path: Path) -> None:
    common = ["--credential", "credential.json", "--timeout", "5", "--workspace", str(tmp_path)]
    projects = _parser().parse_args([*common, "projects", "show", "project"])
    assert _request_arguments(projects, str(tmp_path)) == (
        "projects",
        None,
        {"action": "show", "project_id": "project"},
    )
    (tmp_path / "request.md").write_text("A private request")
    handoff = _parser().parse_args(
        [*common, "handoff", "--key", "request-1", "project", "request.md"]
    )
    assert _request_arguments(handoff, str(tmp_path)) == (
        "handoff",
        "request-1",
        {"project_id": "project", "message_file": "request.md"},
    )


def test_handoff_task_and_delivery_claim_commit_together(tmp_path: Path) -> None:
    from rcp.service import RunRequest
    from rcp.storage import AppStore
    from tests.helpers import authorized_human
    from tests.test_storage import _task_record

    store = AppStore(tmp_path / "rcp.sqlite3")
    actor = authorized_human(store)
    threads = store.ensure_agent_threads(actor.user_id, ["project"], default_names=("Pip",))
    source = next(row for row in threads if row.kind == "space")
    target = next(row for row in threads if row.kind == "project")
    handoff = store.create_agent_handoff(
        source_thread_id=source.thread_id,
        source_operation_id="source",
        target_thread_id=target.thread_id,
        idempotency_key="key",
        message="Please check",
    )
    record = _task_record(
        store.now(),
        operation_id="target",
        kind="project_chat",
        authorized_by=actor,
        request=RunRequest(
            chat_scope="project",
            chat_id=target.thread_id,
            mode="work",
            source="handoff",
            message=handoff.message,
        ).model_dump(mode="json"),
    )
    stored = store.create_agent_task(record, handoff_id=handoff.handoff_id)
    assert stored.operation_id == "target"
    assert store.pending_agent_handoffs() == []
    assert store.create_agent_task(record, handoff_id=handoff.handoff_id).operation_id == "target"
    wrong = record.model_copy(update={"operation_id": "duplicate"})
    with pytest.raises(ValueError):
        store.create_agent_task(wrong, handoff_id=handoff.handoff_id)
    assert store.agent_task("duplicate") is None


def test_projects_and_handoff_share_cards_and_member_ownership(manifest, tmp_path: Path) -> None:
    from rcp.agents.command_mailbox import CommandTurnIdentity
    from rcp.agents.command_protocol import HandoffArguments, HandoffCommandRequest
    from rcp.runs.agent_thread_commands import ThreadCommandHandler, projects_projection
    from tests.helpers import authorized_human, create_named_app
    from tests.test_storage import _task_record

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store, catalog = app.state.background_tasks.store, app.state.catalog
    actor = authorized_human(store)
    project_id = app.state.default_project_id
    threads = store.ensure_agent_threads(actor.user_id, [project_id], default_names=("Pip",))
    source = next(row for row in threads if row.kind == "space")
    cards = projects_projection(store, catalog, actor.user_id)
    assert len(cards) == 1
    assert cards[0]["id"] == project_id
    assert cards[0]["same_machine"] is True
    assert cards[0]["thread_id"] == next(
        row.thread_id for row in threads if row.project_id == project_id
    )
    store.create_agent_task(
        _task_record(
            store.now(),
            operation_id="source",
            project_id=store.space_id,
            kind="space_chat",
            authorized_by=actor,
            request={"chat_id": source.thread_id, "chat_scope": "project", "mode": "work"},
        )
    )
    (tmp_path / "message.md").write_text("Please check the project")
    handler = ThreadCommandHandler(store, catalog, "source", tmp_path)
    request = HandoffCommandRequest(
        verb="handoff",
        mailbox_id="a" * 32,
        request_id="b" * 32,
        credential="c" * 64,
        idempotency_key="once",
        arguments=HandoffArguments(project_id=project_id, message_file="message.md"),
    )
    identity = CommandTurnIdentity(None, "source", "turn", "broker")
    first = handler(request, identity)
    assert first.status == "ok"
    assert first.result["state"] == "pending"
    assert handler(request, identity).result == first.result
    assert len(store.pending_agent_handoffs()) == 1
    denied = handler(request, CommandTurnIdentity(None, "another-task", "turn", "broker"))
    assert denied.status == "invalid"
    (tmp_path / "symlink.md").symlink_to(tmp_path / "message.md")
    unsafe = request.model_copy(
        update={
            "idempotency_key": "unsafe",
            "arguments": HandoffArguments(project_id=project_id, message_file="symlink.md"),
        }
    )
    assert handler(unsafe, identity).status == "invalid"


def test_pending_handoff_delivers_on_settle_then_wakes_space_once(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    from contextlib import nullcontext

    from rcp.runs.agent_thread_commands import dispatch_thread_handoffs
    from rcp.service import RunRequest
    from tests.helpers import authorized_human, create_named_app
    from tests.test_storage import _task_record

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    background = app.state.background_tasks
    store, catalog = background.store, app.state.catalog
    actor = authorized_human(store)
    project_id = app.state.default_project_id
    threads = store.ensure_agent_threads(actor.user_id, [project_id], default_names=("Pip",))
    source = next(row for row in threads if row.kind == "space")
    target = next(row for row in threads if row.kind == "project")
    source_request = RunRequest(
        provider="codex",
        model="gpt-5.6-sol",
        reasoning="medium",
        run_on=manifest.base_machine,
        chat_scope="project",
        chat_id=source.thread_id,
        mode="work",
        message="Delegate",
    )
    store.create_agent_task(
        _task_record(
            store.now(),
            operation_id="source",
            project_id=store.space_id,
            kind="space_chat",
            status="succeeded",
            authorized_by=actor,
            request=source_request.model_dump(mode="json"),
        )
    )
    store.create_agent_task(
        _task_record(
            store.now(),
            operation_id="busy",
            project_id=project_id,
            kind="project_chat",
            authorized_by=actor,
            request=RunRequest(
                chat_scope="project", chat_id=target.thread_id, mode="work"
            ).model_dump(mode="json"),
        )
    )
    handoff = store.create_agent_handoff(
        source_thread_id=source.thread_id,
        source_operation_id="source",
        target_thread_id=target.thread_id,
        idempotency_key="one",
        message="Please check",
    )
    monkeypatch.setattr(background, "admit_provider_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(background, "_spawn_record", lambda record, request, **kwargs: record)

    def drain():
        return dispatch_thread_handoffs(
            store, catalog, background, project_admission=lambda _: nullcontext()
        )

    assert drain() == []
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status='succeeded',finished_at=? WHERE operation_id='busy'",
            (store.now(),),
        )
    operations = drain()
    assert len(operations) == 1
    turn = store.agent_task(operations[0])
    assert turn is not None
    assert turn.request["source"] == "handoff"
    assert turn.request["trigger"] == "human"
    transcript = catalog.open_execution(project_id).chat_transcript(target.thread_id)
    assert transcript is not None
    assert [(message.source, message.text) for message in transcript.messages] == [
        ("handoff", "Please check")
    ]
    monkeypatch.setattr(background, "_schedule_remote_reconciliation", lambda **kwargs: None)
    background.recover_at_startup()
    recovered = store.agent_task(turn.operation_id)
    assert recovered is not None and recovered.status == "queued"
    assert drain() == []
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status='succeeded',finished_at=?,result_json=? WHERE operation_id=?",
            (store.now(), '{"answer":"Checked"}', turn.operation_id),
        )
    wakes = drain()
    assert len(wakes) == 1
    wake = store.agent_task(wakes[0])
    assert wake is not None
    assert wake.kind == "space_chat"
    assert wake.request["trigger"] == "watcher"
    assert "Checked" in wake.request["message"]
    assert drain() == []
    with store.connection() as connection:
        assert tuple(
            connection.execute(
                "SELECT target_operation_id,notified_operation_id FROM agent_handoffs WHERE handoff_id=?",
                (handoff.handoff_id,),
            ).fetchone()
        ) == (turn.operation_id, wake.operation_id)


def test_member_removal_discards_space_transcript_and_pending_agent_work(tmp_path: Path) -> None:
    from tests.test_server_member_removal import _team

    store, _alice, member, _, _, coordinator = _team(tmp_path)
    threads = store.ensure_agent_threads(member.user_id, ["project"], default_names=("Pip",))
    source = next(row for row in threads if row.kind == "space")
    target = next(row for row in threads if row.kind == "project")
    store.create_agent_handoff(
        source_thread_id=source.thread_id,
        source_operation_id="source",
        target_thread_id=target.thread_id,
        idempotency_key="key",
        message="Pending",
    )
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_schedules(schedule_id,thread_id,created_operation_id,idempotency_key,cron,timezone,note,state,created_at) VALUES('schedule',?,'source','key','0 9 * * *','UTC','note','active',?)",
            (source.thread_id, store.now()),
        )
    directory = store.path.parent / "agent-threads" / member.user_id
    directory.mkdir(parents=True)
    (directory / f"{source.thread_id}.jsonl").write_text("private")
    boundary = coordinator.plan(member.user_id).snapshot.boundary_sha256
    coordinator.advance(member.user_id, boundary_sha256=boundary)
    assert not directory.exists()
    assert not store.pending_agent_handoffs()
    with store.connection() as connection:
        assert connection.execute("SELECT count(*) FROM agent_schedules").fetchone()[0] == 0
    assert store.agent_thread(target.thread_id) is not None  # retain project privacy authority


def test_project_work_thread_exposes_projects_with_open_graph_items(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    from rcp.agents import AgentProcessControl
    from rcp.agents.command_mailbox import CommandTurnIdentity
    from rcp.agents.command_protocol import ProjectsArguments, ProjectsCommandRequest
    from rcp.background import AgentTaskExecution
    from rcp.core.models import Blocker, Decision
    from rcp.runs.questions import work_command_handler
    from tests.helpers import authorized_human, create_named_app
    from tests.test_storage import _task_record

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store, catalog = app.state.background_tasks.store, app.state.catalog
    actor = authorized_human(store)
    project_id = app.state.default_project_id
    thread = next(
        row
        for row in store.ensure_agent_threads(actor.user_id, [project_id], default_names=("Pip",))
        if row.kind == "project"
    )
    store.create_agent_task(
        _task_record(
            store.now(),
            operation_id="turn",
            project_id=project_id,
            kind="project_chat",
            authorized_by=actor,
            request={"chat_id": thread.thread_id, "chat_scope": "project", "mode": "work"},
        )
    )
    service = catalog.open_execution(project_id)
    state = service.history.state().model_copy(deep=True)
    state.nodes["decision"] = Decision(
        id="decision", type="decision", title="Choose", question="Which approach?"
    )
    state.nodes["blocker"] = Blocker(
        id="blocker", type="blocker", title="Missing input", description="Input required"
    )
    monkeypatch.setattr(service.history, "state", lambda: state)
    monkeypatch.setattr(catalog, "open_display", lambda _: service)
    execution = AgentTaskExecution("turn", store, AgentProcessControl())
    handler = work_command_handler(execution, None, service=service)
    assert "projects" in handler.allowed_verbs
    request = ProjectsCommandRequest(
        verb="projects",
        mailbox_id="a" * 32,
        request_id="b" * 32,
        credential="c" * 64,
        arguments=ProjectsArguments(action="show", project_id=project_id),
    )
    response = handler(request, CommandTurnIdentity(None, "turn", "turn", "broker"))
    assert response.status == "ok"
    decisions, blockers = response.result["decisions"], response.result["blockers"]
    assert isinstance(decisions, list) and isinstance(blockers, list)
    assert [node["id"] for node in decisions] == ["decision"]
    assert [node["id"] for node in blockers] == ["blocker"]
    assert "handoff" not in handler.allowed_verbs
