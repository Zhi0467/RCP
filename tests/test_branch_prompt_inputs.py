from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from rcp.agents.write_scope import registered_repository_roots, resolve_project_write_scope
from rcp.core.models import EpisodeWorktreeBinding
from rcp.runs.chat import _chat_read_dirs
from rcp.runs.shared import (
    _stage_context_paths,
    _stage_or_reuse_task_input,
    stage_branch_read_context,
)
from rcp.service import RunRequest
from rcp.transport import RemoteRunStage

from .test_branch_context import branch_services as branch_services


@pytest.mark.parametrize("target", ["main", "branch"])
@pytest.mark.parametrize("remote", [False, True])
def test_main_graph_is_an_immutable_execution_host_input(
    branch_services, tmp_path, monkeypatch, target, remote
):
    main, branch = branch_services
    service = main if target == "main" else branch
    request = RunRequest(chat_scope="project", run_truth_scope=["repo-a"])
    context = service.assemble_chat(request)
    stage = tmp_path / "stage"
    stage.mkdir()
    remote_stage = None
    uploaded: dict[str, tuple[str, int]] = {}
    if remote:
        remote_stage = RemoteRunStage("execution.example")
        remote_stage.root = PurePosixPath("/remote/task")
        service.manifest.repository_map["repo-a"].path = "/remote/shared"

        def put_file(source, label, *, reuse=False):
            assert remote_stage.root is not None
            uploaded[label] = (source.read_text(), source.stat().st_mode & 0o777)
            return str(remote_stage.root / "inputs" / label)

        monkeypatch.setattr(
            remote_stage, "prepare_read_context_input", lambda label: label in uploaded
        )
        monkeypatch.setattr(remote_stage, "put_file", put_file)
        context = context.model_copy(
            update=_stage_context_paths(context, service, remote_stage, "laptop")
        )
    graph_path = context.graph_path
    updated = context.model_copy(
        update=stage_branch_read_context(context, service, None if remote else stage, remote_stage)
    )
    assert updated.graph_path == graph_path
    assert updated.repositories == context.repositories
    if target == "main":
        assert updated.main_graph_path is None and updated.shared_repositories == []
        assert uploaded == {} and list(stage.iterdir()) == []
        return
    path = updated.main_graph_path
    assert path is not None
    if remote:
        assert path.startswith("/remote/task/inputs/")
        content, mode = uploaded[PurePosixPath(path).name]
        assert updated.shared_repositories[0].path == "/remote/shared"
        assert updated.shared_repositories[0].host == ""
    else:
        content, mode = Path(path).read_text(), Path(path).stat().st_mode
        assert Path(path).parent == stage / "inputs"
        assert updated.shared_repositories[0].path == main.manifest.repository_map["repo-a"].path
    assert mode & 0o222 == 0
    assert "ev/main-only" in json.loads(content)["nodes"]
    assert "ev/branch-only" not in json.loads(content)["nodes"]
    assert (
        stage_branch_read_context(context, service, None if remote else stage, remote_stage)[
            "main_graph_path"
        ]
        == path
    )


