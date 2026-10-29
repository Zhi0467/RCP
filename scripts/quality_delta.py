#!/usr/bin/env python3
"""Report code-quality changes before a push; run ``uv run python scripts/quality_delta.py origin/main``.

Report only: it never fails. For source files changed between the merge base
with <base> and the working tree, it prints:

- touched functions with cyclomatic complexity (CC) of 10 or more;
- touched functions whose CC rose against the base;
- duplicate pairs scoring 0.8 or more where one side overlaps a changed line.

The tools run from pinned commits of github.com/unclebob/crapper and
github.com/unclebob/dryer through ``uvx``. Duplicates are searched among the
changed files only, because a whole-tree scan takes minutes.

Pitfalls when running crapper, dryer, or mutator by hand:

- Never let crapper or mutator collect coverage themselves. Their default runs
  the whole pytest suite. Use ``crapper --no-coverage``, or
  ``--use-existing-coverage`` after an affected-files coverage run, and give
  mutator ``--test-command "uv run pytest -n0 <affected tests>"``.
- Run mutator with ``PYTHONPATH=src``. Otherwise the editable install wins and
  every mutant runs against the unmutated source.
- zsh does not split an unquoted ``$T``. Pass a list of test paths as ``${=T}``,
  or pytest gets one bad path and every site reads as uncovered.
- The tools write ``.metrics/`` and ``target/`` into the current directory.
  Git ignores both; delete them after a run.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRAPPER = "git+https://github.com/unclebob/crapper@6fd92c4c9bcc56efb64a2f8743d5a2993bcfd42d"
DRYER = "git+https://github.com/unclebob/dryer@66ff6d21a42c04afcad89c78a80066176d1294b0"
SOURCE_DIRS = ("src/", "web/src/")
SOURCE_SUFFIXES = (".py", ".ts", ".tsx")
CC_LIMIT = 10
DUPLICATE_THRESHOLD = "0.8"
HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", re.M)
PAIR = re.compile(r"DUPLICATE score=([\d.]+)\n\s+(\S+):(\d+)-(\d+)\n\s+(\S+):(\d+)-(\d+)")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True
    ).stdout


def changed_lines(base: str) -> dict[str, set[int]]:
    """Map each added or modified source file to its changed working-tree lines."""
    changed: dict[str, set[int]] = {}
    for path in git("diff", "--name-only", "--diff-filter=AM", base).split():
        if path.startswith(SOURCE_DIRS) and path.endswith(SOURCE_SUFFIXES):
            lines: set[int] = set()
            for hunk in HUNK.finditer(git("diff", "-U0", base, "--", path)):
                start = int(hunk.group(1))
                lines.update(range(start, start + int(hunk.group(2) or 1)))
            changed[path] = lines
    return changed


def print_rows(title: str, rows: list[str]) -> None:
    print(title)
    for row in rows or ["none"]:
        print(f"  {row}")


def complexity_report(base: str) -> None:
    """Runs inside the crapper environment that ``uvx`` provides."""
    from crapper.discover import language_of
    from crapper.languages import functions_in_file

    high: list[tuple[int, str]] = []
    rose: list[tuple[int, str]] = []
    for path, lines in changed_lines(base).items():
        language = language_of(path)
        old = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{base}:{path}"], capture_output=True, text=True
        )
        before = (
            {
                (function.namespace, function.name): function.complexity
                for function in functions_in_file(language, old.stdout, path, str(ROOT))
            }
            if old.returncode == 0
            else {}
        )
        source = (ROOT / path).read_text()
        for function in functions_in_file(language, source, path, str(ROOT)):
            if not any(function.start_line <= line <= function.end_line for line in lines):
                continue
            cc = function.complexity
            where = f"{function.name}  {path}:{function.start_line}"
            if cc >= CC_LIMIT:
                high.append((cc, f"{cc:4d}  {where}"))
            previous = before.get((function.namespace, function.name))
            if previous is not None and cc > previous:
                rose.append((cc - previous, f"{previous:4d} -> {cc:<4d}  {where}"))
    print_rows(
        f"Touched functions with CC >= {CC_LIMIT}:", [row for _, row in sorted(high, reverse=True)]
    )
    print_rows("Touched functions whose CC rose:", [row for _, row in sorted(rose, reverse=True)])


def duplicate_report(base: str) -> None:
    changed = changed_lines(base)
    pairs: list[str] = []
    if changed:
        with tempfile.TemporaryDirectory() as metrics_root:
            report = subprocess.run(
                [
                    "uvx",
                    "-q",
                    "--from",
                    DRYER,
                    "dryer",
                    "--root",
                    metrics_root,
                    "--threshold",
                    DUPLICATE_THRESHOLD,
                    *(str(ROOT / path) for path in changed),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        for score, *sides in PAIR.findall(report):
            spans = [
                (str(Path(path).relative_to(ROOT)), int(first), int(last))
                for path, first, last in zip(sides[0::3], sides[1::3], sides[2::3], strict=True)
            ]
            if any(changed[path] & set(range(first, last + 1)) for path, first, last in spans):
                pairs.append(f"{score}  " + "  <->  ".join(f"{p}:{a}-{b}" for p, a, b in spans))
    print_rows(f"Duplicate pairs >= {DUPLICATE_THRESHOLD} touching changed lines:", pairs)


def main() -> None:
    if sys.argv[1:2] == ["--complexity"]:
        complexity_report(sys.argv[2])
        return
    if len(sys.argv) != 2:
        sys.exit("usage: quality_delta.py <base>")
    base = git("merge-base", sys.argv[1], "HEAD").strip()
    subprocess.run(
        ["uvx", "-q", "--from", CRAPPER, "python", __file__, "--complexity", base], check=True
    )
    duplicate_report(base)


if __name__ == "__main__":
    main()
