from __future__ import annotations

import pytest

from rcp.agents.launcher import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.runs.space_chat import SpaceThreadOwner
from tests.helpers import agent_patch_json, create_named_app, seed_patch
from tests.test_dispatch_authority import _authorizer, _work_authority, _work_request
from tests.test_storage import _task_record


def test_space_collection_exact_binding_and_replay(manifest, tmp_path, monkeypatch):
    from rcp.runs.space_projects import collect_space_projects, prepare_space_projects

    owner, request, execution, stage, projects, excluded = _space_turn(manifest, tmp_path)
    store = owner.store
    assert len(projects) == 1
    project = projects[0]
    assert (
        store.agent_task_authority(
            project.project_id, execution.operation_id
        ).dispatch_authority.scope.run_truth_scope
        == project.context.run_truth_scope
    )
    before = project.service.history.state().revision
    project.patch_path.write_text(agent_patch_json(seed_patch()))
    (stage / "patch.json").write_text("stray")
    (stage / "watch.json").write_text("stray")
    original_receipt = store.record_agent_task_receipt

    def fail_after_collection(operation_id, category, detail, **kwargs):
        if category == "space_project_results":
            raise ValueError("post-collection failure")
        return original_receipt(operation_id, category, detail, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store, "record_agent_task_receipt", fail_after_collection)
        with pytest.raises(ValueError):
            collect_space_projects(owner, execution, projects, excluded, stage)
    assert project.service.history.state().revision == before + 1
    assert not (stage / "patch.json").exists()
    assert not (stage / "watch.json").exists()
    assert not project.patch_path.exists()
    # A post-collection failure retries under a new operation, even if a crash
    # or provider replay restores the consumed file.
    retry = store.create_agent_task(
        store.agent_task(execution.operation_id).model_copy(
            update={
                "operation_id": "space-retry",
                "parent_operation_id": execution.operation_id,
                "attempt": 2,
            }
        )
    )
    execution = AgentTaskExecution(
        retry.operation_id, store, AgentProcessControl(), continuation="retry"
    )
    execution.checkpoint_stage("", str(stage))
    projects, _, excluded = prepare_space_projects(owner, request, execution, stage)
    project = projects[0]
    project.patch_path.write_text(agent_patch_json(seed_patch()))
    assert _statuses(collect_space_projects(owner, execution, projects, excluded, stage)) == {
        project.project_id: "applied"
    }
    assert not project.patch_path.exists()
    assert project.service.history.state().revision == before + 1
    with store.connection() as connection:
        connection.execute(
            "DELETE FROM space_turn_project_bindings WHERE operation_id=?", (retry.operation_id,)
        )
    project.patch_path.write_text("invalid")
    assert _statuses(collect_space_projects(owner, execution, projects, excluded, stage)) == {
        project.project_id: "rejected"
    }
    assert not project.patch_path.exists()


def _statuses(results):
    return {project_id: result.status for project_id, result in results.items()}


def _space_turn(manifest, tmp_path, operation_id="space-test"):
    from rcp.runs.space_projects import prepare_space_projects

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    human = _authorizer(store)
    threads = store.ensure_agent_threads(
        human.user_id, [app.state.default_project_id], default_names=("Pip",)
    )
    thread = next(item for item in threads if item.kind == "space")
    request = _work_request().model_copy(update={"chat_id": thread.thread_id})
    task = store.create_agent_task(
        _task_record(
            store.now(),
            kind="space_chat",
            status="failed",
            operation_id=operation_id,
            project_id=store.space_id,
            request=request.model_dump(mode="json"),
            authorized_by=human,
            dispatch_authority=_work_authority(request),
        )
    )
    owner = SpaceThreadOwner(store, app.state.catalog, thread.thread_id)
    stage = tmp_path / "data" / "run-stage" / operation_id
    stage.mkdir(parents=True)
    execution = AgentTaskExecution(task.operation_id, store, AgentProcessControl())
    execution.checkpoint_stage("", str(stage))
    projects, _, excluded = prepare_space_projects(owner, request, execution, stage)
    return owner, request, execution, stage, projects, excluded


def test_space_collection_uses_work_apply_guards(manifest, tmp_path, monkeypatch):
    from rcp.runs.space_projects import collect_space_projects
    from rcp.transport.state import BatchPublishFailed

    owner, _, execution, stage, projects, excluded = _space_turn(manifest, tmp_path)
    (project,) = projects
    history = project.service.history
    before = len(history.load_patches())
    # An empty Patch appends no transition.
    project.patch_path.write_text(agent_patch_json(seed_patch().model_copy(update={"ops": []})))
    results = collect_space_projects(owner, execution, projects, excluded, stage)
    assert _statuses(results) == {project.project_id: "none"}
    assert len(history.load_patches()) == before
    assert owner.store.agent_task_patch_output(execution.operation_id) is not None
    # A commit point that cannot say whether it published is not a rejection,
    # and the Patch stays for the retry that resolves it.
    project.patch_path.write_text(agent_patch_json(seed_patch()))

    def unknown(*args, **kwargs):
        raise BatchPublishFailed("lost at the commit point", commit_status="unknown")

    monkeypatch.setattr(history, "append", unknown)
    (result,) = collect_space_projects(owner, execution, projects, excluded, stage).values()
    assert (result.status, result.commit_status) == ("unavailable", "unknown")
    assert project.patch_path.exists()


def test_space_binding_change_is_a_visible_rejection(manifest, tmp_path):
    from rcp.runs.space_projects import collect_space_projects, prepare_space_projects

    owner, request, execution, stage, projects, _ = _space_turn(manifest, tmp_path)
    (project,) = projects
    with owner.store.connection() as connection:
        connection.execute(
            "UPDATE space_turn_project_bindings SET write_scope_fingerprint='changed' WHERE operation_id=?",
            (execution.operation_id,),
        )
    projects, _, excluded = prepare_space_projects(owner, request, execution, stage)
    assert projects == [] and project.project_id in excluded
    project.patch_path.write_text(agent_patch_json(seed_patch()))
    results = collect_space_projects(owner, execution, projects, excluded, stage)
    assert _statuses(results) == {project.project_id: "rejected"}
    assert not project.patch_path.exists()
