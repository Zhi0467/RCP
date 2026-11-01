from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from rcp.consolidation import next_occurrence
from rcp.runs.agent_schedules import validate_schedule


def test_cron_grammar_and_dst():
    assert next_occurrence(
        "30 2 * * *", "America/New_York", datetime(2026, 3, 8, tzinfo=UTC)
    ) == datetime(2026, 3, 8, 7, tzinfo=UTC)
    assert next_occurrence(
        "30 1 * * *", "America/New_York", datetime(2026, 11, 1, tzinfo=UTC)
    ) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert next_occurrence(
        "*/15 9-10 * * 1,3", "UTC", datetime(2026, 11, 2, 9, 1, tzinfo=UTC)
    ) == datetime(2026, 11, 2, 9, 15, tzinfo=UTC)
    # A stepped single start runs through the field maximum, as in cron.
    assert next_occurrence(
        "5/2 * * * *", "UTC", datetime(2026, 11, 2, 9, 6, tzinfo=UTC)
    ) == datetime(2026, 11, 2, 9, 7, tzinfo=UTC)


@pytest.mark.parametrize(
    "cron",
    ["09:00", "* * * *", "60 * * * *", "* * * * MON", "*/0 * * * *", "1-0 * * * *", "0 0 30 2 *"],
)
def test_invalid_cron(cron):
    with pytest.raises(ValueError):
        validate_schedule(
            cron=cron, at=None, timezone="UTC", after=datetime(2026, 1, 1, tzinfo=UTC)
        )


def test_local_one_shot():
    assert validate_schedule(
        cron=None,
        at="2026-03-08T02:30",
        timezone="America/New_York",
        after=datetime(2026, 1, 1, tzinfo=UTC),
    ) == datetime(2026, 3, 8, 7, tzinfo=UTC)
    with pytest.raises(ValueError):
        validate_schedule(
            cron=None, at="2020-01-01T12:00", timezone="UTC", after=datetime(2026, 1, 1, tzinfo=UTC)
        )


@pytest.fixture
def schedule_store(tmp_path):
    from rcp.core.models import AuthorizedHuman
    from rcp.service import RunRequest, resolve_dispatch_authority
    from rcp.storage import AppStore
    from tests.test_storage import _project, _task_record

    store = AppStore(tmp_path / "state.sqlite3")
    store.upsert_project(_project("project"))
    owner = store.local_owner
    assert owner is not None
    store.seat_project_member("project", owner.user_id, seated_by=owner.user_id)
    request = RunRequest(chat_id="thread", chat_scope="project", mode="work", message="Check daily")
    task = _task_record(
        store.now(),
        operation_id="origin",
        kind="project_chat",
        status="succeeded",
        request=request.model_dump(mode="json"),
        authorized_by=AuthorizedHuman(
            space_id=store.space_id, user_id=owner.user_id, display_name="Test Member"
        ),
        dispatch_authority=resolve_dispatch_authority("project_chat", request),
    )
    store.create_agent_task(task)
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_threads (thread_id,user_id,kind,project_id,created_at) VALUES (?,?,'project','project',?)",
            ("thread", owner.user_id, store.now()),
        )
    yield store
    store.close()


def command(**arguments):
    from rcp.agents.command_protocol import ScheduleCommandRequest

    return ScheduleCommandRequest.model_validate(
        dict(
            mailbox_id="b" * 32,
            credential="c" * 64,
            request_id="a" * 32,
            verb="schedule",
            idempotency_key="daily",
            arguments=arguments,
        )
    )


def test_old_writer_member_message_can_add_and_idempotently_cancel(schedule_store):
    from rcp.runs.agent_schedules import handle_schedule

    # Replay old-writer admission JSON, without a display transcript.
    old_request = '{"chat_id":"thread","chat_scope":"project","mode":"work","message":"Check daily","trigger":"human"}'
    with schedule_store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=? WHERE operation_id='origin'", (old_request,)
        )
    add = command(action="add", cron="0 9 * * *", timezone="UTC", note="Check results")
    first = handle_schedule(schedule_store, "origin", add)
    assert first.status == "ok"
    assert handle_schedule(schedule_store, "origin", add).result == first.result
    schedule_result = first.result["schedule"]
    assert isinstance(schedule_result, dict)
    schedule_id = schedule_result["schedule_id"]
    cancel = command(action="cancel", schedule_id=schedule_id)
    assert handle_schedule(schedule_store, "origin", cancel).status == "ok"
    assert handle_schedule(schedule_store, "origin", cancel).status == "ok"
    assert schedule_store.agent_schedules("thread")[0].state == "cancelled"


