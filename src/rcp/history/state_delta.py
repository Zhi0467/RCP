"""Exact, sparse snapshot differences for the rebuildable history index."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast, get_origin

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from rcp.core.models import Edge, GraphState
from rcp.core.operations import adapt_persisted_graph_state_document

_SCALAR_TYPES = {str, int, float, bool, type(None), datetime}

_FIELD_ADAPTERS: dict[str, TypeAdapter[Any]] = {
    name: TypeAdapter(field.annotation) for name, field in GraphState.model_fields.items()
}
_EDGE_LAYER: TypeAdapter[Any] = TypeAdapter(Edge.model_fields["layer"].annotation)
_MAP_FIELDS = {
    name for name, field in GraphState.model_fields.items() if get_origin(field.annotation) is dict
}


def copy_state(state: GraphState) -> GraphState:
    """Isolate mutable public snapshot data without copying immutable scalar values."""
    return cast(GraphState, _copy_snapshot(state))


def _copy_snapshot(value: Any) -> Any:
    if type(value) in _SCALAR_TYPES:
        return value
    if isinstance(value, dict):
        return {key: _copy_snapshot(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_snapshot(item) for item in value]
    if isinstance(value, BaseModel):
        copied = value.model_copy(
            update={key: _copy_snapshot(item) for key, item in value.__dict__.items()}
        )
        copied.__pydantic_fields_set__ = value.__pydantic_fields_set__.copy()
        if value.__pydantic_extra__ is not None:
            copied.__pydantic_extra__ = _copy_snapshot(value.__pydantic_extra__)
        if value.__pydantic_private__ is not None:
            copied.__pydantic_private__ = _copy_snapshot(value.__pydantic_private__)
        return copied
    if isinstance(value, tuple):
        return tuple(_copy_snapshot(item) for item in value)
    if isinstance(value, set):
        return set(value)
    return value


class StateDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replacements: dict[str, JsonValue] = Field(default_factory=dict)
    changed: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    removed: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_fields(self) -> StateDelta:
        if set(self.replacements) - (_FIELD_ADAPTERS.keys() - _MAP_FIELDS):
            raise ValueError("snapshot replacement names an unknown or dictionary field")
        if (self.changed.keys() | self.removed.keys()) - _MAP_FIELDS:
            raise ValueError("snapshot dictionary change names an unknown or scalar field")
        for name, removed in self.removed.items():
            if (
                len(removed) != len(set(removed))
                or set(removed) & self.changed.get(name, {}).keys()
            ):
                raise ValueError("snapshot dictionary keys are removed more than once or replaced")
        return self


def capture_state_delta(before: GraphState, after: GraphState) -> StateDelta:
    replacements: dict[str, JsonValue] = {}
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
        elif old != new:
            replacements[name] = cast(JsonValue, adapter.dump_python(new, mode="json"))
    return StateDelta(replacements=replacements, changed=changed, removed=removed)


def apply_state_delta(before: GraphState, delta: StateDelta) -> GraphState:
    updates: dict[str, Any] = {
        name: _FIELD_ADAPTERS[name].validate_python(value)
        for name, value in delta.replacements.items()
    }
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
