"""Node-type knowledge stays in the research layer.

Kernel code asks the project's type (``rcp.core.project_types``) which node
types play which part. Only the research layer below names research node
types, node classes, or research relations. The ratchet counts what each other
backend module still names; a count may fall, never rise. What remains are
persisted or wire names, such as the ``"experiment"`` Auto-research child kind,
that the decision record lists. After lowering a count, lock it in with::

    uv run python -m tests.test_project_types
"""

from __future__ import annotations

import ast
import json
import typing
from pathlib import Path

from rcp.core.models import ALL_NODE_TYPES, RELATION_SPEC, ProjectNode
from rcp.core.project_types import project_type_of

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "rcp"
BASELINE = Path(__file__).resolve().parent / "fixtures" / "node_type_coupling_baseline.json"

# The research layer: the research type, its node models, and the modules that
# hold one node type's own rules or a research-only feature (the Experiment
# loop). Another project type would replace these modules, not edit them.
RESEARCH_LAYER = frozenset(
    {
        "core/models.py",
        "core/research_type.py",
        "core/research_compat.py",
        "core/research_rules.py",
        "core/experiment_guidance.py",
        "core/ontology.py",
        "core/research_md.py",
        "core/attention.py",
        "core/validation/constants.py",
        "core/validation/nodes.py",
        "core/validation/proposals.py",
        "core/validation/approval.py",
        "core/validation/experiment_loop.py",
        "core/validation/quality.py",
        "control.py",
        "agents/schema.py",
        "agents/acceptance.py",
        "runs/experiment_admission.py",
        "runs/experiment_loop.py",
        "runs/tasks/experiment_loop.py",
        "runs/auto_research_experiments.py",
        "api/experiments.py",
        "api/experiment_controls.py",
    }
)
NODE_MODELS = typing.get_args(typing.get_args(ProjectNode)[0])
NODE_CLASS_NAMES = frozenset(model.__name__ for model in NODE_MODELS) | {"RESEARCH"}
# Meta relations such as ``supersedes`` belong to every graph, not to research.
RESEARCH_RELATIONS = frozenset(
    name for name, spec in RELATION_SPEC.items() if spec.layer != "meta"
)
RESEARCH_NAMES = ALL_NODE_TYPES | RESEARCH_RELATIONS


def _node_type(model: type) -> str:
    return typing.get_args(model.model_fields["type"].annotation)[0]


def test_research_type_names_real_types_and_relations() -> None:
    research = project_type_of()
    assert set(research.node_types) == ALL_NODE_TYPES == {_node_type(m) for m in NODE_MODELS}
    assert set(research.labels) == set(research.plural_labels) == ALL_NODE_TYPES
    for role in (
        research.protected_belief_types,
        research.belief_types,
        research.outcome_types,
        research.control_node_types,
        research.chooser_types,
        research.blocker_types,
        research.question_types,
    ):
        assert role <= ALL_NODE_TYPES
    for relations in (
        research.belief_outcome_relations,
        research.protected_relations,
        research.blocking_relations,
    ):
        assert relations <= RELATION_SPEC.keys()


def test_every_node_type_can_be_retired_through_its_lifecycle_field() -> None:
    research = project_type_of()
    for model in NODE_MODELS:
        node_type = _node_type(model)
        field = model.model_fields.get(research.lifecycle_field(node_type))
        assert field is not None, f"{node_type} has no {research.lifecycle_field(node_type)!r}"
        assert research.retired_value in typing.get_args(field.annotation), node_type


def test_research_layer_modules_exist() -> None:
    missing = sorted(module for module in RESEARCH_LAYER if not (SOURCE / module).is_file())
    assert not missing, f"research-layer modules missing: {missing}"


def _module_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        if relative in RESEARCH_LAYER:
            continue
        count = 0
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                count += node.value in RESEARCH_NAMES
            elif isinstance(node, ast.Name):
                count += node.id in NODE_CLASS_NAMES
            elif isinstance(node, ast.Attribute):
                count += node.attr in NODE_CLASS_NAMES
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
        "These modules name more research node types, node classes, or relations than "
        f"before (baseline, now): {grown}. Ask project_type_of() instead, or move a "
        "type's own rule into the research layer."
    )
    fallen = {
        module: (count, current.get(module, 0))
        for module, count in baseline.items()
        if current.get(module, 0) < count
    }
    assert not fallen, (
        f"Node-type coupling fell (baseline, now): {fallen}. Lock in the gain with "
        "`uv run python -m tests.test_project_types`."
    )


if __name__ == "__main__":
    BASELINE.write_text(json.dumps(_module_counts(), indent=2, sort_keys=True) + "\n")
