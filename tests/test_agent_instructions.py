from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


CLOSED_REFACTOR_FILES = {
    "handoff-2026-08-18-backend-structural-refactor.md",
    "handoff-2026-08-19-backend-structural-refactor-pickup.md",
    "rcp_architecture_audit.md",
}


def test_agents_md_stays_compact_and_states_its_bound() -> None:
    text = (ROOT / "AGENTS.md").read_text()
    lines = text.splitlines()

    assert 180 <= len(lines) <= 230
    assert "hard ceiling of 230" in text


def test_closed_backend_refactor_material_is_archived() -> None:
    active = ROOT / "docs" / "handoffs"
    archived = ROOT / "docs" / "archive" / "handoffs"

    for name in CLOSED_REFACTOR_FILES:
        assert not (active / name).exists()
        assert (archived / name).is_file()

    index = (active / "README.md").read_text()
    assert "There are no active implementation handoffs." in index
    assert "2026-08-20-backend-structural-refactor-closure.md" in index
