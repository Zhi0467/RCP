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
    assert ("There are no active implementation handoffs." in index) is (not active_handoffs)


def test_archived_handoffs_are_neither_active_nor_indexed() -> None:
    """Archived material is evidence only, so it must not read as current work.

    This replaces a one-time migration check that pinned three 2026-08 filenames
    by name; the invariant they were a sample of holds for every archived file.
    """

    active = ROOT / "docs" / "handoffs"
    archived = ROOT / "docs" / "archive" / "handoffs"
    index = (active / "README.md").read_text()

    archived_handoffs = [path.name for path in archived.glob("*.md") if path.name != "README.md"]
    assert archived_handoffs

    for name in archived_handoffs:
        assert not (active / name).exists()
        assert f"]({name})" not in index
