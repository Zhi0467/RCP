from __future__ import annotations

import pytest

from rcp.agents.command_protocol import validate_command_request
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.core.transition_models import GraphTargetRef
from rcp.history import HistoryManager
from rcp.history.queries import GraphDiffPage
from rcp.limits import GRAPH_HISTORY_COMMAND_RESPONSE_MAX_BYTES
from rcp.paper import PaperService
from rcp.runs.graph_history_queries import handle_graph_history_command
from rcp.service import ProjectService
from rcp.storage import AppStore

from .helpers import seed_patch, unchecked_write_authority
from .test_branch_history import _branch_metadata, _branch_patch


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


@pytest.fixture
def history_query(manifest, tmp_path):
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    history.append(seed_patch())
    service = ProjectService(
        manifest,
        history,
        PaperService(manifest, AppStore(tmp_path / "app.db")),
        project_id="project",
    )
    branch = service.history.create_branch(_branch_metadata(service.history))
    branch.append(_branch_patch("ev/long", title="界" * 60_000))
    scope = GraphReadScope(project_id=service._project_id, target=branch.graph_target)
    return service, scope, branch


@pytest.mark.parametrize(
    "verb,args",
    [
        ("graph_diff", {"from": "@1", "to": "@head", "ids": ["ev/long"]}),
        ("graph_log", {"id": "ev/long"}),
    ],
)
def test_pages_pin_heads_and_continue_large_values_without_mutation(history_query, verb, args):
    service, scope, branch = history_query
    args = dict(args)
    chunks = []
    original = branch.head_ref()
    while True:
        before = branch.head_ref()
        response = handle_graph_history_command(service, scope, request(verb, **args))
        assert response.status == "ok"
        assert branch.head_ref() == before
        assert len(response.model_dump_json().encode()) <= GRAPH_HISTORY_COMMAND_RESPONSE_MAX_BYTES
        page = response.result
        changes = (
            page["changes"] if verb == "graph_diff" else [e["change"] for e in page["entries"]]
        )
        assert all(change["id"] == "ev/long" for change in changes)
        chunks.extend(f["after"] for c in changes for f in c["fields"] if f["field"] == "title")
        assert (
            page["to_head"] if verb == "graph_diff" else page["at_head"]
        ) == f"{scope.target.key}@2"
        if "cursor" not in args:
            assert page["cursor"] is not None
            branch.append(_branch_patch("ev/later"))
        if page["cursor"] is None:
            break
        args["cursor"] = page["cursor"]
    assert "".join(chunks) == service.history_engine.state_at(original).nodes["ev/long"].title


def test_scope_and_verb_refused_before_engine_access(history_query, monkeypatch):
    service, scope, _branch = history_query

    def forbidden(_self):
        pytest.fail("denied commands must not access the engine")

    monkeypatch.setattr(ProjectService, "history_engine", property(forbidden))
    query = request(**{"from": "@1", "to": "main@head"})
    for denied, code in [
        (scope.model_copy(update={"project_id": "other"}), "graph_history_scope_refused"),
        (scope.model_copy(update={"verbs": frozenset()}), "graph_history_verb_not_granted"),
    ]:
        response = handle_graph_history_command(service, denied, query)
        assert response.status == "invalid"
        assert response.result["code"] == code


def test_cursor_tamper_filter_and_scope_mismatch_are_refused(history_query, monkeypatch):
    service, scope, _branch = history_query
    args = {"from": "@1", "to": "@head", "ids": ["ev/long"]}
    cursor = handle_graph_history_command(service, scope, request(**args)).result["cursor"]
    assert cursor is not None

    def forbidden(_self):
        pytest.fail("invalid cursors must not access the engine")

    monkeypatch.setattr(ProjectService, "history_engine", property(forbidden))
    for changes, query_scope in [
        ({"cursor": ("A" if cursor[0] != "A" else "B") + cursor[1:]}, scope),
        ({"cursor": cursor, "ids": ["other"]}, scope),
        ({"cursor": cursor, "to": "@2"}, scope),
        ({"cursor": cursor}, scope.model_copy(update={"target": GraphTargetRef()})),
    ]:
        response = handle_graph_history_command(service, query_scope, request(**(args | changes)))
        assert response.status == "invalid"
        assert response.result["code"] == "graph_history_cursor_invalid"


