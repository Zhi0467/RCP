from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from rcp.consolidation import next_occurrence
from rcp.runs.agent_schedules import validate_schedule
from tests.lifecycles.schedules import build_schedule_store, schedule_command


def test_cron_grammar_and_dst():
    """A stepped single start runs through the field maximum, as in cron."""
    assert next_occurrence(
        "30 2 * * *", "America/New_York", datetime(2026, 3, 8, tzinfo=UTC)
    ) == datetime(2026, 3, 8, 7, tzinfo=UTC)
    assert next_occurrence(
        "30 1 * * *", "America/New_York", datetime(2026, 11, 1, tzinfo=UTC)
    ) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert next_occurrence(
        "*/15 9-10 * * 1,3", "UTC", datetime(2026, 11, 2, 9, 1, tzinfo=UTC)
    ) == datetime(2026, 11, 2, 9, 15, tzinfo=UTC)
    assert next_occurrence(
        "5/2 * * * *", "UTC", datetime(2026, 11, 2, 9, 6, tzinfo=UTC)
    ) == datetime(2026, 11, 2, 9, 7, tzinfo=UTC)


@pytest.mark.parametrize(
    "cron",
    ["09:00", "* * * *", "60 * * * *", "* * * * XYZ", "*/0 * * * *", "1-0 * * * *", "0 0 30 2 *"],
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


@pytest.fixture(autouse=True)
def admitted_provider(monkeypatch):
    """Fixture requests carry no provider; production admission is checked separately."""
    from rcp.background import BackgroundAgentTasks

    monkeypatch.setattr(BackgroundAgentTasks, "admit_provider_task", lambda *a, **k: None)


@pytest.fixture
def schedule_store(tmp_path):
    store = build_schedule_store(tmp_path / "state.sqlite3")
    yield store
    store.close()


def command(key="daily", **arguments):
    return schedule_command(key, **arguments)


def test_old_writer_member_message_can_add_and_idempotently_cancel(schedule_store):
    from rcp.runs.agent_schedules import handle_schedule

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
    "trigger,source,followup,every_minute_ok",
    [
        ("human", None, False, True),
        ("human", "voice", False, True),
        ("schedule", None, False, False),
        ("watcher", None, False, False),
        ("human", None, True, False),
    ],
)
def test_any_thread_turn_adds_but_only_member_turns_schedule_often(
    schedule_store, monkeypatch, trigger, source, followup, every_minute_ok
):
    import json

    from rcp.runs.agent_schedules import handle_schedule

    with schedule_store.connection() as connection:
        request = dict(schedule_store.agent_task("origin").request)
        request.update(trigger=trigger, source=source, timezone="Europe/Berlin")
        connection.execute(
            "UPDATE graph_runs SET request_json=? WHERE operation_id=?",
            (json.dumps(request), "origin"),
        )
    if followup:
        monkeypatch.setattr(schedule_store, "question_for_followup", lambda _: object())
    often = handle_schedule(
        schedule_store, "origin", command(key="often", action="add", cron="* * * * *", note="Play")
    )
    assert (often.status == "ok") is every_minute_ok
    daily = handle_schedule(
        schedule_store, "origin", command(action="add", cron="@daily", note="Check")
    )
    assert daily.status == "ok", daily.message
    assert daily.result["timezone"] == "Europe/Berlin" and daily.result["now_local"]


