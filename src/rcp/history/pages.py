"""Deterministic, bounded projections of verified history records."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import JsonValue

from rcp import limits
from rcp.core.models import GraphState
from rcp.core.transition_models import GraphHeadRef
from rcp.history.delta import (
    _SEMANTIC_AMBIGUITY_BOOKKEEPING,
    _SEMANTIC_EDGE_BOOKKEEPING,
    _SEMANTIC_GLOSSARY_BOOKKEEPING,
    _SEMANTIC_NODE_BOOKKEEPING,
    _SEMANTIC_PROPOSAL_BOOKKEEPING,
    SemanticDelta,
    semantic_delta,
)
from rcp.history.queries import (
    EntityChange,
    FieldChange,
    GraphDiffPage,
    GraphLogEntry,
    GraphLogPage,
    StepAttribution,
    StepRecord,
    format_head,
)
from rcp.history.store import IndexedStep

# Each semantic collection's public kind, identity field, and excluded bookkeeping.
_COLLECTIONS = (
    ("nodes", "node", "node_id", _SEMANTIC_NODE_BOOKKEEPING),
    ("edges", "edge", "edge_id", _SEMANTIC_EDGE_BOOKKEEPING),
    ("proposals", "proposal", "proposal_id", _SEMANTIC_PROPOSAL_BOOKKEEPING),
    ("ambiguities", "ambiguity", "ambiguity_id", _SEMANTIC_AMBIGUITY_BOOKKEEPING),
    ("glossary", "glossary", "term", _SEMANTIC_GLOSSARY_BOOKKEEPING),
    ("globals", "global", "field", frozenset()),
)


def entity_changes(
    delta: SemanticDelta, ids: frozenset[str] = frozenset()
) -> Iterator[EntityChange]:
    """Use semantic exclusions and filter identities before serializing fields."""
    for collection, kind, identity, bookkeeping in _COLLECTIONS:
        for item in getattr(delta, collection):
            entity_id = getattr(item, identity)
            if ids and entity_id not in ids:
                continue
            if kind == "global":
                yield EntityChange(
                    kind="global",
                    id=entity_id,
                    change="updated",
                    fields=[FieldChange(field=entity_id, before=item.before, after=item.after)],
                )
                continue
            before = (
                item.before.model_dump(mode="json", exclude=set(bookkeeping)) if item.before else {}
            )
            after = (
                item.after.model_dump(mode="json", exclude=set(bookkeeping)) if item.after else {}
            )
            label = after or before
            yield EntityChange(
                kind=cast(Literal["node", "edge", "proposal", "ambiguity", "glossary"], kind),
                id=entity_id,
                type=label.get("type") or label.get("relation"),
                title=label.get("title") or label.get("question") or label.get("term"),
                change=item.change,
                fields=[
                    FieldChange(field=key, before=before.get(key), after=after.get(key))
                    for key in sorted(before.keys() | after.keys())
                    if key not in before or key not in after or before[key] != after[key]
                ],
            )


def attribution(step: StepRecord | IndexedStep) -> StepAttribution:
    return StepAttribution(
        head=format_head(step.after_head),
        producer=step.patch.producer,
        task_id=step.patch.task_id,
        episode_id=step.patch.episode_id,
        summary=step.patch.summary,
        inherited=step.inherited,
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _key(identity: str, query: object) -> str:
    return hashlib.sha256(_json([identity, query]).encode()).hexdigest()


def _cursor(key: str, offset: int) -> str:
    return base64.urlsafe_b64encode(_json([key, offset]).encode()).decode().rstrip("=")


def _position(cursor: str) -> tuple[str, int]:
    try:
        if len(cursor) > limits.GRAPH_HISTORY_CURSOR_MAX_BYTES:
            raise ValueError("history cursor exceeds its bound")
        stored_key, offset = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(stored_key, str) or type(offset) is not int or offset < 0:
            raise ValueError("invalid history cursor position")
        return stored_key, offset
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError("invalid history cursor") from error


def cursor_key(cursor: str) -> str:
    return _position(cursor)[0]


def _offset(cursor: str | None, key: str) -> int:
    if cursor is None:
        return 0
    stored_key, offset = _position(cursor)
    if stored_key != key:
        raise ValueError("history cursor does not match this query")
    return offset


def _parts(value: JsonValue) -> list[JsonValue]:
    encoded = _json(value)
    if len(encoded.encode()) <= limits.GRAPH_HISTORY_VALUE_CHUNK_BYTES:
        return [value]
    # Large structured values are a stream of JSON text; strings remain raw text.
    text = value if isinstance(value, str) else encoded
    result: list[JsonValue] = []
    start = 0
    size = 0
    for index, char in enumerate(text):
        width = 2 if char in '"\\\b\f\n\r\t' else 6 if ord(char) < 32 else len(char.encode())
        if size + width > limits.GRAPH_HISTORY_VALUE_CHUNK_BYTES:
            result.append(text[start:index])
            start, size = index, 0
        size += width
    result.append(text[start:])
    return result


def _fragments(change: EntityChange) -> Iterator[EntityChange]:
    # A title is also present, without abbreviation, among the changed fields.
    # Omit oversized display metadata so that a single fragment remains bounded.
    title = change.title
    if title is not None and len(_json(title).encode()) > limits.GRAPH_HISTORY_VALUE_CHUNK_BYTES:
        title = None
    pending: list[FieldChange] = []
    for field in change.fields:
        before, after = _parts(field.before), _parts(field.after)
        count = max(len(before), len(after))
        for index in range(count):
            fragment = FieldChange(
                field=field.field,
                before=before[index] if index < len(before) else None,
                after=after[index] if index < len(after) else None,
                continued=index + 1 < count,
            )
            candidate = change.model_copy(update={"title": title, "fields": [*pending, fragment]})
            if (
                pending
                and len(candidate.model_dump_json().encode())
                > 3 * limits.GRAPH_HISTORY_VALUE_CHUNK_BYTES
            ):
                yield change.model_copy(update={"title": title, "fields": pending})
                pending = []
            pending.append(fragment)
            if count > 1:
                yield change.model_copy(update={"title": title, "fields": pending})
                pending = []
    if pending:
        yield change.model_copy(update={"title": title, "fields": pending})


def _step_key(step: StepRecord | IndexedStep) -> tuple[str, int, str | None]:
    head = step.after_head
    return head.target.key, head.revision, head.transition_id


def _supports(delta: SemanticDelta, changed: set[tuple[str, str]]) -> bool:
    return any(
        (kind, getattr(item, identity)) in changed
        for collection, kind, identity, _ in _COLLECTIONS
        for item in getattr(delta, collection)
    )


def _matches_ids(step: StepRecord | IndexedStep, ids: frozenset[str]) -> bool:
    # Node/edge deltas can be large; the persisted index rules them out without
    # inspecting their values. Sidecar IDs remain queryable too.
    return bool(
        not ids
        or ids.intersection(step.changed_node_ids)
        or ids.intersection(step.changed_edge_ids)
        or any(
            getattr(item, identity) in ids
            for collection, _, identity, _ in _COLLECTIONS[2:]
            for item in getattr(step.delta, collection)
        )
    )


def diff_key(
    from_head: GraphHeadRef,
    to_head: GraphHeadRef,
    ids: frozenset[str],
    identity: str,
) -> str:
    return _key(
        identity,
        ["diff", from_head.model_dump(mode="json"), to_head.model_dump(mode="json"), sorted(ids)],
    )


@dataclass(frozen=True)
class DiffAnswer:
    key: str
    from_head: str
    to_head: str
    units: tuple[bytes, ...]
    change_count: int
    byte_size: int

    @property
    def retained_bytes(self) -> int:
        return _retained_bytes(self)

    def page(self, cursor: str | None = None) -> GraphDiffPage:
        page = GraphDiffPage(from_head=self.from_head, to_head=self.to_head, changes=[], steps=[])
        _fill(page, self.units, self.key, cursor, self.change_count)
        return page


def compute_diff(
    from_head: GraphHeadRef,
    to_head: GraphHeadRef,
    before: GraphState,
    after: GraphState,
    from_steps: list[StepRecord] | list[IndexedStep],
    to_steps: list[StepRecord] | list[IndexedStep],
    ids: frozenset[str],
    identity: str,
) -> DiffAnswer:
    changes = list(entity_changes(semantic_delta(before, after), ids))
    changed = {(change.kind, change.id) for change in changes}
    left, right = {_step_key(step) for step in from_steps}, {_step_key(step) for step in to_steps}
    common = left & right
    units = [
        fragment.model_dump_json().encode() for change in changes for fragment in _fragments(change)
    ]
    change_count = len(units)
    for step in [*from_steps, *to_steps]:
        if (
            step.accepted
            and _step_key(step) not in common
            and _matches_ids(step, ids)
            and _supports(step.delta, changed)
        ):
            item = attribution(step)
            units.extend(
                item.model_copy(update={"summary": part}).model_dump_json().encode()
                for part in _parts(item.summary)
            )
    return DiffAnswer(
        diff_key(from_head, to_head, ids, identity),
        format_head(from_head),
        format_head(to_head),
        tuple(units),
        change_count,
        sum(map(len, units)),
    )


def _log_units(step: StepRecord | IndexedStep, entity_id: str) -> Iterator[GraphLogEntry]:
    ids = frozenset({entity_id})
    if not step.accepted or not _matches_ids(step, ids):
        return
    for change in entity_changes(step.delta, ids):
        item = attribution(step)
        fragments = list(_fragments(change))
        summaries = _parts(item.summary)
        for index in range(max(len(fragments), len(summaries))):
            yield GraphLogEntry(
                step=item.model_copy(
                    update={"summary": summaries[index] if index < len(summaries) else ""}
                ),
                change=fragments[index]
                if index < len(fragments)
                else change.model_copy(update={"fields": [], "title": None}),
            )


def log_key(entity_id: str, head: GraphHeadRef, identity: str) -> str:
    return _key(identity, ["log", entity_id, head.model_dump(mode="json")])


@dataclass(frozen=True)
class LogAnswer:
    key: str
    entity_id: str
    head: str
    units: tuple[bytes, ...]
    byte_size: int

    @property
    def retained_bytes(self) -> int:
        return _retained_bytes(self)

    def page(self, cursor: str | None = None) -> GraphLogPage:
        page = GraphLogPage(id=self.entity_id, at_head=self.head, entries=[])
        _fill(page, self.units, self.key, cursor)
        return page


def compute_log(
    entity_id: str,
    head: GraphHeadRef,
    steps: list[StepRecord] | list[IndexedStep],
    identity: str,
) -> LogAnswer:
    units = tuple(
        unit.model_dump_json().encode()
        for step in reversed(steps)
        for unit in _log_units(step, entity_id)
    )
    return LogAnswer(
        log_key(entity_id, head, identity),
        entity_id,
        format_head(head),
        units,
        sum(map(len, units)),
    )


def _retained_bytes(answer: DiffAnswer | LogAnswer) -> int:
    # Fields have fixed count; serialized units need only their known payload
    # bytes plus one bytes-object header each. Shared scalars are overcounted.
    namespace = vars(answer)
    return (
        sys.getsizeof(answer)
        + sys.getsizeof(namespace)
        + sum(sys.getsizeof(key) + sys.getsizeof(value) for key, value in namespace.items())
        + answer.byte_size
        + len(answer.units) * sys.getsizeof(b"")
    )


def _fill(
    page: GraphDiffPage | GraphLogPage,
    units: tuple[bytes, ...],
    key: str,
    cursor: str | None,
    change_count: int = 0,
) -> None:
    offset = _offset(cursor, key)
    if cursor is not None and offset >= len(units):
        raise ValueError("history cursor is out of range")
    # The empty envelope includes `cursor:null`; each populated array contributes
    # unit bytes and commas, and the actual next cursor replaces those four bytes.
    size = len(page.model_dump_json().encode())
    for index in range(offset, len(units)):
        if index - offset >= limits.GRAPH_HISTORY_PAGE_MAX_ENTRIES:
            page.cursor = _cursor(key, index)
            return
        unit = units[index]
        if isinstance(page, GraphLogPage):
            comma = bool(page.entries)
        elif index < change_count:
            comma = bool(page.changes)
        else:
            comma = bool(page.steps)
        next_cursor = _cursor(key, index + 1) if index + 1 < len(units) else None
        cursor_size = len(next_cursor) + 2 if next_cursor is not None else 4
        next_size = size + len(unit) + comma
        if next_size + cursor_size - 4 > limits.GRAPH_HISTORY_PAGE_MAX_BYTES:
            if index == offset:
                raise ValueError("history entry metadata exceeds page size")
            page.cursor = _cursor(key, index)
            return
        if isinstance(page, GraphLogPage):
            page.entries.append(GraphLogEntry.model_validate_json(unit))
        elif index < change_count:
            page.changes.append(EntityChange.model_validate_json(unit))
        else:
            page.steps.append(StepAttribution.model_validate_json(unit))
        size = next_size
    page.cursor = None
