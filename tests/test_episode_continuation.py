"""Adding turns to an ended episode continues it on its branch and in its session."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.testclient import TestClient

from rcp.agents import AgentEvent
from rcp.api.episodes import episode_on_branch
from rcp.background import AgentTaskExecution
from rcp.runs.auto_research import AutoResearchRunRequest
from rcp.service import RunRequest
from rcp.transport import StateUnavailable

from .helpers import create_named_app, wait_for_task
from .test_episode_api import _sse, create_terminal_auto_episode
from .test_experiment_stop import EXPERIMENT_ID, _Loop


def _session_resuming_stream(
    *, kind: str, session_id: str, stage_root: str, seen: list[tuple[str | None, str | None]]
):
    async def stream(
        _project_id: str,
        task_kind: str,
        request: object,
        execution: AgentTaskExecution,
    ) -> AsyncIterator[str]:
        assert task_kind == kind
        request_session = getattr(request, "session_id", None)
        seen.append((request_session, execution.stage_root))
        assert request_session == session_id
        assert execution.stage_root == stage_root
        yield _sse(AgentEvent(event="session", session_id=session_id))
        yield _sse(AgentEvent(event="done"))

    return stream


def test_continue_resumes_an_ended_auto_research_episode_in_its_session(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    tasks = app.state.background_tasks
    store = tasks.store
    original, original_root, _ = create_terminal_auto_episode(
        store,
        app.state.catalog.open(project_id).history,
        project_id,
        episode_id="exhausted-episode",
        invocation_ceiling=2,
        starting_instruction="Resolve the disputed interpretation.",
        report_error="The report output was invalid.",
    )
    assert original.status == "needs_action"
    assert original_root.native_session_id and original_root.stage_root
    seen: list[tuple[str | None, str | None]] = []
    tasks.stream = _session_resuming_stream(
        kind="auto_research",
        session_id=original_root.native_session_id,
        stage_root=original_root.stage_root,
        seen=seen,
    )
    request_id = str(uuid.uuid4())

    with TestClient(app) as client:
        before = client.get(f"/api/projects/{project_id}/episodes").json()[0]
        assert before["episode_id"] == original.episode_id
        assert before["can_continue"] is True
        assert before["continues_episode_id"] is None
        assert before["continued_by_episode_id"] is None
        assert "can_reauthorize" not in before

        base = f"/api/projects/{project_id}/episodes/{original.episode_id}"
        assert client.post(f"{base}/continue", json={"invocation_ceiling": 4}).status_code == 422
        assert client.post(f"{base}/reauthorize", json={"invocation_ceiling": 4}).status_code in {
            404,
            405,
        }

        response = client.post(
            f"{base}/continue", json={"invocation_ceiling": 4, "request_id": request_id}
        )
        assert response.status_code == 202, response.text
        payload = response.json()
        continuation_id = payload["episode_id"]
        continuation_root_id = payload["root_operation_id"]
        assert continuation_id != original.episode_id
        assert continuation_root_id != original_root.operation_id
        assert payload["mode"] == "auto_research"
        assert payload["continues_episode_id"] == original.episode_id
        assert payload["continued_by_episode_id"] is None
        assert payload["can_continue"] is False
        # The branch keeps the chain root's identity; the newest member writes to it.
        assert payload["graph_target"] == {"kind": "branch", "branch_id": original.episode_id}
        assert payload["graph_branch"]["branch_id"] == original.episode_id
        assert payload["graph_branch"]["episode_id"] == original.episode_id
        assert payload["graph_branch"]["current_episode_id"] == continuation_id
        assert payload["tasks"][0]["graph_target"] == payload["graph_target"]
        assert payload["starting_instruction"] == "Resolve the disputed interpretation."
        assert payload["budget"]["invocation_ceiling"] == 4
        assert payload["budget"]["invocations_used"] == 1
        assert payload["ending"] is None
        assert payload["wrapup_state"] == "not_started"

        replay = client.post(
            f"{base}/continue", json={"invocation_ceiling": 4, "request_id": request_id}
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["episode_id"] == continuation_id
        other = client.post(
            f"{base}/continue", json={"invocation_ceiling": 4, "request_id": str(uuid.uuid4())}
        )
        assert other.status_code == 409, other.text
        assert "already been continued" in other.json()["detail"]

        continuation_root = wait_for_task(store, continuation_root_id, expect="succeeded")
        assert seen == [(original_root.native_session_id, original_root.stage_root)]

        # A lost response is replayed even while the project cannot take a write.
        def unavailable(state=None):
            # Only the explicit admission check is refused; state passthrough stays coherent.
            if state is None:
                raise StateUnavailable("the canonical state is read-only right now")
            return state

        monkeypatch.setattr(
            app.state.catalog.open(project_id).history, "require_writable", unavailable
        )
        replay_read_only = client.post(
            f"{base}/continue", json={"invocation_ceiling": 4, "request_id": request_id}
        )
        assert replay_read_only.status_code == 200, replay_read_only.text
        assert replay_read_only.json()["episode_id"] == continuation_id
        monkeypatch.undo()

        old_after = next(
            item
            for item in client.get(f"/api/projects/{project_id}/episodes").json()
            if item["episode_id"] == original.episode_id
        )
        assert old_after["status"] == "needs_action"
        assert old_after["ending"] == "exhausted"
        assert old_after["wrapup_state"] == "failed"
        assert old_after["wrapup_error"] == "The report output was invalid."
        assert old_after["budget"]["invocations_used"] == 1
        assert old_after["continued_by_episode_id"] == continuation_id
        assert old_after["can_continue"] is False
        assert old_after["graph_branch"]["current_episode_id"] == continuation_id

        timeline = client.get(f"/api/projects/{project_id}/episodes/{continuation_id}/timeline")
        assert timeline.status_code == 200
        events = {event["event_id"]: event for event in timeline.json()["events"]}
        boundary = events[f"lifecycle:continued:{continuation_id}"]
        assert boundary["status"] == "4"
        assert boundary["links"]["episode_id"] == continuation_id
        assert f"{original.episode_id}:turn:{original_root.operation_id}" in events
        assert f"wake:{continuation_root_id}" in events

    request = AutoResearchRunRequest.model_validate(continuation_root.request)
    assert continuation_root.episode_id == continuation_id
    assert continuation_root.parent_operation_id is None
    assert continuation_root.native_session_id == original_root.native_session_id
    assert continuation_root.stage_root == original_root.stage_root
    assert request.episode_id == continuation_id
    assert request.actor_operation_id == continuation_root_id
    assert request.session_id == original_root.native_session_id
    assert request.wake_cause == "lifecycle"
    assert request.instruction is None
    assert store.agent_task_continuation_cause(continuation_root_id) == "lifecycle_wake"
    notices = store.auto_research_lifecycle_notices(continuation_id, newest=10)
    assert [(notice.source_event, notice.source_id) for notice in notices] == [
        ("reauthorized", original.episode_id)
    ]
    assert notices[0].payload["ceiling"] == 4
    assert notices[0].payload["continues_episode_id"] == original.episode_id
    chain = store.episode_chain(original.episode_id)
    assert [member.episode_id for member in chain] == [original.episode_id, continuation_id]
    # Child routes moved to the continuation still belong to the branch: the
    # consumers that once required the root's id accept any chain member.
    assert episode_on_branch(store, continuation_id, original.episode_id)
    assert episode_on_branch(store, original.episode_id, original.episode_id)
    assert not episode_on_branch(store, original.episode_id, continuation_id)
    assert not episode_on_branch(store, None, original.episode_id)

    # Once the newest member alone overflows the response, the source is not
    # hydrated at all and the response says it is truncated.
    with store.connection() as connection:
        for index in range(401):
            connection.execute(
                "INSERT INTO auto_research_lifecycle_notices (notice_id, episode_id, source_kind,"
                " source_id, source_event, state, payload_json, created_at)"
                " VALUES (?, ?, 'watcher', ?, 'completed', 'pending', '{}', ?)",
                (f"extra-{index:04}", continuation_id, f"extra-{index}", store.now()),
            )
    with TestClient(app) as client:
        response = client.get(f"/api/projects/{project_id}/episodes/{continuation_id}/timeline")
    assert response.status_code == 200
    assert response.json()["truncated"] is True
    assert not any(
        event["event_id"].startswith(f"{original.episode_id}:")
        for event in response.json()["events"]
    )


def test_continuation_preflight_failure_leaves_the_source_unchanged(
    manifest,
    tmp_path,
    monkeypatch,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    service = app.state.catalog.open(project_id)
    original, _root, _ = create_terminal_auto_episode(
        store,
        service.history,
        project_id,
        episode_id="exhausted-episode",
        report_error="The report output was invalid.",
    )
    episodes_before = [item.model_dump(mode="json") for item in store.episodes(project_id)]
    tasks_before = [item.model_dump(mode="json") for item in store.agent_tasks(project_id)]

    def reject_authority(*_args, **_kwargs):
        raise ValueError("the pinned orchestrator profile is unavailable")

    monkeypatch.setattr(
        "rcp.runs.auto_research_admission.resolved_dispatch_authority", reject_authority
    )

    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project_id}/episodes/{original.episode_id}/continue",
            json={"invocation_ceiling": 4, "request_id": str(uuid.uuid4())},
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "the pinned orchestrator profile is unavailable"}
    assert [item.model_dump(mode="json") for item in store.episodes(project_id)] == episodes_before
    assert [item.model_dump(mode="json") for item in store.agent_tasks(project_id)] == tasks_before


def test_continuation_refuses_a_stage_frozen_on_another_machine(manifest, tmp_path) -> None:
    """A repointed execution alias cannot resume the saved session; nothing is chained."""

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    original, root, _ = create_terminal_auto_episode(
        store,
        app.state.catalog.open(project_id).history,
        project_id,
        episode_id="exhausted-episode",
        report_error="The report output was invalid.",
    )
    store.checkpoint_agent_task(
        root.operation_id, stage_host="other-host", stage_root=root.stage_root
    )
    episodes_before = [item.model_dump(mode="json") for item in store.episodes(project_id)]
    tasks_before = [item.model_dump(mode="json") for item in store.agent_tasks(project_id)]

    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project_id}/episodes/{original.episode_id}/continue",
            json={"invocation_ceiling": 4, "request_id": str(uuid.uuid4())},
        )

    assert response.status_code == 409
    assert "Start a new Auto-research episode instead." in response.json()["detail"]
    assert [item.model_dump(mode="json") for item in store.episodes(project_id)] == episodes_before
    assert [item.model_dump(mode="json") for item in store.agent_tasks(project_id)] == tasks_before


def test_continue_refuses_a_live_or_unbound_episode(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    store = app.state.background_tasks.store
    loop = _Loop(app)
    loop.start_episode(status="running")
    with TestClient(app) as client:
        base = f"/api/projects/{project_id}/episodes/{loop.episode_id}"
        live = client.post(
            f"{base}/continue", json={"invocation_ceiling": 2, "request_id": str(uuid.uuid4())}
        )
        assert live.status_code == 409, live.text
        assert "ended episode" in live.json()["detail"]
        store.complete_agent_task("loop-root", applied_revision=None, result={})
        loop.settle_exhausted_ending()
        listed = client.get(f"/api/projects/{project_id}/episodes?mode=experiment_loop").json()
        assert listed[0]["can_continue"] is False
        unbound = client.post(
            f"{base}/continue", json={"invocation_ceiling": 2, "request_id": str(uuid.uuid4())}
        )
        assert unbound.status_code == 409, unbound.text
        assert "Start a new episode" in unbound.json()["detail"]
    assert store.episode_continuation(loop.episode_id) is None


def test_experiment_continuation_refuses_a_stage_frozen_on_another_machine(
    manifest, tmp_path: Path
) -> None:
    """A repointed execution alias cannot resume the saved session; nothing is chained."""

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    loop = _Loop(app)
    loop.start_episode()
    stage = tmp_path / "loop-stage"
    stage.mkdir()
    # The same binding `bind_session` commits, frozen on a machine the alias no longer names.
    loop.store.commit_experiment_episode_turn(
        episode_id=loop.episode_id,
        project_id=loop.project_id,
        control_node_id=EXPERIMENT_ID,
        provider="codex",
        execution_machine="laptop",
        execution_host="other-host",
        native_session_id="native-session-abc",
        stage_host="other-host",
        stage_root=str(stage),
        chat_id=loop.chat_id,
        operation_id="loop-root",
        invocation=1,
        graph_result="applied",
        watcher_ids=[],
        context_baseline={},
    )
    loop.settle_exhausted_ending()
    store = app.state.background_tasks.store
    episodes_before = [item.model_dump(mode="json") for item in store.episodes(loop.project_id)]

    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{loop.project_id}/episodes/{loop.episode_id}/continue",
            json={"invocation_ceiling": 3, "request_id": str(uuid.uuid4())},
        )

    assert response.status_code == 409, response.text
    assert "Start a new episode instead." in response.json()["detail"]
    assert store.episode_continuation(loop.episode_id) is None
    assert [
        item.model_dump(mode="json") for item in store.episodes(loop.project_id)
    ] == episodes_before


def test_continue_resumes_an_ended_experiment_episode_in_its_session(
    manifest, tmp_path: Path
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    loop = _Loop(app)
    loop.start_episode()
    stage = tmp_path / "loop-stage"
    loop.bind_session(stage, native_session_id="native-session-abc")
    loop.settle_exhausted_ending()
    tasks = app.state.background_tasks
    store = tasks.store
    seen: list[tuple[str | None, str | None]] = []
    tasks.stream = _session_resuming_stream(
        kind="node_chat", session_id="native-session-abc", stage_root=str(stage), seen=seen
    )
    request_id = str(uuid.uuid4())

    with TestClient(app) as client:
        base = f"/api/projects/{loop.project_id}/episodes/{loop.episode_id}"
        source = client.get(f"/api/projects/{loop.project_id}/episodes?mode=experiment_loop").json()
        assert source[0]["episode_id"] == loop.episode_id
        assert source[0]["can_continue"] is True

        response = client.post(
            f"{base}/continue", json={"invocation_ceiling": 3, "request_id": request_id}
        )
        assert response.status_code == 202, response.text
        payload = response.json()
        continuation_id = payload["episode_id"]
        assert continuation_id != loop.episode_id
        assert payload["mode"] == "experiment_loop"
        assert payload["control_node_id"] == EXPERIMENT_ID
        assert payload["continues_episode_id"] == loop.episode_id
        assert payload["graph_target"] == {"kind": "main", "branch_id": None}
        assert payload["budget"]["invocation_ceiling"] == 3
        assert payload["budget"]["invocations_used"] == 1
        assert payload["can_continue"] is False
        root_id = payload["root_operation_id"]

        replay = client.post(
            f"{base}/continue", json={"invocation_ceiling": 3, "request_id": request_id}
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["episode_id"] == continuation_id
        refused = client.post(
            f"{base}/continue", json={"invocation_ceiling": 3, "request_id": str(uuid.uuid4())}
        )
        assert refused.status_code == 409, refused.text

        root = wait_for_task(store, root_id, expect="succeeded")
        assert seen == [("native-session-abc", str(stage))]
        listed = client.get(f"/api/projects/{loop.project_id}/episodes?mode=experiment_loop").json()
        by_id = {item["episode_id"]: item for item in listed}
        assert by_id[loop.episode_id]["continued_by_episode_id"] == continuation_id
        assert by_id[loop.episode_id]["can_continue"] is False
        assert by_id[loop.episode_id]["ending"] == "exhausted"

    request = RunRequest.model_validate(root.request)
    assert root.parent_operation_id is None
    assert root.episode_id == continuation_id
    assert root.native_session_id == "native-session-abc"
    assert root.stage_root == str(stage)
    assert request.trigger == "experiment_run"
    assert request.control_invocation == 1
    assert request.control_invocation_ceiling == 3
    assert request.control_episode_id == continuation_id
    assert request.control_node_id == EXPERIMENT_ID
    assert request.session_id == "native-session-abc"
    assert request.chat_id == loop.chat_id
    assert request.watcher_ids == []
    stored = store.episode(continuation_id)
    assert stored is not None
    assert stored.continuation_request_id == request_id
    assert stored.authorized_by is not None
