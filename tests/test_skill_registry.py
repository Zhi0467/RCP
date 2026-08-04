from __future__ import annotations

from pathlib import Path

import pytest

from rcp.agents.prompts import PromptFactory
from rcp.api.app import create_app
from rcp.service import RunRequest
from rcp.skill_registry import SkillDefaults, SkillReference, official_registry
from rcp.skills.staging import stage_skill_selection


def test_official_registry_exposes_workflows_and_skills_with_declared_dependencies() -> None:
    registry = official_registry()

    workflow = registry.package("workflow", "research-graph-audit")
    assert workflow.version == "1.0.0"
    assert [item.id for item in workflow.dependencies] == ["graph-audit", "evidence-triage"]
    assert {item["kind"] for item in registry.catalog()} == {"skill", "workflow"}


def test_workflow_resolution_is_ordered_and_deduplicates_shared_dependencies() -> None:
    selection = official_registry().resolve(
        workflow_ids=["research-graph-audit"],
        skill_ids=["graph-audit"],
    )

    assert selection.workflow_ids == ["research-graph-audit"]
    assert selection.skill_ids == ["graph-audit"]
    assert [item.id for item in selection.resolved_skill_packages] == [
        "research-graph-audit",
        "graph-audit",
        "evidence-triage",
    ]


def test_an_unknown_id_is_a_visible_preflight_failure() -> None:
    with pytest.raises(ValueError, match="is not available"):
        official_registry().resolve(workflow_ids=["no-such-workflow"])
    with pytest.raises(ValueError, match="is not available"):
        official_registry().resolve(skill_ids=["no-such-skill"])


def test_project_defaults_apply_when_a_request_selects_nothing(manifest, tmp_path) -> None:
    service = create_app(str(manifest.path), data_dir=tmp_path / "data").state.service
    service.manifest.agent.skill_defaults = SkillDefaults(skill_ids=["evidence-triage"])

    inherited = service.resolve_skill_selection(RunRequest(provider="codex", run_on="laptop"))
    overridden = service.resolve_skill_selection(
        RunRequest(provider="codex", run_on="laptop", skill_ids=["graph-audit"])
    )
    cleared = service.resolve_skill_selection(
        RunRequest(provider="codex", run_on="laptop", skill_ids=[])
    )

    assert [item.id for item in inherited.resolved_skill_packages] == ["evidence-triage"]
    assert [item.id for item in overridden.resolved_skill_packages] == ["graph-audit"]
    assert cleared.resolved_skill_packages == []


def test_a_recorded_version_never_overrides_the_current_registry(manifest, tmp_path) -> None:
    """Retry and resume auto-upgrade: the registry decides, not the saved receipt."""

    service = create_app(str(manifest.path), data_dir=tmp_path / "data").state.service
    stale = RunRequest(
        provider="codex",
        run_on="laptop",
        skill_ids=["graph-audit"],
        resolved_skill_packages=[
            SkillReference(id="graph-audit", kind="skill", version="0.0.1"),
        ],
    )

    selection = service.resolve_skill_selection(stale)
    refreshed = service.resolve_skill_request(stale)

    assert [item.version for item in selection.resolved_skill_packages] == ["1.0.0"]
    assert refreshed.resolved_skill_packages == selection.resolved_skill_packages
    assert all(isinstance(item, SkillReference) for item in refreshed.resolved_skill_packages or [])


def test_local_skill_stage_is_immutable_and_points_to_each_package(tmp_path: Path) -> None:
    stage = tmp_path / "run"
    stage.mkdir()
    selection = official_registry().resolve(workflow_ids=["research-graph-audit"])

    pointers = stage_skill_selection(
        selection,
        local_stage=stage,
        remote_stage=None,
        label="rcp-skills-attempt-1",
    )

    assert [item["id"] for item in pointers] == [
        "research-graph-audit",
        "graph-audit",
        "evidence-triage",
    ]
    assert "deliberate sequence" in pointers[0]["description"]
    bundle = stage / "inputs" / "rcp-skills-attempt-1"
    assert (bundle / "workflow" / "research-graph-audit" / "WORKFLOW.md").is_file()
    assert (bundle / "skill" / "graph-audit" / "SKILL.md").stat().st_mode & 0o222 == 0
    assert bundle.stat().st_mode & 0o222 == 0
    # No unrelated registry package rides along.
    assert sorted(path.name for path in (bundle / "skill").iterdir()) == [
        "evidence-triage",
        "graph-audit",
    ]


