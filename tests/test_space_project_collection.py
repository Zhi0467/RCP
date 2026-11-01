from __future__ import annotations

from rcp.agents.launcher import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.runs.space_chat import SpaceThreadOwner
from tests.helpers import agent_patch_json, create_named_app, seed_patch
from tests.test_dispatch_authority import _authorizer, _record, _work_authority, _work_request


def test_space_collection_exact_binding_and_replay(manifest, tmp_path):
    from rcp.runs.space_projects import collect_space_projects, prepare_space_projects

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    human = _authorizer(store)
    threads = store.ensure_agent_threads(
        human.user_id, [app.state.default_project_id], default_names=("Pip",)
    )
    thread = next(item for item in threads if item.kind == "space")
    request = _work_request().model_copy(update={"chat_id": thread.thread_id})
    task = _record(
        store,
        operation_id="space-test",
        project_id=store.space_id,
        request=request,
        authorized_by=human,
        dispatch_authority=_work_authority(request),
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET kind='space_chat' WHERE operation_id=?", (task.operation_id,)
        )
    owner = SpaceThreadOwner(store, app.state.catalog, thread.thread_id)
    stage = tmp_path / "data" / "run-stage" / "space-test"
    stage.mkdir(parents=True)
    execution = AgentTaskExecution(task.operation_id, store, AgentProcessControl())
    execution.checkpoint_stage("", str(stage))
    projects, scope = prepare_space_projects(owner, request, execution, stage)
    assert len(projects) == 1
    project = projects[0]
    assert (
        store.agent_task_authority(
            project.project_id, task.operation_id
        ).dispatch_authority.scope.run_truth_scope
        == project.context.run_truth_scope
    )
    before = project.service.history.state().revision
    project.patch_path.write_text(agent_patch_json(seed_patch()))
    (stage / "patch.json").write_text("stray")
    (stage / "watch.json").write_text("stray")
    results = collect_space_projects(owner, execution, projects, stage)
    assert results == {project.project_id: "applied"}
    assert project.service.history.state().revision == before + 1
    assert not (stage / "patch.json").exists()
    assert not (stage / "watch.json").exists()
    assert collect_space_projects(owner, execution, projects, stage) == results
    assert project.service.history.state().revision == before + 1
    with store.connection() as connection:
        connection.execute(
            "DELETE FROM space_turn_project_bindings WHERE operation_id=?", (task.operation_id,)
        )
    assert collect_space_projects(owner, execution, projects, stage) == {
        project.project_id: "rejected"
    }