@pytest.mark.parametrize(
    "trigger,source,followup,member_message",
    [
        ("schedule", None, False, True),
        ("watcher", None, False, True),
        ("human", "slack", False, True),
        ("human", None, True, True),
        ("human", None, False, False),
    ],
)
def test_add_requires_admitting_member_message(
    schedule_store, monkeypatch, trigger, source, followup, member_message
):
    import json

    from rcp.runs.agent_schedules import handle_schedule

    with schedule_store.connection() as connection:
        request = dict(schedule_store.agent_task("origin").request)
        request.update(trigger=trigger, message="Check" if member_message else "")
        if source:
            request["source"] = source
        connection.execute(
            "UPDATE graph_runs SET request_json=? WHERE operation_id=?",
            (json.dumps(request), "origin"),
        )
    if followup:
        monkeypatch.setattr(schedule_store, "question_for_followup", lambda _: object())
    result = handle_schedule(
        schedule_store,
        "origin",
        command(action="add", cron="0 9 * * *", timezone="UTC", note="Check"),
    )
    assert result.status == "invalid"
    assert schedule_store.agent_schedules() == []


def test_due_schedules_combine_missed_fires_and_restore_disables(schedule_store):
    from rcp.runs.agent_schedules import schedule_wake_message

    for key in ("a", "b"):
        schedule_store.add_agent_schedule(
            thread_id="thread",
            operation_id="origin",
            key=key,
            cron="0 9 * * *",
            at=None,
            timezone="UTC",
            note=key,
            next_fire_at="2026-01-01T09:00:00+00:00",
        )
    now = datetime(2026, 1, 3, 10, tzinfo=UTC)
    pending = schedule_store.advance_agent_schedules(now)
    assert len(pending) == 2
    assert all(row.pending_since == "2026-01-01T09:00:00+00:00" for row in pending)
    assert all(row.next_fire_at == "2026-01-04T09:00:00+00:00" for row in pending)
    assert schedule_wake_message(pending, now).count('"missed_fires": 2') == 2
    schedule_store.detach_restored_lifecycle(
        diagnostic="test copy", confirmed_by="test", detached_at=now.isoformat()
    )
    assert all(
        row.state == "disabled" and row.disabled_reason == "restore" and row.pending_since is None
        for row in schedule_store.agent_schedules()
    )


def test_busy_thread_keeps_pending_then_claims_one_combined_wake(schedule_store, monkeypatch):
    from types import SimpleNamespace

    from rcp.runs.agent_schedules import dispatch_agent_schedules

    for key in ("a", "b"):
        schedule_store.add_agent_schedule(
            thread_id="thread",
            operation_id="origin",
            key=key,
            cron=None,
            at="2026-01-01T09:00",
            timezone="UTC",
            note=key,
            next_fire_at="2026-01-01T09:00:00+00:00",
        )
    with schedule_store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='running' WHERE operation_id='origin'")
    launched = []

    def launch(operation_id):
        schedule_store.mark_agent_task_running(operation_id)
        launched.append(operation_id)

    from rcp.background import BackgroundAgentTasks
    from tests.helpers import fake_launch_admitted

    async def stream(*_args) -> AsyncIterator[str]:
        raise AssertionError("The validating launch fake must not start a provider")
        yield ""

    tasks = BackgroundAgentTasks(schedule_store, stream)
    fake_launch_admitted(monkeypatch, tasks, launch)
    poller = SimpleNamespace(store=schedule_store, tasks=tasks)
    now = datetime.fromisoformat(schedule_store.now())
    dispatch_agent_schedules(poller, now, schedule_store.advance_agent_schedules(now))
    assert launched == []
    assert all(
        row.pending_since is not None and row.state == "done"
        for row in schedule_store.agent_schedules()
    )
    with schedule_store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='succeeded' WHERE operation_id='origin'")
    dispatch_agent_schedules(poller, now, schedule_store.advance_agent_schedules(now))
    assert len(launched) == 1
    task = schedule_store.agent_task(launched[0])
    assert task.request["trigger"] == "schedule"
    assert task.authorized_by == schedule_store.agent_task("origin").authorized_by
    assert task.request["message"].count('"missed_fires": 0') == 2
    assert all(row.pending_since is None for row in schedule_store.agent_schedules())
    dispatch_agent_schedules(poller, now, schedule_store.advance_agent_schedules(now))
    assert len(launched) == 1


