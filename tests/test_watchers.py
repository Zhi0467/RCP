from __future__ import annotations

import shlex
import subprocess
import uuid

import pytest
from pydantic import ValidationError

from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    WatcherClaimConflict,
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
            "node_id": "exp-one",
            "provider": "codex",
            "model": "gpt-5",
            "reasoning": "medium",
            "run_on": "laptop",
            "run_truth_scope": ["state"],
            "mode": "work",
            "trigger": "watcher",
            "patch_kind": "work",
            "control_node_id": None,
            "workflow_ids": [],
            "skill_ids": [],
            "invoked_workflow_ids": [],
            "invoked_skill_ids": [],
            "resolved_skill_packages": [],
            "watcher_ids": watcher_ids,
        },
        created_at=now,
        updated_at=now,
        status_message="Queued watcher wake.",
    )


def _loop_task(
    store: AppStore,
    operation_id: str,
    *,
    episode_id: str,
    invocation: int,
    ceiling: int = 2,
    watcher_ids: list[str] | None = None,
    parent_operation_id: str | None = None,
) -> AgentTaskRecord:
    now = store.now()
    return AgentTaskRecord(
        operation_id=operation_id,
        project_id="project",
        kind="node_chat",
        status="queued",
        request={
            "chat_id": "chat",
            "node_id": "exp-one",
            "provider": "codex",
            "model": "gpt-5",
            "reasoning": "medium",
            "run_on": "laptop",
            "run_truth_scope": ["state"],
            "mode": "work",
            "trigger": "watcher" if watcher_ids else "experiment_run",
            "patch_kind": "experiment_loop",
            "control_node_id": "exp-one",
            "control_revision": 0,
            "control_episode_id": episode_id,
            "control_invocation": invocation,
            "control_invocation_ceiling": ceiling,
            "control_decision_bundle": [],
            "control_completion_criteria": [],
            "workflow_ids": [],
            "skill_ids": [],
            "invoked_workflow_ids": [],
            "invoked_skill_ids": [],
            "resolved_skill_packages": [],
            "watcher_ids": watcher_ids or [],
        },
        created_at=now,
        updated_at=now,
        status_message="Queued loop invocation.",
        parent_operation_id=parent_operation_id,
        attempt=2 if parent_operation_id else 1,
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
                update={
                    "patch_kind": "experiment_loop",
                    "control_node_id": "exp-one",
                    "control_episode_id": str(uuid.uuid4()),
                    "control_invocation": 1,
                    "control_invocation_ceiling": 2,
                }
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
                update={
                    "patch_kind": "experiment_loop",
                    "control_node_id": "exp/one",
                    "control_episode_id": str(uuid.uuid4()),
                    "control_invocation": 1,
                    "control_invocation_ceiling": 2,
                }
            )
        }
    )
    store.create_watchers([bound])
    store.create_watchers([_record("watch-plain")])

    assert store.experiment_watcher_ids("project", "exp/one") == ["watch-bound"]
    assert store.experiment_watcher_ids("project", "exp/other") == []

    store.stop_watchers("project", ["watch-bound"])
    assert store.experiment_watcher_ids("project", "exp/one") == []


