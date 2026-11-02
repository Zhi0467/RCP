#!/usr/bin/env python3
"""Ratchet inline Python comments in tracked files down, file by file.

Code carries no comments: name it, test it, or put the why in the owning spec.
Allowed are invariant citations (``invariant 10f``), lint pragmas (``noqa``,
``type: ignore``, ``pyright:``, ``basedpyright:``, ``fmt:``, coverage
``pragma:``), and the shebang and encoding lines. Docstrings are strings, not
comments. Each banned comment line counts once.

``.ratchet/baseline.json`` records ``{file: {rule: count}}``, sorted, one file
per line, so concurrent branches merge by line. A run fails when a file's count
rises above its baseline. When counts fall or files vanish, it rewrites the
baseline and fails, so the shrunk baseline gets committed.

    uv run python scripts/comment_ratchet.py          # check, shrinking the baseline
    uv run python scripts/comment_ratchet.py --write  # regenerate the whole baseline

Use ``--write`` only to rebuild the baseline after merging branches that each
shrank it, or after renaming a file whose comments move with it; never to admit
new comments.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / ".ratchet" / "baseline.json"
RULE = "comment"

_ALLOWED = re.compile(
    r"(?i)invariant \d+[a-z]?|\bnoqa\b|\btype:\s*ignore\b|\b(?:based)?pyright:|\bfmt:|\bpragma:"
)
_ENCODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")

Counts = dict[str, dict[str, int]]


@dataclass(frozen=True)
class Rise:
    path: str
    count: int
    allowed: int


def count_comments(source: str) -> int:
    count = 0
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        line = token.start[0]
        text = token.string
        if line == 1 and text.startswith("#!"):
            continue
        if line <= 2 and _ENCODING.match(text):
            continue
        if _ALLOWED.search(text):
            continue
        count += 1
    return count


def rises(current: Counts, baseline: Counts) -> list[Rise]:
    found = []
    for path, rules in sorted(current.items()):
        for rule, count in sorted(rules.items()):
            allowed = baseline.get(path, {}).get(rule, 0)
            if count > allowed:
                found.append(Rise(path, count, allowed))
    return found


def render(counts: Counts) -> str:
    rows = [
        (path, {rule: count for rule, count in sorted(rules.items()) if count})
        for path, rules in sorted(counts.items())
    ]
    lines = [f"  {json.dumps(path)}: {json.dumps(rules)}" for path, rules in rows if rules]
    return "{\n" + ",\n".join(lines) + ("\n" if lines else "") + "}\n"


def tracked_counts(root: Path) -> Counts:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    counts: Counts = {}
    for path in filter(None, listed.split("\0")):
        file = root / path
        if not file.is_file():
            continue
        counts[path] = {RULE: count_comments(file.read_text(encoding="utf-8"))}
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="regenerate the whole baseline")
    args = parser.parse_args(argv)

    current = tracked_counts(ROOT)
    previous = BASELINE.read_text(encoding="utf-8") if BASELINE.exists() else "{}\n"
    if not args.write:
        risen = rises(current, json.loads(previous))
        if risen:
            for rise in risen:
                print(
                    f"{rise.path}: {rise.count} comment lines, baseline allows {rise.allowed}",
                    file=sys.stderr,
                )
            print("Remove the new comments; docstrings and invariant citations stay allowed.")
            return 1
    text = render(current)
    if text == previous:
        return 0
    BASELINE.parent.mkdir(exist_ok=True)
    BASELINE.write_text(text, encoding="utf-8")
    if args.write:
        return 0
    print(f"Comment counts fell; commit the rewritten {BASELINE.relative_to(ROOT)}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
