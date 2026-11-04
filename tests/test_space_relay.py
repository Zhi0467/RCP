"""The Space relay: the member's Running list and the dock's plain questions."""

from __future__ import annotations

import uuid
from urllib.parse import quote

from rcp.api import agent_threads as agent_thread_routes
from rcp.core.transition_models import GraphTargetRef
from rcp.storage import AppStore
from rcp.storage.agent_threads import AgentThreadRecord
from tests import test_api_questions as api_fixtures
from tests.helpers import signed_in_client
from tests.test_agent_threads_storage import _insert_watcher
from tests.test_api_questions import _question
from tests.test_episode_storage import _episode
from tests.test_experiment_index import (
    _record_branch_target_child_experiment,
    _seed_indexed_project,
    create_app,
)
from tests.test_storage import _task_record

questions_api = api_fixtures.questions_api


def _project_thread(client) -> dict:
    return next(
        row
        for row in client.get("/api/agent-threads").json()["threads"]
        if row["kind"] == "project"
    )


def _other_member_thread(store: AppStore, project_id: str) -> AgentThreadRecord:
    """A personal space has one member, so another member's thread is written directly."""
    thread_id = str(uuid.uuid4())
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_threads(thread_id,user_id,kind,project_id,created_at) "
            "VALUES (?,'someone-else','project',?,?)",
            (thread_id, project_id, store.now()),
        )
    thread = store.agent_thread(thread_id)
    assert thread is not None
    return thread


def _turn(
    store: AppStore,
    project_id: str,
    thread_id: str,
    operation_id: str,
    status: str,
    *,
    failure_kind: str | None = None,
    error: str | None = None,
) -> str:
    now = store.now()
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status='interrupted', finished_at=? "
            "WHERE json_extract(request_json, '$.chat_id')=? "
            "AND status IN ('queued', 'running', 'pausing', 'paused')",
            (now, thread_id),
        )
    store.create_agent_task(
        _task_record(
            now,
            operation_id=operation_id,
            project_id=project_id,
            kind="project_chat",
            request={"chat_id": thread_id},
        )
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status=?, error=?, failure_kind=?, finished_at=? "
            "WHERE operation_id=?",
            (status, error, failure_kind, now if status == "failed" else None, operation_id),
        )
    return now


def test_running_lists_live_episodes_and_the_members_own_active_thread(
    manifest, tmp_path, monkeypatch
):
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id, current_episode = _seed_indexed_project(app)
    parent, child = _record_branch_target_child_experiment(app, node_id="exp/never-run")
    store = app.state.background_tasks.store
    client = signed_in_client(app)
    assert client.get(f"/api/projects/{project_id}").status_code == 200
    thread = _project_thread(client)
    thread_id = thread["thread_id"]
    other = _other_member_thread(store, project_id)
    _turn(store, project_id, other.thread_id, "other-turn", "running")
    _question(store, project_id, owner_id=other.thread_id, key="other")

    def running() -> dict:
        response = client.get("/api/agent-threads/running")
        assert response.status_code == 200, response.text
        return response.json()

    quiet = running()
    assert {(row["kind"], row["id"]) for row in quiet["rows"]} == {
        ("experiment", current_episode),
        ("auto_research", parent.episode_id),
    }
    assert (quiet["total"], quiet["needs"]) == (
        2,
        sum(row["needs_you"] for row in quiet["rows"]),
    )

    _insert_watcher(store, "thread-watch", project_id=project_id, chat_id=thread_id)
    _insert_watcher(
        store,
        "episode-watch",
        project_id=project_id,
        chat_id="loop-chat",
        episode_id=current_episode,
        status="degraded",
    )
    _insert_watcher(
        store,
        "settled-watch",
        project_id=project_id,
        chat_id="loop-chat",
        episode_id=parent.episode_id,
        status="completed",
    )
    _insert_watcher(
        store,
        "worker-watch",
        project_id=project_id,
        chat_id="loop-chat",
        episode_id=current_episode,
    )
    with store.connection() as connection:
        connection.execute("UPDATE watchers SET worker_id='worker' WHERE watcher_id='worker-watch'")
    watched = running()
    runs = {entry["episode_id"]: entry for entry in client.get("/api/space/runs").json()}
    rows = {row["id"]: row for row in watched["rows"]}
    assert set(rows) == {current_episode, parent.episode_id, thread_id}
    assert child.episode_id not in rows
    for episode_id in (current_episode, parent.episode_id):
        entry = runs[episode_id]
        target = GraphTargetRef.model_validate(entry["graph_target"]).key
        assert rows[episode_id] == {
            "kind": "experiment" if entry["mode"] == "experiment_loop" else "auto_research",
            "id": episode_id,
            "project_id": project_id,
            "project_name": entry["project_name"],
            "title": entry["title"],
            "needs_you": entry["run_section"] == "actionable",
            "label": entry["health_label"],
            "watching": 1 if episode_id == current_episode else 0,
            "at": entry["last_activity_at"],
            "link": f"#/projects/{project_id}/targets/{quote(target, safe='')}"
            f"/episode/{episode_id}",
        }
    assert rows[thread_id] | {"label": ""} == {
        "kind": "thread",
        "id": thread_id,
        "project_id": project_id,
        "project_name": thread["project_name"],
        "title": thread["agent_name"],
        "needs_you": False,
        "label": "",
        "watching": 1,
        "at": None,
        "link": f"#/agent/{thread_id}",
    }
    assert rows[thread_id]["label"]

    labels = {}
    for operation_id, status, failure_kind, error, needs_you in [
        ("working", "running", None, None, False),
        ("waiting", "queued", None, None, False),
        ("held", "paused", None, None, True),
        ("signed-out", "failed", "provider_auth", "You've hit your limit", True),
        ("limited", "failed", None, "You've hit your limit", True),
        ("broken", "failed", None, "boom", True),
    ]:
        at = _turn(
            store,
            project_id,
            thread_id,
            operation_id,
            status,
            failure_kind=failure_kind,
            error=error,
        )
        row = next(row for row in running()["rows"] if row["id"] == thread_id)
        assert (row["needs_you"], row["at"]) == (needs_you, at)
        labels[operation_id] = row["label"]
    assert labels["working"] == labels["waiting"]
    assert len({labels[key] for key in ("working", "held", "signed-out", "limited", "broken")}) == 5
    _question(store, project_id, owner_id=thread_id, key="own")
    asked = running()
    assert next(row for row in asked["rows"] if row["id"] == thread_id)["needs_you"] is True
    assert other.thread_id not in {row["id"] for row in asked["rows"]}

    flags = [row["needs_you"] for row in asked["rows"]]
    assert flags == sorted(flags, reverse=True)
    for group in (True, False):
        times = [row["at"] or "" for row in asked["rows"] if row["needs_you"] is group]
        assert times == sorted(times, reverse=True)
    monkeypatch.setattr(agent_thread_routes, "SPACE_RUNNING_MAX_ROWS", 1)
    cut = running()
    assert (cut["total"], cut["needs"]) == (asked["total"], asked["needs"]) == (3, sum(flags))
    assert cut["rows"] == asked["rows"][:1]

    _turn(store, project_id, thread_id, "done", "succeeded")
    with store.connection() as connection:
        connection.execute("UPDATE watchers SET status='completed'")
        connection.execute("UPDATE questions SET state='dismissed'")
    assert thread_id not in {row["id"] for row in running()["rows"]}


