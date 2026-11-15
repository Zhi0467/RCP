from __future__ import annotations

from rcp.paper import changes as changes_module
from rcp.paper.changes import line_changes


def test_line_diff_compares_only_a_bounded_middle(monkeypatch):
    """Regression: repeated lines once made one inserted line a quadratic comparison."""
    repeated = "entry\n" * 10000
    assert [change.after for change in line_changes(repeated, "changed\n" + repeated)] == [
        "changed\n"
    ]
    before = "".join(f"line {i}\n" for i in range(100))
    edited = before.replace("line 40\n", "x\n").replace("line 46\n", "y\n")
    assert [change.first_line for change in line_changes(before, edited)] == [41, 47]
    monkeypatch.setattr(changes_module, "PAPER_CHANGES_DIFF_MAX_LINE_PAIRS", 4)
    (change,) = line_changes(before, edited)
    assert change.first_line == 41
    assert change.before.splitlines() == [f"line {i}" for i in range(40, 47)]
