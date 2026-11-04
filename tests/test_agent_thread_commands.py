from __future__ import annotations

import uuid
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
    """Claiming fails while the delivered turn has not settled, and a refusal is final."""
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
    report = record.model_copy(
        update={
            "operation_id": "report",
            "kind": "space_chat",
            "project_id": store.space_id,
            "request": {"chat_scope": "project", "chat_id": source.thread_id, "mode": "work"},
        }
    )
    with pytest.raises(ValueError):
        store.create_agent_task(report, handoff_wake_id=handoff.handoff_id)
    refused = store.create_agent_handoff(
        source_thread_id=source.thread_id,
        source_operation_id="source",
        target_thread_id=target.thread_id,
        idempotency_key="refused",
        message="Please check",
    )
    store.refuse_agent_handoff(refused.handoff_id, "thread_closed")
    with pytest.raises(ValueError):
        store.create_agent_task(wrong, handoff_id=refused.handoff_id)
    assert store.agent_task("report") is None and store.agent_task("duplicate") is None


def test_projects_and_handoff_share_cards_and_member_ownership(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    """A reused key with other arguments is refused before it can repick. The handoff waits on the
    running Space turn, whose Stop cancels it with no wake.
    """
    from contextlib import nullcontext

    from rcp.agents.command_mailbox import CommandTurnIdentity
    from rcp.agents.command_protocol import HandoffArguments, HandoffCommandRequest
    from rcp.limits import AGENT_HANDOFF_MAX_CHARS
    from rcp.runs.agent_thread_commands import (
        ThreadCommandHandler,
        dispatch_thread_handoffs,
        project_details,
        projects_projection,
    )
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
    for name in ("Initial name", "Renamed colleague"):
        store.set_chat_title(project_id, cards[0]["thread_id"], actor.user_id, name)
        assert projects_projection(store, catalog, actor.user_id)[0]["agent_name"] == name
        assert project_details(store, catalog, actor.user_id, project_id)["agent_name"] == name
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
    from rcp.agents.command_protocol import AskCommandRequest
    from rcp.runs.agent_thread_commands import space_command_usages

    ask = AskCommandRequest.model_validate(
        {
            **request.model_dump(),
            "verb": "ask",
            "arguments": {
                "question": "Approve?",
                "action": {
                    "kind": "truth_scope",
                    "project_id": project_id,
                    "alias": "repo-a",
                    "writable": True,
                },
            },
        }
    )
    assert handler(ask, identity).status == "invalid"
    usages = space_command_usages(handler.allowed_verbs)
    assert {usage.split()[0] for usage in usages} == handler.allowed_verbs
    assert "ask" not in handler.allowed_verbs and "handoff" in handler.allowed_verbs
    assert {usage.split()[0] for usage in space_command_usages(frozenset({"handoff"}))} == {
        "handoff"
    }
    first = handler(request, identity)
    assert first.status == "ok"
    assert first.result["state"] == "pending"
    assert handler(request, identity).result == first.result
    assert len(store.pending_agent_handoffs()) == 1
    denied = handler(request, CommandTurnIdentity(None, "another-task", "turn", "broker"))
    assert denied.status == "invalid"
    (tmp_path / "symlink.md").symlink_to(tmp_path / "message.md")
    (tmp_path / "large.md").write_text("x" * (AGENT_HANDOFF_MAX_CHARS + 1))
    (tmp_path / "blank.md").write_text("  ")
    (tmp_path / "binary.md").write_bytes(b"\xff")
    for name in ("symlink.md", "large.md", "blank.md", "binary.md", "missing.md"):
        unsafe = request.model_copy(
            update={
                "idempotency_key": name,
                "arguments": HandoffArguments(project_id=project_id, message_file=name),
            }
        )
        assert handler(unsafe, identity).status == "invalid"
    picked = request.model_copy(
        update={
            "idempotency_key": "picked",
            "arguments": HandoffArguments(
                project_id=project_id, message_file="message.md", provider="claude"
            ),
        }
    )
    target_id = cards[0]["thread_id"]
    monkeypatch.setattr(
        "rcp.runs.auto_research_admission.auto_research_child_profile_choices",
        lambda *args, **kwargs: {"claude": {}},
    )
    assert handler(picked, identity).result["state"] == "pending"
    assert store.agent_thread(target_id).agent["provider"] == "claude"
    (tmp_path / "other.md").write_text("Something else")
    monkeypatch.setattr(
        "rcp.runs.auto_research_admission.auto_research_child_profile_choices",
        lambda *args, **kwargs: {"claude": {}, "codex": {}},
    )
    reused = picked.model_copy(
        update={
            "arguments": HandoffArguments(
                project_id=project_id, message_file="other.md", provider="codex"
            )
        }
    )
    assert handler(reused, identity).status == "invalid"
    assert store.agent_thread(target_id).agent["provider"] == "claude"
    monkeypatch.setattr(
        "rcp.runs.auto_research_admission.auto_research_child_profile_choices",
        lambda *args, **kwargs: {},
    )
    unready = picked.model_copy(update={"idempotency_key": "unready"})
    assert handler(unready, identity).status == "invalid"
    assert len(store.pending_agent_handoffs()) == 2
    background = app.state.background_tasks
    assert (
        dispatch_thread_handoffs(
            store, catalog, background, project_admission=lambda _: nullcontext()
        )
        == []
    )
    assert len(store.pending_agent_handoffs()) == 2
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='paused' WHERE operation_id='source'")
    assert (
        dispatch_thread_handoffs(
            store, catalog, background, project_admission=lambda _: nullcontext()
        )
        == []
    )
    refused = handler(request, identity).result
    assert (refused["state"], refused["reason"]) == ("refused", "source_turn_paused")


@pytest.mark.parametrize(
    "refusal",
    [
        None,
        "archived",
        "closed",
        "closed_queued",
        "deferred",
        "paused",
        "retried",
        "signed_out",
        "stood_down",
        "unprojected",
    ],
)
def test_pending_handoff_delivers_on_settle_then_wakes_space_once(
    manifest, tmp_path: Path, monkeypatch, refusal
) -> None:
    """A refused delivery gives up after a bounded wait and a queued turn whose thread closed fails
    and reports. Stop reports at once, a sign-in failure waits for a retry, and a lost link RCP
    retries itself is not a settlement.
    """
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
    spawned: list[str] = []

    def spawn(record, request, **kwargs):
        spawned.append(record.operation_id)
        return record

    monkeypatch.setattr(background, "_spawn_record", spawn)

    def drain():
        return dispatch_thread_handoffs(
            store, catalog, background, project_admission=lambda _: nullcontext()
        )

    if refusal in {"archived", "closed"}:
        if refusal == "archived":
            with store.connection() as connection:
                connection.execute(
                    "UPDATE graph_runs SET status='succeeded' WHERE operation_id='busy'"
                )
            store.set_chat_archived(project_id, target.thread_id, actor.user_id, archived=True)
        else:
            store.close_agent_thread(target.thread_id, reason="access_lost")
            with store.connection() as connection:
                connection.execute(
                    "DELETE FROM project_members WHERE project_id=? AND user_id=?",
                    (project_id, actor.user_id),
                )
        wakes = drain()
        assert len(wakes) == 1
        wake = store.agent_task(wakes[0])
        assert wake.kind == "space_chat"
        report = wake.request["handoff_report"]
        assert (report["status"], report["reason"]) == (
            "refused",
            "chat_archived" if refusal == "archived" else "thread_closed",
        )
        assert store.pending_agent_handoffs() == []
        store.set_chat_archived(project_id, target.thread_id, actor.user_id, archived=False)
        store.ensure_agent_threads(actor.user_id, [project_id], default_names=("Pip",))
        assert drain() == []
        assert store.agent_task_events("source") == []
        return
    assert drain() == []
    for thread, provider in ((source, "claude"), (target, "codex")):
        store.set_agent_thread_pick(
            thread.thread_id, provider=provider, model=None, reasoning=None, run_on=None
        )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status='succeeded',finished_at=? WHERE operation_id='busy'",
            (store.now(),),
        )
    if refusal == "deferred":
        from datetime import datetime, timedelta

        from rcp.limits import AGENT_HANDOFF_DELIVERY_TIMEOUT_SECONDS
        from rcp.runs.provider_login import MachineDependenciesMissing

        def missing(scope, *args, **kwargs):
            if scope == project_id:
                raise MachineDependenciesMissing("codex is missing")

        monkeypatch.setattr(background, "admit_provider_task", missing)
        assert drain() == [] and drain() == []
        assert len(store.agent_task_events("source")) == 1
        later = datetime.fromisoformat(store.now()) + timedelta(
            seconds=AGENT_HANDOFF_DELIVERY_TIMEOUT_SECONDS
        )
        monkeypatch.setattr(store, "now", lambda: later.isoformat())
        report = store.agent_task(drain()[0]).request["handoff_report"]
        assert report["status"] == "refused" and report["reason"].startswith("delivery_timed_out")
        return
    operations = drain()
    assert len(operations) == 1
    turn = store.agent_task(operations[0])
    assert turn is not None
    assert turn.request["source"] == "handoff"
    assert turn.request["trigger"] == "human"
    assert turn.request["provider"] == "codex"
    transcript = catalog.open_execution(project_id).chat_transcript(target.thread_id)
    assert transcript is not None
    assert [(message.source, message.text) for message in transcript.messages] == [
        ("handoff", "Please check")
    ]
    monkeypatch.setattr(background, "_schedule_remote_reconciliation", lambda **kwargs: None)
    if refusal == "closed_queued":
        store.close_agent_thread(target.thread_id, reason="access_lost")
        spawned.clear()
        background.recover_at_startup()
        assert spawned == []
        report = store.agent_task(drain()[0]).request["handoff_report"]
        assert report["status"] == "failed"
        return
    background.recover_at_startup()
    recovered = store.agent_task(turn.operation_id)
    assert recovered is not None and recovered.status == "queued"
    from rcp.runs import chat

    append = chat._append_chat_records
    copies: list[object] = []

    def counted(*args, **kwargs):
        copies.append(args)
        return append(*args, **kwargs)

    monkeypatch.setattr(chat, "_append_chat_records", counted)
    assert drain() == []
    assert copies == []
    settled = turn.operation_id
    store.mark_agent_task_running(settled)
    if refusal == "paused":
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status='paused' WHERE operation_id=?", (settled,)
            )
        report = store.agent_task(drain()[0]).request["handoff_report"]
        assert report["status"] == "paused"
        return
    if refusal == "unprojected":
        store.complete_agent_task(
            settled, applied_revision=None, result={"turn_outcome": {"status": "done"}}
        )
        with store.connection() as connection:
            connection.execute(
                "UPDATE agent_handoffs SET message_projected_at=NULL WHERE handoff_id=?",
                (handoff.handoff_id,),
            )

        def unavailable(*args, **kwargs):
            raise OSError("workspace unavailable")

        monkeypatch.setattr(chat, "_append_chat_records", unavailable)
        assert drain() == []
        noted = len(store.agent_task_events(settled))
        assert drain() == []
        assert len(store.agent_task_events(settled)) == noted
        monkeypatch.setattr(chat, "_append_chat_records", append)
        assert len(drain()) == 1
        return
    if refusal == "signed_out":
        store.fail_agent_task(settled, "signed out", failure_kind="provider_auth")
        assert drain() == []
        store.create_agent_task(
            _task_record(
                store.now(),
                operation_id="later",
                project_id=project_id,
                kind="project_chat",
                status="succeeded",
                authorized_by=actor,
                request=RunRequest(
                    chat_scope="project", chat_id=target.thread_id, mode="work"
                ).model_dump(mode="json"),
            )
        )
        report = store.agent_task(drain()[0]).request["handoff_report"]
        assert (report["status"], report["failure_kind"]) == ("failed", "provider_auth")
        assert report["provider"] == "codex"
        return
    if refusal == "stood_down":
        store.fail_agent_task(settled, "link lost", failure_kind="transport_lost")
        store.record_agent_task_receipt(settled, "transport_auto_retry", {"attempt": 1})
        assert drain() == []
        store.record_agent_task_receipt(
            settled, "transport_auto_retry_superseded", {"reason": "not retryable"}
        )
        report = store.agent_task(drain()[0]).request["handoff_report"]
        assert (report["status"], report["failure_kind"]) == ("failed", "transport_lost")
        return
    if refusal == "retried":
        import threading

        store.fail_agent_task(settled, "link lost", failure_kind="transport_lost")
        background._workers[settled] = threading.current_thread()
        assert drain() == []
        background._forget_control(settled)
        store.record_agent_task_receipt(settled, "transport_auto_retry", {"attempt": 1})
        assert drain() == []
        settled = "retried"
        store.create_agent_task(
            turn.model_copy(
                update={"operation_id": settled, "parent_operation_id": turn.operation_id}
            ),
            continuation_cause="retry",
        )
        store.mark_agent_task_running(settled)
    store.complete_agent_task(
        settled,
        applied_revision=None,
        result={"messages": ["Looking.", "Checked"], "turn_outcome": {"status": "done"}},
    )
    space_turn = source_request.model_copy(update={"message": "Busy"})
    store.create_agent_task(
        _task_record(
            store.now(),
            operation_id="space-busy",
            project_id=store.space_id,
            kind="space_chat",
            authorized_by=actor,
            request=space_turn.model_dump(mode="json"),
        )
    )
    assert drain() == []
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status='succeeded' WHERE operation_id='space-busy'"
        )
        connection.execute(
            "UPDATE space_users SET removal_started_at=? WHERE user_id=?",
            (store.now(), actor.user_id),
        )
    assert drain() == []
    with store.connection() as connection:
        connection.execute(
            "UPDATE space_users SET removal_started_at=NULL WHERE user_id=?", (actor.user_id,)
        )
    wakes = drain()
    assert len(wakes) == 1
    wake = store.agent_task(wakes[0])
    assert wake is not None
    assert wake.kind == "space_chat"
    assert (wake.request["source"], wake.request["provider"]) == ("handoff_report", "claude")
    report = wake.request["handoff_report"]
    assert (report["answer"], report["turn_outcome"]["status"]) == ("Checked", "done")
    assert report["provider"] == "codex"
    assert drain() == []
    with store.connection() as connection:
        assert tuple(
            connection.execute(
                "SELECT target_operation_id,notified_operation_id FROM agent_handoffs WHERE handoff_id=?",
                (handoff.handoff_id,),
            ).fetchone()
        ) == (turn.operation_id, wake.operation_id)