@pytest.mark.parametrize("sender_trigger,every_minute_ok", [("human", True), ("schedule", False)])
def test_a_delivered_handoff_schedules_often_only_for_a_member_sender(
    schedule_store, monkeypatch, sender_trigger, every_minute_ok
):
    import json
    from types import SimpleNamespace

    from rcp.runs.agent_schedules import handle_schedule

    origin = schedule_store.agent_task("origin")
    schedule_store.create_agent_task(
        origin.model_copy(
            update={
                "operation_id": "sender",
                "request": {**origin.request, "trigger": sender_trigger},
            }
        )
    )
    with schedule_store.connection() as connection:
        request = {**origin.request, "trigger": "human", "source": "handoff", "timezone": "UTC"}
        connection.execute(
            "UPDATE graph_runs SET request_json=? WHERE operation_id='origin'",
            (json.dumps(request),),
        )
    monkeypatch.setattr(
        schedule_store,
        "agent_handoff_delivered_as",
        lambda _: SimpleNamespace(source_operation_id="sender"),
    )
    often = handle_schedule(
        schedule_store, "origin", command(key="often", action="add", cron="* * * * *", note="Play")
    )
    assert (often.status == "ok") is every_minute_ok, often.message


def test_cron_names_aliases_and_vixie_stepped_day():
    """2026-11-02 is a Monday. A stepped day-of-month starting with * is unrestricted, so both days
    must match.
    """
    after = datetime(2026, 11, 2, tzinfo=UTC)
    assert next_occurrence("0 9 * JAN-DEC MON-FRI", "UTC", after) == datetime(
        2026, 11, 2, 9, tzinfo=UTC
    )
    assert next_occurrence("@weekly", "UTC", after) == datetime(2026, 11, 8, tzinfo=UTC)
    assert next_occurrence("0 0 */2 * 1", "UTC", after) == datetime(2026, 11, 9, tzinfo=UTC)


def test_retry_readds_the_same_schedule_and_list_carries_the_member_clock(schedule_store):
    from rcp.runs.agent_schedules import handle_schedule

    origin = schedule_store.agent_task("origin")
    schedule_store.create_agent_task(
        origin.model_copy(update={"operation_id": "retry", "parent_operation_id": "origin"}),
        continuation_cause="retry",
    )
    add = command(action="add", cron="0 9 * * *", timezone="UTC", note="Check")
    first = handle_schedule(schedule_store, "origin", add)
    assert (
        handle_schedule(schedule_store, "retry", add).result["schedule"]
        == (first.result["schedule"])
    )
    assert len(schedule_store.agent_schedules("thread")) == 1
    listed = handle_schedule(schedule_store, "retry", command(action="list"))
    assert "now_utc" in listed.result or listed.result["now_local"]


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
    schedule_store.add_agent_schedule(
        thread_id="thread",
        operation_id="origin",
        key="once",
        cron=None,
        at="2026-01-02T09:00",
        timezone="UTC",
        note="once",
        next_fire_at="2026-01-02T09:00:00+00:00",
    )
    schedule_store.advance_agent_schedules(now)
    schedule_store.detach_restored_lifecycle(
        diagnostic="test copy", confirmed_by="test", detached_at=now.isoformat()
    )
    rows = {row.note: row for row in schedule_store.agent_schedules()}
    assert all(row.pending_since is None for row in rows.values())
    assert {note: (row.state, row.disabled_reason) for note, row in rows.items()} == {
        "a": ("disabled", "restore"),
        "b": ("disabled", "restore"),
        "once": ("done", None),
    }


