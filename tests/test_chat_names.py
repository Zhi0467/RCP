from __future__ import annotations

import gzip
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from rcp.limits import AGENT_NAME_MAX_CHARS
from rcp.storage import AgentTaskRecord, AppStore
from rcp.storage.chat_names import ChatNameError
from tests.test_auto_research_children_storage import _identity, _project


def _turn(store, chat_id, message, at):
    return store.create_agent_task(
        AgentTaskRecord(
            operation_id=str(uuid.uuid4()),
            project_id="project",
            kind="project_chat",
            status="succeeded",
            request={"chat_id": chat_id, "message": message},
            created_at=at,
            updated_at=at,
            status_message="",
            authorized_by=_identity(store),
        )
    )


def test_conversation_name_is_created_once_and_human_rename_replaces_it(tmp_path):
    store = AppStore(tmp_path / "store.sqlite")
    _project(store)
    chat = str(uuid.uuid4())
    _turn(store, chat, " First\n task  message ", "2026-01-01T00:00:00+00:00")
    _turn(store, chat, "Later message", "2026-01-02T00:00:00+00:00")
    assert store.chat_inventory("project")[0]["title"] == "First task message"
    store.set_chat_title("project", chat, "human", " Human   name ")
    store.set_chat_archived("project", chat, "human", archived=True)
    store.set_chat_archived("project", chat, "human", archived=False)
    assert store.chat_inventory("project")[0]["title"] == "Human name"
    assert store.chat_display("project", "human")["titles"] == {chat: "Human name"}
    for name in (None, " "):
        with pytest.raises(ChatNameError, match="chat_name_empty"):
            store.set_chat_title("project", chat, "human", name)
    headed = str(uuid.uuid4())
    _turn(
        store, headed, "Context line\n# Goal: probe tranche ##\nbody", "2026-01-03T00:00:00+00:00"
    )
    assert store.agent_name_state("project", headed)["name"] == "Goal: probe tranche"


def test_self_name_is_once_and_cannot_replace_human_name(tmp_path):
    store = AppStore(tmp_path / "store.sqlite")
    _project(store)
    for human in (False, True):
        chat = str(uuid.uuid4())
        task = _turn(store, chat, "Starting name", store.now())
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status='running' WHERE operation_id=?", (task.operation_id,)
            )
        assert store.agent_name_state("project", chat)["can_self_name"]
        if human:
            store.set_chat_title("project", chat, "human", "Chosen name")
        else:
            for name, code in (
                (" ", "chat_name_empty"),
                ("x" * (AGENT_NAME_MAX_CHARS + 1), "chat_name_too_long"),
            ):
                with pytest.raises(ChatNameError, match=code):
                    store.self_name_chat_for_task(task.operation_id, name)
            assert store.self_name_chat_for_task(task.operation_id, " Task  focused name ") == {
                "name": "Task focused name",
                "can_self_name": False,
            }
        with pytest.raises(ChatNameError, match="chat_name_not_available"):
            store.self_name_chat_for_task(task.operation_id, "Replacement")


def test_agent_name_cap_cuts_starting_names_and_refuses_longer_names(tmp_path):
    store = AppStore(tmp_path / "store.sqlite")
    _project(store)
    chat = str(uuid.uuid4())
    # The cut lands just after a word, so the trailing space is trimmed too.
    words = "w" * (AGENT_NAME_MAX_CHARS - 1) + " tail"
    task = _turn(store, chat, words, store.now())
    assert store.agent_name_state("project", chat)["name"] == "w" * (AGENT_NAME_MAX_CHARS - 1)
    # A cut never ends inside a word.
    mid_word = str(uuid.uuid4())
    _turn(store, mid_word, "abcdefg " * AGENT_NAME_MAX_CHARS, store.now())
    assert set(store.agent_name_state("project", mid_word)["name"].split(" ")) == {"abcdefg"}
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status='running' WHERE operation_id=?", (task.operation_id,)
        )
    longest = "n" * AGENT_NAME_MAX_CHARS
    for name_it in (
        lambda name: store.self_name_chat_for_task(task.operation_id, name),
        lambda name: store.set_chat_title("project", chat, "human", name),
    ):
        with pytest.raises(ChatNameError, match="chat_name_too_long"):
            name_it(longest + "n")
        name_it(longest)
        assert store.agent_name_state("project", chat)["name"] == longest