def test_schedule_controls_are_owner_only_and_reenable_disabled(schedule_store):
    import uuid
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from rcp.api.agent_schedules import router
    from rcp.api.dependencies import get_identity_access, get_store

    schedule = schedule_store.add_agent_schedule(
        thread_id="thread",
        operation_id="origin",
        key="api",
        cron="0 9 * * *",
        at=None,
        timezone="UTC",
        note="Check",
        next_fire_at="2099-01-01T09:00:00+00:00",
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: schedule_store
    identity = SimpleNamespace(
        require_patch_capable_identity=lambda _: SimpleNamespace(
            user_id=schedule_store.local_owner.user_id
        )
    )
    app.dependency_overrides[get_identity_access] = lambda: identity
    client = TestClient(app)
    path = "/api/agent-threads/thread/schedules"
    assert client.get(path).json()["schedules"][0]["schedule_id"] == schedule.schedule_id
    identity.require_patch_capable_identity = lambda _: SimpleNamespace(user_id=str(uuid.uuid4()))
    assert client.get(path).status_code == 404
    assert client.delete(path + "/" + schedule.schedule_id).status_code == 404
    identity.require_patch_capable_identity = lambda _: SimpleNamespace(
        user_id=schedule_store.local_owner.user_id
    )
    with schedule_store.connection() as connection:
        connection.execute("UPDATE agent_schedules SET state='disabled', disabled_reason='restore'")
    assert client.post(path + "/" + schedule.schedule_id + "/enable").json()["state"] == "active"
    assert client.delete(path + "/" + schedule.schedule_id).status_code == 204
    assert client.get(path).json()["schedules"][0]["state"] == "cancelled"


def test_schedule_wake_rechecks_transfer_fence(schedule_store, monkeypatch):
    from types import SimpleNamespace

    from rcp.runs.agent_schedules import dispatch_agent_schedules

    schedule_store.add_agent_schedule(
        thread_id="thread",
        operation_id="origin",
        key="fence",
        cron="0 9 * * *",
        at=None,
        timezone="UTC",
        note="Check",
        next_fire_at="2026-01-01T09:00:00+00:00",
    )
    now = schedule_store.now()
    with schedule_store.connection() as connection:
        connection.execute(
            "INSERT INTO project_transfer_requests (request_id,side,phase,project_id,source_space_id,target_space_id,record_json,revision,created_at,updated_at) VALUES ('fenced','source','source_fenced','project','source','target','{}',0,?,?)",
            (now, now),
        )
    launched = []
    from rcp.background import BackgroundAgentTasks
    from tests.helpers import fake_launch_admitted

    async def stream(*_args) -> AsyncIterator[str]:
        raise AssertionError("The validating launch fake must not start a provider")
        yield ""

    tasks = BackgroundAgentTasks(schedule_store, stream)
    fake_launch_admitted(monkeypatch, tasks, launched.append)
    poller = SimpleNamespace(store=schedule_store, tasks=tasks)
    dispatch_agent_schedules(
        poller,
        datetime.fromisoformat(now),
        schedule_store.advance_agent_schedules(datetime.fromisoformat(now)),
    )
    assert launched == []
    assert schedule_store.agent_schedules()[0].pending_since is not None
    with schedule_store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == 1


def test_every_minute_long_downtime_count_and_dst_coalescing():
    from rcp.consolidation import cron_occurrence_count

    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)
    assert (
        cron_occurrence_count("* * * * *", "UTC", start, end)
        == int((end - start).total_seconds() / 60) + 1
    )
    # Missing 02:00..02:59 all resolve to 03:00, counted once.
    assert (
        cron_occurrence_count(
            "* * * * *",
            "America/New_York",
            datetime(2026, 3, 8, 5, tzinfo=UTC),
            datetime(2026, 3, 9, 4, tzinfo=UTC),
        )
        == 23 * 60 + 1
    )


@pytest.mark.parametrize("source", ["voice", "handoff"])
def test_member_sources_can_add(schedule_store, source):
    import json

    from rcp.runs.agent_schedules import handle_schedule

    task = schedule_store.agent_task("origin")
    with schedule_store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=? WHERE operation_id=?",
            (json.dumps({**task.request, "source": source}), "origin"),
        )
    assert (
        handle_schedule(
            schedule_store,
            "origin",
            command(action="add", cron="0 9 * * *", timezone="UTC", note="Check"),
        ).status
        == "ok"
    )