@pytest.mark.parametrize("archived", [False, True])
def test_busy_thread_keeps_pending_then_claims_one_combined_wake(
    schedule_store, monkeypatch, archived
):
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
    stage = schedule_store.path.parent / "stage"
    stage.mkdir()
    schedule_store.checkpoint_agent_task(
        "origin", native_session_id="current-session", stage_root=str(stage)
    )
    previous = schedule_store.agent_task("origin")
    schedule_store.create_agent_task(
        previous.model_copy(
            update={
                "operation_id": "failed",
                "status": "failed",
                "native_session_id": None,
                "created_at": schedule_store.now(),
                "request": {**previous.request, "session_id": None},
            }
        )
    )
    if archived:
        schedule_store.set_chat_archived("project", "thread", "member", archived=True)
        dispatch_agent_schedules(poller, now, schedule_store.advance_agent_schedules(now))
        assert launched == []
        with schedule_store.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == 2
        assert all(row.pending_since for row in schedule_store.agent_schedules())
        schedule_store.set_chat_archived("project", "thread", "member", archived=False)
    dispatch_agent_schedules(poller, now, schedule_store.advance_agent_schedules(now))
    assert len(launched) == 1
    task = schedule_store.agent_task(launched[0])
    assert task.request["trigger"] == "schedule"
    assert task.native_session_id == task.request["session_id"] == "current-session"
    receipts = [
        r.payload
        for r in schedule_store.agent_task_receipts(task.operation_id)
        if r.category == "chat_session_resolution"
    ]
    assert len(receipts) == 1 and receipts[0]["outcome"] == "continued"
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
    identity = SimpleNamespace(user_id=schedule_store.local_owner.user_id)
    identity.acting_user = lambda _: SimpleNamespace(user_id=identity.user_id)
    identity.require_patch_capable_identity = identity.acting_user
    app.dependency_overrides[get_identity_access] = lambda: identity
    client = TestClient(app)
    path = "/api/agent-threads/thread/schedules"
    assert client.get(path).json()["schedules"][0]["schedule_id"] == schedule.schedule_id
    identity.user_id = str(uuid.uuid4())
    assert client.get(path).status_code == 404
    assert client.delete(path + "/" + schedule.schedule_id).status_code == 404
    assert client.post(path + "/" + schedule.schedule_id + "/pause").status_code == 404
    identity.user_id = schedule_store.local_owner.user_id
    assert client.post(path + "/" + schedule.schedule_id + "/pause").json()["state"] == "paused"
    assert client.post(path + "/" + schedule.schedule_id + "/resume").json()["state"] == "active"
    assert client.post(path + "/" + schedule.schedule_id + "/resume").status_code == 409
    with schedule_store.connection() as connection:
        connection.execute("UPDATE agent_schedules SET state='disabled', disabled_reason='restore'")
    assert client.post(path + "/" + schedule.schedule_id + "/enable").json()["state"] == "active"
    past = schedule_store.add_agent_schedule(
        thread_id="thread",
        operation_id="origin",
        key="past",
        cron=None,
        at="2020-01-01T09:00",
        timezone="UTC",
        note="Old",
        next_fire_at="2020-01-01T09:00:00+00:00",
    )
    with schedule_store.connection() as connection:
        connection.execute(
            "UPDATE agent_schedules SET state='disabled' WHERE schedule_id=?", (past.schedule_id,)
        )
    refused = client.post(path + "/" + past.schedule_id + "/enable")
    assert refused.status_code == 409
    assert client.delete(path + "/" + past.schedule_id).status_code == 204
    assert client.delete(path + "/" + schedule.schedule_id).status_code == 204
    assert client.get(path).json()["schedules"][0]["state"] == "cancelled"


def test_nameless_owner_can_list_schedules(schedule_store):
    from types import SimpleNamespace

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from rcp.api.agent_schedules import router
    from rcp.api.dependencies import get_identity_access, get_store

    def unnamed(_):
        raise HTTPException(status_code=428, detail={"code": "identity_name_required"})

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: schedule_store
    app.dependency_overrides[get_identity_access] = lambda: SimpleNamespace(
        acting_user=lambda _: SimpleNamespace(user_id=schedule_store.local_owner.user_id),
        require_patch_capable_identity=unnamed,
    )
    client = TestClient(app)
    path = "/api/agent-threads/thread/schedules"
    assert client.get(path).json() == {"schedules": []}
    assert client.delete(path + "/missing").status_code == 428


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
    row = schedule_store.agent_schedules()[0]
    assert row.pending_since is not None and row.deferred_reason
    with schedule_store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == 1


