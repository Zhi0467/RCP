"""Deterministic, bounded projections of verified history records."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
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


def entity_changes(delta: SemanticDelta) -> Iterator[EntityChange]:
    """Use the semantic comparison's bookkeeping exclusions for field output too."""
    groups = (
        ("node", delta.nodes, "node_id", _SEMANTIC_NODE_BOOKKEEPING),
        ("edge", delta.edges, "edge_id", _SEMANTIC_EDGE_BOOKKEEPING),
        ("proposal", delta.proposals, "proposal_id", _SEMANTIC_PROPOSAL_BOOKKEEPING),
        ("ambiguity", delta.ambiguities, "ambiguity_id", _SEMANTIC_AMBIGUITY_BOOKKEEPING),
        ("glossary", delta.glossary, "term", _SEMANTIC_GLOSSARY_BOOKKEEPING),
    )
    for kind, changes, identity, bookkeeping in groups:
        for item in changes:
            before = (
                item.before.model_dump(mode="json", exclude=set(bookkeeping)) if item.before else {}
            )
            after = (
                item.after.model_dump(mode="json", exclude=set(bookkeeping)) if item.after else {}
            )
            label = after or before
            yield EntityChange(
                kind=cast(Literal["node", "edge", "proposal", "ambiguity", "glossary"], kind),
                id=getattr(item, identity),
                type=label.get("type") or label.get("relation"),
                title=label.get("title") or label.get("question") or label.get("term"),
                change=item.change,
                fields=[
                    FieldChange(field=key, before=before.get(key), after=after.get(key))
                    for key in sorted(before.keys() | after.keys())
                    if key not in before or key not in after or before[key] != after[key]
                ],
            )
    for item in delta.globals:
        yield EntityChange(
            kind="global",
            id=item.field,
            change="updated",
            fields=[FieldChange(field=item.field, before=item.before, after=item.after)],
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


def _offset(cursor: str | None, key: str) -> int:
    if cursor is None:
        return 0
    try:
        if len(cursor) > limits.GRAPH_HISTORY_CURSOR_MAX_BYTES:
            raise ValueError("history cursor exceeds its bound")
        stored_key, offset = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if stored_key != key or type(offset) is not int or offset < 0:
            raise ValueError("history cursor does not match this query")
        return offset
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError("invalid history cursor") from error


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
        width = len(_json(char).encode()) - 2
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
    return (
        any(("node", item.node_id) in changed for item in delta.nodes)
        or any(("edge", item.edge_id) in changed for item in delta.edges)
        or any(("proposal", item.proposal_id) in changed for item in delta.proposals)
        or any(("ambiguity", item.ambiguity_id) in changed for item in delta.ambiguities)
        or any(("glossary", item.term) in changed for item in delta.glossary)
        or any(("global", item.field) in changed for item in delta.globals)
    )


def _diff_units(
    changes: list[EntityChange],
    steps: list[StepRecord | IndexedStep],
    offset: int,
) -> Iterator[tuple[int, EntityChange | StepAttribution]]:
    position = 0
    for change in changes:
        for fragment in _fragments(change):
            if position >= offset:
                yield position, fragment
            position += 1
    for step in steps:
        summaries = _parts(step.patch.summary)
        if position + len(summaries) <= offset:
            position += len(summaries)
            continue
        item = attribution(step)
        for part in summaries:
            if position >= offset:
                yield position, item.model_copy(update={"summary": part})
            position += 1
    if position < offset:
        raise ValueError("history cursor is out of range")


def diff_page(
    from_head: GraphHeadRef,
    to_head: GraphHeadRef,
    before: GraphState,
    after: GraphState,
    from_steps: list[StepRecord] | list[IndexedStep],
    to_steps: list[StepRecord] | list[IndexedStep],
    ids: frozenset[str],
    cursor: str | None,
    identity: str,
) -> GraphDiffPage:
    changes = [
        change
        for change in entity_changes(semantic_delta(before, after))
        if not ids or change.id in ids
    ]
    changed = {(change.kind, change.id) for change in changes}
    left, right = {_step_key(step) for step in from_steps}, {_step_key(step) for step in to_steps}
    common = left & right
    steps = [
        step
        for step in [*from_steps, *to_steps]
        if step.accepted and _step_key(step) not in common and _supports(step.delta, changed)
    ]
    key = _key(
        identity,
        ["diff", from_head.model_dump(mode="json"), to_head.model_dump(mode="json"), sorted(ids)],
    )
    offset = _offset(cursor, key)
    page = GraphDiffPage(
        from_head=format_head(from_head), to_head=format_head(to_head), changes=[], steps=[]
    )
    _fill(page, _diff_units(changes, steps, offset), key)
    return page


def _log_cursor(key: str, step: int, fragment: int) -> str:
    return base64.urlsafe_b64encode(_json([key, step, fragment]).encode()).decode().rstrip("=")


def _log_position(cursor: str | None, key: str) -> tuple[int, int]:
    if cursor is None:
        return 0, 0
    try:
        if len(cursor) > limits.GRAPH_HISTORY_CURSOR_MAX_BYTES:
            raise ValueError("history cursor exceeds its bound")
        stored_key, step, fragment = json.loads(
            base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        )
        if stored_key != key or any(
            type(value) is not int or value < 0 for value in (step, fragment)
        ):
            raise ValueError("history cursor does not match this query")
        return step, fragment
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError("invalid history cursor") from error


def _id_changes(delta: SemanticDelta, entity_id: str) -> Iterator[EntityChange]:
    filtered = delta.model_copy(
        update={
            "nodes": [item for item in delta.nodes if item.node_id == entity_id],
            "edges": [item for item in delta.edges if item.edge_id == entity_id],
            "proposals": [item for item in delta.proposals if item.proposal_id == entity_id],
            "ambiguities": [item for item in delta.ambiguities if item.ambiguity_id == entity_id],
            "glossary": [item for item in delta.glossary if item.term == entity_id],
            "globals": [item for item in delta.globals if item.field == entity_id],
        }
    )
    return entity_changes(filtered)


def _log_units(step: StepRecord | IndexedStep, entity_id: str) -> Iterator[GraphLogEntry]:
    if not step.accepted:
        return
    for change in _id_changes(step.delta, entity_id):
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


def log_page(
    entity_id: str,
    head: GraphHeadRef,
    steps: list[StepRecord] | list[IndexedStep],
    cursor: str | None,
    identity: str,
) -> GraphLogPage:
    key = _key(identity, ["log", entity_id, head.model_dump(mode="json")])
    start, fragment_start = _log_position(cursor, key)
    if cursor is not None and (
        start >= len(steps) or fragment_start >= sum(1 for _ in _log_units(steps[start], entity_id))
    ):
        raise ValueError("history cursor is out of range")
    page = GraphLogPage(id=entity_id, at_head=format_head(head), entries=[])
    for step_index in range(start, len(steps)):
        for fragment_index, unit in enumerate(_log_units(steps[step_index], entity_id)):
            if step_index == start and fragment_index < fragment_start:
                continue
            if len(page.entries) >= limits.GRAPH_HISTORY_PAGE_MAX_ENTRIES:
                page.cursor = _log_cursor(key, step_index, fragment_index)
                return page
            page.entries.append(unit)
            page.cursor = _log_cursor(key, step_index, fragment_index + 1)
            if len(page.model_dump_json().encode()) > limits.GRAPH_HISTORY_PAGE_MAX_BYTES:
                page.entries.pop()
                if not page.entries:
                    raise ValueError("history entry metadata exceeds page size")
                page.cursor = _log_cursor(key, step_index, fragment_index)
                return page
    page.cursor = None
    return page


def _fill(
    page: GraphDiffPage,
    units: Iterator[tuple[int, EntityChange | StepAttribution]],
    key: str,
) -> None:
    for count, (index, unit) in enumerate(units):
        if count >= limits.GRAPH_HISTORY_PAGE_MAX_ENTRIES:
            page.cursor = _cursor(key, index)
            return
        if isinstance(unit, EntityChange):
            page.changes.append(unit)
        else:
            page.steps.append(unit)
        page.cursor = _cursor(key, index + 1)
        if len(page.model_dump_json().encode()) > limits.GRAPH_HISTORY_PAGE_MAX_BYTES:
            if isinstance(unit, EntityChange):
                page.changes.pop()
            else:
                page.steps.pop()
            if not count:
                raise ValueError("history entry metadata exceeds page size")
            page.cursor = _cursor(key, index)
            return
    page.cursor = None
