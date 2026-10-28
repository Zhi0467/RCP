"""Scope-checked history commands; query results never enter Patch collection."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from rcp.agents.command_protocol import (
    CommandRequest,
    CommandResponse,
    GraphDiffCommandRequest,
    GraphLogCommandRequest,
)
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history.queries import HeadSpec, parse_head
from rcp.limits import (
    GRAPH_HISTORY_COMMAND_CURSOR_MAX_LENGTH,
    GRAPH_HISTORY_COMMAND_RESPONSE_MAX_BYTES,
)

if TYPE_CHECKING:
    from rcp.history.queries import HistoryEngine
    from rcp.service import ProjectService

# A restart invalidates continuations, rather than silently resolving a new head.
_CURSOR_KEY = secrets.token_bytes(32)


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding: str
    heads: list[GraphHeadRef]
    engine_cursor: str


def _binding(scope: GraphReadScope, request: CommandRequest) -> str:
    content = {
        "scope": scope.model_dump(mode="json"),
        "verb": request.verb,
        "arguments": request.arguments.model_dump(mode="json", exclude={"cursor"}),
    }
    content["scope"]["verbs"] = sorted(scope.verbs)
    if isinstance(request, GraphDiffCommandRequest):
        content["arguments"]["ids"] = sorted(set(request.arguments.ids))
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def _encode_cursor(cursor: _Cursor) -> str:
    payload = cursor.model_dump_json().encode()
    signature = hmac.digest(_CURSOR_KEY, payload, "sha256")
    token = base64.urlsafe_b64encode(signature + payload).decode()
    if len(token) > GRAPH_HISTORY_COMMAND_CURSOR_MAX_LENGTH:
        raise ValueError("history cursor exceeds the command limit")
    return token


def _decode_cursor(token: str, binding: str) -> _Cursor:
    try:
        raw = base64.b64decode(token, altchars=b"-_", validate=True)
        signature, payload = raw[:32], raw[32:]
        if not hmac.compare_digest(signature, hmac.digest(_CURSOR_KEY, payload, "sha256")):
            raise ValueError("cursor signature mismatch")
        cursor = _Cursor.model_validate_json(payload)
        if cursor.binding != binding:
            raise ValueError("cursor query mismatch")
        return cursor
    except (ValueError, UnicodeError) as exc:
        raise ValueError("graph_history_cursor_invalid") from exc


def handle_graph_history_command(
    service: ProjectService,
    scope: GraphReadScope,
    request: CommandRequest,
    *,
    resolve_project: Callable[[str], ProjectService] | None = None,
) -> CommandResponse:
    """Resolve once, then bind each engine continuation to its query and read scope."""

    def refuse(code: str) -> CommandResponse:
        return CommandResponse(
            request_id=request.request_id, status="invalid", message=code, result={"code": code}
        )

    if not isinstance(request, (GraphDiffCommandRequest, GraphLogCommandRequest)):
        return refuse("graph_history_verb_not_granted")
    if request.verb not in scope.verbs:
        return refuse("graph_history_verb_not_granted")
    arguments = request.arguments
    project_id = arguments.project or scope.project_id
    default_target = scope.target if project_id == scope.project_id else GraphTargetRef()
    if not scope.readable(project_id, default_target):
        return refuse("graph_history_scope_refused")
    if project_id != service._project_id:
        if resolve_project is None:
            return refuse("graph_history_scope_refused")
        try:
            service = resolve_project(project_id)
        except KeyError:
            return refuse("graph_history_scope_refused")
    if service._project_id != project_id:
        return refuse("graph_history_scope_refused")
    if isinstance(request, GraphDiffCommandRequest):
        args = request.arguments
        specs = (
            [parse_head(f"branch:{args.branch_changes}@head")]
            if args.branch_changes is not None
            else [parse_head(args.from_head or ""), parse_head(args.to_head or "")]
        )
    else:
        specs = [parse_head(request.arguments.at)]
    names = service.paper.store.branch_names(project_id)
    ids_by_name = {name: branch_id for branch_id, name in names.items()}
    for index, spec in enumerate(specs):
        if spec.target is None or spec.target.branch_id is None:
            continue
        branch_id = ids_by_name.get(spec.target.branch_id)
        if branch_id is not None:
            specs[index] = spec.model_copy(
                update={"target": GraphTargetRef(kind="branch", branch_id=branch_id)}
            )
    if any(
        not scope.readable(
            project_id,
            spec.target or default_target,
            spec.revision if isinstance(spec.revision, int) else None,
        )
        for spec in specs
    ):
        return refuse("graph_history_scope_refused")
    binding = _binding(scope, request)
    try:
        continuation = _decode_cursor(arguments.cursor, binding) if arguments.cursor else None
    except ValueError:
        return refuse("graph_history_cursor_invalid")
    if continuation is not None and any(
        not scope.readable(project_id, head.target, head.revision) for head in continuation.heads
    ):
        return refuse("graph_history_scope_refused")
    try:
        engine: HistoryEngine = service.history_engine
        if continuation is not None:
            heads = continuation.heads
            # Recheck exact retained heads, including transition identities.
            if any(
                engine.resolve(
                    HeadSpec(target=head.target, revision=head.revision),
                    default_target=default_target,
                )
                != head
                for head in heads
            ):
                return refuse("graph_history_cursor_invalid")
        else:
            heads = [engine.resolve(spec, default_target=default_target) for spec in specs]
            if isinstance(request, GraphDiffCommandRequest) and request.arguments.branch_changes:
                steps = engine.steps(heads[0])
                heads.insert(0, steps[0].before_head if steps else heads[0])
        if any(not scope.readable(project_id, head.target, head.revision) for head in heads):
            return refuse("graph_history_scope_refused")
        cursor = continuation.engine_cursor if continuation else None
        if isinstance(request, GraphDiffCommandRequest):
            if len(heads) != 2:
                return refuse("graph_history_cursor_invalid")
            page = engine.diff(
                heads[0], heads[1], ids=frozenset(request.arguments.ids), cursor=cursor
            )
        else:
            if len(heads) != 1:
                return refuse("graph_history_cursor_invalid")
            page = engine.log(request.arguments.id, heads[0], cursor=cursor)
        result = page.model_dump(mode="json")
        result["branch_names"] = {
            head.target.branch_id: names[head.target.branch_id]
            for head in heads
            if head.target.branch_id in names
        }
        if page.cursor is not None:
            result["cursor"] = _encode_cursor(
                _Cursor(binding=binding, heads=heads, engine_cursor=page.cursor)
            )
        response = CommandResponse(request_id=request.request_id, status="ok", result=result)
        if (
            len(response.model_dump_json().encode("utf-8"))
            > GRAPH_HISTORY_COMMAND_RESPONSE_MAX_BYTES
        ):
            return refuse("graph_history_response_too_large")
        return response
    except (KeyError, ValueError):
        return refuse(
            "graph_history_cursor_invalid" if continuation else "graph_history_query_invalid"
        )