def test_every_minute_long_downtime_count_and_dst_coalescing():
    """Missing 02:00..02:59 all resolve to 03:00, counted once."""
    from rcp.consolidation import cron_occurrence_count

    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)
    assert (
        cron_occurrence_count("* * * * *", "UTC", start, end)
        == int((end - start).total_seconds() / 60) + 1
    )
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
            "ping",
            "--every",
            "1m",
            "--from",
            "2026-11-01T23:47",
            "--until",
            "2026-11-01T23:56",
            "--count",
            "10",
            "--note",
            "Ping",
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
    ).arguments
    assert (parsed.every, parsed.start, parsed.until, parsed.count) == (
        "1m",
        "2026-11-01T23:47",
        "2026-11-01T23:56",
        10,
    )


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


def test_schedule_waits_quietly_for_a_stopped_turn_then_survives_restart(
    schedule_store, monkeypatch, caplog
):
    from types import SimpleNamespace

    from rcp.background import BackgroundAgentTasks
    from rcp.runs.agent_schedules import dispatch_agent_schedules
    from rcp.storage import AppStore
    from tests.helpers import fake_launch_admitted

    schedule_store.add_agent_schedule(
        thread_id="thread",
        operation_id="origin",
        key="daily",
        cron=None,
        at="2026-01-01T09:00",
        timezone="UTC",
        note="n",
        next_fire_at="2026-01-01T09:00:00+00:00",
    )
    with schedule_store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status='paused', native_session_id='s1' WHERE operation_id='origin'"
        )
    launched = []
    tasks = BackgroundAgentTasks(schedule_store, lambda *_args: None)
    fake_launch_admitted(monkeypatch, tasks, launched.append)
    poller = SimpleNamespace(store=schedule_store, tasks=tasks)
    now = datetime.fromisoformat(schedule_store.now())
    dispatch_agent_schedules(poller, now, schedule_store.advance_agent_schedules(now))
    assert launched == [] and not caplog.records
    assert schedule_store.agent_schedules()[0].pending_since
    resumed = schedule_store.agent_task("origin").model_copy(
        update={"operation_id": "resumed", "parent_operation_id": "origin", "status": "queued"}
    )
    schedule_store.create_agent_task(resumed, continuation_cause="resume")
    with schedule_store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='succeeded' WHERE operation_id='resumed'")
    schedule_store.set_agent_thread_pick(
        "thread", provider="claude", model=None, reasoning=None, run_on=None
    )
    dispatch_agent_schedules(poller, now, schedule_store.advance_agent_schedules(now))
    assert len(launched) == 1
    assert schedule_store.agent_task(launched[0]).request["provider"] == "claude"
    restarted = BackgroundAgentTasks(AppStore(schedule_store.path), lambda *_args: None)
    monkeypatch.setattr(restarted, "_schedule_remote_reconciliation", lambda **kwargs: None)
    restarted.recover_at_startup()
    assert restarted.store.agent_task(launched[0]).status == "queued"


def test_unschedulable_row_is_disabled_without_blocking_consolidation(schedule_store, monkeypatch):
    from unittest.mock import Mock

    from rcp.consolidation import ConsolidationPoller

    for key in ("good", "poison"):
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
    with schedule_store.connection() as connection:
        connection.execute("UPDATE agent_schedules SET timezone='Nowhere/Gone' WHERE note='poison'")
    poller = ConsolidationPoller(
        schedule_store, Mock(), service_for=Mock(), clock=lambda: "2026-01-01T10:00:00+00:00"
    )
    monkeypatch.setattr("rcp.consolidation.seconds_until_automatic_launch", lambda: 0)
    monkeypatch.setattr(poller, "dispatch_due", Mock())
    monkeypatch.setattr(poller, "dispatch_agent_schedules", Mock())
    poller.poll_once()
    poller.dispatch_due.assert_called_once()
    pending = poller.dispatch_agent_schedules.call_args.args[1]
    assert [row.note for row in pending] == ["good"]
    states = {
        row.note: (row.state, row.disabled_reason) for row in schedule_store.agent_schedules()
    }
    assert states["poison"][0] == "disabled" and states["poison"][1]
    assert states["good"] == ("active", None)


