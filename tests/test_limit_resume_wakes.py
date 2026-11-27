from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rcp.agents import AgentLauncher
from rcp.background import BackgroundAgentTasks
from rcp.core.models import AuthorizedHuman
from rcp.keyed_locks import ExperimentAdmission, KeyedLocks
from rcp.projects import ProjectCatalog
from rcp.runs import provider_login
from rcp.storage import AgentTaskRecord, AppStore
from tests.lifecycles.agent_task import seeded_store


@pytest.fixture
def limited(tmp_path, monkeypatch):
    store = seeded_store(tmp_path)
    project = store.project("project")
    assert project is not None
    with store.connection() as connection:
        connection.execute(
            "UPDATE projects SET project_id=? WHERE project_id=?",
            ("00000000-0000-4000-8000-000000000123", "project"),
        )
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr(store, "now", lambda: clock[0].isoformat())
    member = store.local_owner
    assert member is not None
    store.rename_space_user(member.user_id, "Owner")
    store.seat_project_member("00000000-0000-4000-8000-000000000123", member.user_id)
    human = AuthorizedHuman(space_id=store.space_id, user_id=member.user_id, display_name="Owner")
    task = AgentTaskRecord(
        operation_id="limited",
        project_id="00000000-0000-4000-8000-000000000123",
        kind="project_chat",
        status="failed",
        request={"provider": "codex", "chat_id": "chat"},
        created_at=store.now(),
        updated_at=store.now(),
        finished_at=store.now(),
        status_message="",
        error="usage limit",
        native_session_id="session",
        authorized_by=human,
    )
    store.create_agent_task(task)
    store.record_agent_task_receipt(
        task.operation_id,
        "provider_limit_reset",
        {
            "resets_at": (clock[0] + timedelta(seconds=60)).isoformat(),
            "estimated": False,
        },
    )

    async def stream(*_args):
        yield ""

    background = BackgroundAgentTasks(store, stream)
    calls = []

    def resume(previous, authorized_by, **kwargs):
        calls.append(previous.operation_id)
        child = previous.model_copy(
            update={
                "operation_id": f"child-{len(calls)}",
                "parent_operation_id": previous.operation_id,
                "status": "succeeded",
                "created_at": store.now(),
                "updated_at": store.now(),
            }
        )
        store.create_agent_task(child, continuation_cause="resume")
        store.record_agent_task_receipt(child.operation_id, kwargs["marker"], {})
        return child

    monkeypatch.setattr("rcp.api.tasks.resume_task", resume)

    catalog = ProjectCatalog(tmp_path, store, AgentLauncher())
    locks = KeyedLocks()

    def tick():
        provider_login.resume_limited_turns(
            background,
            catalog=catalog,
            experiment_admission=ExperimentAdmission(locks, lambda _: None),
            operation_lock=locks,
        )

    return store, clock, task, calls, tick, background


def test_wake_at_reset_once_after_restart_and_login_clear(limited, monkeypatch):
    store, clock, task, calls, tick, background = limited
    tick()
    assert calls == []
    clock[0] += timedelta(seconds=60)
    reopened = AppStore(store.path)
    monkeypatch.setattr(reopened, "now", store.now)
    background.store = reopened
    tick()
    tick()
    assert calls == [task.operation_id], store.agent_task_receipts(task.operation_id)


@pytest.mark.parametrize("reason", ["old", "switch", "declined", "episode", "later", "active"])
def test_ineligible_turns_stay_failed(limited, reason):
    store, clock, task, calls, tick, _ = limited
    clock[0] += timedelta(seconds=60)
    with store.connection() as connection:
        if reason == "old":
            connection.execute(
                "DELETE FROM graph_run_receipts WHERE category='provider_limit_reset'"
            )
        elif reason == "switch":
            connection.execute("UPDATE space_users SET auto_resume_after_limits=0")
        elif reason == "episode":
            connection.execute("UPDATE graph_runs SET episode_id='episode'")
    if reason == "declined":
        store.record_agent_task_receipt(
            task.operation_id, "limit_auto_resume_stopped", {"reason": "declined"}
        )
    if reason in {"later", "active"}:
        store.create_agent_task(
            task.model_copy(
                update={
                    "operation_id": "later",
                    "status": "running" if reason == "active" else "succeeded",
                }
            )
        )
    tick()
    assert calls == []


