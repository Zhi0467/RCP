from __future__ import annotations

from pathlib import Path

from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.storage import AgentTaskRecord, AppStore
from rcp.storage.models import WatcherCheckRecord, WatcherRecord
from tests.helpers import signed_in_client

from .helpers import create_named_app
from .test_branch_chats import _app_branch


def _turn(store: AppStore, project_id: str, operation_id: str, **request: object) -> None:
    now = store.now()
    with store.connection() as connection:
        store._insert_agent_task(
            connection,
            AgentTaskRecord(
                operation_id=operation_id,
                project_id=project_id,
                kind="node_chat",
                status="succeeded",
                request=request,
                created_at=now,
                updated_at=now,
                status_message="Done",
            ),
            continuation_cause="fresh",
        )


def _external(store: AppStore, project_id: str, watcher_id: str, origin: str, *checks: str):
    now = store.now()
    return store.arm_watcher(
        WatcherRecord(
            watcher_id=watcher_id,
            agent_id="chat",
            project_id=project_id,
            purpose="Training job",
            origin_turn_id=origin,
            created_at=now,
            checks=[
                WatcherCheckRecord(
                    check_id=check_id,
                    watcher_id=watcher_id,
                    machine_id=BACKEND_MACHINE_ID,
                    definition="external",
                    check_command="true",
                    log_path=f"/tmp/{check_id}.log",
                    cwd="/tmp",
                    created_at=now,
                )
                for check_id in checks or (watcher_id,)
            ],
        )
    )


def test_project_watchers_lists_one_object_per_check(manifest, tmp_path: Path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    _turn(store, project_id, "origin", node_id="exp-one", chat_id="chat")
    _external(store, project_id, "single", "origin")
    _external(store, project_id, "group", "origin", "first", "second")
    store.cancel_watchers(["group"], by="rcp", reason="Episode stopped.")
    client = signed_in_client(app)

    listed = client.get(f"/api/projects/{project_id}/watchers")
    assert listed.status_code == 200, listed.text
    rows = {row["watcher_id"]: row for row in listed.json()}
    assert set(rows) == {"single", "first", "second"}
    single = rows["single"]
    assert (single["status"], single["chat_id"], single["node_id"]) == ("active", "chat", "exp-one")
    assert single["wakes_chat"] is True and single["group_id"] is None
    assert single["continuation"]["patch_kind"] == "work"
    assert "condition" not in single
    stopped = rows["first"]
    assert (stopped["status"], stopped["stopped_by"], stopped["stop_reason"]) == (
        "stopped",
        "loop",
        "Episode stopped.",
    )
    assert (stopped["group_id"], stopped["group_label"]) == ("group", "Training job")
    assert stopped["wakes_chat"] is False and stopped["notified"] is True


def test_branch_watcher_listing_scopes_to_the_arming_turn_target_unless_all(manifest, tmp_path):
    app, _main, episode, root = _app_branch(manifest, tmp_path)
    store = app.state.catalog.store
    _turn(store, episode.project_id, "main-origin")
    _external(store, episode.project_id, "main-observer", "main-origin")
    _external(store, episode.project_id, "branch-observer", root.operation_id)
    client = signed_in_client(app)
    url = f"/api/projects/{episode.project_id}/watchers"
    params = {"branch_id": episode.graph_target.branch_id}

    listing = client.get(url, params={**params, "all_targets": "true"})
    assert listing.status_code == 200, listing.text
    assert {row["watcher_id"] for row in listing.json()} == {"main-observer", "branch-observer"}
    scoped = client.get(url, params=params)
    assert scoped.status_code == 200, scoped.text
    assert [row["watcher_id"] for row in scoped.json()] == ["branch-observer"]


def test_only_an_open_work_watcher_of_a_chat_turn_wakes_the_chat(manifest, tmp_path) -> None:
    from rcp.api.watchers import watcher_check_views

    app, _, episode, loop_turn = _app_branch(manifest, tmp_path, owner_mode="experiment_loop")
    project_id = episode.project_id
    store = app.state.background_tasks.store
    _turn(store, project_id, "work-turn")
    work = _external(store, project_id, "work", "work-turn")
    loop = _external(store, project_id, "loop", loop_turn.operation_id)

    def wakes(watcher: WatcherRecord) -> bool:
        (view,) = watcher_check_views(store, [watcher])
        return bool(view["wakes_chat"])

    assert wakes(work)
    assert not wakes(loop)
    assert not wakes(work.model_copy(update={"delivery_closed_at": store.now()}))
    assert not wakes(work.model_copy(update={"cancelled_at": store.now(), "cancelled_by": "human"}))
