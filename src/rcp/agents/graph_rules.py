"""The one description of the research graph that agent contracts include.

Field and relation meaning lives on the models in `rcp.core.models`; this module
only renders it, together with the code facts that constrain edits: relation
endpoints and derived layers, which edges carry an assessment or an expectation,
id prefixes, and value vocabularies. Authority is not rendered here. Each
contract states its own beside this block.
"""

from __future__ import annotations

import hashlib
import types
from typing import Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from rcp.core.models import (
    RELATION_SPEC,
    BaseNode,
    Blocker,
    Decision,
    Edge,
    Evidence,
    EvidenceAssessment,
    Experiment,
    ExperimentProxy,
    Hypothesis,
    ResearchQuestion,
)
from rcp.core.ontology import type_pair_layer
from rcp.core.validation.constants import NODE_PREFIXES
from rcp.core.validation.ops import ASSESSMENT_REQUIRED_FOR, EXPECTATION_RELATIONS

_NODE_MODELS: tuple[type[BaseNode], ...] = (
    ResearchQuestion,
    Hypothesis,
    Decision,
    Experiment,
    Evidence,
    Blocker,
)
_TYPE_NAMES = {
    get_args(model.model_fields["type"].annotation)[0]: model.__name__ for model in _NODE_MODELS
}
_NESTED_MODELS: dict[type[BaseModel], str] = {
    ExperimentProxy: "{stands_for, measure}",
    EvidenceAssessment: "{relevance, weight, scope, qualifications}",
}

_READING_METHOD = """Reading the graph:
- `graph.json` holds every node's full prose and grows with the project. Search it for a hit list of
  `{id, type, title, status, standing}` first, then read the full records of only the few nodes the
  question actually turns on. A search that returns whole matched nodes stops fitting as the graph
  grows, and a truncated read is indistinguishable from a small graph.
"""

_EXTENSION_RULES = f"""Ontology extensions:
- This project's supplied graph carries extension definitions in its `ontology` field.
  Use only its active (non-deprecated) type, field, and relation
  definitions. The {len(_NODE_MODELS)} base node types and {len(RELATION_SPEC)} base relations above
  remain available alongside them.
- An extension node keeps its base shape in `type`, sets `extension_type` to the exact active custom
  type name, uses `<extension_type>/<kebab-slug>` as its id, and puts only custom field values in
  `extension_fields`. Never put a custom field at the node's top level. RCP verifies that the custom
  type's declared `base_type` matches `type`.
- Obey every active field definition: use its declared `kind`, include every required field, and
  never write a field whose `agent_writable` value is false. Do not author deprecated types or
  fields. Custom relations likewise use only active relation definitions and their declared source
  and target types.
"""

_EDIT_METHOD = """Editing the graph:
These are methods for authorized graph changes, not additional graph or filesystem authority.
- If the active ontology cannot express a needed node or edge, state that plainly
  in the final answer, name the missing vocabulary, and continue with the records that can be
  expressed. Do not create a node for the gap or use a definition that is not already active.
- Keep node prose concise. When a useful durable design, plan, result, or handoff already exists or
  is naturally produced within the task, cite its exact repository-relative path and purpose in an
  allowed field. Never create a ceremonial file for this rule; temporary previews are not durable
  substitutes. If authorized new work reopens a completed Experiment, update its status,
  `current_summary`, and `next_action` consistently. A clarification alone need not reopen it.
- Internal-run Evidence connects to its producing Experiment and carries honest provenance;
  cite primary artifacts or valid SourceRefs. External or analytic Evidence need not invent an
  Experiment or conversation source.

Local causal check for this Patch:
- Separate an Experiment's inputs from what its results will determine. A Decision or Blocker that
  the Experiment is meant to settle is downstream, not its own prerequisite.
- For an empirical gate, identify the precursor Experiment and what observation would inform the
  Decision or address the Blocker. While that work is planned, describe the intended handoff in the
  Experiment's design or expected outcomes; do not invent Evidence or result edges.
- Once an observation exists, connect Experiment `produces` Evidence, then Evidence `informs`
  Decision or `addresses` Blocker as appropriate. Check edge direction against the actual causal
  story. These edges do not themselves choose the Decision or change the Blocker's status.
- An Experiment whose objective is to verify infrastructure, integration, or recovery — a smoke
  test — is itself how that uncertainty gets resolved. Never block it on the state it exists to
  show: unpinned launch parameters, an unbuilt image, or an unrun check are steps of its own
  `design`, `expected_outcomes`, and `interpretation_rules`. An open Blocker reached through
  `blocked_by` keeps RCP from starting the Experiment, so the smoke carries that edge only for a
  constraint the run cannot remove itself, such as a missing credential or hardware allocation,
  with a `resolution_condition` that does not require running the Experiment. The unverified
  infrastructure may still gate a downstream main Experiment: keep that Blocker, put `blocked_by`
  on the main Experiment, and let the smoke's Evidence `addresses` it.
Example: before a calibration, record the planned comparison and unresolved parameter choice.
After measurements exist, record their bounded Evidence and its `informs` edge to that choice.
Apply only changes this task authorizes; in a correction, preserve unaffected operations.
"""


