from __future__ import annotations

from pathlib import Path

import pytest

from rcp.agents.prompts import PromptFactory
from rcp.api.app import create_app
from rcp.service import RunRequest
from rcp.skill_registry import SkillDefaults, SkillReference, SkillSelection, official_registry
from rcp.skills.staging import skill_bundle_label, stage_skill_selection


def test_official_registry_exposes_workflows_and_skills_with_declared_dependencies() -> None:
    registry = official_registry()

    workflow = registry.package("workflow", "research-graph-audit")
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
    slash_invoked = service.resolve_skill_request(
        RunRequest(
            provider="codex",
            run_on="laptop",
            invoked_skill_ids=["evidence-triage"],
        )
    )
    with pytest.raises(ValueError, match="not enabled in project skill defaults"):
        service.resolve_skill_request(
            RunRequest(provider="codex", run_on="laptop", invoked_skill_ids=["graph-audit"])
        )

    assert [item.id for item in inherited.resolved_skill_packages] == ["evidence-triage"]
    assert slash_invoked.workflow_ids == []
    assert slash_invoked.skill_ids == ["evidence-triage"]
    assert slash_invoked.invoked_skill_ids == ["evidence-triage"]


def test_settings_always_control_staging_even_when_legacy_ids_are_supplied(
    manifest, tmp_path
) -> None:
    service = create_app(str(manifest.path), data_dir=tmp_path / "data").state.service
    service.manifest.agent.skill_defaults = SkillDefaults(skill_ids=["evidence-triage"])

    resolved = service.resolve_skill_request(
        RunRequest(provider="codex", run_on="laptop", skill_ids=["graph-audit"])
    )

    assert resolved.skill_ids == ["evidence-triage"]
    assert [item.id for item in resolved.resolved_skill_packages or []] == ["evidence-triage"]


def test_a_recorded_version_never_overrides_the_current_registry(manifest, tmp_path) -> None:
    """Retry and resume auto-upgrade: the registry decides, not the saved receipt."""

    service = create_app(str(manifest.path), data_dir=tmp_path / "data").state.service
    service.manifest.agent.skill_defaults = SkillDefaults(skill_ids=["graph-audit"])
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

    assert [item.version for item in selection.resolved_skill_packages] == [
        official_registry().package("skill", "graph-audit").version
    ]
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
    assert (
        pointers[0]["description"]
        == official_registry().package("workflow", "research-graph-audit").description
    )
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


def test_skill_bundle_label_is_stable_for_the_resolved_package_content() -> None:
    selection = official_registry().resolve(workflow_ids=["research-graph-audit"])
    reordered = SkillSelection(
        resolved_skill_packages=list(reversed(selection.resolved_skill_packages))
    )
    upgraded = selection.model_copy(deep=True)
    upgraded.resolved_skill_packages[0].version = "9.9.9"

    label = skill_bundle_label(selection)

    assert label == skill_bundle_label(selection.model_copy(deep=True))
    assert label == skill_bundle_label(reordered)
    assert label.startswith("rcp-skills-v1-")
    assert label != skill_bundle_label(upgraded)


def test_local_content_addressed_skill_stage_reuses_one_immutable_bundle(tmp_path: Path) -> None:
    stage = tmp_path / "chat"
    stage.mkdir()
    selection = official_registry().resolve(workflow_ids=["research-graph-audit"])
    label = skill_bundle_label(selection)

    first = stage_skill_selection(
        selection,
        local_stage=stage,
        remote_stage=None,
        label=label,
        reuse_existing=True,
    )
    second = stage_skill_selection(
        selection,
        local_stage=stage,
        remote_stage=None,
        label=label,
        reuse_existing=True,
    )

    assert first == second
    assert len(list((stage / "inputs").iterdir())) == 1
    assert all(f"/{label}/" in str(pointer["path"]) for pointer in second)


def test_local_content_addressed_skill_stage_rejects_unsafe_or_wrong_existing_entry(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "chat"
    stage.mkdir()
    selection = official_registry().resolve(skill_ids=["graph-audit"])
    label = skill_bundle_label(selection)
    stage_skill_selection(
        selection,
        local_stage=stage,
        remote_stage=None,
        label=label,
        reuse_existing=True,
    )
    skill_file = stage / "inputs" / label / "skill" / "graph-audit" / "SKILL.md"
    original = skill_file.read_text(encoding="utf-8")
    skill_file.chmod(0o600)
    skill_file.write_text(original + "\nchanged", encoding="utf-8")
    skill_file.chmod(0o400)

    with pytest.raises(ValueError, match="does not match"):
        stage_skill_selection(
            selection,
            local_stage=stage,
            remote_stage=None,
            label=label,
            reuse_existing=True,
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
    selection = official_registry().resolve(workflow_ids=["research-graph-audit"])
    pointers = stage_skill_selection(
        selection,
        local_stage=stage,
        remote_stage=None,
        label="rcp-skills-attempt-1",
    )
    body = official_registry().package_body("workflow", "research-graph-audit")

    contract = PromptFactory.graph_task_contract(
        "seed",
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=True,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        provider_log_roots={},
        ingestion_watermark=None,
        repositories=[{"alias": "repo-a", "host": "", "path": "/repo-a"}],
        patch_path="/stage/workspace/patch.json",
        output_schema_path="/stage/inputs/patch-schema.json",
        validator_command="python /stage/validator.py /stage/workspace/patch.json",
        skill_pointers=pointers,
    )

    registry = official_registry()
    for reference in selection.resolved_skill_packages:
        package = registry.package(reference.kind, reference.id)
        assert f"{package.label} ({reference.kind} {reference.id} v{reference.version})" in contract
        assert package.description in " ".join(contract.split())
    assert "builds on:" in contract
    assert str(stage / "inputs" / "rcp-skills-attempt-1" / "workflow" / "research-graph-audit") in (
        contract
    )
    assert "when the task would benefit from it" in contract
    # The body stays in the staged folder; the contract only points at it.
    assert "Run the structural review" in body
    assert "Run the structural review" not in contract


def test_a_contract_without_a_selection_has_no_skill_section() -> None:
    contract = PromptFactory.graph_task_contract(
        "seed",
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=True,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        provider_log_roots={},
        ingestion_watermark=None,
        repositories=[],
        patch_path="/stage/workspace/patch.json",
        output_schema_path="/stage/inputs/patch-schema.json",
        validator_command="python /stage/validator.py /stage/workspace/patch.json",
    )

    assert "Official RCP skills and workflows available to this run" not in contract


def test_the_read_only_package_inspector_serves_the_package_text(manifest, tmp_path) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(create_app(str(manifest.path), data_dir=tmp_path / "data"))

    workflow = client.get("/api/skills/workflow/research-graph-audit")
    missing = client.get("/api/skills/skill/no-such-skill")
    bad_kind = client.get("/api/skills/recipe/research-graph-audit")

    assert workflow.status_code == 200
    payload = workflow.json()
    assert (
        payload["version"]
        == official_registry().package("workflow", "research-graph-audit").version
    )
    assert payload["dependencies"][0]["id"] == "graph-audit"
    assert payload["body"].startswith("# Research graph audit")
    assert "id: research-graph-audit" not in payload["body"]
    assert missing.status_code == 404
    assert bad_kind.status_code == 404
