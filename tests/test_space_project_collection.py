from __future__ import annotations

import pytest

from rcp.agents.launcher import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.runs.space_chat import SpaceThreadOwner
from tests.helpers import agent_patch_json, create_named_app, seed_patch
from tests.test_dispatch_authority import _authorizer, _work_authority, _work_request
from tests.test_storage import _task_record


def test_space_collection_exact_binding_and_replay(manifest, tmp_path, monkeypatch):
    from rcp.runs.space_projects import collect_space_projects

    owner, request, execution, stage, found = _space_turn(manifest, tmp_path)
    projects = found.writers
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
            collect_space_projects(owner, execution, found, stage)
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
    found = _prepared(owner, request, execution, stage)
    projects = found.writers
    project = projects[0]
    project.patch_path.write_text(agent_patch_json(seed_patch()))
    assert _statuses(collect_space_projects(owner, execution, found, stage)) == {
        project.project_id: "applied"
    }
    assert not project.patch_path.exists()
    assert project.service.history.state().revision == before + 1
    with store.connection() as connection:
        connection.execute(
            "DELETE FROM space_turn_project_bindings WHERE operation_id=?", (retry.operation_id,)
        )
    project.patch_path.write_text("invalid")
    assert _statuses(collect_space_projects(owner, execution, found, stage)) == {
        project.project_id: "rejected"
    }
    assert not project.patch_path.exists()


def _statuses(results):
    return {project_id: result.status for project_id, result in results.items()}


def _space_turn(manifest, tmp_path, operation_id="space-test"):
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
    return owner, request, execution, stage, _prepared(owner, request, execution, stage)


def _prepared(owner, request, execution, stage):
    from rcp.runs.space_projects import bind_space_projects, prepare_space_projects

    found = prepare_space_projects(owner, request, execution, stage)
    bind_space_projects(owner, execution, stage, found)
    return found


def test_space_collection_uses_work_apply_guards(manifest, tmp_path, monkeypatch):
    from rcp.runs.space_projects import collect_space_projects
    from rcp.transport.state import BatchPublishFailed

    owner, _, execution, stage, found = _space_turn(manifest, tmp_path)
    projects = found.writers
    (project,) = projects
    history = project.service.history
    before = len(history.load_patches())
    # An empty Patch appends no transition.
    project.patch_path.write_text(agent_patch_json(seed_patch().model_copy(update={"ops": []})))
    results = collect_space_projects(owner, execution, found, stage)
    assert _statuses(results) == {project.project_id: "none"}
    assert len(history.load_patches()) == before
    assert owner.store.agent_task_patch_output(execution.operation_id) is not None
    # A commit point that cannot say whether it published is not a rejection,
    # and the Patch stays for the retry that resolves it.
    project.patch_path.write_text(agent_patch_json(seed_patch()))

    def unknown(*args, **kwargs):
        raise BatchPublishFailed("lost at the commit point", commit_status="unknown")

    monkeypatch.setattr(history, "append", unknown)
    (result,) = collect_space_projects(owner, execution, found, stage).values()
    assert (result.status, result.commit_status) == ("unavailable", "unknown")
    assert project.patch_path.exists()


def test_space_binding_change_is_a_visible_rejection(manifest, tmp_path, monkeypatch):
    from rcp.runs.space_projects import collect_space_projects

    owner, request, execution, stage, found = _space_turn(manifest, tmp_path)
    projects = found.writers
    (project,) = projects
    with owner.store.connection() as connection:
        connection.execute(
            "UPDATE space_turn_project_bindings SET write_scope_fingerprint='changed' WHERE operation_id=?",
            (execution.operation_id,),
        )
    found = _prepared(owner, request, execution, stage)
    projects = found.writers
    assert projects == [] and found.excluded[project.project_id]["code"] == "binding_changed"
    project.patch_path.write_text(agent_patch_json(seed_patch()))
    # A project the member gained mid-turn is reported, not silently discarded.
    late = stage / "projects" / "late-project" / "patch.json"
    late.parent.mkdir(parents=True)
    late.write_text(agent_patch_json(seed_patch()))
    members = owner.store.member_project_ids(owner.thread.user_id)
    monkeypatch.setattr(owner.store, "member_project_ids", lambda _: {*members, "late-project"})
    results = collect_space_projects(owner, execution, found, stage)
    assert _statuses(results) == {project.project_id: "rejected", "late-project": "rejected"}
    assert found.excluded["late-project"]["code"] == "added_after_start"
    assert not project.patch_path.exists() and not late.exists()


def test_space_remote_base_project_gets_a_read_only_client(manifest, tmp_path, monkeypatch):
    from rcp.agents.command_protocol import GraphLogCommandRequest, ValidateCommandRequest
    from rcp.runs.space_projects import prepare_space_projects
    from rcp.service import ProjectService

    owner, request, execution, stage, found = _space_turn(manifest, tmp_path)
    (writer,) = found.writers
    assert writer.read_scope.verbs | {"bulletin", "validate"} == writer.verbs
    loaded = ProjectService.manifest.fget

    def remote_base(service):
        local = loaded(service)
        machines = [item.model_copy(update={"host": "gpu.example"}) for item in local.machines]
        return local.model_copy(update={"machines": machines})

    with monkeypatch.context() as patch:
        patch.setattr(ProjectService, "manifest", property(remote_base))
        found = prepare_space_projects(owner, request, execution, stage)
    # State readable here keeps a read client; writing goes through handoff.
    (reader,) = found.readers
    assert found.writers == [] and found.excluded[reader.project_id]["code"] == "base_remote"
    assert reader.verbs == reader.read_scope.verbs | {"bulletin"}
    identity = {"mailbox_id": "a" * 32, "request_id": "b" * 32, "credential": "c" * 64}
    validate = ValidateCommandRequest(verb="validate", arguments={"patch": "{}"}, **identity)
    log = GraphLogCommandRequest(verb="graph_log", arguments={"id": "missing"}, **identity)
    assert reader.command(validate, None).status == "invalid"
    assert reader.command(log, None).status == "ok"


def test_space_git_failure_leaves_out_only_that_project(manifest, tmp_path):
    from rcp.agents.git_access import ProviderGitAccess
    from rcp.runs.space_chat import _prepare_space_git

    owner, request, execution, stage, found = _space_turn(manifest, tmp_path)
    (project,) = found.writers
    access = ProviderGitAccess(None, tmp_path, "", (("repo", "/checkout"),))
    _prepare_space_git(owner, execution, found, lambda *_: access)
    assert found.writers and execution.git_access == access

    def unreachable(*_):
        raise OSError("transport down")

    _prepare_space_git(owner, execution, found, unreachable)
    assert found.writers == []
    assert found.excluded[project.project_id]["code"] == "git_unavailable"


def test_space_limits_derive_lacking_commands_from_grants(manifest, tmp_path):
    from rcp.agents.write_scope import CompositeWriteScope
    from rcp.runs.space_chat import space_limits_section

    _, _, _, stage, found = _space_turn(manifest, tmp_path)
    scope = CompositeWriteScope(stage_root=str(stage))
    section = space_limits_section(frozenset({"handoff", "schedule"}), scope, found)
    # Granted verbs never show as lacking; lifecycle verbs the turn lacks do.
    assert "`episode`" in section and "`launch`" in section
    assert "`schedule`" not in section and "`bulletin`" not in section


def test_task_result_keeps_bounded_space_project_results():
    import json

    from rcp.storage import AppStore

    stored = AppStore._bounded_result_json({"project_results": {"p": "applied", "q": 3}})
    assert stored is not None
    assert json.loads(stored)["project_results"] == {"p": "applied"}
