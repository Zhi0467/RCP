from __future__ import annotations

import fcntl
import logging
import os
import shlex
import signal
import subprocess
import sys
from contextlib import suppress
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from rcp.background import BackgroundAgentTasks
from rcp.core.models import AuthorizedHuman
from rcp.limits import WATCHER_GROUP_DIAGNOSTIC_ERROR_COUNT
from rcp.runs.watcher_admission import start_plain_watcher_wake
from rcp.storage import AgentTaskRecord, AppStore, WatcherRecord, watcher_next_check_at
from rcp.storage.models import WatcherCheckRecord
from rcp.watcher_folder import WatcherFile, reconcile_watcher_files
from rcp.watchers import (
    AgentWatchSpec,
    GraphWatcherRetryRegistry,
    WatcherBinding,
    WatcherCheckResult,
    WatcherDelivery,
    WatcherInitialCheckError,
    WatcherPoller,
    WatchSpec,
    run_watcher_check,
)

from .helpers import fabricated_authorizer, fake_launch_admitted, wait_until


@pytest.fixture
def store(tmp_path) -> AppStore:
    return AppStore(tmp_path / "rcp.sqlite3")


def armed(
    store: AppStore,
    watcher_id: str,
    *,
    agent_id: str = "chat",
    origin_turn_id: str = "origin",
    project_id: str = "project",
    completed: bool = False,
    cwd: str = "/tmp",
    machine_id: str = "backend",
) -> WatcherRecord:
    """Arm one watcher with one external check, optionally already finished."""

    now = store.now()
    return store.arm_watcher(
        WatcherRecord(
            watcher_id=watcher_id,
            agent_id=agent_id,
            project_id=project_id,
            purpose="Job finishes",
            origin_turn_id=origin_turn_id,
            created_at=now,
            checks=[
                WatcherCheckRecord(
                    check_id=watcher_id,
                    watcher_id=watcher_id,
                    machine_id=machine_id,
                    definition="external",
                    check_command="true",
                    log_path=f"{cwd}/{watcher_id}.log",
                    cwd=cwd,
                    created_at=now,
                    completed_at=now if completed else None,
                )
            ],
        )
    )


def _binding() -> WatcherBinding:
    return WatcherBinding(
        project_id="project", agent_id="chat", origin_turn_id="origin", machine_id="backend"
    )


def _chat_turn(
    store: AppStore,
    operation_id: str,
    *,
    authorized_by: AuthorizedHuman | None = None,
    session_id: str | None = "chat-session",
    status: str = "succeeded",
    kind: str = "node_chat",
    **request: object,
) -> AgentTaskRecord:
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id="project",
            kind=kind,
            status="queued",
            request={
                "chat_id": "chat",
                "node_id": "exp-one" if kind == "node_chat" else None,
                "chat_scope": "node" if kind == "node_chat" else "project",
                "provider": "codex",
                "model": "gpt-5",
                "reasoning": "medium",
                "run_on": "backend",
                "run_truth_scope": ["state"],
                "mode": "work",
                "trigger": "human",
                "patch_kind": "work",
                "message": "Run the job.",
                **request,
            },
            created_at=now,
            updated_at=now,
            status_message="Queued.",
            authorized_by=authorized_by,
        )
    )
    if status == "queued":
        return store.agent_task(operation_id)
    store.mark_agent_task_running(operation_id)
    if session_id is not None:
        store.checkpoint_agent_task(
            operation_id,
            native_session_id=session_id,
            stage_machine_id="",
            stage_root=str(store.path.parent / "chat-stage"),
        )
    if status == "succeeded":
        store.complete_agent_task(operation_id, applied_revision=None, result={})
    return store.agent_task(operation_id)


def _delivery(store: AppStore, plain, owners=None) -> WatcherDelivery:
    return WatcherDelivery(
        store,
        retry=GraphWatcherRetryRegistry(),
        graph_project_service=lambda *_: None,
        episode_owners=owners or {},
        plain_wake=plain,
        state_unavailable=lambda _exc: False,
        task_graph_capable=lambda *_: False,
        task_experiment_episode_id=lambda _request: None,
        reconcile_experiment_episode=lambda *_, **__: None,
        reconcile_graph_boundaries=lambda *_, **__: None,
        logger=logging.getLogger("test"),
        launch_wait=lambda: 0,
    )


