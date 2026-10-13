from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from rcp.agents.command_protocol import validate_command_request
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history.queries import (
    EntityChange,
    FieldChange,
    GraphDiffPage,
    GraphLogEntry,
    GraphLogPage,
    HeadSpec,
    StepAttribution,
    format_head,
)
from rcp.limits import GRAPH_HISTORY_COMMAND_RESPONSE_MAX_BYTES
from rcp.runs.graph_history_queries import handle_graph_history_command
from rcp.service import ProjectService


class Engine:
    def __init__(self):
        self.revision = 3
        self.calls = []
        self.value = "界" * 60_000
        self.chunk = 10_000

    def resolve(self, spec: HeadSpec, *, default_target: GraphTargetRef):
        return GraphHeadRef(
            target=spec.target or default_target,
            revision=self.revision if spec.revision == "head" else spec.revision,
        )

    def steps(self, head, *, include_inherited=False):
        return [SimpleNamespace(before_head=GraphHeadRef(target=head.target, revision=1))]

    def change(self, cursor):
        start = int(cursor or "0")
        end = min(start + self.chunk, len(self.value))
        return EntityChange(
            kind="node",
            id="n",
            change="updated",
            fields=[
                FieldChange(
                    field="title", after=self.value[start:end], continued=end < len(self.value)
                )
            ],
        ), str(end) if end < len(self.value) else None

    def diff(self, from_head, to_head, *, ids=frozenset(), cursor=None):
        self.calls.append((from_head, to_head, ids))
        change, next_cursor = self.change(cursor)
        return GraphDiffPage(
            from_head=format_head(from_head),
            to_head=format_head(to_head),
            changes=[change],
            steps=[],
            cursor=next_cursor,
        )

    def log(self, entity_id, at_head, *, cursor=None):
        self.calls.append((entity_id, at_head))
        change, next_cursor = self.change(cursor)
        return GraphLogPage(
            id=entity_id,
            at_head=format_head(at_head),
            entries=[
                GraphLogEntry(
                    step=StepAttribution(head=format_head(at_head), producer="human"), change=change
                )
            ],
            cursor=next_cursor,
        )


def request(verb="graph_diff", **arguments):
    import json

    return validate_command_request(
        json.dumps(
            {
                "version": 1,
                "mailbox_id": "a" * 32,
                "request_id": "b" * 32,
                "credential": "c" * 64,
                "verb": verb,
                "arguments": arguments,
            }
        )
    )


def setup():
    engine = Engine()
    service = cast(
        ProjectService,
        SimpleNamespace(_project_id="p", history_engine=engine),
    )
    scope = GraphReadScope(project_id="p", target=GraphTargetRef(kind="branch", branch_id="b"))
    return engine, service, scope


@pytest.mark.parametrize(
    "verb,args", [("graph_diff", {"from": "@1", "to": "@head"}), ("graph_log", {"id": "n"})]
)
def test_pages_pin_heads_and_continue_large_values_without_mutation(verb, args):
    engine, service, scope = setup()
    chunks = []
    while True:
        response = handle_graph_history_command(service, scope, request(verb, **args))
        assert response.status == "ok"
        assert len(response.model_dump_json().encode()) <= GRAPH_HISTORY_COMMAND_RESPONSE_MAX_BYTES
        page = response.result
        change = page["changes"][0] if verb == "graph_diff" else page["entries"][0]["change"]
        chunks.append(change["fields"][0]["after"])
        assert (page["to_head"] if verb == "graph_diff" else page["at_head"]) == "branch:b@3"
        engine.revision = 10
        if page["cursor"] is None:
            break
        args["cursor"] = page["cursor"]
    assert "".join(chunks) == engine.value
    assert len(engine.calls) == 6
    # The fake exposes no Patch, collection, storage, or mutation methods.


def test_scope_and_verb_refused_before_engine_access():
    engine, service, scope = setup()
    query = request(**{"from": "@1", "to": "main@head"})
    for denied, code in [
        (scope.model_copy(update={"project_id": "other"}), "graph_history_scope_refused"),
        (scope.model_copy(update={"verbs": frozenset()}), "graph_history_verb_not_granted"),
    ]:
        response = handle_graph_history_command(service, denied, query)
        assert response.status == "invalid"
        assert response.result["code"] == code
    assert not engine.calls


def test_cursor_tamper_filter_and_scope_mismatch_are_refused():
    engine, service, scope = setup()
    args = {"from": "@1", "to": "@head", "ids": ["n"]}
    cursor = handle_graph_history_command(service, scope, request(**args)).result["cursor"]
    for changes, query_scope in [
        ({"cursor": cursor[:-2] + "AA"}, scope),
        ({"cursor": cursor, "ids": ["other"]}, scope),
        ({"cursor": cursor, "to": "@2"}, scope),
        ({"cursor": cursor}, scope.model_copy(update={"target": GraphTargetRef()})),
    ]:
        response = handle_graph_history_command(service, query_scope, request(**(args | changes)))
        assert response.status == "invalid"
        assert response.result["code"] == "graph_history_cursor_invalid"
    assert len(engine.calls) == 1


def test_branch_changes_uses_branch_base_and_oversized_engine_page_is_refused():
    engine, service, scope = setup()
    response = handle_graph_history_command(service, scope, request(branch_changes="other"))
    assert response.result["from_head"] == "branch:other@1"
    assert response.result["to_head"] == "branch:other@3"
    engine.chunk = len(engine.value)
    response = handle_graph_history_command(service, scope, request(branch_changes="other"))
    assert response.status == "invalid"
    assert response.result["code"] == "graph_history_response_too_large"
