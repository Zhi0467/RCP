from __future__ import annotations

import pytest

from rcp.agents.command_protocol import validate_command_request
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.agents.group_context import GroupContext, GroupProjectContext
from rcp.core.transition_models import GraphTargetRef
from rcp.history import HistoryManager
from rcp.history.queries import GraphDiffPage
from rcp.limits import GRAPH_HISTORY_COMMAND_RESPONSE_MAX_BYTES
from rcp.paper import PaperService
from rcp.runs.graph_history_queries import handle_graph_history_command
from rcp.service import ProjectService
from rcp.storage import AppStore
from rcp.transport import RepositoryAccess

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
    scope = GraphReadScope.resolve("project", nested, store=service.paper.store)
    assert scope.ancestry_ceilings == (
        fork.head_ref(),
        parent_head,
        branch.branch_metadata().base_head,
    )
    assert (
        GraphReadScope.resolve("project", branch, store=service.paper.store).ancestry_ceilings
        is None
    )
    episode_root = service.history.create_branch(
        fork_metadata(service.history, service.history.head_ref()).model_copy(
            update={"kind": "auto_research"}
        )
    )
    assert (
        GraphReadScope.resolve("project", episode_root, store=service.paper.store).ancestry_ceilings
        is None
    )
    # Saved pre-change grants retain their project-wide semantics.
    assert GraphReadScope.model_validate({"project_id": "project", "target": {}}) == (
        GraphReadScope.resolve("project", service.history, store=service.paper.store)
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


def group_context(sibling_id):
    return GroupContext(
        project_id="own",
        project_name="Own",
        group_name="Group",
        siblings=(
            GroupProjectContext(
                project_id=sibling_id,
                name="Sibling",
                graph=RepositoryAccess(
                    alias="graph", machine="local", path="/sibling/.research/graph.json"
                ),
                research_md=RepositoryAccess(
                    alias="research", machine="local", path="/sibling/.research/research.md"
                ),
                repositories=(),
            ),
        ),
    )


def test_group_scope_allows_all_sibling_targets_only():
    scope = GraphReadScope(
        project_id="own", target=GraphTargetRef(), group_context=group_context("sibling")
    )
    assert "readable_project_ids" not in scope.model_dump()
    assert GraphReadScope.model_validate_json(scope.model_dump_json()) == scope
    unavailable = group_context("sibling").model_copy(
        update={
            "siblings": (
                GroupProjectContext(
                    project_id="sibling", name="Sibling", unavailable_reason="manifest unavailable"
                ),
            )
        }
    )
    assert not GraphReadScope(
        project_id="own", target=GraphTargetRef(), group_context=unavailable
    ).readable("sibling", GraphTargetRef())
    for target in (GraphTargetRef(), GraphTargetRef(kind="branch", branch_id="branch")):
        assert scope.readable("own", target)
        assert scope.readable("sibling", target)
        assert not scope.readable("outside", target)


@pytest.mark.parametrize("verb", ["graph_log", "graph_diff"])
def test_group_history_resolves_sibling_and_binds_cursor(history_query, verb):
    service, scope, branch = history_query
    own = ProjectService(service.manifest, service.history, service.paper, project_id="own")
    scope = GraphReadScope(
        project_id="own", target=scope.target, group_context=group_context("project")
    )
    args = (
        {"id": "ev/long", "at": f"{branch.graph_target.key}@head"}
        if verb == "graph_log"
        else {"branch_changes": branch.branch_id}
    )
    resolved = []

    def resolve(project_id):
        resolved.append(project_id)
        return service

    response = handle_graph_history_command(
        own, scope, request(verb, project="project", **args), resolve_project=resolve
    )
    assert response.status == "ok"
    assert resolved == ["project"]
    cursor = response.result["cursor"]
    assert cursor is not None
    for project, code in [
        ("own", "graph_history_cursor_invalid"),
        ("outside", "graph_history_scope_refused"),
    ]:
        response = handle_graph_history_command(
            own,
            scope,
            request(verb, project=project, cursor=cursor, **args),
            resolve_project=resolve,
        )
        assert response.result["code"] == code
    assert resolved == ["project"]


@pytest.mark.parametrize("verb", ["graph_log", "graph_diff", "branch_changes"])
def test_branch_names_resolve_at_command_time(history_query, monkeypatch, verb):
    service, scope, branch = history_query
    branch.append(_branch_patch("ev/named"))
    names = {branch.branch_id: "vla-if"}
    monkeypatch.setattr(service.paper.store, "branch_names", lambda project: dict(names))

    def query(ref):
        if verb == "graph_log":
            return request(verb, id="ev/named", at=f"branch:{ref}@head")
        if verb == "branch_changes":
            return request(branch_changes=ref, ids=["ev/named"])
        return request(**{"from": "main@1", "to": f"branch:{ref}@head", "ids": ["ev/named"]})

    by_id = handle_graph_history_command(service, scope, query(branch.branch_id))
    by_name = handle_graph_history_command(service, scope, query("vla-if"))
    assert by_name.status == by_id.status == "ok"
    assert by_name.result == by_id.result
    assert by_name.result["entries" if verb == "graph_log" else "changes"]
    assert by_name.result["branch_names"] == names
    names[branch.branch_id] = "renamed-branch"
    assert handle_graph_history_command(service, scope, query("renamed-branch")).status == "ok"
    for missing in ("vla-if", "unknown-branch"):
        refused = handle_graph_history_command(service, scope, query(missing))
        assert refused.result["code"] == "graph_history_query_invalid"


def test_branch_inventory_respects_fork_read_scope(history_query, monkeypatch):
    from .test_history_forks import fork_metadata

    service, _, parent = history_query
    fork = service.history.create_branch(fork_metadata(service.history, parent.head_ref()))
    sibling = service.history.create_branch(fork_metadata(service.history, parent.head_ref()))
    names = {
        parent.branch_id: "parent-branch",
        fork.branch_id: "my-fork",
        sibling.branch_id: "sibling",
    }
    monkeypatch.setattr(service.paper.store, "branch_names", lambda project: names)
    from types import SimpleNamespace

    monkeypatch.setattr(
        service.paper.store,
        "graph_branches",
        lambda project: [
            SimpleNamespace(
                branch_id=branch.branch_id,
                name=names[branch.branch_id],
                owner_episode_id=branch.branch_metadata().episode_id,
                parent_head=branch.branch_metadata().effective_parent_head,
            )
            for branch in (parent, fork, sibling)
        ],
    )
    scope = GraphReadScope.resolve("project", fork, store=service.paper.store)
    assert {item.id for item in scope.branches} == {parent.branch_id, fork.branch_id}
    assert (
        next(item for item in scope.branches if item.id == fork.branch_id).parent == "parent-branch"
    )
    denied = handle_graph_history_command(
        service, scope, request("graph_log", id="hyp/main", at="branch:sibling@head")
    )
    assert denied.result["code"] == "graph_history_scope_refused"
