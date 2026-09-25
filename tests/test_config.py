from __future__ import annotations

from typing import get_args

import pytest

from rcp.config import (
    AgentExecutionProfile,
    AgentSurface,
    ComputeConnectionConfig,
    MachineConfig,
    Manifest,
    load_manifest,
    permissions_for,
    write_agent_settings,
)
from rcp.limits import COMPUTE_CONNECTION_MAX_COUNT
from rcp.providers import AgentCapability


def test_capability_permissions_are_fixed_and_narrow() -> None:
    discuss = permissions_for("discuss")
    work = permissions_for("work_auto")
    scratch = permissions_for("scratch_patch")
    paper = permissions_for("paper_readonly")

    assert discuss.write_graph_patch is False
    assert discuss.write_project_files is False
    assert work.write_graph_patch is True
    assert work.write_project_files is True
    assert scratch.write_graph_patch is True
    assert scratch.write_project_files is False
    assert paper.write_graph_patch is False
    assert paper.write_project_files is False


def test_orchestrate_is_a_distinct_capability_with_work_permissions() -> None:
    assert "orchestrate" in get_args(AgentCapability)
    assert "orchestrate" not in get_args(AgentSurface)
    assert any("orchestrator" in get_args(item) for item in get_args(AgentExecutionProfile))
    assert permissions_for("orchestrate") == permissions_for("work_auto")


def test_unknown_agent_capability_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown agent surface or capability"):
        permissions_for("unfamiliar")  # type: ignore[arg-type]


def test_surface_permissions_default_conversations_to_discuss(manifest) -> None:
    assert manifest.agent_profile("node_chat").permissions == permissions_for("discuss")
    assert manifest.agent_profile("project_chat").permissions == permissions_for("discuss")
    assert manifest.agent_profile("seed").permissions == permissions_for("scratch_patch")
    assert manifest.agent_profile("refresh").permissions == permissions_for("scratch_patch")
    assert manifest.agent_profile("orchestrator").permissions == permissions_for("orchestrate")


def test_legacy_manifest_derives_an_independent_orchestrator_from_refresh(manifest) -> None:
    original = manifest.path.read_text(encoding="utf-8")
    refresh = manifest.agent_profile("refresh")
    orchestrator = manifest.agent_profile("orchestrator")

    assert orchestrator is not refresh
    assert orchestrator.permissions is not refresh.permissions
    assert orchestrator.model_dump(exclude={"permissions"}) == refresh.model_dump(
        exclude={"permissions"}
    )
    assert orchestrator.permissions == permissions_for("orchestrate")
    assert manifest.path.read_text(encoding="utf-8") == original
    assert "[agent.orchestrator]" not in original


def test_legacy_manifest_defaults_auto_research_ceiling_without_writing(manifest) -> None:
    original = manifest.path.read_bytes()

    assert manifest.agent.default_auto_research_invocation_ceiling == 10
    assert manifest.path.read_bytes() == original
    assert b"default_campaign_invocation_ceiling" not in original


def test_legacy_manifest_decodes_campaign_ceiling_without_writing(manifest) -> None:
    original = manifest.path.read_text(encoding="utf-8")
    legacy = original.replace(
        "[agent]",
        "[agent]\ndefault_campaign_invocation_ceiling = 7",
    )
    manifest.path.write_text(legacy, encoding="utf-8")

    migrated = load_manifest(manifest.path)

    assert migrated.agent.default_auto_research_invocation_ceiling == 7
    assert manifest.path.read_text(encoding="utf-8") == legacy


def test_legacy_manifest_decodes_campaign_report_skill_without_writing(manifest) -> None:
    original = manifest.path.read_text(encoding="utf-8")
    legacy = original.replace(
        'default_run_truth_scope = ["repo-a"]\n',
        'default_run_truth_scope = ["repo-a"]\n\n[agent.skill_defaults]\n'
        'workflow_ids = []\nskill_ids = ["campaign-report", "graph-audit"]\n',
    )
    manifest.path.write_text(legacy, encoding="utf-8")

    migrated = load_manifest(manifest.path)

    assert migrated.agent.skill_defaults.skill_ids == ["episode-report", "graph-audit"]
    assert manifest.path.read_text(encoding="utf-8") == legacy