def test_schedule_cli_roundtrips_strict_protocol():
    from rcp.agents.command_protocol import ScheduleCommandRequest
    from rcp.agents.staged_command_client import _parser, _request_arguments

    namespace = _parser().parse_args(
        [
            "--credential",
            "credential.json",
            "--timeout",
            "1",
            "--workspace",
            ".",
            "schedule",
            "add",
            "--key",
            "daily",
            "--cron",
            "0 9 * * *",
            "--timezone",
            "UTC",
            "--note",
            "Check",
        ]
    )
    verb, key, args = _request_arguments(namespace, None)
    parsed = ScheduleCommandRequest.model_validate(
        dict(
            mailbox_id="b" * 32,
            credential="c" * 64,
            request_id="a" * 32,
            verb=verb,
            idempotency_key=key,
            arguments=args,
        )
    )
    assert parsed.arguments.cron == "0 9 * * *"
    assert parsed.arguments.note == "Check"
    namespace = _parser().parse_args(
        [
            "--credential",
            "credential.json",
            "--timeout",
            "1",
            "--workspace",
            ".",
            "schedule",
            "list",
        ]
    )
    assert _request_arguments(namespace, None) == ("schedule", None, {"action": "list"})


def test_tabs_are_cron_whitespace():
    assert validate_schedule(
        cron="0\t9\t*\t*\t*", at=None, timezone="UTC", after=datetime(2026, 1, 1, tzinfo=UTC)
    ) == datetime(2026, 1, 1, 9, tzinfo=UTC)


def test_due_schedule_is_marked_during_automatic_launch_cooldown(schedule_store, monkeypatch):
    from unittest.mock import Mock

    from rcp.consolidation import ConsolidationPoller

    schedule_store.add_agent_schedule(
        thread_id="thread",
        operation_id="origin",
        key="cooldown",
        cron=None,
        at="2026-01-01T09:00",
        timezone="UTC",
        note="Check",
        next_fire_at="2026-01-01T09:00:00+00:00",
    )
    tasks = Mock()
    poller = ConsolidationPoller(
        schedule_store, tasks, service_for=Mock(), clock=lambda: "2026-01-01T10:00:00+00:00"
    )
    monkeypatch.setattr("rcp.consolidation.seconds_until_automatic_launch", lambda: 60)
    poller.poll_once()
    schedule = schedule_store.agent_schedules()[0]
    assert schedule.state == "done"
    assert schedule.pending_since == "2026-01-01T09:00:00+00:00"
    tasks.launch_admitted.assert_not_called()


def test_space_thread_commands_create_and_wake_its_own_schedule(manifest, tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    from rcp.agents.command_mailbox import CommandTurnIdentity
    from rcp.runs.agent_schedules import dispatch_agent_schedules
    from rcp.runs.agent_thread_commands import ThreadCommandHandler
    from rcp.runs.space_chat import SpaceThreadOwner, append_space_message
    from rcp.service import RunRequest, resolve_dispatch_authority
    from tests.helpers import authorized_human, create_named_app
    from tests.test_storage import _task_record

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store, catalog = app.state.background_tasks.store, app.state.catalog
    actor = authorized_human(store)
    thread = next(
        item
        for item in store.ensure_agent_threads(actor.user_id, [], default_names=("Pip",))
        if item.kind == "space"
    )
    request = RunRequest(
        chat_id=thread.thread_id,
        chat_scope="project",
        mode="work",
        message="Remind me tomorrow",
        timezone="UTC",
    )
    store.create_agent_task(
        _task_record(
            store.now(),
            operation_id="space-origin",
            project_id=store.space_id,
            kind="space_chat",
            status="succeeded",
            authorized_by=actor,
            request=request.model_dump(mode="json"),
            dispatch_authority=resolve_dispatch_authority("space_chat", request),
        )
    )
    assert request.message is not None
    append_space_message(
        SpaceThreadOwner(store, catalog, thread.thread_id),
        request,
        "space-origin",
        role="user",
        text=request.message,
    )
    handler = ThreadCommandHandler(store, catalog, "space-origin")
    identity = CommandTurnIdentity(None, "space-origin", "turn", "broker")
    now = datetime.fromisoformat(store.now())
    added = handler(
        command(
            action="add", at=(now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"), note="Check"
        ),
        identity,
    )
    assert added.status == "ok", added.message
    listed = handler(command(action="list"), identity)
    assert listed.result["schedules"] == [added.result["schedule"]]
    launched = []
    from tests.helpers import fake_launch_admitted

    fake_launch_admitted(monkeypatch, app.state.background_tasks, launched.append)
    poller = SimpleNamespace(store=store, tasks=app.state.background_tasks)
    due = now + timedelta(days=2)
    dispatch_agent_schedules(poller, due, store.advance_agent_schedules(due))
    assert len(launched) == 1
    wake = store.agent_task(launched[0])
    assert wake.kind == "space_chat" and wake.project_id == store.space_id
    assert wake.request["chat_id"] == thread.thread_id
    assert wake.request["trigger"] == "schedule"
    assert wake.authorized_by == actor