@pytest.mark.parametrize("error", [provider_login.ProviderLoginsLimited, OSError])
def test_transient_failure_defers_then_retries(limited, monkeypatch, error):
    store, clock, task, calls, tick, _ = limited
    original = __import__("rcp.api.tasks", fromlist=["resume_task"]).resume_task

    def refuse(*args, **kwargs):
        raise error("waiting")

    monkeypatch.setattr("rcp.api.tasks.resume_task", refuse)
    clock[0] += timedelta(seconds=60)
    tick()
    assert store.agent_task_has_receipt(task.operation_id, "limit_auto_resume_deferred"), (
        store.agent_task_receipts(task.operation_id)
    )
    monkeypatch.setattr("rcp.api.tasks.resume_task", original)
    tick()
    assert not calls
    clock[0] += timedelta(seconds=60)
    tick()
    assert calls == [task.operation_id], store.agent_task_receipts(task.operation_id)


@pytest.mark.parametrize("reason", ["signed_out", "member", "session", "cap", "lineage"])
def test_permanent_refusal_and_caps_stop(limited, monkeypatch, reason):
    store, clock, task, calls, tick, _ = limited
    clock[0] += timedelta(seconds=60)
    if reason == "signed_out":

        def refuse(*args, **kwargs):
            raise provider_login.ProviderSignedOut("signed out")

        monkeypatch.setattr("rcp.api.tasks.resume_task", refuse)
    elif reason == "member":
        with store.connection() as connection:
            connection.execute("DELETE FROM project_members")
    elif reason == "session":
        with store.connection() as connection:
            connection.execute("UPDATE graph_runs SET native_session_id=NULL")
    elif reason == "cap":
        for _ in range(120):
            store.record_agent_task_receipt(task.operation_id, "diagnostic", {})
        for _ in range(59):
            store.record_agent_task_receipt(
                task.operation_id, "limit_auto_resume_deferred", {"reason": "busy"}
            )
        clock[0] += timedelta(seconds=60)

        def refuse(*args, **kwargs):
            raise provider_login.ProviderLoginsLimited("busy")

        monkeypatch.setattr("rcp.api.tasks.resume_task", refuse)
    else:
        for operation_id, parent in [("grandparent", None), ("parent", "grandparent")]:
            store.create_agent_task(
                task.model_copy(
                    update={
                        "operation_id": operation_id,
                        "parent_operation_id": parent,
                        "request": {"provider": "codex", "chat_id": operation_id},
                    }
                )
            )
            store.record_agent_task_receipt(operation_id, "limit_auto_resume", {})
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET parent_operation_id='parent' WHERE operation_id='limited'"
            )
        store.record_agent_task_receipt(task.operation_id, "limit_auto_resume", {})
    tick()
    assert store.agent_task_has_receipt(task.operation_id, "limit_auto_resume_stopped")
    assert not calls
    tick()
    assert (
        len(store.agent_task_receipts_by_category(task.operation_id, "limit_auto_resume_deferred"))
        <= 60
    )


@pytest.mark.parametrize("attempted", [False, True])
def test_restart_preserves_only_proven_unstarted_auto_resume(limited, monkeypatch, attempted):
    store, clock, task, calls, tick, background = limited
    child = store.create_agent_task(
        task.model_copy(
            update={
                "operation_id": "committed-child",
                "parent_operation_id": task.operation_id,
                "status": "queued",
                "finished_at": None,
            }
        ),
        continuation_cause="resume",
        marker="limit_auto_resume",
    )
    if attempted:
        store.record_agent_task_receipt(
            child.operation_id,
            "operation_dispatch_attempt",
            {
                "dispatch_attempt_id": "ambiguous",
            },
        )
    launched = []

    def launch(operation_id):
        launched.append(operation_id)
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status='running' WHERE operation_id=?", (operation_id,)
            )
        return store.agent_task(operation_id)

    monkeypatch.setattr(background, "launch_admitted", launch)
    monkeypatch.setattr(background, "_schedule_remote_reconciliation", lambda **_: None)
    background.recover_at_startup()
    clock[0] += timedelta(seconds=60)
    tick()
    tick()
    assert launched == ([] if attempted else [child.operation_id])
    assert store.agent_task(child.operation_id).status == (
        "interrupted" if attempted else "running"
    )
    assert calls == []
