from __future__ import annotations

import pytest

from rcp.config import Manifest, permissions_for


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


def test_unknown_agent_capability_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown agent surface or capability"):
        permissions_for("unfamiliar")  # type: ignore[arg-type]


def test_surface_permissions_default_conversations_to_discuss(manifest) -> None:
    assert manifest.agent_profile("node_chat").permissions == permissions_for("discuss")
    assert manifest.agent_profile("project_chat").permissions == permissions_for("discuss")
    assert manifest.agent_profile("seed").permissions == permissions_for("scratch_patch")
    assert manifest.agent_profile("refresh").permissions == permissions_for("scratch_patch")


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
