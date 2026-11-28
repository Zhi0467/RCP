from __future__ import annotations

from pathlib import Path

import pytest

from rcp.agents.agent_owner import LimitResumeMemberGone, task_write_admission_as
from rcp.api.tasks import task_limit_reset
from rcp.keyed_locks import KeyedLocks
from rcp.storage import AgentTaskRecord, AppStore, ProjectRecord
from tests.test_auto_research_children_storage import _identity


def _limited(store: AppStore) -> AgentTaskRecord:
    identity = _identity(store)
    project_id = "00000000-0000-4000-8000-000000000555"
    store.upsert_project(
        ProjectRecord(
            project_id=project_id,
            locator=str(store.path.parent / "manifest.yaml"),
            name="Project",
            added_at=store.now(),
            state_machine_id="backend",
            state_path=str(store.path.parent / "state"),
        )
    )
    store.seat_project_member(project_id, identity.user_id)
    return store.create_agent_task(
        AgentTaskRecord(
            operation_id="limited",
            project_id=project_id,
            kind="project_chat",
            status="failed",
            request={"chat_id": "chat", "provider": "codex"},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="failed",
            authorized_by=identity,
        )
    )


def test_limit_row_requires_receipt_and_tracks_member_opt_out(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "state.sqlite3")
    task = _limited(store)
    assert task_limit_reset(store, task) is None
    store.record_agent_task_receipt(
        task.operation_id, "provider_terminal_error", {"classification": "session_limit"}
    )
    assert task_limit_reset(store, task) is None
    reset = "2030-01-01T00:00:00+00:00"
    store.record_agent_task_receipt(
        task.operation_id,
        "provider_terminal_error",
        {"classification": "session_limit", "resets_at": reset, "estimated": False},
    )
    assert task_limit_reset(store, task) == {
        "resets_at": reset,
        "estimated": False,
        "state": "waiting",
        "reason": None,
    }
    assert task.authorized_by is not None
    store.set_auto_resume_after_limits(task.authorized_by.user_id, False)
    disabled = task_limit_reset(store, task)
    assert disabled is not None and disabled["reason"] == "switch_off"
    store.record_agent_task_receipt(
        task.operation_id, "limit_auto_resume_stopped", {"reason": "declined"}
    )
    declined = task_limit_reset(store, task)
    assert declined is not None and declined["reason"] == "declined"
    assert task_limit_reset(store, task.model_copy(update={"status": "succeeded"})) is None


def test_limit_row_for_turn_that_never_auto_resumes_is_stopped(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "state.sqlite3")
    task = _limited(store)
    store.record_agent_task_receipt(
        task.operation_id,
        "provider_terminal_error",
        {
            "classification": "session_limit",
            "resets_at": "2030-01-01T00:00:00+00:00",
            "estimated": False,
        },
    )
    edit = task.model_copy(update={"kind": "artifact_edit"})
    limit = task_limit_reset(store, edit)
    assert limit is not None
    assert (limit["state"], limit["reason"]) == ("stopped", "not_resumable")


def test_request_free_admission_requires_current_member(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "state.sqlite3")
    task = _limited(store)
    assert task.authorized_by is not None
    with task_write_admission_as(
        task, task.authorized_by, store=store, operation_lock=KeyedLocks()
    ):
        pass
    departed = task.authorized_by.model_copy(
        update={"user_id": "00000000-0000-4000-8000-000000000123"}
    )
    with (
        pytest.raises(LimitResumeMemberGone),
        task_write_admission_as(task, departed, store=store, operation_lock=KeyedLocks()),
    ):
        pass


