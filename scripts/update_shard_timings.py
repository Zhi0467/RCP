"""Rewrite tests/shard_timings.json from CI pytest JUnit reports.

Download every shard's report of one green run, then pass them all:

    gh run download <run-id> --pattern 'pytest-timings-*' --dir <dir>
    uv run python scripts/update_shard_timings.py <dir>/*/junit.xml

A file's weight is its summed test seconds, averaged over the Python versions
that ran it.
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ElementTree
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMINGS = ROOT / "tests" / "shard_timings.json"


def classname_file(classname: str, root: Path) -> str:
    """The longest dotted prefix of a JUnit classname that names a test file."""
    parts = classname.split(".")
    for end in range(len(parts), 0, -1):
        path = "/".join(parts[:end]) + ".py"
        if (root / path).is_file():
            return path
    raise ValueError(f"no test file for {classname}")


def report_seconds(report: Path, root: Path) -> dict[str, float]:
    seconds: dict[str, float] = defaultdict(float)
    for case in ElementTree.parse(report).iter("testcase"):
        seconds[classname_file(case.attrib["classname"], root)] += float(case.attrib["time"])
    return seconds


def file_timings(reports: list[Path], root: Path) -> dict[str, float]:
    runs: dict[str, list[float]] = defaultdict(list)
    for report in reports:
        for path, seconds in report_seconds(report, root).items():
            runs[path].append(seconds)
    return {path: round(sum(values) / len(values), 1) for path, values in sorted(runs.items())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    args = parser.parse_args()
    timings = file_timings(args.reports, ROOT)
    TIMINGS.write_text(json.dumps(timings, indent=2) + "\n", encoding="utf-8")
    print(f"{len(timings)} files, {sum(timings.values()):.0f} s")


if __name__ == "__main__":
    main()
