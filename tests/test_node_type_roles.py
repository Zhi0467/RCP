"""Node-type questions stay answered in one place.

``rcp.core.roles`` answers the node-type questions kernel code asks. The
ratchet below counts the node-type names and node classes each backend module
still spells out itself; a count may fall, never rise. After moving a site to
``rcp.core.roles``, lower the baseline with::

    uv run python -m tests.test_node_type_roles
"""

from __future__ import annotations

import ast
import json
import typing
from pathlib import Path

from rcp.core.models import ALL_NODE_TYPES, RELATION_SPEC, ProjectNode
from rcp.core.roles import (
    BELIEF_OUTCOME_RELATIONS,
    PROTECTED_BELIEF_TYPES,
    RETIRED_VALUE,
    lifecycle_field,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "rcp"
BASELINE = Path(__file__).resolve().parent / "fixtures" / "node_type_coupling_baseline.json"
# The modules that define node types and answer questions about them.
OWNERS = frozenset({"core/models.py", "core/roles.py"})
NODE_MODELS = typing.get_args(typing.get_args(ProjectNode)[0])
NODE_CLASS_NAMES = frozenset(model.__name__ for model in NODE_MODELS)


def test_roles_name_real_types_and_relations() -> None:
    assert PROTECTED_BELIEF_TYPES <= ALL_NODE_TYPES
    assert RELATION_SPEC.keys() >= BELIEF_OUTCOME_RELATIONS
    assert {model.model_fields["type"].annotation.__args__[0] for model in NODE_MODELS} == (
        ALL_NODE_TYPES
    )


def test_every_node_type_can_be_retired_through_its_lifecycle_field() -> None:
    for model in NODE_MODELS:
        node_type = model.model_fields["type"].annotation.__args__[0]
        field = model.model_fields.get(lifecycle_field(node_type))
        assert field is not None, f"{node_type} has no {lifecycle_field(node_type)!r} field"
        assert RETIRED_VALUE in typing.get_args(field.annotation), node_type


def _module_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        if relative in OWNERS:
            continue
        count = 0
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and node.value in ALL_NODE_TYPES
                or isinstance(node, ast.Name)
                and node.id in NODE_CLASS_NAMES
                or isinstance(node, ast.Attribute)
                and node.attr in NODE_CLASS_NAMES
            ):
                count += 1
        if count:
            counts[relative] = count
    return counts


def test_node_type_coupling_only_falls() -> None:
    baseline: dict[str, int] = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = _module_counts()
    grown = {
        module: (baseline.get(module, 0), count)
        for module, count in current.items()
        if count > baseline.get(module, 0)
    }
    assert not grown, (
        "These modules name more node types or node classes than before "
        f"(baseline, now): {grown}. Ask the question through rcp.core.roles instead."
    )
    fallen = {
        module: (count, current.get(module, 0))
        for module, count in baseline.items()
        if current.get(module, 0) < count
    }
    assert not fallen, (
        f"Node-type coupling fell (baseline, now): {fallen}. Lock in the gain with "
        "`uv run python -m tests.test_node_type_roles`."
    )


if __name__ == "__main__":
    BASELINE.write_text(json.dumps(_module_counts(), indent=2, sort_keys=True) + "\n")