def test_deferred_wake_keeps_notes_then_fires_after_restore_and_closing_settles(
    schedule_store, monkeypatch
):
    import json
    from types import SimpleNamespace

    from rcp.background import BackgroundAgentTasks
    from rcp.runs.agent_schedules import dispatch_agent_schedules
    from rcp.runs.provider_login import ProviderSignedOut
    from tests.helpers import fake_launch_admitted

    for key, cron, at in (("once", None, "2026-01-01T09:00"), ("daily", "0 9 * * *", None)):
        schedule_store.add_agent_schedule(
            thread_id="thread",
            operation_id="origin",
            key=key,
            cron=cron,
            at=at,
            timezone="Europe/Berlin",
            note=key,
            next_fire_at="2026-01-01T08:00:00+00:00",
        )
    with schedule_store.connection() as connection:
        connection.execute("UPDATE graph_runs SET history_only=1 WHERE operation_id='origin'")
    launched = []
    tasks = BackgroundAgentTasks(schedule_store, lambda *_args: None)
    fake_launch_admitted(monkeypatch, tasks, launched.append)

    def signed_out(*_args, **_kwargs):
        raise ProviderSignedOut("Sign in to Codex")

    monkeypatch.setattr(tasks, "admit_provider_task", signed_out)
    poller = SimpleNamespace(store=schedule_store, tasks=tasks)
    now = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
    dispatch_agent_schedules(poller, now, schedule_store.advance_agent_schedules(now))
    assert launched == []
    assert all(
        row.pending_since and "Sign in" in (row.deferred_reason or "")
        for row in schedule_store.agent_schedules()
    )
    monkeypatch.setattr(tasks, "admit_provider_task", lambda *_a, **_k: None)
    dispatch_agent_schedules(poller, now, schedule_store.advance_agent_schedules(now))
    assert len(launched) == 1
    wake = schedule_store.agent_task(launched[0])
    notes = json.loads(wake.request["message"].split("\n", 1)[1])
    assert {note["due_at"] for note in notes} == {"2026-01-01T09:00+01:00"}
    assert {note["now"] for note in notes} == {"2026-01-01T10:30+01:00"}
    assert all(row.deferred_reason is None for row in schedule_store.agent_schedules())
    schedule_store.close_agent_thread("thread", reason="access_lost")
    later = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
    dispatch_agent_schedules(poller, later, schedule_store.advance_agent_schedules(later))
    assert schedule_store.agent_task(launched[0]).status == "failed"
    states = {row.note: row.state for row in schedule_store.agent_schedules()}
    assert states == {"once": "done", "daily": "disabled"}


def test_a_turn_without_a_zone_uses_the_members_latest_composer_zone(schedule_store):
    from rcp.runs.agent_schedules import handle_schedule, schedule_instructions

    origin = schedule_store.agent_task("origin")
    schedule_store.create_agent_task(
        origin.model_copy(
            update={
                "operation_id": "composer",
                "created_at": "2000-01-01T00:00:00+00:00",
                "request": {**origin.request, "timezone": "Asia/Tokyo"},
            }
        )
    )
    assert "Asia/Tokyo" in schedule_instructions(schedule_store, "origin")
    added = handle_schedule(
        schedule_store, "origin", command(action="add", cron="0 9 * * *", note="Check")
    )
    assert added.result["schedule"]["timezone"] == added.result["timezone"] == "Asia/Tokyo"


def test_a_turn_with_a_non_iana_zone_still_renders_the_schedule_rule(schedule_store):
    from rcp.runs.agent_schedules import member_timezone, schedule_instructions

    origin = schedule_store.agent_task("origin")
    schedule_store.create_agent_task(
        origin.model_copy(
            update={
                "operation_id": "odd-zone",
                "request": {**origin.request, "timezone": "Mars/Olympus"},
            }
        )
    )
    assert member_timezone(schedule_store, schedule_store.agent_task("odd-zone")) is None
    assert "Mars/Olympus" not in schedule_instructions(schedule_store, "odd-zone")