def _queue_world(manifest, tmp_path: Path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from rcp.runs.agent_thread_commands import dispatch_thread_handoffs
    from tests.helpers import authorized_human, create_named_app

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    background = app.state.background_tasks
    store, catalog = background.store, app.state.catalog
    actor = authorized_human(store)
    project_id = app.state.default_project_id
    threads = store.ensure_agent_threads(actor.user_id, [project_id], default_names=("Pip",))
    monkeypatch.setattr(background, "admit_provider_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(background, "_spawn_record", lambda record, request, **kwargs: record)
    world = SimpleNamespace(
        app=app,
        background=background,
        store=store,
        actor=actor,
        project_id=project_id,
        space=next(row for row in threads if row.kind == "space"),
        thread=next(row for row in threads if row.kind == "project"),
    )
    world.drain = lambda: dispatch_thread_handoffs(
        store, catalog, background, project_admission=lambda _: nullcontext()
    )
    world.queue = lambda key, message: store.queue_agent_thread_message(
        queue_id=str(uuid.uuid4()),
        thread_id=world.thread.thread_id,
        user_id=actor.user_id,
        idempotency_key=key,
        message=message,
    )
    return world


def _finish(store, operation_id: str) -> None:
    store.mark_agent_task_running(operation_id)
    store.complete_agent_task(operation_id, applied_revision=None, result={})


def test_handoffs_start_before_queued_messages_and_each_kind_is_fifo(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    from rcp.service import RunRequest
    from tests.test_storage import _task_record

    world = _queue_world(manifest, tmp_path, monkeypatch)
    store = world.store
    store.create_agent_task(
        _task_record(
            store.now(),
            operation_id="source",
            project_id=store.space_id,
            kind="space_chat",
            status="succeeded",
            authorized_by=world.actor,
            request=RunRequest(
                chat_scope="project", chat_id=world.space.thread_id, mode="work"
            ).model_dump(mode="json"),
        )
    )
    first = world.queue("first", "First")
    second = world.queue("second", "Second")
    store.create_agent_handoff(
        source_thread_id=world.space.thread_id,
        source_operation_id="source",
        idempotency_key="handoff",
        target_thread_id=world.thread.thread_id,
        message="From Space",
    )
    (delivered,) = world.drain()
    assert store.agent_task(delivered).request["source"] == "handoff"
    assert store.agent_thread_queue_item(first.queue_id).operation_id is None
    _finish(store, delivered)
    world.drain()
    started = store.agent_thread_queue_item(first.queue_id).operation_id
    turn = store.agent_task(started)
    assert turn is not None and turn.kind == "project_chat"
    assert turn.request["message"] == "First"
    assert turn.request["trigger"] == "human" and "source" not in turn.request
    assert turn.authorized_by.user_id == world.actor.user_id
    assert turn.authorized_by.display_name == world.actor.display_name
    assert store.agent_thread_queue_item(second.queue_id).operation_id is None
    _finish(store, started)
    world.drain()
    assert store.agent_task(store.agent_thread_queue_item(second.queue_id).operation_id)


@pytest.mark.parametrize(
    "case", ["paused", "closed", "not_member", "removed", "blank_name", "deferred"]
)
def test_queued_message_waits_on_pause_and_is_refused_for_good(
    manifest, tmp_path: Path, monkeypatch, case
) -> None:
    from datetime import datetime, timedelta

    from rcp.limits import AGENT_HANDOFF_DELIVERY_TIMEOUT_SECONDS
    from rcp.runs.provider_login import MachineDependenciesMissing
    from rcp.service import RunRequest
    from tests.test_storage import _task_record

    world = _queue_world(manifest, tmp_path, monkeypatch)
    store, user_id = world.store, world.actor.user_id
    queued = world.queue("key", "Next")
    with store.connection() as connection:
        if case == "closed":
            store.close_agent_thread(world.thread.thread_id, reason="access_lost")
        elif case == "not_member":
            connection.execute("DELETE FROM project_members WHERE user_id=?", (user_id,))
        elif case == "removed":
            connection.execute(
                "UPDATE space_users SET removal_started_at=? WHERE user_id=?",
                (store.now(), user_id),
            )
        elif case == "blank_name":
            connection.execute(
                "UPDATE space_users SET display_name=NULL WHERE user_id=?", (user_id,)
            )
    if case == "paused":
        store.create_agent_task(
            _task_record(
                store.now(),
                operation_id="paused",
                project_id=world.project_id,
                kind="project_chat",
                status="paused",
                native_session_id="native",
                authorized_by=world.actor,
                request=RunRequest(
                    chat_scope="project", chat_id=world.thread.thread_id, mode="work"
                ).model_dump(mode="json"),
            )
        )
        assert world.drain() == [] and world.drain() == []
        row = store.agent_thread_queue_item(queued.queue_id)
        assert (row.operation_id, row.refusal_reason, row.deferred_at) == (None, None, None)
        return
    if case == "deferred":

        def missing(scope, *args, **kwargs):
            raise MachineDependenciesMissing("codex is missing")

        monkeypatch.setattr(world.background, "admit_provider_task", missing)
        assert world.drain() == [] and world.drain() == []
        row = store.agent_thread_queue_item(queued.queue_id)
        assert row.deferred_at is not None and row.refusal_reason is None
        later = datetime.fromisoformat(store.now()) + timedelta(
            seconds=AGENT_HANDOFF_DELIVERY_TIMEOUT_SECONDS
        )
        monkeypatch.setattr(store, "now", lambda: later.isoformat())
    assert world.drain() == []
    row = store.agent_thread_queue_item(queued.queue_id)
    assert row.operation_id is None and row.refusal_reason
    if case == "deferred":
        assert row.refusal_reason.startswith("delivery_timed_out")
    assert store.agent_tasks(world.project_id) == []


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
    store.queue_agent_thread_message(
        queue_id="queued",
        thread_id=target.thread_id,
        user_id=member.user_id,
        idempotency_key="key",
        message="Next",
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
        assert connection.execute("SELECT count(*) FROM agent_thread_queue").fetchone()[0] == 0
    assert store.agent_thread(target.thread_id) is not None


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


def test_only_the_thread_members_own_turn_gets_thread_grants(manifest, tmp_path: Path) -> None:
    """Memory, projects and schedules share one binding: a turn on another member's
    thread, or of the wrong kind or scope, is not the thread's turn."""
    import uuid

    from rcp.agents import AgentProcessControl
    from rcp.agents.memory_files import task_member_memory_owner
    from rcp.background import AgentTaskExecution
    from rcp.core.models import AuthorizedHuman
    from rcp.runs.questions import work_command_handler
    from tests.helpers import authorized_human, create_named_app
    from tests.test_storage import _task_record

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store, catalog = app.state.background_tasks.store, app.state.catalog
    actor = authorized_human(store)
    stranger = AuthorizedHuman(space_id=store.space_id, user_id=str(uuid.uuid4()), display_name="S")
    project_id = app.state.default_project_id
    thread = next(
        row
        for row in store.ensure_agent_threads(actor.user_id, [project_id], default_names=("Pip",))
        if row.kind == "project"
    )
    service = catalog.open_execution(project_id)
    request = {"chat_id": thread.thread_id, "chat_scope": "project", "mode": "work"}
    turns = {
        "own": (actor, "project_chat", project_id),
        "stranger": (stranger, "project_chat", project_id),
        "kind": (actor, "space_chat", store.space_id),
    }
    for operation_id, (human, kind, scope) in turns.items():
        store.create_agent_task(
            _task_record(
                store.now(),
                operation_id=operation_id,
                project_id=scope,
                kind=kind,
                status="succeeded",
                finished_at=store.now(),
                authorized_by=human,
                request=request,
            )
        )
    for operation_id in turns:
        task = store.agent_task(operation_id)
        verbs = work_command_handler(
            AgentTaskExecution(operation_id, store, AgentProcessControl()), None, service=service
        ).allowed_verbs
        own = operation_id == "own"
        assert (task_member_memory_owner(store, task) == actor.user_id) is own
        assert ("projects" in verbs, "schedule" in verbs) == (own, own)


def test_every_verb_a_thread_turn_can_be_granted_has_a_capability_line(monkeypatch) -> None:
    from types import SimpleNamespace

    from rcp.agents.graph_read_scope import GraphReadScope
    from rcp.runs import agent_names, agent_schedules, approval_cards, questions
    from rcp.runs.agent_thread_commands import _THREAD_VERBS, thread_capabilities
    from rcp.runs.tasks.compute_commands import WorkComputeCommands

    assert all(verb.capability for verb in _THREAD_VERBS.values())
    task = SimpleNamespace(
        dispatch_authority=SimpleNamespace(task_contract="work_auto"),
        request={"chat_id": "thread"},
    )
    store = SimpleNamespace(
        consolidation_run_for_operation=lambda _: None,
        agent_task=lambda _: task,
        bulletin_enabled_for_task=lambda _: True,
    )
    monkeypatch.setattr(agent_names, "resolve_agent_name_state", lambda _: {"can_self_name": True})
    monkeypatch.setattr(agent_schedules, "schedule_thread", lambda *_: {})
    monkeypatch.setattr(questions, "bound_thread", lambda *_: object())
    monkeypatch.setattr(approval_cards, "approval_card_member", lambda *_: None)
    monkeypatch.setattr(questions, "work_ask_authorized", lambda _: True)
    monkeypatch.setattr(questions, "work_experiment_allowed", lambda _: True)
    monkeypatch.setattr(questions, "member_work_chat_turn", lambda _: True)
    compute = SimpleNamespace(
        allowed_verbs=WorkComputeCommands.allowed_verbs.fget(
            SimpleNamespace(helper_backend=object())
        )
    )
    handler = questions.work_command_handler(
        SimpleNamespace(operation_id="turn", store=store),  # pyright: ignore[reportArgumentType]
        compute,  # pyright: ignore[reportArgumentType]
        service=SimpleNamespace(project_catalog=object()),  # pyright: ignore[reportArgumentType]
        graph_read_scope=GraphReadScope.model_construct(
            verbs=GraphReadScope.model_fields["verbs"].default
        ),
    )
    assert handler.allowed_verbs == set(questions.WORK_THREAD_VERB_CAPABILITIES)
    space, project = thread_capabilities("space"), thread_capabilities("project")
    assert space and project and space != project
    assert len(set(project)) == len(project)


def test_the_handoff_usage_names_every_project_agent_capability() -> None:
    from rcp.runs.agent_thread_commands import _handoff_usages, thread_capabilities

    (usage,) = _handoff_usages()
    assert all(line in usage for line in thread_capabilities("project"))


def test_a_refused_merge_in_a_member_thread_points_to_the_approval_card() -> None:
    import uuid

    from rcp.agents.command_protocol import MergeCommandRequest
    from rcp.core.models import AuthorizedHuman
    from rcp.runs.questions import WorkCommandHandler

    request = MergeCommandRequest.model_construct(request_id=uuid.uuid4().hex, verb="merge")
    member = AuthorizedHuman(
        space_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()), display_name="M"
    )
    carded = WorkCommandHandler(None, None, frozenset({"ask"}), approval_card_member=member)
    plain = WorkCommandHandler(None, None, frozenset({"ask"}))
    identity = None
    assert carded(request, identity).result == {"code": "approval_card_required"}  # pyright: ignore[reportArgumentType]
    assert plain(request, identity).result == {}  # pyright: ignore[reportArgumentType]


def test_a_queued_message_waits_out_a_racing_turn_and_its_deferral_restarts(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    from rcp.runs.provider_login import MachineDependenciesMissing
    from rcp.service import RunRequest
    from tests.test_storage import _task_record

    world = _queue_world(manifest, tmp_path, monkeypatch)
    store = world.store
    queued = world.queue("key", "Next")

    def missing(*args, **kwargs):
        raise MachineDependenciesMissing("codex is missing")

    def racing_turn(*args, **kwargs):
        store.create_agent_task(
            _task_record(
                store.now(),
                operation_id="racer",
                project_id=world.project_id,
                kind="project_chat",
                status="running",
                authorized_by=world.actor,
                request=RunRequest(
                    chat_scope="project", chat_id=world.thread.thread_id, mode="work"
                ).model_dump(mode="json"),
            )
        )
        raise ValueError("This conversation already has an active or paused turn.")

    monkeypatch.setattr(world.background, "admit_provider_task", missing)
    world.drain()
    assert store.agent_thread_queue_item(queued.queue_id).deferred_at is not None
    monkeypatch.setattr(world.background, "admit_provider_task", racing_turn)
    assert world.drain() == []
    row = store.agent_thread_queue_item(queued.queue_id)
    assert (row.operation_id, row.refusal_reason) == (None, None)
    world.drain()
    assert store.agent_thread_queue_item(queued.queue_id).deferred_at is None