def test_old_database_backfills_first_turn_and_preserves_human_name(tmp_path):
    # A released old writer produced this database, before conversation names.
    fixture = Path(__file__).parent / "fixtures/server_upgrade/pre-graph-branches-v29-daad53e"
    metadata = json.loads((fixture / "fixture.json").read_text())
    path = tmp_path / "store.sqlite"
    path.write_bytes(gzip.decompress((fixture / "data/rcp.sqlite3.gz").read_bytes()))
    project_id = metadata["project_id"]
    first, renamed, long_human = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    long_name = "h" * (AGENT_NAME_MAX_CHARS - 1) + " " + "h" * 60
    with sqlite3.connect(path) as connection:
        assert "self_named" not in {
            row[1] for row in connection.execute("PRAGMA table_info(chat_display)")
        }
        for chat in (first, renamed, long_human):
            for message, at in (
                (" Original  task " + "o" * 120, "2026-01-01T00:00:00+00:00"),
                ("Newer turn", "2026-01-02T00:00:00+00:00"),
            ):
                connection.execute(
                    "INSERT INTO graph_runs (operation_id, project_id, kind, status, "
                    "request_json, created_at, updated_at, status_message) "
                    "VALUES (?, ?, 'project_chat', 'succeeded', ?, ?, ?, '')",
                    (
                        str(uuid.uuid4()),
                        project_id,
                        json.dumps({"chat_id": chat, "message": message}),
                        at,
                        at,
                    ),
                )
        connection.execute(
            "INSERT INTO chat_display (project_id, chat_id, title, titled_user_id) "
            "VALUES (?, ?, 'Human choice', ?)",
            (project_id, renamed, metadata["user_id"]),
        )
        # Names stored under the earlier, longer caps, human renames included.
        connection.execute(
            "INSERT INTO chat_display (project_id, chat_id, title, titled_user_id) "
            "VALUES (?, ?, ?, ?)",
            (project_id, long_human, long_name, metadata["user_id"]),
        )
    store = AppStore(path)
    titles = store.chat_display(project_id, metadata["user_id"])["titles"]
    assert titles[first] == "Original task"
    assert titles[renamed] == "Human choice"
    assert titles[long_human] == "h" * (AGENT_NAME_MAX_CHARS - 1)
    root = metadata["legacy_branch_id"]
    assert titles[root] == metadata["legacy_branch_name"]
    store.close()
    store = AppStore(path)
    assert store.agent_name_state(project_id, renamed) == {
        "name": "Human choice",
        "can_self_name": False,
    }
    assert store.agent_name_state(project_id, root)["name"] == titles[root]


@pytest.mark.parametrize("node_id, expected", [("exp/node", "exp/node"), (None, "Chat")])
@pytest.mark.parametrize("message", ["", "  "])
def test_empty_node_message_uses_node_id_then_chat(tmp_path, node_id, expected, message):
    store = AppStore(tmp_path / "store.sqlite")
    _project(store)
    task = AgentTaskRecord(
        operation_id=str(uuid.uuid4()),
        project_id="project",
        kind="node_chat",
        status="succeeded",
        request={"chat_id": "empty", "node_id": node_id, "message": message},
        created_at=store.now(),
        updated_at=store.now(),
        status_message="",
        authorized_by=_identity(store),
    )
    store.create_agent_task(task)
    assert store.agent_name_state("project", "empty")["name"] == expected


@pytest.mark.parametrize(
    "trigger, expected", [("schedule", "Graph consolidation"), ("human", "Probe the floor")]
)
def test_only_the_scheduled_run_is_named_graph_consolidation(tmp_path, trigger, expected):
    store = AppStore(tmp_path / "store.sqlite")
    _project(store)
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=str(uuid.uuid4()),
            project_id="project",
            kind="project_chat",
            status="succeeded",
            request={
                "chat_id": "c",
                "message": "Probe the floor",
                "trigger": trigger,
                "workflow_ids": ["research-graph-audit", "graph-consolidation"],
            },
            created_at=store.now(),
            updated_at=store.now(),
            status_message="",
            authorized_by=_identity(store),
        )
    )
    assert store.agent_name_state("project", "c")["name"] == expected


def test_transcript_only_name_is_persisted_and_rename_is_read(tmp_path):
    store = AppStore(tmp_path / "store.sqlite")
    _project(store)
    assert store.chat_name("project", "transcript", "# Original\nBody") == "Original"
    assert store.chat_name("project", "transcript", "Later message") == "Original"
    store.set_chat_title("project", "transcript", "human", "Human title")
    assert store.chat_name("project", "transcript", "Original") == "Human title"


def test_orchestrator_keeps_its_initial_branch_name(tmp_path):
    from tests.test_auto_research_children_storage import _setup_parent

    store, episode, _ = _setup_parent(tmp_path)
    branch_id = episode.graph_target.branch_id
    name = store.branch_names(episode.project_id)[branch_id]
    assert store.agent_name_state(episode.project_id, episode.episode_id)["name"] == name
    store.rename_branch(episode.project_id, branch_id, "renamed-branch")
    assert store.chat_inventory(episode.project_id)[0]["title"] == name
