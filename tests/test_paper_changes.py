from __future__ import annotations

from rcp.paper import changes as changes_module
from rcp.paper.changes import line_changes


def test_line_diff_merges_nearby_and_rewrites_large_changes():
    before = "".join(f"line {i}\n" for i in range(100))
    nearby = before.replace("line 3\n", "changed 3\n").replace("line 7\n", "changed 7\n")
    assert len(line_changes(before, nearby)) == 1
    distant = nearby.replace("line 30\n", "changed 30\n")
    assert len(line_changes(before, distant)) == 2
    assert line_changes(before, "rewritten\n")[0].kind == "rewrite"
    many = before
    for index in range(0, 65, 5):
        many = many.replace(f"line {index}\n", f"changed {index}\n")
    assert line_changes(before, many)[0].kind == "rewrite"


def test_line_diff_compares_only_a_bounded_middle(monkeypatch):
    """Regression: repeated lines once made one inserted line a quadratic comparison."""
    repeated = "entry\n" * 10000
    assert [change.after for change in line_changes(repeated, "changed\n" + repeated)] == [
        "changed\n"
    ]
    before = "".join(f"line {i}\n" for i in range(100))
    edited = before.replace("line 40\n", "x\n").replace("line 46\n", "y\n")
    assert len(line_changes(before, edited)) == 2
    monkeypatch.setattr(changes_module, "PAPER_CHANGES_DIFF_MAX_LINE_PAIRS", 4)
    (change,) = line_changes(before, edited)
    assert (change.kind, change.first_line, change.last_line) == ("edit", 41, 47)