def _result(state: str, exit_code: int | None, error: str | None = None) -> WatcherCheckResult:
    return WatcherCheckResult(
        state=state, checked_at="2026-08-01T00:01:00+00:00", exit_code=exit_code, error=error
    )


def test_a_watcher_file_is_strict_and_has_exactly_one_definition() -> None:
    check = {"check_command": "squeue -h -j 4471 >/dev/null", "log_path": "/logs/4471.log"}
    parsed = WatcherFile.model_validate(
        {"purpose": "job finishes", "checks": [check | {"cwd": "/work"}]}
    )
    assert parsed.checks[0].cwd == "/work"
    cancellable = WatcherFile.model_validate(
        {
            "purpose": "job finishes",
            "checks": [check | {"cwd": "/work", "cancel_command": "scancel 1"}],
        }
    )
    assert cancellable.checks[0].cancel_command == "scancel 1"
    graph = WatcherFile.model_validate(
        {"purpose": "node resolves", "graph": {"node_id": "blk/waiting", "status_in": ["resolved"]}}
    )
    assert graph.graph is not None and graph.graph.node_id == "blk/waiting"
    for payload in (
        {"purpose": "job finishes"},
        {"purpose": "job finishes", "checks": []},
        {"purpose": "job finishes", "checks": [check | {"cwd": "relative"}]},
        {"purpose": "job finishes", "checks": [check | {"cwd": "/work", "host": "bad"}]},
        {
            "purpose": "job finishes",
            "checks": [check | {"cwd": "/work", "cancel_command": " "}],
        },
        {"purpose": " ", "checks": [check | {"cwd": "/work"}]},
        {
            "purpose": "both",
            "checks": [check | {"cwd": "/work"}],
            "graph": {"node_id": "blk/waiting", "status_in": ["resolved"]},
        },
        {"purpose": "job finishes", "graph": {"node_id": "blk/waiting", "standing": "accepted"}},
    ):
        with pytest.raises(ValidationError):
            WatcherFile.model_validate(payload)


def test_check_runs_from_declared_cwd_and_uses_exit_table(tmp_path) -> None:
    cwd = str(tmp_path)
    common = {"log_path": str(tmp_path / "job.log"), "cwd": cwd}

    complete = run_watcher_check(
        AgentWatchSpec(
            purpose="job finishes", check_command=f'test "$PWD" = {shlex.quote(cwd)}', **common
        )
    )
    active = run_watcher_check(
        AgentWatchSpec(purpose="job finishes", check_command="exit 1", **common)
    )
    error = run_watcher_check(
        AgentWatchSpec(purpose="job finishes", check_command="echo broken >&2; exit 9", **common)
    )

    assert (complete.state, complete.exit_code) == ("complete", 0)
    assert (active.state, active.exit_code) == ("active", 1)
    assert (error.state, error.exit_code) == ("error", 9)


def test_check_has_a_hard_timeout(tmp_path) -> None:
    result = run_watcher_check(
        AgentWatchSpec(
            purpose="job finishes",
            check_command="sleep 1",
            log_path=str(tmp_path / "job.log"),
            cwd=str(tmp_path),
        ),
        timeout=0.01,
    )

    assert result.state == "error"
    assert result.exit_code is None
    assert result.error


