"""Every agent reads every project repository; only the graph's truth scope is written."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents.context import ContextAssembler
from rcp.agents.group_context import build_group_context
from rcp.agents.project_repositories import admitted_write_repositories
from rcp.agents.prompts import render_chat_read_context
from rcp.agents.schema import AgentPatch, prepare_agent_patch
from rcp.core.models import GraphState, Patch
from rcp.core.transition_models import GraphTargetRef
from rcp.core.validation import ValidationContext, validate_patch
from rcp.history import HistoryManager
from rcp.paper import PaperService
from rcp.service import ProjectService, RunRequest
from rcp.storage import AppStore
from tests.helpers import create_named_app, signed_in_client, unchecked_write_authority
from tests.test_branch_context import branch_services as branch_services
from tests.test_write_scope import _resolve_local


def test_a_read_only_repository_reaches_every_launch_but_is_never_a_write_root(
    manifest, tmp_path
) -> None:
    state = GraphState(project_truth_scope=["repo-a"])  # repo-b: agents only read it
    assembler = ContextAssembler(manifest)
    run = assembler.assemble(state)
    chat = assembler.chat_context(state, graph_target=GraphTargetRef())
    coach = assembler.paper_pointers()

    for context in (run, chat, coach):
        assert [pointer.alias for pointer in context.repositories] == ["repo-a", "repo-b"]
    assert run.run_truth_scope == chat.run_truth_scope == ["repo-a"]
    with pytest.raises(ValueError):
        assembler.chat_context(state, graph_target=GraphTargetRef(), writable=["repo-b"])
    # A client's pick never selects; only a worktree names its one writable repository.
    assert admitted_write_repositories(state, ["repo-b"], worktree=False) == ["repo-a"]
    assert admitted_write_repositories(state, ["repo-a"], worktree=True) == ["repo-a"]
    with pytest.raises(ValueError):
        admitted_write_repositories(state, ["repo-b"], worktree=True)
    with pytest.raises(ValueError, match="outside this project"):
        _resolve_local(manifest, tmp_path, aliases=["repo-b"], truth_scope=["repo-a"])


@pytest.mark.parametrize("mode", ["admission", "replay"])
def test_agent_patches_record_reads_of_any_project_repository(mode) -> None:
    # An older Patch stamped a narrower run scope than the truth scope; it still replays.
    patch = Patch(
        kind="refresh",
        author="agent",
        summary="Read a repository agents cannot write.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a", "repo-b"],
        ops=[],
    )
    if mode == "replay":
        patch = patch.model_copy(update={"revision": 1})
    context = ValidationContext(["repo-a", "repo-c"], ["repo-a", "repo-b", "repo-c"], mode=mode)
    # A Patch written before reads were stamped keeps its exact bytes.
    stored = patch.model_dump(mode="json")
    assert Patch.model_validate(stored).model_dump(mode="json") == stored
    assert "run_read_scope" not in stored

    assert not validate_patch(GraphState(), patch, context).rejected
    # Without a stamp, a read outside this project is refused, a group sibling's included.
    for outside in ("repo-z", "11111111-1111-4111-8111-111111111111:repo-a"):
        unread = patch.model_copy(update={"repositories_read": [outside]})
        assert "read-outside-run-scope" in {
            message.code for message in validate_patch(GraphState(), unread, context).messages
        }


def test_a_group_sibling_repository_read_is_stamped_and_admitted(
    branch_services, tmp_path, monkeypatch
) -> None:
    service, _ = branch_services
    sibling_id = "22222222-2222-4222-8222-222222222222"
    sibling = service.manifest.model_copy(deep=True)
    sibling.repository_map["repo-a"].path = str(tmp_path / "sibling-a")
    sibling.repository_map["repo-b"].path = str(tmp_path / "sibling-b")
    monkeypatch.setattr("rcp.agents.group_context.load_manifest", lambda _: sibling)
    store = SimpleNamespace(
        project_group_for=lambda _: SimpleNamespace(name="Benchmarks"),
        project_group_project_ids=lambda _: [service._project_id, sibling_id],
        project=lambda _: SimpleNamespace(locator="sibling-manifest", name="Sibling"),
    )
    context = service.assemble_chat(RunRequest(chat_scope="project"))
    context.group_context = build_group_context(service, store, "laptop")

    read_scope = context.read_scope
    sibling_refs = [f"{sibling_id}:repo-a", f"{sibling_id}:repo-b"]
    assert read_scope.refs == sorted(["repo-a", "repo-b", *sibling_refs])
    # The prompt names the same refs RCP stamps.
    rendered = render_chat_read_context(context)
    assert all(ref in rendered for ref in sibling_refs)

    draft = AgentPatch.model_validate(
        {
            "summary": "Compared against the sibling's baseline.",
            "ops": [],
            "repositories_read": ["repo-a", str(tmp_path / "sibling-a/run.py"), sibling_refs[1]],
        }
    )
    patch = prepare_agent_patch(
        draft, kind="work", run_truth_scope=["repo-a"], read_scope=read_scope
    ).model_copy(update={"source_operation_id": "work-turn"})
    assert patch.repositories_read == ["repo-a", *sibling_refs]
    assert patch.run_read_scope == read_scope.refs

    # Apply checks the stamp, never the live group: the verdict holds after the sibling leaves.
    validation = ValidationContext(["repo-a", "repo-b"], ["repo-a", "repo-b"])
    assert not validate_patch(GraphState(), patch, validation).rejected
    stranger = patch.model_copy(
        update={"repositories_read": ["33333333-3333-4333-8333-333333333333:repo-a"]}
    )
    assert "read-outside-run-scope" in {
        message.code for message in validate_patch(GraphState(), stranger, validation).messages
    }


def test_a_resumed_task_keeps_its_recorded_write_set(manifest, tmp_path) -> None:
    # Authorized under an older, narrower scope; resuming must not widen it.
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    service = ProjectService(
        manifest, history, PaperService(manifest, AppStore(tmp_path / "app.sqlite3"))
    )
    assert history.state().project_truth_scope == ["repo-a", "repo-b"]
    stored = RunRequest(run_truth_scope=["repo-a"], chat_scope="project")

    chat = service.assemble_chat(stored)
    run = ContextAssembler(manifest).assemble(history.state(), writable=stored.run_truth_scope)

    for context in (chat, run):
        assert context.run_truth_scope == ["repo-a"]
        assert [pointer.alias for pointer in context.repositories] == ["repo-a", "repo-b"]


def test_personal_folder_add_and_writable_toggle_are_human_approvals(manifest, tmp_path) -> None:
    folder, plain = tmp_path / "notes", tmp_path / "plain"
    plain.mkdir()
    folder.mkdir()
    for argv in (
        ["init", "-q"],
        ["-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "x"],
    ):  # fmt: skip
        subprocess.run(["git", "-C", str(folder), *argv], check=True)
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    with signed_in_client(app) as client:
        project = f"/api/projects/{app.state.default_project_id}"

        def add(alias: str, path: Path):
            return client.post(f"{project}/repositories", json={"alias": alias, "path": str(path)})

        assert add("plain", plain).status_code == 422
        added = add("notes", folder)
        assert added.status_code == 200, added.text
        assert "notes" in [repository["alias"] for repository in added.json()["repositories"]]
        assert "notes" not in added.json()["project_truth_scope"]

        writable = client.put(f"{project}/repositories/notes/writable", json={"writable": True})
        assert "notes" in writable.json()["project_truth_scope"]
        state = manifest.state.repository
        refused = client.put(f"{project}/repositories/{state}/writable", json={"writable": False})
        assert refused.status_code == 422
