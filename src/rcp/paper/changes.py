"""Line diffs between two texts of one Paper file."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Literal, NamedTuple

from rcp.limits import PAPER_CHANGES_DIFF_MAX_LINE_PAIRS

_CARD_MERGE_GAP_LINES = 3
# Past this share of changed lines, or this many cards, one rewrite card reads better.
_REWRITE_CHANGED_SHARE = 0.4
_REWRITE_CARD_COUNT = 12


class LineChange(NamedTuple):
    kind: Literal["edit", "rewrite"]
    start: int
    end: int
    before: str
    after: str
    first_line: int
    last_line: int


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
    """Return deterministic offset ranges for review against the saved base."""
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    edits = [op for op in _line_opcodes(old, new) if op[0] != "equal"]
    if not edits:
        return []
    regions: list[tuple[int, int, int, int]] = []
    for _tag, a, b, c, d in edits:
        if regions and a - regions[-1][1] <= _CARD_MERGE_GAP_LINES:
            first = regions.pop()
            regions.append((first[0], b, first[2], d))
        else:
            regions.append((a, b, c, d))
    changed = max(sum(b - a for _, a, b, _, _ in edits), sum(d - c for _, _, _, c, d in edits))
    rewrite = (
        changed / max(len(old), len(new), 1) > _REWRITE_CHANGED_SHARE
        or len(regions) > _REWRITE_CARD_COUNT
    )
    if rewrite:
        regions = [(0, len(old), 0, len(new))]
    offsets = [0]
    for line in old:
        offsets.append(offsets[-1] + len(line))
    return [
        LineChange(
            "rewrite" if rewrite else "edit",
            offsets[a],
            offsets[b],
            "".join(old[a:b]),
            "".join(new[c:d]),
            a + 1,
            max(a + 1, b),
        )
        for a, b, c, d in regions
    ]
