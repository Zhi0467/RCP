from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents.continuation_prompt import context_delta
from rcp.agents.group_context import build_group_context
from rcp.agents.prompts import render_chat_read_context
from rcp.agents.write_scope import registered_repository_roots, resolve_project_write_scope
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.runs.chat import chat_prompt_values
from rcp.service import RunRequest
from rcp.storage import AppStore

from .test_branch_context import branch_services as branch_services


@pytest.mark.parametrize("grouped", [False, True])
def test_group_payload_and_read_roots_leave_writes_unchanged(
    branch_services, tmp_path, monkeypatch, grouped
):
    """The second repository is on another state machine, so it is a read pointer only."""
    service, _ = branch_services
    sibling = service.manifest.model_copy(deep=True)
    sibling.repository_map["repo-a"].path = str(tmp_path / "sibling")
    sibling.repository_map["repo-b"].path = "/other/repository"
    sibling.machines.append("remote")
    sibling.repository_map["repo-b"].machine = "remote"
    monkeypatch.setattr("rcp.agents.group_context.load_manifest", lambda _: sibling)
    store = SimpleNamespace(
        project_group_for=lambda _: SimpleNamespace(name="Benchmarks") if grouped else None,
        project_group_project_ids=lambda _: ["project", "sibling"] if grouped else ["project"],
        project=lambda _: SimpleNamespace(locator="sibling-manifest", name="Sibling"),
        space_machine={"remote": SimpleNamespace(host="remote.example")}.__getitem__,
    )
    backend = AppStore(tmp_path / "cards.sqlite3").backend_space_machine()
    context = service.assemble_chat(RunRequest(chat_scope="project", run_truth_scope=["repo-a"]))
    stage = tmp_path / "stage"
    (stage / "workspace").mkdir(parents=True)

    def write_scope():
        return resolve_project_write_scope(
            manifest=service.manifest,
            truth_scope=service.manifest.project.truth_scope,
            project_id="project",
            machine=backend,
            capability="work_auto",
            stage_root=str(stage),
            workspace_root=str(stage / "workspace"),
            admitted_aliases=context.run_truth_scope,
            repository_pointers=context.repositories,
            remote_stage=None,
            app_data_dir=tmp_path / "data",
            repository_inventory=registered_repository_roots(
                service.manifest, project_id="project"
            ),
        )

    before = write_scope()
    context.group_context = build_group_context(service, store, BACKEND_MACHINE_ID)
    assert write_scope() == before
    rendered = render_chat_read_context(context)
    if not grouped:
        assert context.group_context is None
        assert rendered == ""
        return
    group = context.group_context
    assert group is not None
    assert group.group_name == "Benchmarks"
    payload = json.loads(rendered.split("```json\n")[1].split("\n```")[0])["group_context"]
    assert payload == group.model_dump(mode="json")
    assert group.readable_project_ids == {"project", "sibling"}
    assert payload["siblings"][0]["graph"]["path"] == str(tmp_path / "sibling/.research/graph.json")
    assert group.siblings[0].repositories[1].host == "remote.example"
    assert Path("/other/repository") not in group.read_dirs()
    assert Path(tmp_path / "sibling") in group.read_dirs()
    assert str(tmp_path / "sibling") not in before.writable_roots

    def values():
        """A continued chat whose project leaves the group is told so by its delta."""
        return chat_prompt_values(
            context,
            RunRequest(),
            repositories=[],
            compute_profiles=[],
            skill_pointers=[],
            experiment_watcher_resources=[],
            workspace="workspace",
        )

    grouped_values = values()
    context.group_context = None
    assert context_delta(grouped_values, values()) == {"group": None}


@pytest.mark.parametrize("broken", ["manifest", "machine"])
def test_unavailable_sibling_is_reported_and_grants_no_reads(
    branch_services, monkeypatch, caplog, broken
):
    service, _ = branch_services
    sibling = service.manifest.model_copy(deep=True)
    machine_id = "execution" if broken == "machine" else BACKEND_MACHINE_ID

    def load(_):
        if broken == "manifest":
            raise FileNotFoundError("missing manifest")
        return sibling

    monkeypatch.setattr("rcp.agents.group_context.load_manifest", load)
    store = SimpleNamespace(
        project_group_for=lambda _: SimpleNamespace(name="Benchmarks"),
        project_group_project_ids=lambda _: ["project", "sibling"],
        project=lambda _: SimpleNamespace(locator="sibling-manifest", name="Sibling"),
        backend_space_machine=lambda: SimpleNamespace(host=""),
    )
    group = build_group_context(service, store, machine_id)
    assert group is not None
    assert group.readable_project_ids == {"project"}
    assert group.read_dirs() == []
    failed = group.siblings[0]
    assert failed.project_id == "sibling" and failed.unavailable_reason
    assert failed.graph is None and failed.research_md is None and not failed.repositories
    assert any(
        record.levelname == "WARNING" and record.args[0] == "sibling" for record in caplog.records
    )