def test_manifest_rejects_conflicting_auto_research_ceiling_keys(manifest) -> None:
    payload = manifest.model_dump(mode="python")
    payload["agent"]["default_campaign_invocation_ceiling"] = 8

    with pytest.raises(ValueError, match="conflicts"):
        Manifest.model_validate(payload)


def test_current_five_profile_manifest_without_orchestrator_still_normalizes(manifest) -> None:
    payload = manifest.model_dump(mode="python")
    payload["execution"] = None
    payload["agent"].pop("orchestrator")

    migrated = Manifest.model_validate(payload)

    assert (
        migrated.agent_profile("orchestrator").provider
        == migrated.agent_profile("refresh").provider
    )
    assert migrated.agent_profile("orchestrator").permissions == permissions_for("orchestrate")


def test_orchestrator_profile_is_fixed_and_must_run_beside_canonical_state(manifest) -> None:
    wrong_permissions = manifest.model_dump(mode="python")
    wrong_permissions["agent"]["orchestrator"]["permissions"] = permissions_for(
        "scratch_patch"
    ).model_dump(mode="python")
    with pytest.raises(ValueError, match="orchestrator safety contract"):
        Manifest.model_validate(wrong_permissions)

    wrong_machine = manifest.model_dump(mode="python")
    wrong_machine["machines"].append({"alias": "other", "host": "", "provider_paths": {}})
    wrong_machine["agent"]["orchestrator"]["run_on"] = "other"
    with pytest.raises(ValueError, match="agent.orchestrator.run_on must be the canonical"):
        Manifest.model_validate(wrong_machine)


def test_write_agent_settings_persists_all_six_execution_profiles(manifest) -> None:
    names = (
        "seed",
        "refresh",
        "node_chat",
        "project_chat",
        "paper_coach",
        "orchestrator",
    )
    profiles = {name: manifest.agent_profile(name).model_copy(deep=True) for name in names}
    profiles["orchestrator"].model = "orchestrator-model"
    manifest.path.write_text(
        manifest.path.read_text(encoding="utf-8").replace(
            "[agent]",
            "[agent]\ndefault_campaign_invocation_ceiling = 10",
        ),
        encoding="utf-8",
    )

    updated = write_agent_settings(
        manifest,
        list(manifest.agent.default_run_truth_scope),
        profiles,
        default_auto_research_invocation_ceiling=14,
    )

    assert updated.agent_profile("orchestrator").model == "orchestrator-model"
    assert updated.agent.default_auto_research_invocation_ceiling == 14
    assert "[agent.orchestrator]" in manifest.path.read_text(encoding="utf-8")
    content = manifest.path.read_text(encoding="utf-8")
    assert "default_auto_research_invocation_ceiling = 14" in content
    assert "default_campaign_invocation_ceiling" not in content


def test_compute_connections_persist_without_changing_execution_profiles(manifest) -> None:
    profiles = {
        surface: manifest.agent_profile(surface).model_copy(deep=True)
        for surface in (
            "seed",
            "refresh",
            "node_chat",
            "project_chat",
            "paper_coach",
            "orchestrator",
        )
    }
    original_run_on = {surface: profile.run_on for surface, profile in profiles.items()}

    updated = write_agent_settings(
        manifest,
        list(manifest.agent.default_run_truth_scope),
        profiles,
        compute_connections=[
            ComputeConnectionConfig(
                id="gpu-vm",
                name="GPU VM",
                kind="ssh",
                ssh_target="researcher@gpu.example",
                access_hint="Use the shared scratch directory",
            )
        ],
    )

    assert updated.compute_connections[0].ssh_target == "researcher@gpu.example"
    assert {
        surface: updated.agent_profile(surface).run_on for surface in original_run_on
    } == original_run_on
    content = updated.path.read_text(encoding="utf-8")
    assert "[[compute_connections]]" in content
    assert "password" not in content.casefold()

    removed = write_agent_settings(
        updated,
        list(updated.agent.default_run_truth_scope),
        profiles,
        compute_connections=[],
    )
    assert removed.compute_connections == []
    assert "[[compute_connections]]" not in removed.path.read_text(encoding="utf-8")


