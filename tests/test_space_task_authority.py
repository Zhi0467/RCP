from __future__ import annotations

import json

import pytest

from rcp.core.transition_models import GraphTargetRef
from rcp.storage import AppStore
from tests.test_dispatch_authority import _authorizer, _record, _work_authority, _work_request


def test_space_patch_requires_exact_persisted_project_binding(tmp_path):
    store = AppStore(tmp_path / "app.sqlite3")
    human = _authorizer(store)
    request = _work_request()
    authority = _work_authority(request)
    # Old writer task shape, upgraded in place into the new kind: provenance
    # continues to use the existing task row and human snapshot.
    task = _record(
        store,
        operation_id="space-turn",
        project_id=store.space_id,
        request=request,
        authorized_by=human,
        dispatch_authority=authority,
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET kind='space_chat' WHERE operation_id=?", (task.operation_id,)
        )
        connection.execute(
            "INSERT INTO space_turn_project_bindings VALUES (?,?,?,?,?)",
            (
                task.operation_id,
                "allowed-project",
                authority.model_dump_json(),
                "scope",
                json.dumps({"kind": "main"}),
            ),
        )
    bound = store.agent_task_authority("allowed-project", task.operation_id)
    assert bound.project_id == "allowed-project"
    assert bound.authorized_by == human
    assert bound.dispatch_authority == authority
    assert bound.apply_target == GraphTargetRef()
    for project_id in ("other-project", store.space_id):
        with pytest.raises(KeyError):
            store.agent_task_authority(project_id, task.operation_id)
    assert AppStore(store.path).agent_task_authority("allowed-project", task.operation_id) == bound


def test_task_owner_resolves_space_without_opening_a_project(manifest, tmp_path):
    from rcp.runs.agent_threads import task_owner
    from rcp.runs.space_chat import SpaceThreadOwner
    from tests.helpers import create_named_app

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    human = _authorizer(store)
    thread = store.ensure_agent_threads(human.user_id, [], default_names=("Pip",))[0]
    request = _work_request().model_copy(update={"chat_id": thread.thread_id})
    task = _record(
        store,
        operation_id="space-owner",
        project_id=store.space_id,
        request=request,
        authorized_by=human,
        dispatch_authority=_work_authority(request),
    )
    task = task.model_copy(update={"kind": "space_chat"})
    owner = task_owner(store, task, catalog=app.state.catalog)
    assert isinstance(owner, SpaceThreadOwner)
    assert owner.thread.thread_id == thread.thread_id
    with pytest.raises(ValueError, match="binding"):
        task_owner(
            store, task.model_copy(update={"project_id": "wrong"}), catalog=app.state.catalog
        )


def test_space_task_cannot_be_a_watcher_origin(tmp_path):
    from tests.test_watchers import _record as watcher_record

    store = AppStore(tmp_path / "app.sqlite3")
    task = _record(store, operation_id="space-origin", project_id="project")
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET kind='space_chat' WHERE operation_id=?", (task.operation_id,)
        )
    with pytest.raises(ValueError, match="Space"):
        store.create_watchers([watcher_record("watch", origin=task.operation_id)])
