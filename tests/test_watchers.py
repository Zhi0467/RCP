from __future__ import annotations

import shlex
import subprocess

import pytest
from pydantic import ValidationError

from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    WatcherContinuation,
    WatcherRecord,
)
from rcp.watchers import (
    WatcherBinding,
    WatcherCheckResult,
    WatcherInitialCheckError,
    WatcherPoller,
    WatchSpec,
    arm_watchers,
    parse_watch_json,
    run_watcher_check,
)


def _continuation() -> WatcherContinuation:
    return WatcherContinuation(
        provider="codex",
        model="gpt-5",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["state"],
        patch_kind="work",
    )


def _binding(origin: str = "origin") -> WatcherBinding:
    return WatcherBinding(
        project_id="project",
        origin_operation_id=origin,
        origin_task_kind="node_chat",
        chat_id="chat",
        node_id="exp-one",
        continuation=_continuation(),
    )


def _record(
    watcher_id: str,
    *,
    origin: str = "origin",
    status: str = "active",
) -> WatcherRecord:
    return WatcherRecord(
        watcher_id=watcher_id,
        project_id="project",
        origin_operation_id=origin,
        origin_task_kind="node_chat",
        chat_id="chat",
        node_id="exp-one",
        check_command="true",
        log_path=f"/tmp/{watcher_id}.log",
        cwd="/tmp",
        continuation=_continuation(),
        status=status,
        created_at="2026-08-01T00:00:00+00:00",
        completed_at=("2026-08-01T00:01:00+00:00" if status == "completed" else None),
    )


def _task(store: AppStore, operation_id: str, watcher_ids: list[str]) -> AgentTaskRecord:
    now = store.now()
    return AgentTaskRecord(
        operation_id=operation_id,
        project_id="project",
        kind="node_chat",
        status="queued",
        request={
            "chat_id": "chat",
            "mode": "work",
            "trigger": "watcher",
            "watcher_ids": watcher_ids,
        },
        created_at=now,
        updated_at=now,
        status_message="Queued watcher wake.",
    )


def test_watch_json_is_a_nonempty_strict_three_field_list() -> None:
    parsed = parse_watch_json(
        '[{"check_command":"squeue -h -j 4471 >/dev/null",'
        '"log_path":"/logs/4471.log","cwd":"/work"}]'
    )

    assert parsed == [
        WatchSpec(
            check_command="squeue -h -j 4471 >/dev/null",
            log_path="/logs/4471.log",
            cwd="/work",
        )
    ]
    for payload in (
        "[]",
        '{"check_command":"true","log_path":"/tmp/x","cwd":"/tmp"}',
        '[{"check_command":"true","log_path":"relative","cwd":"/tmp"}]',
        '[{"check_command":"true","log_path":"/tmp/x","cwd":"/tmp","host":"bad"}]',
    ):
        with pytest.raises(ValidationError):
            parse_watch_json(payload)


def test_check_runs_from_declared_cwd_and_uses_exit_table(tmp_path) -> None:
    cwd = str(tmp_path)
    common = {"log_path": str(tmp_path / "job.log"), "cwd": cwd}

    complete = run_watcher_check(
        WatchSpec(check_command=f'test "$PWD" = {shlex.quote(cwd)}', **common)
    )
    active = run_watcher_check(WatchSpec(check_command="exit 1", **common))
    error = run_watcher_check(WatchSpec(check_command="echo broken >&2; exit 9", **common))

    assert complete.state == "complete"
    assert complete.exit_code == 0
    assert active.state == "active"
    assert active.exit_code == 1
    assert error.state == "error"
    assert error.exit_code == 9
    assert error.error == "check exited with status 9: broken"


def test_check_has_a_hard_timeout(tmp_path) -> None:
    result = run_watcher_check(
        WatchSpec(
            check_command="sleep 1",
            log_path=str(tmp_path / "job.log"),
            cwd=str(tmp_path),
        ),
        timeout=0.01,
    )

    assert result.state == "error"
    assert result.exit_code is None
    assert result.error == "check timed out after 0.01 seconds"


