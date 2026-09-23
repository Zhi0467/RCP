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

_EXTENSION_RULES = """Ontology extensions (this project defines custom types in `graph.json`'s `ontology`):
- Use only active (non-deprecated) custom types, fields, and relations, alongside the base ones above.
- An extension node keeps its base type in `type`, names the custom type in `extension_type`, uses
  `<extension_type>/<kebab-slug>` as its id, and puts only custom field values in
  `extension_fields`, never at the node's top level.
- Follow each field definition: its `kind`, every required field, and never a field whose
  `agent_writable` is false. A custom relation connects only its declared source and target types.
"""

_EDIT_METHOD = """Editing the graph:
- If the active ontology cannot express a needed node or edge, say so in the final answer, name the
  missing vocabulary, and record what can be expressed. Do not create a node for the gap or use a
  definition that is not active.
- Keep node prose concise. When a durable design, plan, result, or handoff file already exists or
  the task naturally produces one, cite its repository-relative path in an allowed field. Do not
  create a file only to cite it; a preview artifact is not durable.

Causal check:
- Separate an Experiment's inputs from what its results will decide. A Decision or Blocker the
  Experiment is meant to settle is downstream of it, never its prerequisite.
- For an empirical gate, find the precursor Experiment and the observation that would settle the
  gate, and name that handoff in the precursor's design.
- Check each edge's direction against the actual causal story.
- A smoke Experiment verifies infrastructure, integration, or recovery, so it is how that
  uncertainty gets resolved. Its own setup, such as unpinned parameters, an unbuilt image, or an
  unrun check, belongs in its design, never in a Blocker on it.
- An open Blocker reached through `blocked_by` stops RCP from starting an Experiment. Give a smoke
  `blocked_by` only for a constraint the run cannot remove, such as a missing credential or
  hardware, with a `resolution_condition` that does not require running it.
- The unverified infrastructure can still gate a downstream main Experiment: that Experiment keeps
  `blocked_by` the Blocker, and the smoke's Evidence `addresses` it.
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
