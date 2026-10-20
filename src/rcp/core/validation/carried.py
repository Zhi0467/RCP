"""What a branch merge carries: content its source branch already admitted.

A branch judged its content under the authoring rules of its day, as main judged
its own, and replay never re-runs those rules. A merge that re-judged carried
content under today's rules could refuse a branch forever. So content rules skip
an entity only when the orchestrator's merge Patch writes exactly what the source
branch head holds; anything a merge answer assembles meets today's rules.
Integrity checks (identity, authority, endpoints, protected review) still run on
everything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from rcp.core.models import graph_fields

if TYPE_CHECKING:
    from rcp.core.models import GraphState
    from rcp.core.validation.context import OpContext


def _source(ctx: OpContext) -> GraphState | None:
    merge = ctx.patch.branch_merge or (
        ctx.reference_patch.branch_merge if ctx.reference_patch is not None else None
    )
    if merge is None or ctx.merge_source is None:
        return None
    writer = (
        ctx.reference_patch
        if ctx.reference_patch is not None and ctx.patch.branch_merge is None
        else ctx.patch
    )
    if writer.author != "agent" or writer.profile != "orchestrator":
        return None
    return ctx.merge_source


def _json(value: Any) -> Any:
    return to_jsonable_python(value)


def _matches(payload: dict[str, Any], admitted: BaseModel, *, defaults_ok: bool) -> bool:
    document = admitted.model_dump(mode="json")
    fields = type(admitted).model_fields
    for key, value in payload.items():
        if key == "id":
            continue
        value = _json(value)
        if key in document and document[key] == value:
            continue
        # A merge resets lifecycle fields of what it creates to their defaults.
        if (
            defaults_ok
            and key in fields
            and not fields[key].is_required()
            and _json(fields[key].get_default(call_default_factory=True)) == value
        ):
            continue
        return False
    return True


def carries_edge(ctx: OpContext, edge: BaseModel) -> bool:
    source = _source(ctx)
    if source is None:
        return False
    payload = edge.model_dump(mode="python", exclude_unset=True)
    edge_id = (
        payload.get("id") or f"{payload['source']}::{payload['relation']}::{payload['target']}"
    )
    admitted = source.edges.get(edge_id)
    return admitted is not None and _matches(payload, admitted, defaults_ok=False)


def carries_new_node(ctx: OpContext, node: BaseModel) -> bool:
    source = _source(ctx)
    if source is None:
        return False
    payload = node.model_dump(mode="python", exclude_unset=True)
    admitted = source.nodes.get(payload["id"])
    return (
        admitted is not None
        and admitted.type == payload.get("type")
        and _matches(payload, admitted, defaults_ok=True)
    )


def carries_update(ctx: OpContext, node_id: str, changes: dict[str, Any]) -> bool:
    source = _source(ctx)
    if source is None:
        return False
    admitted = source.nodes.get(node_id)
    return admitted is not None and _matches(changes, admitted, defaults_ok=False)


def carries_staged_node(ctx: OpContext, node_id: str) -> bool:
    """The node as this Patch leaves it is the source's, engine fields aside."""
    source = _source(ctx)
    if source is None:
        return False
    admitted = source.nodes.get(node_id)
    staged = ctx.state.nodes.get(node_id)
    if admitted is None or staged is None or type(admitted) is not type(staged):
        return False
    engine = set(graph_fields(type(staged), role="engine")) | {"standing"}
    return staged.model_dump(mode="json", exclude=engine) == admitted.model_dump(
        mode="json", exclude=engine
    )