def test_branch_changes_uses_branch_base_and_oversized_engine_page_is_refused(
    history_query, monkeypatch
):
    service, scope, branch = history_query
    # Cross-branch reads still resolve the queried branch's base.
    scope = scope.model_copy(update={"target": GraphTargetRef()})
    branch_id = branch.branch_id
    response = handle_graph_history_command(service, scope, request(branch_changes=branch_id))
    assert response.status == "ok"
    assert response.result["from_head"] == f"branch:{branch_id}@1"
    assert response.result["to_head"] == f"branch:{branch_id}@2"
    oversized = GraphDiffPage(
        from_head="x" * GRAPH_HISTORY_COMMAND_RESPONSE_MAX_BYTES,
        to_head="main@2",
        changes=[],
        steps=[],
    )
    monkeypatch.setattr(service.history_engine, "diff", lambda *a, **kw: oversized)
    response = handle_graph_history_command(service, scope, request(branch_changes=branch_id))
    assert response.status == "invalid"
    assert response.result["code"] == "graph_history_response_too_large"


def test_fork_scope_enforces_ancestry_revisions_and_cursor_binding(history_query, monkeypatch):
    from .test_history_forks import fork_metadata

    service, unrestricted, branch = history_query
    parent_head = branch.head_ref()
    fork = service.history.create_branch(fork_metadata(service.history, parent_head))
    nested = service.history.create_branch(fork_metadata(service.history, fork.head_ref()))
    scope = GraphReadScope.resolve("project", nested)
    assert scope.ancestry_ceilings == (
        fork.head_ref(),
        parent_head,
        branch.branch_metadata().base_head,
    )
    assert GraphReadScope.resolve("project", branch).ancestry_ceilings is None
    episode_root = service.history.create_branch(
        fork_metadata(service.history, service.history.head_ref()).model_copy(
            update={"kind": "auto_research"}
        )
    )
    assert GraphReadScope.resolve("project", episode_root).ancestry_ceilings is None
    # Saved pre-change grants retain their project-wide semantics.
    assert GraphReadScope.model_validate({"project_id": "project", "target": {}}) == (
        GraphReadScope.resolve("project", service.history)
    )
    with monkeypatch.context() as guard:
        guard.setattr(
            ProjectService,
            "history_engine",
            property(lambda _: pytest.fail("scope refused before engine read")),
        )
        for revision in (parent_head.revision + 1, 999):
            denied = handle_graph_history_command(
                service,
                scope,
                request("graph_log", id="ev/long", at=f"{parent_head.target.key}@{revision}"),
            )
            assert denied.result["code"] == "graph_history_scope_refused"
    roundtrip = GraphReadScope.model_validate_json(scope.model_dump_json())
    assert roundtrip == scope
    args = {"from": f"{parent_head.target.key}@1", "to": str(parent_head.target.key) + "@2"}
    first = handle_graph_history_command(service, scope, request(**args))
    assert first.status == "ok"
    assert first.result["cursor"] is not None
    branch.append(_branch_patch("ev/post-fork"))
    service.history.append(_branch_patch("ev/post-root"))
    for head in (f"{parent_head.target.key}@head", "main@head"):
        denied = handle_graph_history_command(
            service, scope, request("graph_log", id="ev/long", at=head)
        )
        assert denied.result["code"] == "graph_history_scope_refused"
    continued = handle_graph_history_command(
        service, roundtrip, request(**args, cursor=first.result["cursor"])
    )
    assert continued.status == "ok"
    denied = handle_graph_history_command(
        service, unrestricted, request(**args, cursor=first.result["cursor"])
    )
    assert denied.result["code"] == "graph_history_cursor_invalid"
    sibling = service.history.create_branch(fork_metadata(service.history, parent_head))
    denied = handle_graph_history_command(
        service, scope, request("graph_log", id="ev/long", at=f"{sibling.graph_target.key}@head")
    )
    assert denied.result["code"] == "graph_history_scope_refused"
    assert (
        handle_graph_history_command(service, scope, request("graph_log", id="ev/long")).status
        == "ok"
    )