def test_remote_check_uses_existing_ssh_login_shell(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr("rcp.watchers.run_check_process", fake_run)

    result = run_watcher_check(
        AgentWatchSpec(
            purpose="job finishes",
            check_command="squeue -h -j 4471",
            log_path="/logs/job",
            cwd="/work/a b",
        ),
        "gpu.example",
    )

    command = seen["command"]
    assert isinstance(command, list)
    assert command[0] == "ssh"
    assert command[-2] == "gpu.example"
    remote_command = shlex.split(command[-1])
    assert remote_command[:2] == ["python3", "-c"]
    assert remote_command[3:] == ["/work/a b", "15", "squeue -h -j 4471"]
    assert seen["kwargs"]["cwd"] is None
    assert result.state == "active"


@pytest.mark.parametrize("remote", [False, True])
def test_timeout_kills_check_children_without_stopping_observed_job(tmp_path, monkeypatch, remote):
    """Runs the exact shipped SSH payload locally; the remote watchdog must clean up
    after the transport client times out.
    """
    if remote:

        def ssh_bridge(_host, command):
            return [
                sys.executable,
                "-c",
                "import subprocess, sys; "
                f"child = subprocess.Popen({shlex.split(command)!r}, start_new_session=True); "
                "sys.exit(child.wait())",
            ]

        monkeypatch.setattr("rcp.watchers.ssh_arguments", ssh_bridge)
    child_pid_path = tmp_path / "check-child.pid"
    child_lock_path = tmp_path / "check-child.lock"
    child_code = (
        "import fcntl, os, time; from pathlib import Path; "
        f"lease = open({str(child_lock_path)!r}, 'w'); "
        "fcntl.flock(lease, fcntl.LOCK_EX); "
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    check_command = shlex.join([sys.executable, "-c", child_code]) + " & wait"
    child_pid = None
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as observed:
        try:
            result = run_watcher_check(
                AgentWatchSpec(
                    purpose="job finishes",
                    check_command=check_command,
                    log_path=str(tmp_path / "log"),
                    cwd=str(tmp_path),
                ),
                "fixture-host" if remote else "",
                timeout=2,
            )
            assert result.state == "error"
            child_pid = int(child_pid_path.read_text())

            def child_stopped():
                """Process exit releases the lease before the zombie is reaped."""
                with child_lock_path.open("a") as lease:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return False
                    return True

            wait_until(child_stopped, detail="the timed-out check left its child running")
            assert observed.poll() is None
        finally:
            observed.kill()
            observed.wait()
            if child_pid is None and child_pid_path.exists():
                child_pid = int(child_pid_path.read_text())
            if child_pid is not None:
                with suppress(ProcessLookupError):
                    os.kill(child_pid, signal.SIGKILL)


def test_check_schedule_backs_off_on_errors_and_resets_after_a_healthy_check(store) -> None:
    armed(store, "scheduled")
    check = store.watcher("scheduled").checks[0]
    assert store.due_external_checks(as_of=check.created_at) == []
    assert [item.check_id for item in store.due_external_checks(as_of=check.next_check_at)] == [
        "scheduled"
    ]

    checked_at = "2026-08-01T00:00:00+00:00"
    for error_count, minutes in enumerate((2, 4, 8, 15, 30, 30), start=1):
        store.record_watcher_check(
            "scheduled", status="degraded", exit_code=255, error="ssh down", checked_at=checked_at
        )
        check = store.watcher("scheduled").checks[0]
        assert check.consecutive_error_count == error_count
        delay = datetime.fromisoformat(check.next_check_at) - datetime.fromisoformat(checked_at)
        assert minutes * 60 * 0.9 <= delay.total_seconds() <= minutes * 60 * 1.1
        checked_at = check.next_check_at
    early = (datetime.fromisoformat(checked_at) - timedelta(seconds=1)).isoformat()
    assert AppStore(store.path).due_external_checks(as_of=early) == []

    store.record_watcher_check(
        "scheduled", status="active", exit_code=1, error=None, checked_at=checked_at
    )
    healthy = store.watcher("scheduled").checks[0]
    assert healthy.consecutive_error_count == 0
    assert healthy.last_error is None
    assert healthy.next_check_at == watcher_next_check_at("scheduled", checked_at, 0)


def test_poll_records_each_check_then_hands_off_to_delivery_once(store) -> None:
    armed(store, "bad")
    armed(store, "done")
    handed_off: list[list[str]] = []

    def first(spec: WatchSpec, _host: str, _timeout: float) -> WatcherCheckResult:
        if spec.log_path.endswith("bad.log"):
            return _result("error", 255, "ssh unavailable")
        return _result("complete", 0)

    def hand_off() -> None:
        handed_off.append([item.watcher_id for item in store.ready_watchers()])
        raise RuntimeError("a failed delivery pass never breaks polling")

    poller = WatcherPoller(
        store, check_runner=first, on_poll_completed=hand_off, clock=lambda: "9999-01-01"
    )
    poller.poll_once()

    assert handed_off == [["done"]]
    bad = store.watcher("bad")
    assert bad.state == "check_failing"
    assert bad.checks[0].last_error == "ssh unavailable"
    assert store.watcher("done").state == "finished"

    poller.check_runner = lambda *_: _result("active", 1)
    poller.poll_once()

    assert store.watcher("bad").state == "watching"
    assert store.watcher("bad").checks[0].last_error is None
    assert [item.check_id for item in store.due_external_checks(as_of="9999-01-01")] == ["bad"]


def test_an_unobservable_check_still_wakes_its_agent(store) -> None:
    armed(store, "lost")
    for _ in range(WATCHER_GROUP_DIAGNOSTIC_ERROR_COUNT - 1):
        store.record_watcher_check("lost", status="degraded", exit_code=255, error="ssh down")
    assert store.ready_watchers() == []

    store.record_watcher_check("lost", status="degraded", exit_code=255, error="ssh down")

    (ready,) = store.ready_watchers()
    assert ready.watcher_id == "lost"
    assert ready.checks[0].unobservable


def test_initial_error_arms_none_then_corrected_folder_persists_atomically(store) -> None:
    _chat_turn(store, "origin")
    files = {
        f"{name}.json": WatcherFile.model_validate(
            {
                "purpose": "job finishes",
                "checks": [{"check_command": name, "log_path": f"/tmp/{name}.log", "cwd": "/tmp"}],
            }
        ).model_dump_json()
        for name in ("one", "two")
    }

    def one_bad(spec: WatchSpec, _host: str, _timeout: float) -> WatcherCheckResult:
        return (
            _result("error", 2, "exit 2") if spec.check_command == "two" else _result("active", 1)
        )

    with pytest.raises(WatcherInitialCheckError):
        reconcile_watcher_files(store, _binding(), {}, files, check_runner=one_bad)
    assert store.agent_watchers("chat") == []

    def corrected(spec: WatchSpec, _host: str, _timeout: float) -> WatcherCheckResult:
        return _result("complete", 0) if spec.check_command == "two" else _result("active", 1)

    records = reconcile_watcher_files(store, _binding(), {}, files, check_runner=corrected)

    assert {record.watcher_id: record.state for record in records} == {
        "one": "watching",
        "two": "finished",
    }
    assert {record.watcher_id for record in AppStore(store.path).agent_watchers("chat")} == {
        "one",
        "two",
    }


def test_plain_delivery_is_authorized_by_the_arming_turns_human(store) -> None:
    human = fabricated_authorizer("Researcher")
    _chat_turn(store, "origin", authorized_by=human)
    armed(store, "done", completed=True)
    calls = []

    _delivery(store, lambda *args: calls.append(args)).deliver_ready_watchers(source="test")

    ((agent_id, ready, authorized_by, fence),) = calls
    assert (agent_id, [item.watcher_id for item in ready], authorized_by, fence) == (
        "chat",
        ["done"],
        human,
        None,
    )


@pytest.mark.parametrize("case", ["missing", "unattributed", "conflicting"])
def test_unprovable_authorizer_closes_delivery_without_a_wake(store, case) -> None:
    if case == "unattributed":
        _chat_turn(store, "origin")
    elif case == "conflicting":
        _chat_turn(store, "origin", authorized_by=fabricated_authorizer("One"))
        _chat_turn(store, "other", authorized_by=fabricated_authorizer("Two"))
        armed(store, "second", origin_turn_id="other", completed=True)
    armed(store, "done", completed=True)
    calls = []

    _delivery(store, lambda *args: calls.append(args)).deliver_ready_watchers(source="test")

    assert calls == []
    assert store.ready_watchers() == []
    closed = store.agent_watchers("chat")
    assert closed and all(item.delivery_closed_at and item.cancel_reason for item in closed)


def test_thread_closure_cancels_its_watchers_and_refuses_later_wakes(store) -> None:
    human = fabricated_authorizer("Researcher")
    _chat_turn(store, "origin", authorized_by=human)
    armed(store, "live")
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_threads(thread_id,user_id,kind,project_id,created_at) "
            "VALUES('chat','member','project','project',?)",
            (store.now(),),
        )
    store.close_agent_thread("chat", reason="access_lost")
    assert store.watcher("live").cancelled_by == "rcp"

    armed(store, "late", completed=True)
    calls = []
    _delivery(store, lambda *args: calls.append(args)).deliver_ready_watchers(source="test")

    assert calls == []
    assert store.watcher("late").state == "cancelled"
    assert store.agent_watchers("chat", open_only=True) == []


def _task_count(store: AppStore) -> int:
    with store.connection() as connection:
        return connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0]


