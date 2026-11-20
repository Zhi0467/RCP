"""Shared code does not ask what kind an agent is.

An agent's placement branches belong to one profile module, its episode
branches to the episode owners, and its storage and locking to the agent owner.
Every other backend module is counted, and a count may fall, never rise. The
agent-kind ratchet counts:

- a string literal that is an agent task or patch kind, alone or quoted inside
  SQL text;
- a comparison of a kind field (``kind``, ``task_kind``, ``patch_kind``,
  ``thread_kind``, ``origin_task_kind``) with a thread or patch kind.

The handoff-name ratchet counts string literals that contain a turn handoff
file name. Only ``transport/`` and the profile module may name those files.

After lowering a count, lock it in with::

    uv run python -m tests.test_agent_kinds
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "rcp"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
KIND_BASELINE = FIXTURES / "agent_kind_baseline.json"
HANDOFF_BASELINE = FIXTURES / "handoff_name_baseline.json"

PROFILE_MODULE = "agents/agent_profile.py"
AGENT_OWNER_MODULE = "agents/agent_owner.py"
EPISODE_OWNERS = frozenset(
    {
        "runs/experiment_loop.py",
        "runs/tasks/experiment_loop.py",
        "runs/experiment_admission.py",
        "runs/experiment_recovery.py",
        "runs/auto_research.py",
        "runs/auto_research_admission.py",
        "runs/auto_research_delivery.py",
        "runs/auto_research_experiments.py",
        "runs/tasks/auto_research_stream.py",
    }
)
NOT_YET_BUILT = frozenset({PROFILE_MODULE, AGENT_OWNER_MODULE})
KIND_ALLOWLIST = EPISODE_OWNERS | NOT_YET_BUILT

AGENT_KINDS = frozenset(
    {"space_chat", "node_chat", "project_chat", "auto_research", "experiment_loop"}
)
KIND_FIELDS = frozenset({"kind", "task_kind", "patch_kind", "thread_kind", "origin_task_kind"})
COMPARED_KINDS = frozenset({"space", "project", "work"})
HANDOFF_NAMES = ("watch.json", "patch.json", "experiment-watch")


def _strings(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _is_kind_field(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id in KIND_FIELDS) or (
        isinstance(node, ast.Attribute) and node.attr in KIND_FIELDS
    )


def _agent_kind_count(tree: ast.AST) -> int:
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            count += node.value in AGENT_KINDS
            count += sum(f"'{kind}'" in node.value for kind in AGENT_KINDS)
        elif isinstance(node, ast.Compare) and any(
            _is_kind_field(operand) for operand in (node.left, *node.comparators)
        ):
            count += not COMPARED_KINDS.isdisjoint(_strings(node))
    return count


def _handoff_name_count(tree: ast.AST) -> int:
    return sum(any(name in text for name in HANDOFF_NAMES) for text in _strings(tree))


def _module_counts(count: Callable[[ast.AST], int], allowed: Callable[[str], bool]) -> dict:
    counts: dict[str, int] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        if allowed(relative):
            continue
        found = count(ast.parse(path.read_text(encoding="utf-8")))
        if found:
            counts[relative] = found
    return counts


def _kind_counts() -> dict[str, int]:
    return _module_counts(_agent_kind_count, KIND_ALLOWLIST.__contains__)


def _handoff_counts() -> dict[str, int]:
    return _module_counts(
        _handoff_name_count,
        lambda module: module.startswith("transport/") or module == PROFILE_MODULE,
    )


def _assert_only_falls(baseline_path: Path, current: dict[str, int], what: str) -> None:
    baseline: dict[str, int] = json.loads(baseline_path.read_text(encoding="utf-8"))
    grown = {
        module: (baseline.get(module, 0), count)
        for module, count in current.items()
        if count > baseline.get(module, 0)
    }
    assert not grown, f"These modules name more {what} than before (baseline, now): {grown}."
    fallen = {
        module: (count, current.get(module, 0))
        for module, count in baseline.items()
        if current.get(module, 0) < count
    }
    assert not fallen, (
        f"{what} fell (baseline, now): {fallen}. Lock in the gain with "
        "`uv run python -m tests.test_agent_kinds`."
    )


def test_allowlisted_owner_modules_exist() -> None:
    missing = sorted(
        module for module in KIND_ALLOWLIST - NOT_YET_BUILT if not (SOURCE / module).is_file()
    )
    assert not missing, f"agent-kind owner modules missing: {missing}"


def test_agent_kind_coupling_only_falls() -> None:
    _assert_only_falls(KIND_BASELINE, _kind_counts(), "agent kinds")


def test_handoff_file_names_only_fall() -> None:
    _assert_only_falls(HANDOFF_BASELINE, _handoff_counts(), "handoff file names")


def test_the_counters_see_each_form() -> None:
    literal = ast.parse('x = "space_chat"')
    sql = ast.parse("q = \"WHERE kind IN ('node_chat', 'project_chat')\"")
    compared = ast.parse('ok = thread.kind == "space"')
    unrelated = ast.parse('ok = shape.kind == "box" or name == "space"')
    assert [_agent_kind_count(tree) for tree in (literal, sql, compared, unrelated)] == [1, 2, 1, 0]
    assert _handoff_name_count(ast.parse('p = stage / "watch.json"; q = "notes.json"')) == 1


if __name__ == "__main__":
    for path, counts in ((KIND_BASELINE, _kind_counts()), (HANDOFF_BASELINE, _handoff_counts())):
        path.write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n")