def test_remote_check_uses_existing_ssh_login_shell(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr("rcp.watchers.subprocess.run", fake_run)

    result = run_watcher_check(
        WatchSpec(check_command="squeue -h -j 4471", log_path="/logs/job", cwd="/work/a b"),
        "gpu.example",
    )

    command = seen["command"]
    assert isinstance(command, list)
    assert command[0] == "ssh"
    assert command[-2] == "gpu.example"
    assert shlex.split(command[-1]) == [
        "bash",
        "-lic",
        "cd '/work/a b' && squeue -h -j 4471",
    ]
    assert seen["kwargs"]["cwd"] is None
    assert result.state == "active"


def test_initial_error_arms_none_then_corrected_list_persists_atomically(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    specs = [
        WatchSpec(check_command="one", log_path="/tmp/one.log", cwd="/tmp"),
        WatchSpec(check_command="two", log_path="/tmp/two.log", cwd="/tmp"),
    ]

    def one_bad(spec: WatchSpec, _host: str, _timeout: float) -> WatcherCheckResult:
        if spec.check_command == "two":
            return WatcherCheckResult(
                state="error",
                checked_at="2026-08-01T00:00:00+00:00",
                exit_code=2,
                error="check exited with status 2",
            )
        return WatcherCheckResult(
            state="active", checked_at="2026-08-01T00:00:00+00:00", exit_code=1
        )

    with pytest.raises(WatcherInitialCheckError, match="watcher 2"):
        arm_watchers(store, specs, _binding(), check_runner=one_bad)
    assert store.watchers("project") == []

    def corrected(spec: WatchSpec, _host: str, _timeout: float) -> WatcherCheckResult:
        return WatcherCheckResult(
            state="complete" if spec.check_command == "two" else "active",
            checked_at="2026-08-01T00:01:00+00:00",
            exit_code=0 if spec.check_command == "two" else 1,
        )

    records = arm_watchers(store, specs, _binding(), check_runner=corrected)

    assert len(records) == 2
    assert {record.status for record in records} == {"active", "completed"}
    reopened = AppStore(store.path)
    assert {record.watcher_id for record in reopened.watchers("project")} == {
        record.watcher_id for record in records
    }


def test_runtime_error_degrades_only_that_watcher_and_later_clears(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.create_watchers([_record("bad"), _record("done")])

    def first(spec: WatchSpec, _host: str, _timeout: float) -> WatcherCheckResult:
        if spec.log_path.endswith("bad.log"):
            return WatcherCheckResult(
                state="error",
                checked_at="2026-08-01T00:01:00+00:00",
                exit_code=255,
                error="ssh unavailable",
            )
        return WatcherCheckResult(
            state="complete", checked_at="2026-08-01T00:01:00+00:00", exit_code=0
        )

    groups = WatcherPoller(store, check_runner=first).poll_once()

    assert store.watcher("bad").status == "degraded"
    assert store.watcher("bad").last_error == "ssh unavailable"
    assert store.watcher("done").status == "completed"
    assert [[item.watcher_id for item in group] for group in groups] == [["done"]]

    def recovered(_spec: WatchSpec, _host: str, _timeout: float) -> WatcherCheckResult:
        return WatcherCheckResult(
            state="active", checked_at="2026-08-01T00:02:00+00:00", exit_code=1
        )

    WatcherPoller(store, check_runner=recovered).poll_once()

    assert store.watcher("bad").status == "active"
    assert store.watcher("bad").last_error is None


def test_completed_groups_do_not_merge_different_origin_policies(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.create_watchers([_record("one", status="completed")])
    different_policy = _record("two", status="completed").model_copy(
        update={
            "continuation": _continuation().model_copy(
                update={"patch_kind": "experiment_loop", "control_node_id": "exp-one"}
            )
        }
    )
    store.create_watchers([different_policy])

    groups = store.completed_watcher_groups()

    assert {tuple(item.watcher_id for item in group) for group in groups} == {("one",), ("two",)}


def test_completed_groups_merge_compatible_watchers_from_different_work_turns(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.create_watchers([_record("one", origin="work-one", status="completed")])
    store.create_watchers([_record("two", origin="work-two", status="completed")])

    groups = store.completed_watcher_groups()

    assert [[item.watcher_id for item in group] for group in groups] == [["one", "two"]]
    queued = store.create_watcher_notification_task(
        _task(store, "watcher-turn", ["one", "two"]), ["one", "two"]
    )
    assert queued is not None
    assert all(item.notified for item in store.watchers("project"))


def test_queue_and_notified_ledger_are_atomic_and_wait_behind_live_task(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.create_watchers([_record("one", status="completed"), _record("two", status="completed")])
    live = _task(store, "human-turn", [])
    store.create_agent_task(live)

    assert (
        store.create_watcher_notification_task(
            _task(store, "watcher-turn-blocked", ["one", "two"]), ["one", "two"]
        )
        is None
    )
    assert store.agent_task("watcher-turn-blocked") is None
    assert all(not item.notified for item in store.watchers("project"))

    store.fail_agent_task("human-turn", "done")
    queued = store.create_watcher_notification_task(
        _task(store, "watcher-turn", ["one", "two"]), ["one", "two"]
    )

    assert queued is not None
    assert queued.status == "queued"
    reopened = AppStore(store.path)
    assert reopened.agent_task("watcher-turn") is not None
    assert all(item.notified for item in reopened.watchers("project"))
    assert {item.notification_operation_id for item in reopened.watchers("project")} == {
        "watcher-turn"
    }


def test_a_human_release_takes_a_watcher_out_of_the_polling_set(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.create_watchers([_record("watch-live"), _record("watch-degraded")])
    store.record_watcher_check(
        "watch-degraded",
        status="degraded",
        exit_code=255,
        error="ssh: connect to host gpu01 port 22: No route to host",
    )
    assert {record.watcher_id for record in store.pollable_watchers()} == {
        "watch-live",
        "watch-degraded",
    }

    stopped = store.stop_watchers("project", ["watch-degraded"])

    assert [record.status for record in stopped] == ["stopped"]
    assert [record.watcher_id for record in store.pollable_watchers()] == ["watch-live"]
    # A stopped watcher is already accounted for, so it can never wake a turn.
    assert store.watcher("watch-degraded").notified is True
    assert store.completed_watcher_groups() == []


def test_experiment_watchers_are_found_by_the_loop_that_armed_them(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    bound = _record("watch-bound")
    bound = bound.model_copy(
        update={
            "continuation": bound.continuation.model_copy(
                update={"patch_kind": "experiment_loop", "control_node_id": "exp/one"}
            )
        }
    )
    store.create_watchers([bound])
    store.create_watchers([_record("watch-plain")])

    assert store.experiment_watcher_ids("project", "exp/one") == ["watch-bound"]
    assert store.experiment_watcher_ids("project", "exp/other") == []

    store.stop_watchers("project", ["watch-bound"])
    assert store.experiment_watcher_ids("project", "exp/one") == []