@pytest.mark.parametrize("restart", [False, True])
def test_resume_marker_is_present_before_worker_starts(
    tmp_path: Path, monkeypatch, restart
) -> None:
    from rcp.agents import AgentEvent
    from rcp.background import BackgroundAgentTasks
    from tests.helpers import fake_launch_admitted, wait_for_task
    from tests.test_background_policy_matrix import _chat_request, _record, _store

    store = _store(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    previous = _record(
        store,
        operation_id="resume-parent",
        kind="project_chat",
        request=_chat_request(),
        status="paused",
        native_session_id="owned-session",
        stage_root=str(stage),
    )
    seen = []

    async def stream(_project_id, _kind, _request, execution):
        seen.append(store.agent_task_has_receipt(execution.operation_id, "limit_auto_resume"))
        yield f"data: {AgentEvent(event='done').model_dump_json()}\n\n"

    tasks = BackgroundAgentTasks(store, stream)
    launch = tasks.launch_admitted
    if restart:
        fake_launch_admitted(monkeypatch, tasks)
    resumed = tasks.resume(
        previous.operation_id, authorized_by=previous.authorized_by, limit_auto_resume=True
    )
    if restart:
        assert store.agent_task_dispatch_was_proven_not_started(resumed.operation_id)
        fake_launch_admitted(monkeypatch, tasks, launch)
        tasks.recover_at_startup()
        assert store.agent_task(resumed.operation_id).status == "queued"
        tasks.launch_admitted(resumed.operation_id)
    wait_for_task(store, resumed.operation_id, expect="succeeded")
    assert seen == [True]


@pytest.mark.parametrize("surface", ["project", "agent", "space"])
@pytest.mark.parametrize("continued", [False, True])
def test_limit_decline_route_and_task_projection(
    manifest, tmp_path: Path, surface: str, continued: bool
) -> None:
    from tests.helpers import create_named_app, signed_in_client

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = store.space_id if surface == "space" else app.state.default_project_id
    identity = _identity(store)
    with signed_in_client(app) as client:
        thread = next(
            t
            for t in client.get("/api/agent-threads").json()["threads"]
            if t["project_id"] == (None if surface == "space" else project_id)
        )
        agent_id = thread["thread_id"]
        task = store.create_agent_task(
            AgentTaskRecord(
                operation_id="limited-route",
                project_id=project_id,
                kind="space_chat" if surface == "space" else "project_chat",
                status="failed",
                request={"chat_id": agent_id, "provider": "codex"},
                created_at=store.now(),
                updated_at=store.now(),
                status_message="failed",
                authorized_by=identity,
            )
        )
        store.record_agent_task_receipt(
            task.operation_id,
            "provider_terminal_error",
            {
                "classification": "session_limit",
                "resets_at": "2030-01-01T00:00:00+00:00",
                "estimated": False,
            },
        )
        base = (
            f"/api/projects/{project_id}"
            if surface == "project"
            else f"/api/agent-threads/{agent_id}"
        )
        url = f"{base}/tasks/{task.operation_id}"
        assert client.get(url).json()["limit_reset"]["state"] == "waiting"
        if continued:
            child = store.create_agent_task(
                task.model_copy(
                    update={
                        "operation_id": "continuation",
                        "parent_operation_id": task.operation_id,
                        "status": "running",
                    }
                ),
                continuation_cause="resume",
            )
        response = client.post(f"{url}/limit-resume/decline", json={})
        if continued:
            assert response.status_code == 409, response.text
            assert not store.agent_task_has_receipt(task.operation_id, "limit_auto_resume_stopped")
            assert store.agent_task(child.operation_id).status == "running"
            return
        assert response.status_code == 202, response.text
        assert response.json() == {"ok": True}
        assert client.get(url).json()["limit_reset"]["reason"] == "declined"
        other = store.create_agent_task(
            task.model_copy(
                update={
                    "operation_id": "other-turn",
                    "authorized_by": identity.model_copy(
                        update={"user_id": "00000000-0000-4000-8000-000000000777"}
                    ),
                }
            )
        )
        store.record_agent_task_receipt(
            other.operation_id,
            "provider_terminal_error",
            {
                "classification": "session_limit",
                "resets_at": "2030-01-01T00:00:00+00:00",
                "estimated": False,
            },
        )
        assert (
            client.post(
                f"{base}/tasks/{other.operation_id}/limit-resume/decline", json={}
            ).status_code
            == 403
        )
        assert not store.agent_task_has_receipt(other.operation_id, "limit_auto_resume_stopped")


@pytest.mark.parametrize("refusal", ["limited", "signed_out"])
def test_automatic_resume_does_not_create_turn_when_admission_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    from rcp.agents import AgentEvent
    from rcp.background import BackgroundAgentTasks
    from rcp.runs.provider_login import ProviderLoginsLimited, ProviderSignedOut
    from tests.test_background_policy_matrix import _chat_request, _record, _store

    store = _store(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    previous = _record(
        store,
        operation_id="parent",
        kind="project_chat",
        request=_chat_request(),
        status="paused",
        native_session_id="owned-session",
        stage_root=str(stage),
    )

    async def stream(*_args):
        yield f"data: {AgentEvent(event='done').model_dump_json()}\n\n"

    tasks = BackgroundAgentTasks(store, stream)
    error = ProviderLoginsLimited if refusal == "limited" else ProviderSignedOut

    def refuse(*_args, automatic=False, **_kwargs):
        if refusal == "signed_out" or automatic:
            raise error("fixture refusal")

    monkeypatch.setattr(tasks, "admit_provider_task", refuse)
    with pytest.raises(error):
        tasks.resume(
            previous.operation_id, authorized_by=previous.authorized_by, limit_auto_resume=True
        )
    assert not store.agent_task_has_continuation(previous.operation_id)
