from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from rcp.agents.write_scope import ProjectWriteScope
from rcp.api.episodes import StartEpisodeBody
from rcp.core.models import EpisodeIsolation, EpisodeWorktreeBinding
from rcp.runs.auto_research import AutoResearchStartRequest
from rcp.runs.episodes.isolation import validate_episode_admission, validate_episode_launch
from rcp.service import RunRequest
from rcp.storage import AppStore, ProjectRecord

from . import test_conversation_worktree_git as worktree_fixture
from .test_auto_research_children_storage import (
    _experiment_route,
    _experiment_task,
    _setup_parent,
)
from .test_episode_storage import _episode

repository = worktree_fixture.repository


def test_run_defaults_and_auto_graph_lock() -> None:
    auto = AutoResearchStartRequest(invocation_ceiling=1)
    experiment = RunRequest()
    assert (auto.code_worktree, auto.graph_isolation) == (True, True)
    assert (experiment.code_worktree, experiment.graph_isolation) == (False, False)
    with pytest.raises(ValidationError) as error:
        StartEpisodeBody(mode="auto_research", invocation_ceiling=1, graph_isolation=False)
    assert error.value.errors()[0]["type"] == "literal_error"


def _store(manifest, tmp_path):
    store = AppStore(tmp_path / "data" / "rcp.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator=str(manifest.path),
            name="project",
            state_location=str(manifest.research_dir),
            state_remote=False,
            added_at=store.now(),
        )
    )
    return store


def test_code_admission_requires_one_repository(manifest, tmp_path) -> None:
    store = _store(manifest, tmp_path)
    with pytest.raises(ValueError) as error:
        validate_episode_admission(
            store,
            "project",
            RunRequest(
                code_worktree=True,
                run_on="laptop",
                run_truth_scope=["repo-a", "repo-b"],
            ),
        )
    assert error.value.args == ("episode_isolation_requires_one_repository",)
    assert store.episodes("project") == []


def test_experiment_graph_creation_is_not_silently_accepted(manifest, tmp_path) -> None:
    store = _store(manifest, tmp_path)
    with pytest.raises(ValueError) as error:
        validate_episode_admission(
            store,
            "project",
            RunRequest(
                graph_isolation=True,
                patch_kind="experiment_loop",
            ),
        )
    assert error.value.args == ("experiment_graph_isolation_not_implemented",)


def test_code_admission_refuses_old_execution_host_git(manifest, tmp_path, monkeypatch) -> None:
    store = _store(manifest, tmp_path)
    operations = []

    def probe(*args, **kwargs):
        operations.append(kwargs["operation"])
        return {"version": [2, 37, 9], "supported": False}

    monkeypatch.setattr("rcp.conversation_worktrees.worktree_command", probe)
    with pytest.raises(ValueError):
        validate_episode_admission(
            store,
            "project",
            RunRequest(code_worktree=True, run_on="laptop", run_truth_scope=["repo-a"]),
        )
    assert operations == ["git_version"]
    assert store.episodes("project") == []


def test_episode_choices_cannot_change_after_binding(manifest, tmp_path) -> None:
    store = _store(manifest, tmp_path)
    owner = _episode(store, "owner").model_copy(update={"isolation_owner_episode_id": "owner"})
    store.create_episode(owner)
    store.create_episode_isolation("project", EpisodeIsolation(owner_episode_id="owner"))
    with pytest.raises(ValueError) as error:
        validate_episode_admission(
            store,
            "project",
            RunRequest(
                control_episode_id="owner",
                code_worktree=True,
                patch_kind="experiment_loop",
            ),
        )
    assert error.value.args == ("episode_isolation_choices_changed",)
    assert store.episode("owner").code_worktree is False