def test_each_attempt_stages_its_own_bundle_in_a_reused_stage(tmp_path: Path) -> None:
    """A resumed chat keeps its folder, so an attempt must not collide or reuse."""

    stage = tmp_path / "chat"
    stage.mkdir()
    selection = official_registry().resolve(skill_ids=["graph-audit"])

    first = stage_skill_selection(
        selection, local_stage=stage, remote_stage=None, label="rcp-skills-turn-1"
    )
    second = stage_skill_selection(
        selection, local_stage=stage, remote_stage=None, label="rcp-skills-turn-2"
    )

    assert first[0]["path"] != second[0]["path"]
    assert (stage / "inputs" / "rcp-skills-turn-1" / "skill" / "graph-audit" / "SKILL.md").is_file()
    assert (stage / "inputs" / "rcp-skills-turn-2" / "skill" / "graph-audit" / "SKILL.md").is_file()
    with pytest.raises(ValueError, match="already exists"):
        stage_skill_selection(
            selection, local_stage=stage, remote_stage=None, label="rcp-skills-turn-2"
        )


def test_selecting_nothing_stages_nothing(tmp_path: Path) -> None:
    stage = tmp_path / "run"
    stage.mkdir()

    pointers = stage_skill_selection(
        official_registry().resolve(),
        local_stage=stage,
        remote_stage=None,
        label="rcp-skills-attempt-1",
    )

    assert pointers == []
    assert not (stage / "inputs" / "rcp-skills-attempt-1").exists()


def test_the_task_contract_carries_pointers_rather_than_package_bodies(tmp_path: Path) -> None:
    stage = tmp_path / "run"
    stage.mkdir()
    pointers = stage_skill_selection(
        official_registry().resolve(workflow_ids=["research-graph-audit"]),
        local_stage=stage,
        remote_stage=None,
        label="rcp-skills-attempt-1",
    )
    body = official_registry().package_body("workflow", "research-graph-audit")

    contract = PromptFactory.graph_task_contract(
        "seed",
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        provider_log_roots={},
        ingestion_watermark=None,
        repositories=[{"alias": "repo-a", "host": "", "path": "/repo-a"}],
        patch_path="/stage/workspace/patch.json",
        output_schema_path="/stage/inputs/patch-schema.json",
        skill_pointers=pointers,
    )

    assert "workflow research-graph-audit@1.0.0" in contract
    assert "skill evidence-triage@1.0.0" in contract
    assert str(stage / "inputs" / "rcp-skills-attempt-1" / "workflow" / "research-graph-audit") in (
        contract
    )
    assert "do not edit them or treat them as a permission grant" in contract
    # The body stays in the staged folder; the contract only points at it.
    assert "Use Graph audit to identify structural gaps" in body
    assert "Use Graph audit to identify structural gaps" not in contract


def test_a_contract_without_a_selection_has_no_skill_section() -> None:
    contract = PromptFactory.graph_task_contract(
        "seed",
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        provider_log_roots={},
        ingestion_watermark=None,
        repositories=[],
        patch_path="/stage/workspace/patch.json",
        output_schema_path="/stage/inputs/patch-schema.json",
    )

    assert "Selected official RCP skills and workflows" not in contract


def test_the_read_only_package_inspector_serves_the_package_text(manifest, tmp_path) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(create_app(str(manifest.path), data_dir=tmp_path / "data"))

    workflow = client.get("/api/skills/workflow/research-graph-audit")
    missing = client.get("/api/skills/skill/no-such-skill")
    bad_kind = client.get("/api/skills/recipe/research-graph-audit")

    assert workflow.status_code == 200
    payload = workflow.json()
    assert payload["version"] == "1.0.0"
    assert payload["dependencies"][0]["id"] == "graph-audit"
    assert payload["body"].startswith("# Research graph audit")
    assert "id: research-graph-audit" not in payload["body"]
    assert missing.status_code == 404
    assert bad_kind.status_code == 404