def test_branch_read_pointers_do_not_expand_worktree_write_or_include_roots(
    branch_services, tmp_path
):
    main, service = branch_services
    context = service.assemble_chat(RunRequest(chat_scope="project", run_truth_scope=["repo-a"]))
    shared = Path(main.manifest.repository_map["repo-a"].path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git_dir = shared / ".git"
    git_dir.mkdir(exist_ok=True)
    stage = tmp_path / "stage"
    workspace = stage / "workspace"
    workspace.mkdir(parents=True)
    binding = EpisodeWorktreeBinding(
        owner_episode_id="owner",
        repository_alias="repo-a",
        machine="laptop",
        execution_host="",
        shared_path=str(shared),
        worktree_path=str(worktree),
        git_common_dir=str(git_dir),
        branch="experiment",
        starting_branch="main",
        starting_commit="a" * 40,
    )
    context.repositories = [
        item.model_copy(update={"path": str(worktree)}) for item in context.repositories
    ]

    def scope(current):
        return resolve_project_write_scope(
            manifest=service.manifest,
            project_id="project",
            execution_machine="laptop",
            capability="work_auto",
            stage_root=str(stage),
            workspace_root=str(workspace),
            admitted_aliases=["repo-a"],
            repository_pointers=current.repositories,
            remote_stage=None,
            app_data_dir=tmp_path / "data",
            repository_inventory=registered_repository_roots(
                service.manifest, project_id="project"
            ),
            conversation_worktree=binding,
        )

    before = scope(context)
    includes = _chat_read_dirs(context, stage, None, service, "laptop")
    updated = context.model_copy(update=stage_branch_read_context(context, service, stage, None))
    assert scope(updated) == before
    assert str(shared) not in before.writable_roots
    assert before.repository_roots == [str(worktree)]
    assert _chat_read_dirs(updated, stage, None, service, "laptop") == includes
    assert shared not in includes
    assert updated.shared_repositories[0].path == str(shared)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("prefix", ["main-graph-", "loop-status-watchers-"])
def test_read_context_inputs_reuse_without_readback_and_retire_only_superseded_snapshots(
    tmp_path, monkeypatch, remote, prefix
):
    root = tmp_path / "stage"
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    other_prefix = "loop-status-watchers-" if prefix == "main-graph-" else "main-graph-"
    kept = ["master-context.md", f"{other_prefix}{'f' * 64}.json"]
    for name in kept:
        (inputs / name).write_text("{}")
        (inputs / name).chmod(0o400)
    stage = RemoteRunStage("execution.example") if remote else None
    uploads = []
    if stage is not None:
        stage.root = PurePosixPath(str(root))

        def ssh(arguments):
            return subprocess.run(arguments, capture_output=True, text=True, check=False)

        def put_file(source, label, *, reuse=False):
            target = inputs / label
            target.write_bytes(source.read_bytes())
            target.chmod(0o400)
            uploads.append(label)
            return str(target)

        def forbidden_read(label):
            pytest.fail(f"readback attempted for {label}")

        monkeypatch.setattr(stage, "_ssh", ssh)
        monkeypatch.setattr(stage, "put_file", put_file)
        monkeypatch.setattr(stage, "read_input_text", forbidden_read)

    previous = None
    for content in ("[]", '[{"id":"watcher"}]'):
        label = f"{prefix}{hashlib.sha256(content.encode()).hexdigest()}.json"
        path = _stage_or_reuse_task_input(None if remote else root, stage, label, content)
        assert Path(path).read_text() == content
        assert Path(path).stat().st_mode & 0o222 == 0
        if previous is not None:
            assert not Path(previous).exists()
        with monkeypatch.context() as guard:
            guard.setattr(Path, "read_text", lambda *args, **kwargs: pytest.fail("input readback"))
            assert (
                _stage_or_reuse_task_input(None if remote else root, stage, label, content) == path
            )
        assert {item.name for item in inputs.iterdir()} == {*kept, label}
        previous = path
    assert len(uploads) == (2 if remote else 0)


def test_branch_chat_turns_expose_staged_main_graph(manifest, tmp_path, monkeypatch):
    import re
    import uuid

    from .helpers import signed_in_client, wait_for_task
    from .test_api import ScriptedLauncher
    from .test_branch_chats import _app_branch

    app, service, episode, _root = _app_branch(manifest, tmp_path)
    launcher = ScriptedLauncher([{}], message="Inspected branch inputs.")
    monkeypatch.setattr(app.state.launcher, "stream", launcher.stream)
    client = signed_in_client(app)
    chat_id = str(uuid.uuid4())
    for mode in ("discuss", "work"):
        response = client.post(
            f"/api/projects/{app.state.default_project_id}/tasks/node_chat?branch_id={episode.graph_target.branch_id}",
            json={
                "chat_id": chat_id,
                "node_id": "rq/learning-after-shift",
                "message": "Inspect branch inputs.",
                "mode": mode,
                "run_truth_scope": ["repo-a"],
            },
        )
        assert response.status_code == 202, response.text
        task = wait_for_task(app.state.catalog.store, response.json()["operation_id"])
        assert task.status == "succeeded", task.error
        assert task.graph_target == episode.graph_target
        blocks = [
            json.loads(value)
            for value in re.findall(r"```json\n(.*?)\n```", launcher.prompts[-1], re.S)
        ]
        pointers = next(block for block in blocks if "main_graph_path" in block)
        path = Path(pointers["main_graph_path"])
        assert path.is_file() and path.stat().st_mode & 0o222 == 0
        assert json.loads(path.read_text()) == json.loads(
            (service.history.root / "graph.json").read_text()
        )
        assert (
            pointers["shared_repositories"][0]["path"]
            == service.manifest.repository_map["repo-a"].path
        )


@pytest.mark.parametrize("owner", ["work", "experiment_loop"])
def test_branch_graph_repair_refreshes_inline_read_context(manifest, tmp_path, monkeypatch, owner):
    import re
    import uuid

    from .helpers import agent_patch_json, shape_invalid_patch, signed_in_client, wait_for_task
    from .test_api import ScriptedLauncher
    from .test_branch_chats import _app_branch

    app, service, branch, _root = _app_branch(manifest, tmp_path, include_experiment=True)
    store = app.state.catalog.store
    client = signed_in_client(app)
    base = f"/api/projects/{branch.project_id}"
    invalid = agent_patch_json(shape_invalid_patch().model_copy(update={"kind": "work"}))
    repaired_patch = json.dumps(
        {
            "summary": "Complete the bounded work.",
            "ops": [
                {
                    "op": "update_nodes",
                    "nodes": [{"id": "exp/bounded-loop", "changes": {"status": "completed"}}],
                }
            ],
            "repositories_read": [],
            "change_summary": [],
        }
    )
    initial_files = {"patch.json": invalid}
    if owner == "experiment_loop":
        repository = service.manifest.repository_map["repo-a"].path
        initial_files["watch.json"] = json.dumps(
            {
                "external": [
                    {
                        "check_command": "false",
                        "log_path": str(Path(repository) / "check.log"),
                        "cwd": repository,
                    }
                ],
                "graph": [],
            }
        )
    launcher = ScriptedLauncher(
        [initial_files] * 3 + [{"patch.json": repaired_patch}], message="Inspected."
    )
    monkeypatch.setattr(app.state.launcher, "stream", launcher.stream)
    payload = {"chat_id": str(uuid.uuid4())}
    if owner == "work":
        route = "tasks/node_chat"
        payload.update(node_id="exp/bounded-loop", message="Inspect this node.", mode="work")
    else:
        route = "experiments/exp%2Fbounded-loop/run"
    started = client.post(f"{base}/{route}?branch_id={branch.episode_id}", json=payload)
    assert started.status_code == 202, started.text
    previous = wait_for_task(store, started.json()["operation_id"])
    assert previous.result is not None and isinstance(previous.result.get("graph_update"), dict), (
        previous.error,
        previous.result,
    )
    graph_update = previous.result["graph_update"]
    assert isinstance(graph_update, dict) and graph_update["repairable"], previous.result
    repaired = client.post(f"{base}/tasks/{previous.operation_id}/repair-graph-update", json={})
    assert repaired.status_code == 202, repaired.text
    task = wait_for_task(store, repaired.json()["operation_id"])
    assert task.status == "succeeded", task.error
    assert task.result is not None
    graph_update = task.result["graph_update"]
    assert isinstance(graph_update, dict) and graph_update["status"] == "applied", task.result
    blocks = [
        json.loads(value)
        for value in re.findall(r"```json\n(.*?)\n```", launcher.prompts[-1], re.S)
    ]
    pointers = next(block for block in blocks if "main_graph_path" in block)
    path = Path(pointers["main_graph_path"])
    assert path.is_file() and path.stat().st_mode & 0o222 == 0
    assert json.loads(path.read_text()) == json.loads(
        (service.history.root / "graph.json").read_text()
    )
    statuses = [block["loop_status"] for block in blocks if "loop_status" in block]
    if owner == "work":
        assert len(statuses) == 1 and statuses[0]["state"] == "none"
        assert Path(statuses[0]["watcher_state_path"]).is_file()
    else:
        assert statuses == []
