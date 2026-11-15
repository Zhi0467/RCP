from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import pytest

from rcp.core.models import AuthorizedHuman, GraphBranchMetadata
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.storage import AgentTaskRecord
from rcp.storage.artifact_models import Artifact
from rcp.team_access import TEAM_ACCESS_URL_ENV
from rcp.transfer.records import TRANSFER_EXCLUDED_PROJECT_TABLES
from rcp.transport import StateUnavailable
from tests.helpers import (
    append_fixture_patch,
    create_named_app,
    seed_patch,
    signed_in_client,
)
from tests.test_project_membership import _create_project, _team_app

ACCESS_URL = "https://team.example.test"
NODE_ID = "rq/learning-after-shift"


@pytest.fixture
def team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(TEAM_ACCESS_URL_ENV, ACCESS_URL)
    app, client, store, people, acting = _team_app(tmp_path, members=3)
    project = _create_project(client, tmp_path / "repo")
    store.seat_project_member(project, people[1].user_id)
    service = app.state.catalog.open(project)
    append_fixture_patch(
        service,
        seed_patch().model_copy(
            update={"run_truth_scope": ["paper-repo"], "repositories_read": ["paper-repo"]}
        ),
    )
    return app, client, store, people, acting, project


def _artifact(store, project: str, artifact_id: str = "shared-note") -> Artifact:
    return store.create_artifact(
        Artifact(
            artifact_id=artifact_id,
            project_id=project,
            supplier="turn",
            supplier_id="turn",
            source_name="note.txt",
            media_type="text/plain",
            created_at=store.now(),
        ),
        data=b"A note",
    )


def _chat_turn(
    store, project: str, chat_id: str, operation_id: str, target: GraphTargetRef | None = None
) -> None:
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project,
            kind="project_chat",
            status="succeeded",
            request={"chat_id": chat_id, "message": "Plan the ablation"},
            created_at=now,
            updated_at=now,
            status_message="done",
            graph_target=target or GraphTargetRef(),
        )
    )


def _branch(app, store, project: str) -> str:
    service = app.state.catalog.open(project)
    base = service.history.head_ref()
    branch_id = str(uuid.uuid4())
    target = GraphTargetRef(kind="branch", branch_id=branch_id)
    service.history.create_branch(
        GraphBranchMetadata(
            branch_id=branch_id,
            episode_id=branch_id,
            project_id=project,
            base_head=base,
            head=GraphHeadRef(
                target=target, revision=base.revision, transition_id=base.transition_id
            ),
            authorized_by=AuthorizedHuman(
                space_id=store.space_id, user_id=str(uuid.uuid4()), display_name="Member"
            ),
        )
    )
    store.create_graph_branch(
        project_id=project,
        branch_id=branch_id,
        owner_episode_id=None,
        parent_head=base,
        created_by="member",
    )
    return branch_id


def _copy(client, project: str, kind: str, item_id: str, graph_target: str = "main"):
    return client.post(
        f"/api/projects/{project}/share-links",
        json={"kind": kind, "item_id": item_id, "graph_target": graph_target},
    )


def _token(response) -> str:
    assert response.status_code == 200, response.text
    url = response.json()["url"]
    prefix = f"{ACCESS_URL}/open#l="
    assert url.startswith(prefix)
    return url.removeprefix(prefix)


def _resolve(client, token: str):
    return client.post("/api/share-links/resolve", json={"token": token})


def test_copy_returns_one_stable_url_per_item(team) -> None:
    _, client, store, _, _, project = team
    _artifact(store, project)
    first = _token(_copy(client, project, "artifact", "shared-note"))
    assert _token(_copy(client, project, "artifact", "shared-note")) == first
    with ThreadPoolExecutor(max_workers=2) as pool:
        urls = list(pool.map(lambda _: _copy(client, project, "node", NODE_ID).json(), range(2)))
    assert urls[0] == urls[1]
    assert _token(_copy(client, project, "node", NODE_ID)) != first


def test_resolve_names_each_kind_and_its_generic_link(team) -> None:
    app, client, store, people, acting, project = team
    _artifact(store, project)
    _chat_turn(store, project, "chat-a", "turn-a")
    paper = client.post(
        f"/api/projects/{project}/paper/uploads", data={"path": "draft.md"}, files={"file": b"# A"}
    )
    file_id = paper.json()["file_ids"][0]
    thread = client.post(
        f"/api/projects/{project}/comment-threads",
        json={
            "target": {"kind": "paper", "id": file_id},
            "anchor": {"kind": "whole"},
            "text": "Hi",
        },
    ).json()["thread"]["thread_id"]
    branch = _branch(app, store, project)
    cases = {
        ("artifact", "shared-note", "main"): "note.txt",
        ("paper", file_id, "main"): "draft.md",
        ("comment", thread, "main"): "draft.md",
        ("node", NODE_ID, "main"): "Learning after task shift",
        ("node", NODE_ID, f"branch:{branch}"): "Learning after task shift",
    }
    tokens = {key: _token(_copy(client, project, *key)) for key in cases}
    tokens["chat", "chat-a", "main"] = _token(_copy(client, project, "chat", "chat-a"))
    tokens["reply", "turn-a", "main"] = _token(_copy(client, project, "reply", "turn-a"))
    acting[0] = people[1].user_id
    for (kind, item_id, graph_target), token in tokens.items():
        response = _resolve(client, token)
        assert response.status_code == 200, (kind, response.text)
        body = response.json()
        bare = graph_target.removeprefix("branch:")
        assert (
            body["link"] == f"#/projects/{project}/targets/{bare}/{kind}/{quote(item_id, safe='')}"
        )
        assert body["kind"] == kind
        assert body["project_id"] == project
        assert body["project_name"] == "membership-paper"
        if (kind, item_id, graph_target) in cases:
            assert body["title"] == cases[kind, item_id, graph_target]