def _set_trigger(store, trigger):
    import json

    request = {**store.agent_task("origin").request, "trigger": trigger}
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=? WHERE operation_id='origin'",
            (json.dumps(request),),
        )


def _added(response) -> dict:
    assert response.status == "ok", response.message
    schedule = response.result["schedule"]
    assert isinstance(schedule, dict)
    return schedule


def _refusal(response) -> str:
    assert response.status == "invalid"
    return response.message or ""


def test_every_with_count_retires_after_its_fires(schedule_store):
    from datetime import timedelta

    from rcp.runs.agent_schedules import handle_schedule

    added = _added(
        handle_schedule(
            schedule_store,
            "origin",
            command(action="add", every="1m", count=3, timezone="UTC", note="Ping"),
        )
    )
    first = datetime.fromisoformat(added["next_fire_at"])
    assert (
        timedelta(0) < first - datetime.fromisoformat(added["created_at"]) <= timedelta(minutes=1)
    )
    for minute, left in ((0, 2), (1, 1)):
        schedule_store.advance_agent_schedules(first + timedelta(minutes=minute, seconds=1))
        row = schedule_store.agent_schedules("thread")[0]
        assert (row.state, row.fires_left) == ("active", left)
        assert row.next_fire_at == (first + timedelta(minutes=minute + 1)).isoformat()
    schedule_store.advance_agent_schedules(first + timedelta(minutes=2, seconds=1))
    row = schedule_store.agent_schedules("thread")[0]
    assert (row.state, row.fires_left, row.next_fire_at) == ("done", 0, None)
    assert row.pending_since == first.isoformat()