@pytest.mark.parametrize("child", [True, False])
def test_child_and_human_branch_experiment_reuse_owner(tmp_path, child) -> None:
    store, owner, root = _setup_parent(tmp_path)
    store.create_episode_isolation(
        "project",
        EpisodeIsolation(
            owner_episode_id=owner.episode_id,
            graph_branch_id=owner.graph_target.branch_id,
        ),
    )
    task = _experiment_task(
        store,
        str(uuid.uuid4()),
        owner.authorized_by,
        node_id="exp/one",
        trigger="orchestrator" if child else "experiment_run",
    )
    route = _experiment_route(store, owner, root, task) if child else None
    stored = store.create_experiment_episode_with_invocation(task, auto_research_route=route)
    episode = store.episode(stored.episode_id)
    assert episode.isolation_owner_episode_id == owner.episode_id
    assert episode.graph_isolation is True
    assert store.episode_isolation("project", episode.episode_id) is None
    reopened = AppStore(store.path)
    assert reopened.episode(episode.episode_id).isolation_owner_episode_id == owner.episode_id
    assert (
        reopened.episode_isolation("project", owner.episode_id).owner_episode_id == owner.episode_id
    )


def test_branch_experiment_host_must_match_owner(manifest, tmp_path) -> None:
    store = _store(manifest, tmp_path)
    owner = _episode(store, "owner", mode="auto_research").model_copy(
        update={
            "code_worktree": True,
            "isolation_owner_episode_id": "owner",
        }
    )
    store.create_episode(owner)
    store.create_episode_isolation(
        "project",
        EpisodeIsolation(
            owner_episode_id="owner",
            graph_branch_id="owner",
            worktree=EpisodeWorktreeBinding(
                owner_episode_id="owner",
                repository_alias="repo-a",
                machine="laptop",
                execution_host="another-host",
                shared_path=manifest.repository_map["repo-a"].path,
                worktree_path=str(tmp_path / "worktree"),
                git_common_dir=str(tmp_path / "git"),
                branch="episode",
                starting_branch="main",
                starting_commit="a" * 40,
            ),
        ),
    )
    with pytest.raises(ValueError) as error:
        validate_episode_admission(
            store,
            "project",
            RunRequest(
                run_on="laptop",
                run_truth_scope=["repo-a"],
                patch_kind="experiment_loop",
            ),
            graph_target=owner.graph_target,
        )
    assert error.value.args == ("episode_isolation_host_mismatch",)


def test_scratch_only_episode_launch_still_checks_missing_worktree(repository, tmp_path) -> None:
    store = AppStore(tmp_path / "data" / "rcp.sqlite3")
    owner = _episode(store, "owner").model_copy(
        update={
            "code_worktree": True,
            "isolation_owner_episode_id": "owner",
        }
    )
    store.create_episode(owner)
    facts = worktree_fixture.run("plan", shared_path=str(repository), owner_episode_id="owner")
    binding = EpisodeWorktreeBinding(
        **facts, repository_alias="repo", machine="local", execution_host=""
    )
    store.create_episode_isolation(
        "project", EpisodeIsolation(owner_episode_id="owner", worktree=binding)
    )
    worktree_fixture.run("create", binding=binding.model_dump())
    store.set_episode_isolation_status(
        "project", "owner", expected_status="creating", status="ready"
    )
    scope = ProjectWriteScope.create(
        project_id="project",
        execution_machine="local",
        execution_host="",
        capability="work_auto",
        stage_root=str(tmp_path / "stage"),
        workspace_root=str(tmp_path / "stage" / "workspace"),
        repositories=[],
        protected_write_paths=[],
    )
    validate_episode_launch(store, "owner", scope)
    Path(binding.worktree_path).rename(tmp_path / "moved-worktree")
    with pytest.raises(ValueError) as error:
        validate_episode_launch(store, "owner", scope)
    assert error.value.args != ("episode_isolation_scope_mismatch",)
    assert scope.repository_roots == []
    assert store.episode_isolation_state("project", "owner").status == "ready"
    assert store.episode_isolation("project", "owner").worktree == binding