def test_compute_connection_is_not_a_machine_or_execution_selector(manifest) -> None:
    payload = manifest.model_dump(mode="python")
    payload["compute_connections"] = [
        {
            "id": "gpu",
            "name": "GPU",
            "kind": "ssh",
            "ssh_target": "alice@gpu.example",
            "access_hint": "",
            "run_on": "laptop",
        }
    ]

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        Manifest.model_validate(payload)

    payload["compute_connections"][0].pop("run_on")
    payload["compute_connections"][0]["access_hint"] = "password=hunter2"
    with pytest.raises(ValueError, match="cannot contain credential-shaped text"):
        Manifest.model_validate(payload)

    payload["compute_connections"][0]["access_hint"] = ""
    payload["compute_connections"][0]["name"] = "password=hunter2"
    with pytest.raises(ValueError, match="name cannot contain credential-shaped text"):
        Manifest.model_validate(payload)

    payload["compute_connections"][0]["name"] = "GPU"
    payload["compute_connections"][0]["ssh_target"] = "sk-ant-secret123@gpu.example"
    with pytest.raises(ValueError, match="SSH target cannot contain credential-shaped text"):
        Manifest.model_validate(payload)

    payload["compute_connections"][0]["access_hint"] = ""
    payload["compute_connections"][0]["ssh_target"] = "-ProxyCommand"
    with pytest.raises(ValueError, match="SSH destination contains unsupported characters"):
        Manifest.model_validate(payload)

    payload["compute_connections"][0]["ssh_target"] = "alice@gpu.example"
    payload["compute_connections"][0]["access_hint"] = "Use ~/.ssh/id_ed25519"
    with pytest.raises(ValueError, match="cannot contain SSH credential paths"):
        Manifest.model_validate(payload)


@pytest.mark.parametrize("host", ["-Ffoo", "-oProxyCommand=sh"])
def test_machine_and_compute_connections_reject_option_shaped_ssh_hosts(host: str) -> None:
    with pytest.raises(ValueError, match="SSH destination contains unsupported characters"):
        MachineConfig(alias="remote", host=host)
    with pytest.raises(ValueError, match="SSH destination contains unsupported characters"):
        ComputeConnectionConfig(id="gpu", name="GPU", kind="ssh", ssh_target=host)


def test_manifest_bounds_compute_connections_before_they_can_be_used(manifest) -> None:
    payload = manifest.model_dump(mode="python")
    payload["compute_connections"] = [
        {"id": f"compute-{index}", "name": f"Compute {index}", "kind": "local"}
        for index in range(COMPUTE_CONNECTION_MAX_COUNT + 1)
    ]

    with pytest.raises(ValueError, match="at most 32 items"):
        Manifest.model_validate(payload)


def test_exact_legacy_chat_permissions_normalize_without_widening(manifest) -> None:
    payload = manifest.model_dump(mode="python")
    payload["agent"]["node_chat"]["permissions"] = permissions_for("scratch_patch").model_dump(
        mode="python"
    )

    migrated = Manifest.model_validate(payload)

    assert migrated.agent_profile("node_chat").permissions == permissions_for("discuss")


@pytest.mark.parametrize(
    ("field", "message"),
    [("machines", "machine aliases"), ("repositories", "repository aliases")],
)
def test_manifest_rejects_duplicate_authority_aliases(manifest, field: str, message: str) -> None:
    payload = manifest.model_dump(mode="python")
    payload[field].append(dict(payload[field][0]))

    with pytest.raises(ValueError, match=rf"{message} must be unique"):
        Manifest.model_validate(payload)