@pytest.mark.parametrize("bound", ["count", "until"])
def test_a_wake_reports_only_the_fires_within_its_bounds(schedule_store, bound):
    """Fifteen minutes late, a schedule bounded to two fires reports two, not fifteen.

    This is the schedule lifecycle explorer's one-tick trace.
    """
    import json
    from datetime import timedelta

    from rcp.runs.agent_schedules import handle_schedule, schedule_wake_message

    now = datetime.fromisoformat(schedule_store.now())
    start = (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    limit = (
        {"count": 2}
        if bound == "count"
        else {"until": (now + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M")}
    )
    _added(
        handle_schedule(
            schedule_store,
            "origin",
            command(action="add", every="1m", start=start, timezone="UTC", note="Ping", **limit),
        )
    )
    later = now + timedelta(minutes=15)
    pending = schedule_store.advance_agent_schedules(later)
    notes = json.loads(schedule_wake_message(pending, later).split("\n", 1)[1])
    assert [note["missed_fires"] for note in notes] == [1]
    assert pending[0].state == "done"


def test_closing_the_thread_drops_next_fires_and_waiting_notes(schedule_store):
    """Closing disables a repeat as lost access does at dispatch; a done one-shot keeps its note."""
    for key, cron, at in (("daily", "0 9 * * *", None), ("once", None, "2026-01-01T09:00")):
        schedule_store.add_agent_schedule(
            thread_id="thread",
            operation_id="origin",
            key=key,
            cron=cron,
            at=at,
            timezone="UTC",
            note=key,
            next_fire_at="2026-01-01T09:00:00+00:00",
        )
    schedule_store.advance_agent_schedules(datetime(2026, 1, 1, 10, tzinfo=UTC))
    schedule_store.close_agent_thread("thread", reason="access_lost")
    rows = {row.note: row for row in schedule_store.agent_schedules()}
    daily, once = rows["daily"], rows["once"]
    assert (daily.state, daily.next_fire_at, daily.pending_since) == ("disabled", None, None)
    assert (once.state, once.pending_since) == ("done", "2026-01-01T09:00:00+00:00")


def test_cron_until_retires_after_its_last_fire_and_from_delays_the_first(schedule_store):
    after = datetime(2026, 1, 1, tzinfo=UTC)
    assert validate_schedule(
        cron="0 9 * * *", at=None, timezone="UTC", after=after, start="2026-01-05T09:00"
    ) == datetime(2026, 1, 5, 9, tzinfo=UTC)
    assert validate_schedule(
        cron=None, at=None, every_minutes=60, timezone="UTC", after=after, start="2026-01-05T10:30"
    ) == datetime(2026, 1, 5, 10, 30, tzinfo=UTC)
    assert validate_schedule(
        cron=None, at=None, every_minutes=60, timezone="UTC", after=after
    ) == datetime(2026, 1, 1, 1, tzinfo=UTC)
    first = validate_schedule(
        cron="0 9 * * *", at=None, timezone="UTC", after=after, until="2026-01-02T09:00"
    )
    schedule_store.add_agent_schedule(
        thread_id="thread",
        operation_id="origin",
        key="until",
        cron="0 9 * * *",
        at=None,
        timezone="UTC",
        note="Daily",
        next_fire_at=first.isoformat(),
        until="2026-01-02T09:00",
    )
    schedule_store.advance_agent_schedules(datetime(2026, 1, 1, 9, 1, tzinfo=UTC))
    row = schedule_store.agent_schedules("thread")[0]
    assert (row.state, row.next_fire_at) == ("active", "2026-01-02T09:00:00+00:00")
    schedule_store.advance_agent_schedules(datetime(2026, 1, 2, 9, 1, tzinfo=UTC))
    assert schedule_store.agent_schedules("thread")[0].state == "done"


def test_pause_skips_fires_and_resume_continues_after_now(schedule_store):
    """Resume takes the next hour after now on the original phase and replays nothing missed; an
    --until that passed while paused retires the schedule.
    """
    from datetime import timedelta

    from rcp.runs.agent_schedules import handle_schedule

    anchor = datetime(2026, 1, 1, 9, tzinfo=UTC)
    for key, until in (("hourly", None), ("bounded", "2026-01-02T09:00")):
        schedule_store.add_agent_schedule(
            thread_id="thread",
            operation_id="origin",
            key=key,
            cron=None,
            at=None,
            every_minutes=60,
            timezone="UTC",
            note=key,
            next_fire_at=anchor.isoformat(),
            until=until,
            count=5,
        )
    ids = {row.note: row.schedule_id for row in schedule_store.agent_schedules("thread")}
    for key in ids:
        pause = command(key=f"pause-{key}", action="pause", schedule_id=ids[key])
        assert handle_schedule(schedule_store, "origin", pause).status == "ok"
        assert handle_schedule(schedule_store, "origin", pause).status == "ok"
    assert schedule_store.advance_agent_schedules(anchor + timedelta(days=1)) == []
    assert {row.state for row in schedule_store.agent_schedules("thread")} == {"paused"}
    for key in ids:
        resume = command(key=f"resume-{key}", action="resume", schedule_id=ids[key])
        assert handle_schedule(schedule_store, "origin", resume).status == "ok"
    rows = {row.note: row for row in schedule_store.agent_schedules("thread")}
    now = datetime.fromisoformat(schedule_store.now())
    resumed = datetime.fromisoformat(rows["hourly"].next_fire_at)
    assert now < resumed <= now + timedelta(hours=1)
    assert (resumed - anchor) % timedelta(hours=1) == timedelta(0)
    assert (rows["hourly"].state, rows["hourly"].fires_left) == ("active", 5)
    assert rows["hourly"].pending_since is None
    assert (rows["bounded"].state, rows["bounded"].next_fire_at) == ("done", None)
    again = command(key="resume-again", action="resume", schedule_id=ids["hourly"])
    assert "schedule_not_paused" in _refusal(handle_schedule(schedule_store, "origin", again))


def test_old_row_without_bounds_loads_and_fires(schedule_store):
    with schedule_store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_schedules (schedule_id,thread_id,created_operation_id,"
            "idempotency_key,cron,at_local,timezone,note,state,next_fire_at,created_at) "
            "VALUES ('old','thread','origin','old','0 9 * * *',NULL,'UTC','Old','active',"
            "'2026-01-01T09:00:00+00:00','2025-12-31T00:00:00+00:00')"
        )
    pending = schedule_store.advance_agent_schedules(datetime(2026, 1, 1, 9, 1, tzinfo=UTC))
    assert [(row.schedule_id, row.state, row.fires_left) for row in pending] == [
        ("old", "active", None)
    ]
    assert pending[0].next_fire_at == "2026-01-02T09:00:00+00:00"


def test_wake_interval_gap_and_invalid_combinations_are_refused(schedule_store):
    from pydantic import ValidationError

    from rcp.runs.agent_schedules import handle_schedule

    _set_trigger(schedule_store, "schedule")
    often = handle_schedule(
        schedule_store,
        "origin",
        command(key="often", action="add", every="5m", timezone="UTC", note="Ping"),
    )
    assert "schedule_too_frequent" in _refusal(often)
    _added(
        handle_schedule(
            schedule_store,
            "origin",
            command(key="ok", action="add", every="15m", timezone="UTC", note="Ping"),
        )
    )
    for bad in ("5x", "0m", "1.5h"):
        refused = handle_schedule(
            schedule_store,
            "origin",
            command(key=bad, action="add", every=bad, timezone="UTC", note="Ping"),
        )
        assert "schedule_every_invalid" in _refusal(refused)
    early = handle_schedule(
        schedule_store,
        "origin",
        command(
            key="early",
            action="add",
            cron="0 9 * * *",
            timezone="UTC",
            note="Ping",
            until="2020-01-01T09:00",
        ),
    )
    assert "schedule_until_before_first" in _refusal(early)
    combinations: list[dict[str, Any]] = [
        dict(at="2099-01-01T09:00", until="2099-01-02T09:00"),
        dict(at="2099-01-01T09:00", count=2),
        dict(cron="0 9 * * *", every="1h"),
        dict(cron="0 9 * * *", at="2099-01-01T09:00"),
    ]
    for arguments in combinations:
        with pytest.raises(ValidationError):
            command(action="add", note="Ping", **arguments)
    with pytest.raises(ValidationError):
        command(action="pause")


@pytest.mark.parametrize(
    ("columns", "values", "missed"),
    [
        ("at_local, state", "'2026-01-15T09:00', 'done'", 0),
        ("every_minutes, next_fire_at, state", "60, '2026-01-15T12:00:00+00:00', 'active'", 2),
        ("cron, next_fire_at, state", "'0 * * * *', '2026-01-15T12:00:00+00:00', 'active'", 2),
    ],
)
def test_a_note_waiting_before_v74_wakes_with_the_fires_it_held(
    schedule_store, columns, values, missed
):
    """Migration v74 counts the fires a waiting note's schedule advanced past, as v73 did."""
    import json

    from rcp.runs.agent_schedules import schedule_wake_message
    from rcp.storage.agent_schedules import migrate_agent_schedule_pending_fires

    with schedule_store.connection() as connection:
        connection.execute("ALTER TABLE agent_schedules DROP COLUMN pending_fires")
        connection.execute(
            "INSERT INTO agent_schedules (schedule_id, thread_id, created_operation_id, "
            f"idempotency_key, timezone, note, pending_since, created_at, {columns}) "
            "VALUES ('s', 'thread', 'op', 'k', 'UTC', 'Check', "
            f"'2026-01-15T09:00:00+00:00', '2026-01-15T08:00:00+00:00', {values})"
        )
        migrate_agent_schedule_pending_fires(connection)
    [schedule] = schedule_store.agent_schedules("thread")

    message = schedule_wake_message([schedule], datetime(2026, 1, 15, 12, 30, tzinfo=UTC))

    [note] = json.loads(message[message.index("\n") + 1 :])
    assert note["missed_fires"] == missed
