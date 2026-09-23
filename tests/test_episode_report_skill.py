from __future__ import annotations

from pathlib import Path

from rcp.skill_registry import official_registry


def test_episode_report_skill_is_versioned_and_packaged() -> None:
    registry = official_registry()
    package = registry.package("skill", "episode-report")

    assert package.version == "1.4.0"

    root = Path(__file__).resolve().parents[1]
    wheel = (root / "pyproject.toml").read_text(encoding="utf-8")
    sidecar = (root / "packaging" / "rcp_backend.spec").read_text(encoding="utf-8")
    assert "src/rcp/skills/episode-report" in wheel
    assert 'SKILL_ROOT / "episode-report"' in sidecar
    assert '"rcp/skills/episode-report"' in sidecar
