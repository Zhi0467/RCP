"""Line diffs between two texts of one Paper file."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import NamedTuple

from rcp.limits import PAPER_CHANGES_DIFF_MAX_LINE_PAIRS


class LineChange(NamedTuple):
    first_line: int
    before: str
    after: str


def _line_opcodes(old: list[str], new: list[str]) -> list[tuple[str, int, int, int, int]]:
    """Line opcodes that diff only the span between the common head and tail.

    Edits are local, and SequenceMatcher is quadratic in the lines it compares, so a
    middle past the budget is one replacement rather than a comparison.
    """
    head = 0
    while head < min(len(old), len(new)) and old[head] == new[head]:
        head += 1
    tail = 0
    while tail < min(len(old), len(new)) - head and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    old_middle, new_middle = old[head : len(old) - tail], new[head : len(new) - tail]
    if len(old_middle) * len(new_middle) > PAPER_CHANGES_DIFF_MAX_LINE_PAIRS:
        middle = [("replace", 0, len(old_middle), 0, len(new_middle))]
    else:
        middle = SequenceMatcher(None, old_middle, new_middle, autojunk=False).get_opcodes()
    return [
        ("equal", 0, head, 0, head),
        *((tag, a + head, b + head, c + head, d + head) for tag, a, b, c, d in middle),
        ("equal", len(old) - tail, len(old), len(new) - tail, len(new)),
    ]


def line_changes(before: str, after: str) -> list[LineChange]:
    """Each exact edit between two texts, as a line diff shows them."""
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    return [
        LineChange(a + 1, "".join(old[a:b]), "".join(new[c:d]))
        for tag, a, b, c, d in _line_opcodes(old, new)
        if tag != "equal"
    ]
