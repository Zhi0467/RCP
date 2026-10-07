from __future__ import annotations

import uuid
from pathlib import Path

from rcp.storage import (
    GraphWatcherRecord,
    NodeStatusGraphCondition,
    WatcherContinuation,
    WatcherRecord,
)
from rcp.watchers import WatcherCheckResult, WatchSpec
from tests.helpers import signed_in_client

from .helpers import create_named_app
from .test_branch_chats import _app_branch


def test_degraded_watcher_can_be_checked_now_through_the_api(manifest, tmp_path: Path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    now = "2026-08-12T01:00:00+00:00"
    watcher = WatcherRecord(
        watcher_id="manual-api-check",
        project_id=project_id,
        origin_operation_id="manual-api-origin",
        origin_task_kind="node_chat",
        chat_id="manual-api-chat",
        node_id="exp-one",
        execution_host="gpu.example",
        check_command="squeue -h -j 4471 >/dev/null",
        log_path="/tmp/4471.log",
        cwd="/tmp",
        continuation=WatcherContinuation(
            provider="codex",
            run_on="laptop",
            patch_kind="work",
        ),
        created_at=now,
    )
    store.create_watchers([watcher])
    degraded = store.record_watcher_check(
        watcher.watcher_id,
        status="degraded",
        exit_code=255,
        error="transport unavailable",
        checked_at=now,
    )
    assert degraded.next_check_at is not None and degraded.next_check_at > now
    calls: list[tuple[str, str, float]] = []

    def recovered(spec: WatchSpec, host: str, timeout: float) -> WatcherCheckResult:
        calls.append((spec.check_command, host, timeout))
        return WatcherCheckResult(
            state="active",
            checked_at="2026-08-12T01:00:01+00:00",
            exit_code=1,
        )

    app.state.watcher_poller.check_runner = recovered
    client = signed_in_client(app)
    listed = client.get(f"/api/projects/{project_id}/watchers")
    assert listed.status_code == 200
    assert listed.json()[0]["can_check_now"] is True
    response = client.post(
        f"/api/projects/{project_id}/watchers/{watcher.watcher_id}/check", json={}
    )

    assert response.status_code == 200
    assert calls == [
        (
            "squeue -h -j 4471 >/dev/null",
            "gpu.example",
            app.state.watcher_poller.timeout,
        )
    ]
    assert response.json()["status"] == "active"
    assert response.json()["consecutive_error_count"] == 0
    assert response.json()["last_error"] is None
    assert response.json()["can_check_now"] is False


def test_check_watcher_now_rejects_missing_graph_and_ineligible_records(
    manifest, tmp_path: Path
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    continuation = WatcherContinuation(provider="codex", run_on="laptop", patch_kind="work")
    active = WatcherRecord(
        watcher_id="active-api-watcher",
        project_id=project_id,
        origin_operation_id="active-api-origin",
        origin_task_kind="node_chat",
        chat_id="active-api-chat",
        check_command="true",
        log_path="/tmp/active-api.log",
        cwd="/tmp",
        continuation=continuation,
        created_at="2026-08-12T01:00:00+00:00",
    )
    graph = GraphWatcherRecord(
        watcher_id="graph-api-watcher",
        project_id=project_id,
        origin_operation_id="graph-api-origin",
        origin_task_kind="node_chat",
        chat_id="graph-api-chat",
        continuation=continuation,
        condition=NodeStatusGraphCondition(node_id="exp-one", status_in=["resolved"]),
        armed_revision=0,
        created_at="2026-08-12T01:00:00+00:00",
    )
    store.create_watchers([active])
    store.create_watchers([graph])
    client = signed_in_client(app)

    missing_project = client.post(
        f"/api/projects/{uuid.uuid4()}/watchers/{active.watcher_id}/check", json={}
    )
    missing_watcher = client.post(
        f"/api/projects/{project_id}/watchers/missing-api-watcher/check", json={}
    )
    active_response = client.post(
        f"/api/projects/{project_id}/watchers/{active.watcher_id}/check", json={}
    )
    graph_response = client.post(
        f"/api/projects/{project_id}/watchers/{graph.watcher_id}/check", json={}
    )

    assert missing_project.status_code == 404
    assert missing_watcher.status_code == 404
    assert active_response.status_code == 409
    assert "degraded watcher awaiting delivery" in active_response.json()["detail"]
    assert graph_response.status_code == 409
    assert "external watcher" in graph_response.json()["detail"]
    for watcher in (active, graph):
        no_action = client.post(
            f"/api/projects/{project_id}/watchers/{watcher.watcher_id}/cancel", json={}
        )
        assert no_action.status_code == 409
        assert "no cancel command" in no_action.json()["detail"]


def test_project_watchers_lists_and_stops_an_ordinary_watcher(manifest, tmp_path: Path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    watcher = WatcherRecord(
        watcher_id="ordinary-api-watcher",
        project_id=project_id,
        origin_operation_id="ordinary-api-origin",
        origin_task_kind="node_chat",
        chat_id="ordinary-api-chat",
        node_id="exp-one",
        check_command="true",
        log_path=str(tmp_path / "ordinary.log"),
        cwd=str(tmp_path),
        continuation=WatcherContinuation(
            provider="codex",
            run_on="laptop",
            patch_kind="work",
        ),
        created_at="2026-08-12T01:00:00+00:00",
    )
    store.create_watchers([watcher])
    client = signed_in_client(app)

    listed = client.get(f"/api/projects/{project_id}/watchers")
    assert listed.status_code == 200
    listed_payload = listed.json()
    assert isinstance(listed_payload, list)
    assert len(listed_payload) == 1
    assert listed_payload[0]["watcher_id"] == watcher.watcher_id
    assert listed_payload[0]["status"] == "active"
    assert listed_payload[0]["can_check_now"] is False

    stopped = client.post(f"/api/projects/{project_id}/watchers/{watcher.watcher_id}/stop", json={})
    assert stopped.status_code == 200
    stopped_payload = stopped.json()
    assert isinstance(stopped_payload, dict)
    assert stopped_payload["watcher_id"] == watcher.watcher_id
    assert stopped_payload["status"] == "stopped"
    stored = store.watcher(watcher.watcher_id)
    assert stored is not None
    assert stored.status == "stopped"


def test_human_cancel_is_attributed_project_scoped_and_write_fenced(
    manifest, tmp_path, monkeypatch
):
    from fastapi import HTTPException

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    store = app.state.services.store
    project_id = app.state.default_project_id
    watcher = WatcherRecord(
        watcher_id="human-cancel",
        project_id=project_id,
        origin_operation_id="origin",
        origin_task_kind="project_chat",
        chat_id="chat",
        check_command="false",
        cancel_command="scancel 331",
        log_path="/tmp/log",
        cwd="/tmp",
        execution_host="rcp@cluster",
        continuation=WatcherContinuation(provider="codex", run_on="laptop", patch_kind="work"),
        created_at=store.now(),
    )
    store.create_watchers([watcher])
    store.create_watchers(
        [watcher.model_copy(update={"watcher_id": "other", "project_id": "other"})]
    )
    calls = []
    app.state.watcher_poller.check_runner = lambda *_: WatcherCheckResult(
        state="active", checked_at=store.now(), exit_code=1
    )
    app.state.watcher_poller.cancel_runner = lambda spec, host, timeout: calls.append(
        (spec.cancel_command, host, spec.cwd)
    )
    url = f"/api/projects/{project_id}/watchers"
    for watcher_id in ("missing", "other"):
        assert client.post(f"{url}/{watcher_id}/cancel", json={}).status_code == 404
    assert calls == []
    assert client.get(url).json()[0]["can_cancel"] is True
    response = client.post(f"{url}/{watcher.watcher_id}/cancel", json={})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["cancel_requested_by"] == store.local_owner.user_id
    assert payload["cancel_requested_at"] and payload["cancel_error"] is None
    assert payload["status"] == "active" and payload["can_cancel"] is False
    assert client.post(f"{url}/{watcher.watcher_id}/cancel", json={}).json() == payload
    assert calls == [("scancel 331", "rcp@cluster", "/tmp")]

    def refuse_identity(_request):
        raise HTTPException(status_code=403, detail="Human identity required")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            app.state.services.identity_access, "require_patch_capable_identity", refuse_identity
        )
        assert client.post(f"{url}/{watcher.watcher_id}/cancel", json={}).status_code == 403

    def refuse_project(_project_id):
        raise ValueError("Project is being removed.")

    monkeypatch.setattr(store, "require_project_accepts_new_work", refuse_project)
    assert client.post(f"{url}/{watcher.watcher_id}/cancel", json={}).status_code == 409
    assert calls == [("scancel 331", "rcp@cluster", "/tmp")]


def test_all_target_watcher_listing_scopes_stop_offer_to_displayed_branch(manifest, tmp_path):
    app, _main, episode, root = _app_branch(manifest, tmp_path)
    store = app.state.catalog.store
    watcher = WatcherRecord(
        watcher_id="main-observer",
        project_id=episode.project_id,
        origin_operation_id="observer-origin",
        origin_task_kind="node_chat",
        chat_id="observer-chat",
        check_command="true",
        log_path=str(tmp_path / "observer.log"),
        cwd=str(tmp_path),
        continuation=WatcherContinuation(provider="codex", run_on="laptop", patch_kind="work"),
        created_at=store.now(),
    )
    branch_watcher = watcher.model_copy(
        update={
            "watcher_id": "branch-observer",
            "origin_operation_id": root.operation_id,
            "origin_task_kind": root.kind,
            "episode_id": episode.episode_id,
            "chat_id": "branch-observer-chat",
            "graph_target": episode.graph_target,
        }
    )
    store.create_watchers([watcher])
    store.create_watchers([branch_watcher])
    client = signed_in_client(app)
    url = f"/api/projects/{episode.project_id}/watchers"
    params = {"branch_id": episode.graph_target.branch_id}

    listing = client.get(url, params={**params, "all_targets": "true"})
    assert listing.status_code == 200, listing.text
    assert {row["watcher_id"]: row["can_stop_watching"] for row in listing.json()} == {
        "main-observer": False,
        "branch-observer": True,
    }
    scoped = client.get(url, params=params)
    assert scoped.status_code == 200, scoped.text
    assert [row["watcher_id"] for row in scoped.json()] == ["branch-observer"]