# Continuations repeat the rules so a long session keeps them. Within one release they are
# the same rules, so they are not described as a replacement unless the version moved.
REPEATED_RULES_NOTE = (
    "The graph rules below repeat the ones this session already holds. They replace the earlier "
    "graph rules only if their version differs from the one this session last received."
)


def graph_rules(*, edits: bool, ontology_extensions: bool) -> str:
    """Render the graph block: definitions always, authoring method only where edits happen.

    Extension rules are authoring rules, so a read-only contract never receives them.
    """

    return f"Graph rules version `{GRAPH_RULES_VERSION}`.\n{_body(edits, ontology_extensions)}"


def _body(edits: bool, ontology_extensions: bool) -> str:
    sections = [_graph_fields(), _READING_METHOD]
    if edits and ontology_extensions:
        sections.append(_EXTENSION_RULES)
    if edits:
        sections.append(_EDIT_METHOD)
    return "\n".join(sections)


def _graph_fields() -> str:
    lines = ["Research graph fields and relations:", "", "Every node has:"]
    lines += _field_lines(BaseNode.model_fields)
    for model in _NODE_MODELS:
        node_type = get_args(model.model_fields["type"].annotation)[0]
        own = {
            name: field
            for name, field in model.model_fields.items()
            if name != "type" and name not in BaseNode.model_fields
        }
        lines += [
            "",
            f"{model.__name__} (`type: {node_type}`, id `{NODE_PREFIXES[node_type]}/<kebab-slug>`):",
        ]
        lines += _field_lines(own)
    lines += ["", "Every edge has:"]
    lines += _field_lines(Edge.model_fields)
    lines += ["", "Relations, as source -> target (layer):"]
    lines += [_relation_line(name) for name in RELATION_SPEC]
    lines += [
        "",
        "An edge accepts an `assessment` or an `expectation` only where the relation above says so.",
        "Proposal ids use `prop/<kebab-slug>`.",
    ]
    return "\n".join(lines) + "\n"


def _field_lines(fields: dict[str, FieldInfo]) -> list[str]:
    lines: list[str] = []
    for name, field in fields.items():
        shape = _shape(field.annotation)
        lines.append(f"- `{name}`{f' ({shape})' if shape else ''}: {field.description}")
        nested = _nested_model(field.annotation)
        if nested is not None:
            lines += ["  " + line for line in _field_lines(nested.model_fields)]
    return lines


def _shape(annotation: object) -> str:
    values = _literal_values(annotation)
    if values:
        return " | ".join(f"`{value}`" for value in values)
    nested = _nested_model(annotation)
    if nested is not None:
        shape = f"`{_NESTED_MODELS[nested]}`"
        return f"list of {shape}" if get_origin(annotation) is list else shape
    return ""


def _literal_values(annotation: object) -> list[str]:
    if get_origin(annotation) is Literal:
        return [str(value) for value in get_args(annotation)]
    if get_origin(annotation) in (Union, types.UnionType):
        return [value for arg in get_args(annotation) for value in _literal_values(arg)]
    return []


def _nested_model(annotation: object) -> type[BaseModel] | None:
    candidates = [annotation, *get_args(annotation)]
    for candidate in candidates:
        if isinstance(candidate, type) and candidate in _NESTED_MODELS:
            return candidate
    return None


def _relation_line(name: str) -> str:
    spec = RELATION_SPEC[name]  # type: ignore[index]
    if spec.same_type:
        endpoints = f"any node -> a node of the same type ({spec.layer})"
    else:
        by_layer: dict[tuple[str, str], list[str]] = {}
        for source in sorted(spec.source_types):
            for target in sorted(spec.target_types):
                layer = type_pair_layer(source, target, spec.layer)
                by_layer.setdefault((target, layer), []).append(_TYPE_NAMES[source])
        endpoints = "; ".join(
            f"{'|'.join(sources)} -> {_TYPE_NAMES[target]} ({layer})"
            for (target, layer), sources in by_layer.items()
        )
    notes = []
    assessed = ASSESSMENT_REQUIRED_FOR.get(name, frozenset())
    if assessed:
        pairs = ", ".join(f"{_TYPE_NAMES[s]} -> {_TYPE_NAMES[t]}" for s, t in sorted(assessed))
        notes.append(f"A new {pairs} edge requires an `assessment`.")
    if name in EXPECTATION_RELATIONS:
        notes.append("It may carry an `expectation`.")
    return f"- `{name}`: {endpoints}. {spec.description}" + "".join(f" {note}" for note in notes)


# One version covers every rendering, so a chat whose modes read different renderings
# still sees a single value, and any change to fields, relations, or method moves it.
GRAPH_RULES_VERSION = hashlib.sha256(
    "\0".join(
        _body(edits, extensions) for edits in (False, True) for extensions in (False, True)
    ).encode("utf-8")
).hexdigest()[:16]
