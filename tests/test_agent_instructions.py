from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_agents_md_stays_within_its_length_bound() -> None:
    text = (ROOT / "AGENTS.md").read_text()
    lines = text.splitlines()

    assert len(lines) <= 230


def test_handoff_index_lists_exactly_the_active_handoffs() -> None:
    active = ROOT / "docs" / "handoffs"
    index = (active / "README.md").read_text()

    active_handoffs = {path.name for path in active.glob("*.md") if path.name != "README.md"}
    indexed_handoffs = set(re.findall(r"\]\(([^/)]+\.md)\)", index))

    assert indexed_handoffs == active_handoffs