def _plain_tasks(store: AppStore) -> BackgroundAgentTasks:
    tasks = BackgroundAgentTasks(store, None)
    tasks.admit_provider_task = lambda *_args, **_kwargs: None
    fake_launch_admitted(pytest.MonkeyPatch(), tasks)
    return tasks


@pytest.mark.parametrize("kind", ["node_chat", "project_chat", "space_chat"])
def test_plain_wake_continues_the_newest_turn_and_claims_its_watchers_once(store, kind) -> None:
    human = fabricated_authorizer("Researcher")
    _chat_turn(store, "origin", kind=kind, authorized_by=human, session_id="old-session")
    newest = _chat_turn(
        store,
        "newest",
        kind=kind,
        authorized_by=human,
        session_id="new-session",
        provider="claude",
        model="m",
    )
    watcher = armed(store, "done", completed=True)
    tasks = _plain_tasks(store)

    wake = start_plain_watcher_wake(tasks, "chat", [watcher], human)

    assert wake is not None and wake.status == "queued"
    assert (wake.request["provider"], wake.request["model"]) == ("claude", "m")
    assert wake.kind == kind
    assert wake.request["trigger"] == "watcher"
    assert wake.request["watcher_ids"] == ["done"]
    assert wake.authorized_by == human
    assert wake.native_session_id == newest.native_session_id
    assert store.agent_task_continuation_cause(wake.operation_id) == "watcher_wake"
    assert store.watcher("done").wake_turn_id == wake.operation_id
    assert start_plain_watcher_wake(tasks, "chat", [watcher], human) is None


