from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents.group_context import build_group_context
from rcp.agents.prompts import render_chat_read_context
from rcp.agents.write_scope import registered_repository_roots, resolve_project_write_scope
from rcp.service import RunRequest

from .test_branch_context import branch_services as branch_services


@pytest.mark.parametrize("grouped", [False, True])
def test_group_payload_and_read_roots_leave_writes_unchanged(
    branch_services, tmp_path, monkeypatch, grouped
):
    service, _ = branch_services
    sibling = service.manifest.model_copy(deep=True)
    sibling.repository_map["repo-a"].path = str(tmp_path / "sibling")
    sibling.repository_map["repo-b"].path = "/other/repository"
    # The second repository is on another state machine; it is a read pointer only.
    remote = sibling.machines[0].model_copy(update={"alias": "remote", "host": "remote.example"})
    sibling.machines.append(remote)
    sibling.repository_map["repo-b"].machine = "remote"
    monkeypatch.setattr("rcp.agents.group_context.load_manifest", lambda _: sibling)
    store = SimpleNamespace(
        project_group_for=lambda _: SimpleNamespace(name="Benchmarks") if grouped else None,
        project_group_project_ids=lambda _: ["project", "sibling"] if grouped else ["project"],
        project=lambda _: SimpleNamespace(locator="sibling-manifest", name="Sibling"),
    )
    context = service.assemble_chat(RunRequest(chat_scope="project", run_truth_scope=["repo-a"]))
    stage = tmp_path / "stage"
    (stage / "workspace").mkdir(parents=True)

    def write_scope():
        return resolve_project_write_scope(
            manifest=service.manifest,
            project_id="project",
            execution_machine="laptop",
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
    context.group_context = build_group_context(service, store, "laptop")
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


@pytest.mark.parametrize("broken", ["manifest", "machine"])
def test_unavailable_sibling_is_reported_and_grants_no_reads(
    branch_services, monkeypatch, caplog, broken
):
    service, _ = branch_services
    sibling = service.manifest.model_copy(deep=True)
    if broken == "machine":
        service.manifest.machines[0].host = "execution.example"

    def load(_):
        if broken == "manifest":
            raise FileNotFoundError("missing manifest")
        return sibling

    monkeypatch.setattr("rcp.agents.group_context.load_manifest", load)
    store = SimpleNamespace(
        project_group_for=lambda _: SimpleNamespace(name="Benchmarks"),
        project_group_project_ids=lambda _: ["project", "sibling"],
        project=lambda _: SimpleNamespace(locator="sibling-manifest", name="Sibling"),
    )
    group = build_group_context(service, store, "laptop")
    assert group is not None
    assert group.readable_project_ids == {"project"}
    assert group.read_dirs() == []
    failed = group.siblings[0]
    assert failed.project_id == "sibling" and failed.unavailable_reason
    assert failed.graph is None and failed.research_md is None and not failed.repositories
    assert any(
        record.levelname == "WARNING" and record.args[0] == "sibling" for record in caplog.records
    )