def test_every_unreadable_link_answers_the_same_404(team) -> None:
    app, client, store, people, acting, project = team
    _artifact(store, project)
    member_chat = next(
        chat
        for chat in store.ensure_agent_threads(people[0].user_id, [project], default_names=("Pip",))
        if chat.project_id == project
    )
    _chat_turn(store, project, member_chat.thread_id, "private-turn")
    branch = _branch(app, store, project)
    artifact = _token(_copy(client, project, "artifact", "shared-note"))
    private_chat = _token(_copy(client, project, "chat", member_chat.thread_id))
    private_reply = _token(_copy(client, project, "reply", "private-turn"))
    on_branch = _token(_copy(client, project, "node", NODE_ID, f"branch:{branch}"))
    assert _resolve(client, private_chat).status_code == 200

    acting[0] = people[1].user_id
    refused = [_resolve(client, token) for token in (private_chat, private_reply)]
    refused.append(_resolve(client, "no-such-token"))
    acting[0] = people[2].user_id
    refused.append(_resolve(client, artifact))
    acting[0] = people[1].user_id
    store.delete_artifact(project, "shared-note")
    refused.append(_resolve(client, artifact))
    with store.connection() as connection:
        connection.execute(
            "UPDATE share_links SET graph_target='branch:gone' WHERE token=?", (on_branch,)
        )
    refused.append(_resolve(client, on_branch))

    assert {response.status_code for response in refused} == {404}
    assert len({response.content for response in refused}) == 1
    assert _copy(client, project, "chat", member_chat.thread_id).status_code == 404
    assert _copy(client, project, "node", "rq/missing").status_code == 404


def test_a_turn_item_links_to_its_own_graph_target(team) -> None:
    app, client, store, _, _, project = team
    branch = _branch(app, store, project)
    _chat_turn(store, project, "chat-b", "turn-b", GraphTargetRef(kind="branch", branch_id=branch))
    for kind, item_id in (("chat", "chat-b"), ("reply", "turn-b")):
        body = _resolve(client, _token(_copy(client, project, kind, item_id))).json()
        assert body["link"] == f"#/projects/{project}/targets/{branch}/{kind}/{item_id}"


def test_unavailable_state_answers_503_only_to_members(team, monkeypatch) -> None:
    app, client, store, people, acting, project = team
    branch = _branch(app, store, project)
    node = _token(_copy(client, project, "node", NODE_ID))
    _chat_turn(store, project, "chat-b", "turn-b", GraphTargetRef(kind="branch", branch_id=branch))
    reply = _token(_copy(client, project, "reply", "turn-b"))

    def unavailable(*_args, **_kwargs):
        raise StateUnavailable("offline")

    monkeypatch.setattr(app.state.catalog, "open", unavailable)
    assert {_resolve(client, token).status_code for token in (node, reply)} == {503}
    acting[0] = people[2].user_id
    assert _resolve(client, node).status_code == 404


def test_copy_needs_an_access_url(team, monkeypatch: pytest.MonkeyPatch) -> None:
    _, client, _, _, _, project = team
    monkeypatch.delenv(TEAM_ACCESS_URL_ENV)
    assert _copy(client, project, "node", NODE_ID).status_code == 409


def test_personal_space_has_no_share_links(manifest, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(TEAM_ACCESS_URL_ENV, ACCESS_URL)
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project = app.state.default_project_id
    append_fixture_patch(app.state.catalog.open(project), seed_patch())
    assert _copy(client, project, "node", NODE_ID).status_code == 404
    assert _resolve(client, "anything").status_code == 404


def test_project_deletion_and_transfer_drop_links(team) -> None:
    _, client, store, _, _, project = team
    _token(_copy(client, project, "node", NODE_ID))
    assert "share_links" in TRANSFER_EXCLUDED_PROJECT_TABLES
    assert store.delete_project_records(project)["share_links"] == 1


def test_open_page_revalidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>RCP</main>", encoding="utf-8")
    monkeypatch.setattr("rcp.api.app.web_dist_path", lambda: dist)
    app, client, *_ = _team_app(tmp_path)
    page = client.get("/open")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    assert page.text == "<main>RCP</main>"