@pytest.mark.parametrize("blocker", ["live_turn", "shutdown"])
def test_plain_wake_waits_without_claiming(store, blocker) -> None:
    human = fabricated_authorizer("Researcher")
    _chat_turn(store, "origin", authorized_by=human)
    watcher = armed(store, "done", completed=True)
    tasks = _plain_tasks(store)
    if blocker == "live_turn":
        _chat_turn(store, "live", authorized_by=human, status="running")
    else:
        tasks.shutdown()
    before = _task_count(store)

    assert start_plain_watcher_wake(tasks, "chat", [watcher], human) is None

    assert _task_count(store) == before
    assert [item.watcher_id for item in store.ready_watchers()] == ["done"]


def test_episode_agents_wake_through_their_episode_owner(store, monkeypatch) -> None:
    armed(store, "done", completed=True)
    episode = SimpleNamespace(mode="experiment_loop")
    monkeypatch.setattr(store, "agent_episode", lambda agent_id: episode)
    owned, plain = [], []

    _delivery(
        store,
        lambda *args: plain.append(args),
        owners={"experiment_loop": lambda *args: owned.append(args)},
    ).deliver_ready_watchers(source="test")

    assert plain == []
    assert [(agent, found, [w.watcher_id for w in ready]) for agent, found, ready, _ in owned] == [
        ("chat", episode, ["done"])
    ]


def test_removing_every_check_cancels_the_watcher_instead_of_leaving_it_empty(store) -> None:
    watcher = armed(store, "pair")
    second = watcher.checks[0].model_copy(update={"check_id": "pair-second", "log_path": "/tmp/b"})
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        store.revise_watcher(
            connection,
            "pair",
            purpose=watcher.purpose,
            removed_check_ids=[],
            new_checks=[second],
            at=store.now(),
        )
    for check_id in ("pair", "pair-second"):
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            store.revise_watcher(
                connection,
                "pair",
                purpose=watcher.purpose,
                removed_check_ids=[check_id],
                new_checks=[],
                at=store.now(),
            )

    stopped = store.watcher("pair")
    assert stopped.state == "cancelled" and stopped.cancelled_by == "agent"
    assert store.agent_watchers("chat", open_only=True) == []