def test_dock_relays_the_members_plain_questions_and_takes_their_answers(
    questions_api, monkeypatch
):
    from rcp.api import questions as question_routes

    _app, client, store, project_id = questions_api
    continued = []
    monkeypatch.setattr(
        question_routes,
        "continue_ended_episode",
        lambda source, **_kwargs: continued.append(source.episode_id),
    )
    thread = _project_thread(client)
    other = _other_member_thread(store, project_id)
    store.create_episode(_episode(store, "ended", project_id=project_id))
    ended = _question(store, project_id, owner_kind="episode", owner_id="ended", key="ended")
    store.end_episode_without_report("ended", ending="exhausted", diagnostic=None)
    store.create_episode(_episode(store, "waiting", project_id=project_id))
    waiting = _question(store, project_id, owner_kind="episode", owner_id="waiting", key="wait")
    shared = _question(store, project_id, owner_id="shared-chat", key="shared")
    own = _question(store, project_id, owner_id=thread["thread_id"], key="own")
    hidden = _question(store, project_id, owner_id=other.thread_id, key="hidden")

    def dock() -> dict[str, dict]:
        response = client.get("/api/agent-threads/cards")
        assert response.status_code == 200, response.text
        return {row["question_id"]: row for row in response.json()["questions"]}

    rows = dock()
    assert set(rows) == {
        ended.question_id,
        waiting.question_id,
        shared.question_id,
        own.question_id,
    }
    assert hidden.question_id not in rows
    assert rows[ended.question_id]["answer_continues"] == "turns"
    assert rows[waiting.question_id]["answer_continues"] is None
    assert rows[own.question_id]["source_label"] == thread["agent_name"]
    assert all(row["project_id"] == project_id and row["project_name"] for row in rows.values())
    assert all(rows[question.question_id]["source_label"] for question in (ended, waiting, own))
    named = client.get("/api/agent-threads/cards", params={"thread_id": thread["thread_id"]})
    assert set(named.json()) == {"proposal_cards", "asked_cards"}

    base = f"/api/projects/{project_id}/questions"
    answered = client.post(
        f"{base}/{ended.question_id}/answer",
        json={"answer": "a", "invocation_ceiling": 2, "request_id": str(uuid.uuid4())},
    )
    assert answered.status_code == 200, answered.text
    assert continued == ["ended"]
    for question in (waiting, own):
        response = client.post(f"{base}/{question.question_id}/answer", json={"answer": "a"})
        assert response.status_code == 200, response.text
    assert set(dock()) == {shared.question_id}
    later = _question(store, project_id, owner_id=thread["thread_id"], key="later")
    assert later.question_id in dock()
    store.set_chat_archived(
        project_id,
        thread["thread_id"],
        store.agent_thread(thread["thread_id"]).user_id,
        archived=True,
    )
    assert set(dock()) == {shared.question_id}
