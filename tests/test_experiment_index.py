from __future__ import annotations

import asyncio
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import rcp.api.app as api_app_module
from rcp.agents import AgentEvent
from rcp.api import create_app
from rcp.core.models import Patch
from rcp.service import RunRequest
from rcp.storage import AgentTaskRecord, ProjectRecord

from .helpers import seed_patch


def _experiment_patch() -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Added experiments for the landing-page index.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "exp/launched",
                        "type": "experiment",
                        "title": "Launched loop",
                        "objective": "Exercise the cross-project loop index.",
                        "completion_criteria": ["The indexed loop reaches a conclusion."],
                        "invocation_ceiling": 3,
                    },
                    {
                        "id": "exp/never-run",
                        "type": "experiment",
                        "title": "Never-run experiment",
                        "objective": "Remain absent from the loop index.",
                        "invocation_ceiling": 2,
                    },
                ],
            }
        ],
    )


def _update_experiment_summary(summary: str) -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Updated the indexed experiment.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "exp/launched",
                        "changes": {"current_summary": summary},
                    }
                ],
            }
        ],
    )


def _update_primary_question(question: str) -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Updated the primary question.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "rq/learning-after-shift",
                        "changes": {"question": question},
                    }
                ],
            }
        ],
    )


def _record_loop(
    store,
    project_id: str,
    *,
    episode_id: str,
    operation_id: str,
    created_at: str,
) -> None:
    request = RunRequest(
        provider="codex",
        model="gpt-5",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        chat_id=str(uuid.uuid4()),
        chat_scope="node",
        node_id="exp/launched",
        mode="work",
        trigger="experiment_run",
        patch_kind="experiment_loop",
        control_node_id="exp/launched",
        control_revision=2,
        control_episode_id=episode_id,
        control_invocation=1,
        control_invocation_ceiling=3,
        control_decision_bundle=[],
        control_completion_criteria=["The indexed loop reaches a conclusion."],
    )
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind="node_chat",
            status="succeeded",
            request=request.model_dump(mode="json"),
            created_at=created_at,
            updated_at=created_at,
            finished_at=created_at,
            status_message="The loop invocation completed.",
            phase="complete",
            last_activity_at=created_at,
        )
    )


def _seed_indexed_project(app) -> tuple[str, str]:
    service = app.state.service
    service.history.append(seed_patch())
    service.history.append(_experiment_patch())
    project_id = app.state.default_project_id
    old_episode = str(uuid.uuid4())
    current_episode = str(uuid.uuid4())
    store = app.state.background_tasks.store
    _record_loop(
        store,
        project_id,
        episode_id=old_episode,
        operation_id="older-loop",
        created_at="2026-08-08T00:00:00+00:00",
    )
    _record_loop(
        store,
        project_id,
        episode_id=current_episode,
        operation_id="current-loop",
        created_at="2026-08-09T00:00:00+00:00",
    )
    return project_id, current_episode


