"""Exact, sparse snapshot differences for the rebuildable history index."""

from __future__ import annotations

from typing import Any, cast, get_origin

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from rcp.core.models import Edge, GraphState
from rcp.core.operations import adapt_persisted_graph_state_document

_FIELD_ADAPTERS: dict[str, TypeAdapter[Any]] = {
    name: TypeAdapter(field.annotation) for name, field in GraphState.model_fields.items()
}
_EDGE_LAYER: TypeAdapter[Any] = TypeAdapter(Edge.model_fields["layer"].annotation)
_MAP_FIELDS = {
    name for name, field in GraphState.model_fields.items() if get_origin(field.annotation) is dict
}

_LIST_FIELDS = {
    name for name, field in GraphState.model_fields.items() if get_origin(field.annotation) is list
}


def copy_state(state: GraphState) -> GraphState:
    """Isolate mutable public snapshot data from the retained generation."""
    return state.model_copy(deep=True)


class ListSplice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=0)
    delete_count: int = Field(ge=0)
    items: list[JsonValue]


class StateDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replacements: dict[str, JsonValue] = Field(default_factory=dict)
    splices: dict[str, ListSplice] = Field(default_factory=dict)
    changed: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    removed: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_fields(self) -> StateDelta:
        if set(self.replacements) - (_FIELD_ADAPTERS.keys() - _MAP_FIELDS):
            raise ValueError("snapshot replacement names an unknown or dictionary field")
        if (self.changed.keys() | self.removed.keys()) - _MAP_FIELDS:
            raise ValueError("snapshot dictionary change names an unknown or scalar field")
        if self.splices.keys() - _LIST_FIELDS or self.splices.keys() & self.replacements.keys():
            raise ValueError("snapshot splice names an unknown, non-list, or replaced field")
        for name, removed in self.removed.items():
            if (
                len(removed) != len(set(removed))
                or set(removed) & self.changed.get(name, {}).keys()
            ):
                raise ValueError("snapshot dictionary keys are removed more than once or replaced")
        return self


def capture_state_delta(before: GraphState, after: GraphState) -> StateDelta:
    replacements: dict[str, JsonValue] = {}
    splices: dict[str, ListSplice] = {}
    changed: dict[str, dict[str, JsonValue]] = {}
    removed: dict[str, list[str]] = {}
    for name, adapter in _FIELD_ADAPTERS.items():
        old, new = getattr(before, name), getattr(after, name)
        if old is new:
            continue
        if name in _MAP_FIELDS:
            updates = {
                key: value
                for key, value in new.items()
                if key not in old or (old[key] is not value and old[key] != value)
            }
            if updates:
                changed[name] = cast(
                    dict[str, JsonValue], adapter.dump_python(updates, mode="json")
                )
            absent = old.keys() - new.keys()
            if absent:
                removed[name] = sorted(absent)
        elif name in _LIST_FIELDS and old != new:
            start = 0
            while start < min(len(old), len(new)) and old[start] == new[start]:
                start += 1
            old_end, new_end = len(old), len(new)
            while old_end > start and new_end > start and old[old_end - 1] == new[new_end - 1]:
                old_end -= 1
                new_end -= 1
            splices[name] = ListSplice(
                start=start,
                delete_count=old_end - start,
                items=cast(list[JsonValue], adapter.dump_python(new[start:new_end], mode="json")),
            )
        elif old != new:
            replacements[name] = cast(JsonValue, adapter.dump_python(new, mode="json"))
    return StateDelta(replacements=replacements, splices=splices, changed=changed, removed=removed)


def apply_state_delta(before: GraphState, delta: StateDelta) -> GraphState:
    updates: dict[str, Any] = {
        name: _FIELD_ADAPTERS[name].validate_python(value)
        for name, value in delta.replacements.items()
    }
    for name, splice in delta.splices.items():
        collection = getattr(before, name)
        end = splice.start + splice.delete_count
        if end > len(collection):
            raise ValueError("snapshot list splice exceeds its predecessor")
        items = _FIELD_ADAPTERS[name].validate_python(splice.items)
        updates[name] = [*collection[: splice.start], *items, *collection[end:]]
    for name in delta.changed.keys() | delta.removed.keys():
        collection = dict(getattr(before, name))
        for key in delta.removed.get(name, []):
            if key not in collection:
                raise ValueError("snapshot dictionary removal is absent from its predecessor")
            del collection[key]
        document = adapt_persisted_graph_state_document({name: delta.changed.get(name, {})})
        validated = _FIELD_ADAPTERS[name].validate_python(document[name])
        if name == "edges":
            validated = {
                key: restore_edge_layer(edge, document[name][key])
                for key, edge in validated.items()
            }
        collection.update(validated)
        updates[name] = collection
    return before.model_copy(update=updates)


def restore_edge_layer(edge: Edge, document: object) -> Edge:
    """Keep the already-derived layer in a verified historical snapshot.

    Edge input validation derives a base layer for new user input; replay has
    already narrowed historical edges using their endpoint types.
    """
    if isinstance(document, dict) and "layer" in document:
        return edge.model_copy(update={"layer": _EDGE_LAYER.validate_python(document["layer"])})
    return edge