def test_loop_root_invocations_are_sequential_and_recovery_preserves_binding(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    episode_id = str(uuid.uuid4())
    first = _loop_task(store, "first", episode_id=episode_id, invocation=1, ceiling=4)
    store.create_agent_task(first)
    store.fail_agent_task("first", "provider failed")

    changed = _loop_task(
        store,
        "bad-retry",
        episode_id=str(uuid.uuid4()),
        invocation=1,
        ceiling=4,
        parent_operation_id="first",
    )
    with pytest.raises(ValueError, match="preserve its control binding"):
        store.create_agent_task(changed)

    recovery = _loop_task(
        store,
        "retry",
        episode_id=episode_id,
        invocation=1,
        ceiling=4,
        parent_operation_id="first",
    )
    store.create_agent_task(recovery)
    runtime = store.experiment_loop_runtime("project", "exp-one")
    assert runtime.invocations_used == 1
    assert runtime.episode_id == episode_id
    store.complete_agent_task("retry", applied_revision=None, result={})

    skipped = _loop_task(store, "third", episode_id=episode_id, invocation=3, ceiling=4)
    skipped.request["trigger"] = "watcher"
    with pytest.raises(ValueError, match="out of sequence; expected 2"):
        store.create_agent_task(skipped)


def test_ceiling_keeps_completion_pending_until_new_episode_claims_it(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    old_episode = str(uuid.uuid4())
    first = _loop_task(store, "first", episode_id=old_episode, invocation=1, ceiling=1)
    store.create_agent_task(first)
    store.complete_agent_task("first", applied_revision=None, result={})
    watcher = _record("done", status="completed").model_copy(
        update={
            "continuation": _continuation().model_copy(
                update={
                    "patch_kind": "experiment_loop",
                    "control_node_id": "exp-one",
                    "control_episode_id": old_episode,
                    "control_invocation": 1,
                    "control_invocation_ceiling": 1,
                }
            )
        }
    )
    store.create_watchers([watcher])

    over_budget = _loop_task(
        store,
        "over-budget",
        episode_id=old_episode,
        invocation=2,
        ceiling=1,
        watcher_ids=["done"],
    )
    with pytest.raises(ValueError, match="exceeds its pinned ceiling"):
        store.create_watcher_notification_task(over_budget, ["done"])
    assert store.watcher("done").notified is False

    new_episode = str(uuid.uuid4())
    claimed = _loop_task(
        store,
        "claimed",
        episode_id=new_episode,
        invocation=1,
        ceiling=1,
        watcher_ids=["done"],
    )
    claimed.request["trigger"] = "experiment_run"
    stored = store.create_watcher_notification_task(claimed, ["done"])

    assert stored is not None
    assert stored.request["control_episode_id"] == new_episode
    assert store.watcher("done").continuation.control_episode_id == old_episode
    assert store.watcher("done").notified is True


def test_runtime_distinguishes_detached_work_from_a_pending_completion_at_ceiling(
    tmp_path,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    episode_id = str(uuid.uuid4())
    first = _loop_task(store, "first", episode_id=episode_id, invocation=1, ceiling=1)
    store.create_agent_task(first)
    store.complete_agent_task("first", applied_revision=None, result={})
    watcher = _record("bounded-work").model_copy(
        update={
            "continuation": _continuation().model_copy(
                update={
                    "patch_kind": "experiment_loop",
                    "control_node_id": "exp-one",
                    "control_episode_id": episode_id,
                    "control_invocation": 1,
                    "control_invocation_ceiling": 1,
                }
            )
        }
    )
    store.create_watchers([watcher])

    running = store.experiment_loop_runtime("project", "exp-one")
    assert running.detached_work_active is True
    assert running.watcher_completion_pending is False
    assert running.paused is True

    store.record_watcher_check(
        "bounded-work",
        status="completed",
        exit_code=0,
        error=None,
    )
    completed = store.experiment_loop_runtime("project", "exp-one")
    assert completed.detached_work_active is False
    assert completed.watcher_completion_pending is True
    assert completed.paused is True


def test_new_episode_adopts_remaining_watchers_without_mutating_their_origin(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    old_episode = str(uuid.uuid4())
    watcher = _record("still-running").model_copy(
        update={
            "continuation": _continuation().model_copy(
                update={
                    "patch_kind": "experiment_loop",
                    "control_node_id": "exp-one",
                    "control_revision": 1,
                    "control_episode_id": old_episode,
                    "control_invocation": 1,
                    "control_invocation_ceiling": 1,
                }
            )
        }
    )
    store.create_watchers([watcher])
    new_episode = str(uuid.uuid4())
    root = _loop_task(
        store,
        "reauthorized",
        episode_id=new_episode,
        invocation=1,
        ceiling=3,
    )
    store.create_agent_task(root)
    store.complete_agent_task("reauthorized", applied_revision=None, result={})

    runtime = store.experiment_loop_runtime("project", "exp-one")

    assert runtime.episode_id == new_episode
    assert runtime.invocations_used == 1
    assert runtime.active is True
    assert store.watcher("still-running").continuation.control_episode_id == old_episode


def test_exit_receipt_on_recovery_child_requires_a_new_human_episode(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    episode_id = str(uuid.uuid4())
    root = _loop_task(store, "root", episode_id=episode_id, invocation=1, ceiling=3)
    store.create_agent_task(root)
    store.fail_agent_task("root", "provider failed")
    child = _loop_task(
        store,
        "repair",
        episode_id=episode_id,
        invocation=1,
        ceiling=3,
        parent_operation_id="root",
    )
    store.create_agent_task(child)
    store.complete_agent_task("repair", applied_revision=4, result={})
    store.record_agent_task_receipt(
        "repair",
        "experiment_loop_exit",
        {"episode_id": episode_id, "invocation": 1},
    )
    watcher = _record("pending", status="completed").model_copy(
        update={
            "continuation": _continuation().model_copy(
                update={
                    "patch_kind": "experiment_loop",
                    "control_node_id": "exp-one",
                    "control_revision": 0,
                    "control_episode_id": episode_id,
                    "control_invocation": 1,
                    "control_invocation_ceiling": 3,
                }
            )
        }
    )
    store.create_watchers([watcher])

    runtime = store.experiment_loop_runtime("project", "exp-one")

    assert runtime.episode_exited is True
    assert runtime.active is False
    assert runtime.paused is False


def test_operational_recovery_rejects_siblings_stale_roots_and_successful_tasks(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    episode_id = str(uuid.uuid4())
    root = _loop_task(store, "root", episode_id=episode_id, invocation=1, ceiling=3)
    store.create_agent_task(root)
    store.fail_agent_task("root", "failed")
    child = _loop_task(
        store,
        "child",
        episode_id=episode_id,
        invocation=1,
        ceiling=3,
        parent_operation_id="root",
    )
    store.create_agent_task(child)
    store.fail_agent_task("child", "failed again")

    sibling = child.model_copy(update={"operation_id": "sibling", "parent_operation_id": "root"})
    with pytest.raises(ValueError, match="already has a recovery child"):
        store.create_agent_task(sibling)

    new_episode = str(uuid.uuid4())
    newer_root = _loop_task(
        store,
        "new-root",
        episode_id=new_episode,
        invocation=1,
        ceiling=3,
    )
    store.create_agent_task(newer_root)
    store.fail_agent_task("new-root", "failed")
    stale = _loop_task(
        store,
        "stale",
        episode_id=episode_id,
        invocation=1,
        ceiling=3,
        parent_operation_id="child",
    ).model_copy(update={"attempt": 3})
    with pytest.raises(ValueError, match="newest loop episode and invocation"):
        store.create_agent_task(stale)

    successful_episode = str(uuid.uuid4())
    successful = _loop_task(
        store,
        "successful",
        episode_id=successful_episode,
        invocation=1,
        ceiling=3,
    )
    store.create_agent_task(successful)
    store.complete_agent_task("successful", applied_revision=None, result={})
    invalid_retry = _loop_task(
        store,
        "retry-success",
        episode_id=successful_episode,
        invocation=1,
        ceiling=3,
        parent_operation_id="successful",
    )
    with pytest.raises(ValueError, match="latest unresolved loop task"):
        store.create_agent_task(invalid_retry)


def test_patch_only_graph_repair_is_not_treated_as_operational_recovery(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    episode_id = str(uuid.uuid4())
    root = _loop_task(store, "root", episode_id=episode_id, invocation=1)
    store.create_agent_task(root)
    store.complete_agent_task(
        "root",
        applied_revision=None,
        result={
            "graph_update": {
                "status": "rejected",
                "repairable": False,
            }
        },
    )
    repair = _loop_task(
        store,
        "repair",
        episode_id=episode_id,
        invocation=1,
        parent_operation_id="root",
    )

    stored = store.create_agent_task(repair)

    assert stored.parent_operation_id == "root"
    assert stored.request["control_invocation"] == 1


def test_experiment_groups_coalesce_across_origin_episode_provenance(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    records = []
    for watcher_id, revision, invocation, ceiling in (
        ("old", 1, 1, 2),
        ("new", 9, 3, 5),
    ):
        record = _record(watcher_id, status="completed")
        records.append(
            record.model_copy(
                update={
                    "continuation": record.continuation.model_copy(
                        update={
                            "patch_kind": "experiment_loop",
                            "control_node_id": "exp-one",
                            "control_revision": revision,
                            "control_episode_id": str(uuid.uuid4()),
                            "control_invocation": invocation,
                            "control_invocation_ceiling": ceiling,
                        }
                    )
                }
            )
        )
    for record in records:
        store.create_watchers([record])

    groups = store.completed_watcher_groups()

    assert [[item.watcher_id for item in group] for group in groups] == [["new", "old"]]


def test_notification_claim_rejects_forged_scope_without_consuming_watchers(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.create_watchers([_record("done", status="completed")])
    forged = _task(store, "forged", ["done"])
    forged.request["provider"] = "claude"

    with pytest.raises(ValueError, match="immutable delivery policy"):
        store.create_watcher_notification_task(forged, ["done"])

    assert store.watcher("done").notified is False
    assert store.agent_task("forged") is None


def test_stop_acknowledges_pending_completion_and_conflicts_after_claim(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.create_watchers([_record("pending", status="completed")])

    stopped = store.stop_watchers("project", ["pending"])

    assert stopped[0].status == "stopped"
    assert stopped[0].notified is True
    assert store.completed_watcher_groups() == []

    store.create_watchers([_record("claimed", status="completed")])
    assert store.create_watcher_notification_task(
        _task(store, "delivery", ["claimed"]), ["claimed"]
    )
    with pytest.raises(WatcherClaimConflict, match="already claimed"):
        store.stop_watchers("project", ["claimed"])


def test_poller_isolates_completion_callback_failures_between_groups(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    first = _record("first", status="completed")
    second = _record("second", status="completed").model_copy(
        update={"continuation": _continuation().model_copy(update={"model": "other"})}
    )
    store.create_watchers([first])
    store.create_watchers([second])
    called: list[str] = []

    def callback(group: list[WatcherRecord]) -> None:
        called.append(group[0].watcher_id)
        if group[0].watcher_id == "first":
            raise RuntimeError("one bad group")

    WatcherPoller(store, on_completed=callback).poll_once()

    assert called == ["first", "second"]
