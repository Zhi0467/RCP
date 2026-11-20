from __future__ import annotations

from pathlib import Path

from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.storage import (
    WatcherContinuation,
    WatcherRecord,
)
from tests.helpers import signed_in_client

from .helpers import create_named_app
from .test_branch_chats import _app_branch


def test_project_watchers_lists_an_ordinary_watcher(manifest, tmp_path: Path) -> None:
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
            run_on=BACKEND_MACHINE_ID,
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
    assert listed_payload[0]["wakes_chat"] is True
    assert listed_payload[0]["status"] == "active"


def test_branch_watcher_listing_scopes_to_the_displayed_target_unless_all(manifest, tmp_path):
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
        continuation=WatcherContinuation(
            provider="codex", run_on=BACKEND_MACHINE_ID, patch_kind="work"
        ),
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
    assert {row["watcher_id"] for row in listing.json()} == {"main-observer", "branch-observer"}
    scoped = client.get(url, params=params)
    assert scoped.status_code == 200, scoped.text
    assert [row["watcher_id"] for row in scoped.json()] == ["branch-observer"]


def test_only_a_chats_own_unwoken_work_watcher_wakes_it(tmp_path: Path) -> None:
    from rcp.api.watchers import wakes_chat

    watcher = WatcherRecord(
        watcher_id="chat-wake",
        project_id="project",
        origin_operation_id="origin",
        origin_task_kind="project_chat",
        chat_id="chat",
        node_id=None,
        check_command="true",
        log_path=str(tmp_path / "job.log"),
        cwd=str(tmp_path),
        continuation=WatcherContinuation(
            provider="codex", run_on=BACKEND_MACHINE_ID, patch_kind="work"
        ),
        created_at="2026-08-12T01:00:00+00:00",
    )
    loop = watcher.continuation.model_copy(update={"patch_kind": "experiment_loop"})

    assert wakes_chat(watcher)
    assert not wakes_chat(watcher.model_copy(update={"continuation": loop}))
    assert not wakes_chat(watcher.model_copy(update={"notified": True}))
    assert not wakes_chat(watcher.model_copy(update={"status": "stopped"}))
