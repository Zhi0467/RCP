from __future__ import annotations

from pathlib import Path

from rcp.skill_registry import official_registry


def test_campaign_report_skill_is_versioned_minimal_and_packaged_for_both_builds() -> None:
    registry = official_registry()
    package = registry.package("skill", "campaign-report")
    body = registry.package_body("skill", "campaign-report")

    assert package.version == "1.0.0"
    assert "valid HTML report" in body
    assert "reasoning and decisions" in body
    assert "what failed" in body
    assert "what progressed" in body
    assert "what still awaits a human" in body
    assert "visualizations or artifacts" in body
    assert "clearly partial" in body
    assert "carries no graph authority" in body
    assert "Required sections" not in body
    assert "## " not in body

    root = Path(__file__).resolve().parents[1]
    wheel = (root / "pyproject.toml").read_text(encoding="utf-8")
    sidecar = (root / "packaging" / "rcp_backend.spec").read_text(encoding="utf-8")
    assert "src/rcp/skills/campaign-report" in wheel
    assert 'SKILL_ROOT / "campaign-report"' in sidecar
    assert '"rcp/skills/campaign-report"' in sidecar
