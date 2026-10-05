"""Tracked paths stay distinct on a case-insensitive filesystem.

The desktop app is built on macOS, where `UpdateNotice.tsx` and `updateNotice.ts`
are the same name to an extensionless import such as `./UpdateNotice`. Linux CI
resolves both, so only this check stops a merge from breaking the Mac build.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
# Extensions an extensionless TypeScript or JavaScript import may resolve to.
MODULE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs"}


def _collisions(paths: list[str]) -> list[list[str]]:
    groups: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        groups[path.lower()].add(path)
        posix = PurePosixPath(path)
        if posix.suffix in MODULE_SUFFIXES:
            groups[f"module:{posix.with_suffix('')}".lower()].add(path)
    return sorted(sorted(group) for group in groups.values() if len(group) > 1)


def test_tracked_paths_do_not_collide_ignoring_case() -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    assert _collisions([path for path in listed.split("\0") if path]) == []


def test_collisions_cover_paths_and_extensionless_modules() -> None:
    assert _collisions(
        [
            "web/src/desktop/UpdateNotice.tsx",
            "web/src/desktop/updateNotice.ts",
            "web/src/ui/AppearancePicker.css",
            "web/src/ui/AppearancePicker.tsx",
            "docs/Readme.md",
            "docs/README.md",
        ]
    ) == [
        ["docs/README.md", "docs/Readme.md"],
        ["web/src/desktop/UpdateNotice.tsx", "web/src/desktop/updateNotice.ts"],
    ]
