from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rcp.agents.write_scope import ProjectWriteScope
from rcp.api.episodes import StartEpisodeBody
from rcp.background import BackgroundAgentTasks
from rcp.core.models import EpisodeIsolation, EpisodeWorktreeBinding
from rcp.core.transition_models import GraphHeadRef
from rcp.runs.auto_research import AutoResearchRunRequest, AutoResearchStartRequest
from rcp.runs.auto_research_admission import reserve_auto_research
from rcp.runs.episodes.isolation import validate_episode_admission, validate_episode_launch
from rcp.service import RunRequest
from rcp.storage import AppStore, ProjectRecord

from . import test_conversation_worktree_git as worktree_fixture
from .helpers import create_named_app, fabricated_authorizer
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
    assert (auto.code_worktree, auto.graph_isolation) == (False, True)
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


@pytest.mark.parametrize("ineligible", [None, "repositories", "host", "git", "grant"])
def test_auto_code_default_is_resolved_and_persisted(manifest, tmp_path, monkeypatch, ineligible):
    if ineligible == "host":
        text = manifest.path.read_text()
        text = text.replace(
            '[[repositories]]\nalias = "repo-b"\nmachine = "laptop"',
            '[[repositories]]\nalias = "repo-b"\nmachine = "gpu"',
        ).replace('default_run_truth_scope = ["repo-a"]', 'default_run_truth_scope = ["repo-b"]')
        manifest.path.write_text(
            text.replace(
                "[[repositories]]",
                '[[machines]]\nalias = "gpu"\nhost = "gpu"\n\n[[repositories]]',
                1,
            )
        )
    if ineligible == "repositories":
        manifest.path.write_text(
            manifest.path.read_text().replace(
                'default_run_truth_scope = ["repo-a"]',
                'default_run_truth_scope = ["repo-a", "repo-b"]',
            )
        )
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.catalog.store
    monkeypatch.setattr(
        app.state.background_tasks, "_spawn_record", lambda record, *a, **kw: record
    )
    monkeypatch.setattr(
        "rcp.conversation_worktrees.worktree_command",
        lambda *a, **kw: {"version": [2, 38, 0], "supported": ineligible != "git"},
    )
    if ineligible == "grant":
        card = store.space_machine_for("") or store.create_space_machine(
            name="local", host="", os_account=""
        )
        store.update_space_machine(card.machine_id, writable_paths=[str(tmp_path)])
    response = TestClient(app).post(
        f"/api/projects/{app.state.default_project_id}/episodes",
        json={"mode": "auto_research", "invocation_ceiling": 1},
    )
    assert response.status_code == 202, response.text
    episode = store.episode(response.json()["episode_id"])
    assert episode.code_worktree is (ineligible is None)
    assert store.agent_task(episode.root_operation_id).request["code_worktree"] is (
        ineligible is None
    )


def test_internal_code_defaults_skip_eligibility(manifest, tmp_path, monkeypatch):
    store = _store(manifest, tmp_path)

    def unexpected(*args, **kwargs):
        raise AssertionError("unexpected eligibility check")

    monkeypatch.setattr("rcp.runs.episodes.isolation.load_manifest", unexpected)
    monkeypatch.setattr("rcp.runs.episodes.isolation.worktree_command", unexpected)
    start = AutoResearchStartRequest(
        invocation_ceiling=1, provider="codex", run_truth_scope=["repo-a"]
    )
    for request in (
        start,
        AutoResearchRunRequest(episode_id="new", role="orchestrator"),
        RunRequest(),
    ):
        assert request.code_worktree is False
        validate_episode_admission(store, "project", request)
    episode, task, resolved = reserve_auto_research(
        BackgroundAgentTasks(store, None),
        "project",
        start,
        authorized_by=fabricated_authorizer("Researcher"),
        graph_base_head=GraphHeadRef(revision=0),
    )
    assert episode.code_worktree is resolved.code_worktree is task.request["code_worktree"] is False


def test_code_admission_resolves_the_profile_machine(manifest, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "rcp.conversation_worktrees.worktree_command",
        lambda *a, **kw: {"version": [2, 38, 0], "supported": True},
    )
    assert "local" not in manifest.machine_map
    validate_episode_admission(
        _store(manifest, tmp_path),
        "project",
        RunRequest(code_worktree=True, run_truth_scope=["repo-a"]),
    )


def test_code_admission_refuses_old_execution_host_git(manifest, tmp_path, monkeypatch) -> None:
    store = _store(manifest, tmp_path)
    operations = []

    def probe(*args, **kwargs):
        operations.append(kwargs["operation"])
        return {"version": [2, 37, 9], "supported": False}

    monkeypatch.setattr("rcp.conversation_worktrees.worktree_command", probe)
    with pytest.raises(ValueError) as error:
        validate_episode_admission(
            store,
            "project",
            RunRequest(code_worktree=True, run_on="laptop", run_truth_scope=["repo-a"]),
        )
    assert error.value.args == ("episode_isolation_git_version",)
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