def _event_frame(event: AgentEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def _wait_for_task(store, operation_id: str) -> AgentTaskRecord:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = store.agent_task(operation_id)
        assert record is not None
        if record.status not in {"queued", "running"}:
            return record
        time.sleep(0.01)
    raise AssertionError("background task did not finish")


def test_experiment_index_uses_only_cached_graph_and_batches_project_runtime(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id, current_episode = _seed_indexed_project(app)
    client = TestClient(app)

    cached = client.get(f"/api/projects/{project_id}")
    assert cached.status_code == 200
    app.state.service.history.append(_update_experiment_summary("Current graph summary."))

    def refuse_current_state_read():
        raise AssertionError("landing polling must not read current project history")

    monkeypatch.setattr(app.state.service.history, "state", refuse_current_state_read)

    store = app.state.background_tasks.store
    original = store.experiment_loop_runtimes
    calls: list[tuple[str, tuple[str, ...]]] = []

    def capture(requested_project_id, experiment_ids):
        requested_ids = tuple(experiment_ids)
        calls.append((requested_project_id, requested_ids))
        return original(requested_project_id, requested_ids)

    monkeypatch.setattr(store, "experiment_loop_runtimes", capture)
    response = client.get("/api/experiment-loops")

    assert response.status_code == 200
    assert calls == [(project_id, ("exp/launched", "exp/never-run"))]
    assert len(response.json()) == 1
    entry = response.json()[0]
    assert set(entry) == {
        "project_id",
        "project_name",
        "project_reachable",
        "node",
        "control",
    }
    assert entry["project_id"] == project_id
    assert entry["project_name"] == manifest.name
    assert entry["project_reachable"] is True
    assert entry["node"]["id"] == "exp/launched"
    assert entry["node"]["current_summary"] == ""
    assert entry["control"]["episode_id"] == current_episode
    assert entry["control"]["invocations_used"] == 1
    assert entry["control"]["invocation_ceiling"] == 3
    assert entry["control"]["operational"]["current_operation_id"] == "current-loop"
    assert entry["control"]["operational"]["current_status"] == "succeeded"


def test_experiment_index_keeps_cached_unavailable_project_without_opening_it(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    first_app = create_app(str(manifest.path), data_dir=data_dir)
    project_id, current_episode = _seed_indexed_project(first_app)
    first_client = TestClient(first_app)
    snapshot = first_client.get(f"/api/projects/{project_id}").json()
    snapshot["canonical_state"]["reachable"] = False
    snapshot["canonical_state"]["error"] = "Project host is unavailable."
    first_app.state.catalog.update_summary(project_id, snapshot)
    first_app.state.catalog.write_cached_snapshot(project_id, snapshot)

    record = first_app.state.catalog.store.project(project_id)
    assert record is not None
    first_app.state.catalog.store.upsert_project(
        ProjectRecord(
            project_id="unusable-project",
            locator=str(tmp_path / "missing" / "manifest.toml"),
            name="Unusable project",
            state_location=str(tmp_path / "missing" / ".research"),
            state_remote=False,
            added_at=record.added_at,
        )
    )

    restarted = create_app(data_dir=data_dir)

    def refuse_open(_project_id):
        raise AssertionError("the experiment index must not open inactive projects")

    monkeypatch.setattr(restarted.state.catalog, "_open_service", refuse_open)
    response = TestClient(restarted).get("/api/experiment-loops")

    assert response.status_code == 200
    assert len(response.json()) == 1
    entry = response.json()[0]
    assert entry["project_id"] == project_id
    assert entry["project_reachable"] is False
    assert entry["control"]["episode_id"] == current_episode
    assert project_id not in restarted.state.catalog._services
    assert "unusable-project" not in restarted.state.catalog._services


def test_graph_capable_background_stream_refreshes_cached_experiment_semantics(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id, current_episode = _seed_indexed_project(app)
    client = TestClient(app)
    assert client.get(f"/api/projects/{project_id}").status_code == 200
    release = threading.Event()
    operation: dict[str, str] = {}

    async def update_graph(service, _launcher, _request, _data_dir, *, execution):
        del execution
        await asyncio.to_thread(release.wait)
        service.history.append(_update_experiment_summary("Refreshed after Work."))
        yield _event_frame(AgentEvent(event="answer", text="Updated the experiment."))
        yield _event_frame(AgentEvent(event="done"))

    monkeypatch.setattr(api_app_module, "stream_work_run", update_graph)
    store = app.state.background_tasks.store
    original_commit = app.state.catalog.commit_cached_snapshot
    stream_closed_statuses: list[str] = []
    stream_closed = threading.Event()

    def capture_commit(requested_project_id, snapshot, *, generation, patch_log_head=None):
        record = store.agent_task(operation["id"])
        assert record is not None
        stream_closed_statuses.append(record.status)
        committed = original_commit(
            requested_project_id,
            snapshot,
            generation=generation,
            patch_log_head=patch_log_head,
        )
        stream_closed.set()
        return committed

    monkeypatch.setattr(app.state.catalog, "commit_cached_snapshot", capture_commit)
    request = RunRequest(
        provider="codex",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Update the indexed experiment.",
        mode="work",
        patch_kind="work",
    )
    task = app.state.background_tasks.start(project_id, "project_chat", request)
    operation["id"] = task.operation_id
    release.set()
    completed = _wait_for_task(store, task.operation_id)

    assert completed.status == "succeeded"
    assert stream_closed.wait(timeout=2)
    assert stream_closed_statuses == ["running"]
    cached = client.get(f"/api/projects/{project_id}/cached").json()
    assert cached["graph"]["nodes"]["exp/launched"]["current_summary"] == ("Refreshed after Work.")
    assert (
        cached["experiment_control"]["exp/launched"]["operational"]["current_status"] == "succeeded"
    )
    response = client.get("/api/experiment-loops")
    assert response.status_code == 200
    assert response.json()[0]["node"]["current_summary"] == "Refreshed after Work."
    assert response.json()[0]["control"]["episode_id"] == current_episode


def test_experiment_index_runtime_projection_failure_fails_the_request(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id, _current_episode = _seed_indexed_project(app)
    assert TestClient(app).get(f"/api/projects/{project_id}").status_code == 200

    def fail_runtime_projection(_project_id, _experiment_ids):
        raise RuntimeError("runtime projection broke")

    monkeypatch.setattr(
        app.state.background_tasks.store,
        "experiment_loop_runtimes",
        fail_runtime_projection,
    )
    response = TestClient(app, raise_server_exceptions=False).get("/api/experiment-loops")

    assert response.status_code == 500


def test_display_cache_refresh_failure_is_diagnostic_not_task_failure(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id, _current_episode = _seed_indexed_project(app)
    assert TestClient(app).get(f"/api/projects/{project_id}").status_code == 200

    async def finish_work(_service, _launcher, _request, _data_dir, *, execution):
        del execution
        yield _event_frame(AgentEvent(event="answer", text="Work completed."))
        yield _event_frame(AgentEvent(event="done"))

    def fail_cache_write(_project_id, _snapshot, *, generation, patch_log_head=None):
        del generation, patch_log_head
        raise OSError("display cache is unavailable")

    monkeypatch.setattr(api_app_module, "stream_work_run", finish_work)
    monkeypatch.setattr(app.state.catalog, "commit_cached_snapshot", fail_cache_write)
    store = app.state.background_tasks.store
    request = RunRequest(
        provider="codex",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Complete graph-capable work.",
        mode="work",
        patch_kind="work",
    )
    task = app.state.background_tasks.start(project_id, "project_chat", request)
    completed = _wait_for_task(store, task.operation_id)

    assert completed.status == "succeeded"
    deadline = time.monotonic() + 2
    failure = None
    while time.monotonic() < deadline and failure is None:
        failure = next(
            (
                item
                for item in store.agent_task_receipts(task.operation_id)
                if item.category == "display_cache_refresh_failed"
            ),
            None,
        )
        time.sleep(0.01)
    assert failure is not None
    assert failure.payload["exception_type"] == "OSError"


def test_versioned_cache_commit_cannot_regress_graph_or_project_summary(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id, _current_episode = _seed_indexed_project(app)
    client = TestClient(app)
    older = client.get(f"/api/projects/{project_id}").json()
    app.state.service.history.append(_update_primary_question("What is the newest question?"))
    state = app.state.service.history.materialize(write_outputs=False).state
    newer = app.state.service.project_snapshot(state=state)
    newer["id"] = project_id

    catalog = app.state.catalog
    newer_generation = catalog.reserve_cached_snapshot_generation(project_id)
    older_generation = catalog.reserve_cached_snapshot_generation(project_id)
    entered = threading.Event()
    release = threading.Event()
    original_write = catalog._write_cached_snapshot_locked

    def block_older_write(requested_project_id, snapshot):
        if snapshot["revision"] == older["revision"]:
            entered.set()
            assert release.wait(timeout=2)
        original_write(requested_project_id, snapshot)

    monkeypatch.setattr(catalog, "_write_cached_snapshot_locked", block_older_write)
    results: dict[str, bool] = {}
    newer_thread = threading.Thread(
        target=lambda: results.setdefault(
            "newer",
            catalog.commit_cached_snapshot(
                project_id,
                newer,
                generation=newer_generation,
            ),
        )
    )
    older_thread = threading.Thread(
        target=lambda: results.setdefault(
            "older",
            catalog.commit_cached_snapshot(
                project_id,
                older,
                generation=older_generation,
            ),
        )
    )

    older_thread.start()
    assert entered.wait(timeout=1)
    newer_thread.start()
    release.set()
    newer_thread.join(timeout=2)
    older_thread.join(timeout=2)

    assert not newer_thread.is_alive()
    assert not older_thread.is_alive()
    assert results == {"older": True, "newer": True}
    cached = catalog.cached_snapshot(project_id)
    assert cached is not None
    assert cached["revision"] == newer["revision"]
    assert cached["primary_question"]["question"] == "What is the newest question?"
    record = catalog.store.project(project_id)
    assert record is not None
    assert record.revision == newer["revision"]
    assert record.primary_question == "What is the newest question?"

    regressive_generation = catalog.reserve_cached_snapshot_generation(project_id)
    assert not catalog.commit_cached_snapshot(
        project_id,
        older,
        generation=regressive_generation,
    )


def test_cache_generation_rejects_out_of_order_same_revision_reachability(
    manifest, tmp_path: Path
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id, _current_episode = _seed_indexed_project(app)
    client = TestClient(app)
    assert client.get(f"/api/projects/{project_id}").status_code == 200
    catalog = app.state.catalog
    first_read = threading.Event()
    release_first = threading.Event()
    results: dict[str, bool] = {}

    def first_writer() -> None:
        generation = catalog.reserve_cached_snapshot_generation(project_id)
        snapshot = catalog.cached_snapshot(project_id)
        assert snapshot is not None
        first_read.set()
        assert release_first.wait(timeout=2)
        results["first"] = catalog.commit_cached_snapshot(
            project_id,
            snapshot,
            generation=generation,
        )

    thread = threading.Thread(target=first_writer)
    thread.start()
    assert first_read.wait(timeout=1)
    newer_generation = catalog.reserve_cached_snapshot_generation(project_id)
    newer = catalog.cached_snapshot(project_id)
    assert newer is not None
    newer = {**newer, "canonical_state": {**newer["canonical_state"], "reachable": False}}
    assert catalog.commit_cached_snapshot(
        project_id,
        newer,
        generation=newer_generation,
    )
    release_first.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert results == {"first": False}
    cached = catalog.cached_snapshot(project_id)
    assert cached is not None
    assert cached["canonical_state"]["reachable"] is False
    record = app.state.catalog.store.project(project_id)
    assert record is not None
    assert record.reachable is False


def test_experiment_loop_cache_blocks_terminal_runtime_until_graph_is_visible(
    manifest, tmp_path: Path, monkeypatch
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id, _current_episode = _seed_indexed_project(app)
    client = TestClient(app)
    assert client.get(f"/api/projects/{project_id}").status_code == 200
    entered_cache = threading.Event()
    release_cache = threading.Event()

    async def update_graph(service, _launcher, _request, _data_dir, *, execution):
        del execution
        service.history.append(_update_experiment_summary("Graph visible with terminal task."))
        yield _event_frame(AgentEvent(event="answer", text="Updated the experiment."))
        yield _event_frame(AgentEvent(event="done"))

    monkeypatch.setattr(api_app_module, "stream_work_run", update_graph)
    catalog = app.state.catalog
    original_commit = catalog.commit_cached_snapshot

    def block_cache(requested_project_id, snapshot, *, generation, patch_log_head=None):
        if snapshot["graph"]["nodes"]["exp/launched"]["current_summary"]:
            entered_cache.set()
            assert release_cache.wait(timeout=2)
        return original_commit(
            requested_project_id,
            snapshot,
            generation=generation,
            patch_log_head=patch_log_head,
        )

    monkeypatch.setattr(catalog, "commit_cached_snapshot", block_cache)
    episode_id = str(uuid.uuid4())
    request = RunRequest(
        provider="codex",
        model="gpt-5",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        chat_id=str(uuid.uuid4()),
        chat_scope="node",
        node_id="exp/launched",
        message="Continue the experiment loop.",
        mode="work",
        trigger="experiment_run",
        patch_kind="experiment_loop",
        control_node_id="exp/launched",
        control_revision=2,
        control_episode_id=episode_id,
        control_invocation=1,
        control_invocation_ceiling=3,
        control_decision_bundle=[],
        control_completion_criteria=["The indexed loop reaches a conclusion."],
    )
    task = app.state.background_tasks.start(project_id, "node_chat", request)
    assert entered_cache.wait(timeout=1)

    running = app.state.background_tasks.store.agent_task(task.operation_id)
    assert running is not None
    assert running.status == "running"
    before_release = client.get("/api/experiment-loops").json()[0]
    assert before_release["node"]["current_summary"] == ""
    assert before_release["control"]["episode_id"] == episode_id
    assert before_release["control"]["operational"]["current_status"] == "running"

    release_cache.set()
    completed = _wait_for_task(app.state.background_tasks.store, task.operation_id)
    assert completed.status == "succeeded"
    after_release = client.get("/api/experiment-loops").json()[0]
    assert after_release["node"]["current_summary"] == ("Graph visible with terminal task.")
    assert after_release["control"]["episode_id"] == episode_id
    assert after_release["control"]["operational"]["current_status"] == "succeeded"


@pytest.mark.parametrize(
    ("terminal_event", "expected_status"),
    [("error", "failed"), ("paused", "paused")],
)
def test_stream_closed_cache_hook_runs_before_error_and_pause_verdicts(
    manifest, tmp_path: Path, monkeypatch, terminal_event: str, expected_status: str
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id, _current_episode = _seed_indexed_project(app)
    client = TestClient(app)
    assert client.get(f"/api/projects/{project_id}").status_code == 200

    async def update_then_stop(service, _launcher, _request, _data_dir, *, execution):
        del execution
        service.history.append(_update_experiment_summary(f"Graph before {terminal_event}."))
        yield _event_frame(AgentEvent(event=terminal_event, text=f"Task {terminal_event}."))

    monkeypatch.setattr(api_app_module, "stream_work_run", update_then_stop)
    request = RunRequest(
        provider="codex",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Update before stopping.",
        mode="work",
        patch_kind="work",
    )
    task = app.state.background_tasks.start(project_id, "project_chat", request)
    completed = _wait_for_task(app.state.background_tasks.store, task.operation_id)

    assert completed.status == expected_status
    indexed = client.get("/api/experiment-loops").json()[0]
    assert indexed["node"]["current_summary"] == f"Graph before {terminal_event}."


def test_experiment_index_fails_for_malformed_existing_cache(manifest, tmp_path: Path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id, _current_episode = _seed_indexed_project(app)
    client = TestClient(app)
    assert client.get(f"/api/projects/{project_id}").status_code == 200
    app.state.catalog._cached_snapshot_path(project_id).write_text("{", encoding="utf-8")

    response = client.get("/api/experiment-loops")

    assert response.status_code == 503


def test_experiment_index_fails_when_revisioned_project_cache_is_missing(
    manifest, tmp_path: Path
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id, _current_episode = _seed_indexed_project(app)
    client = TestClient(app)
    assert client.get(f"/api/projects/{project_id}").status_code == 200
    app.state.catalog._cached_snapshot_path(project_id).unlink()

    response = client.get("/api/experiment-loops")

    assert response.status_code == 503
