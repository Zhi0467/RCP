from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_agents_md_stays_within_its_length_bound() -> None:
    text = (ROOT / "AGENTS.md").read_text()
    lines = text.splitlines()

    assert len(lines) <= 230
