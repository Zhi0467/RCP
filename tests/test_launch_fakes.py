"""Every test fake of the task launch keeps the real request validation.

0.4.35 shipped a queued task whose stored request could never launch, because
the test replaced ``launch_admitted`` with a fake that skipped validation.
``helpers.fake_launch_admitted`` is the one way to fake the launch: it runs the
real parse and roundtrip check on a queued row first. This guard fails when a
test replaces ``launch_admitted`` any other way, so the allowlist stays empty.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
HELPER = TESTS / "helpers.py"
TARGET = "launch_admitted"

# Path relative to tests/ -> why that file may fake the launch without the helper.
ALLOWED: dict[str, str] = {}


def _raw_fakes(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            named = isinstance(node.func, ast.Attribute) and node.func.attr == "setattr"
            named = named or isinstance(node.func, ast.Name) and node.func.id == "setattr"
            if named and any(
                isinstance(arg, ast.Constant) and arg.value == TARGET for arg in node.args
            ):
                lines.append(node.lineno)
            lines.extend(keyword.value.lineno for keyword in node.keywords if keyword.arg == TARGET)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(item, ast.Attribute) and item.attr == TARGET for item in targets):
                lines.append(node.lineno)
    return sorted(lines)


def raw_launch_fakes(root: Path = TESTS) -> dict[str, list[int]]:
    found = {}
    for path in sorted(root.rglob("*.py")):
        if path == HELPER:
            continue
        lines = _raw_fakes(ast.parse(path.read_text(encoding="utf-8")))
        if lines:
            found[str(path.relative_to(root))] = lines
    return found


def test_every_launch_fake_goes_through_the_validating_helper() -> None:
    found = raw_launch_fakes()
    assert {path: lines for path, lines in found.items() if path not in ALLOWED} == {}
    assert set(ALLOWED) <= set(found), "An allowlisted file no longer fakes the launch."
