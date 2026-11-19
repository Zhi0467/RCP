from __future__ import annotations

import gzip
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from rcp.storage import AppStore, AutoResearchMessageRecord
from tests.test_auto_research_children_storage import _auto_parent
from tests.test_chat_question_followup import _answered

CHILD = "5f0c3d55-6f3c-4a59-9a43-0e9b5c1e7a10"


def _mailbox(tmp_path, ceiling=2):
    store = AppStore(tmp_path / "store.sqlite3")
    _answered(store)
    episode, root = _auto_parent(store, ceiling=ceiling)
    return store, episode, root


def _mail(store, episode, sender, message_id="mail", **updates):
    return store.record_episode_message(
        AutoResearchMessageRecord(
            message_id=message_id,
            episode_id=episode.episode_id,
            sender_role="orchestrator",
            sender_task_id=sender.operation_id,
            recipient_kind="chat",
            recipient_chat_id="chat",
            body="Result",
            created_at=store.now(),
            **updates,
        )
    )


def test_atomic_wake_replays_and_recovers_claim_before_launch(tmp_path):
    store, episode, root = _mailbox(tmp_path)
    _mail(store, episode, root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = list(
            pool.map(
                lambda _: store.admit_chat_wake("project", "chat", "episode_mail", ["mail"]),
                range(2),
            )
        )
    assert tasks[0].operation_id == tasks[1].operation_id
    restarted = AppStore(store.path)
    assert [task.operation_id for task in restarted.pending_chat_wakes()] == [tasks[0].operation_id]
    claimed = restarted.claim_episode_mail("chat", "chat", [tasks[0].operation_id])
    assert [item.message_id for item in claimed] == ["mail"]
    assert restarted.pending_episode_mail("chat", "chat") == []


def test_busy_chat_leaves_mail_for_turn_staging(tmp_path):
    store, episode, root = _mailbox(tmp_path)
    _mail(store, episode, root)
    first = store.admit_chat_wake("project", "chat", "episode_mail", ["mail"])
    _mail(store, episode, root, "second")
    with pytest.raises(ValueError, match="chat_wake_busy"):
        store.admit_chat_wake("project", "chat", "episode_mail", ["second"])
    assert [item.message_id for item in store.pending_episode_mail("chat", "chat")] == ["second"]
    assert first is not None


def test_wake_cap_and_cooldown_are_per_child_chain(tmp_path, monkeypatch):
    store, episode, root = _mailbox(tmp_path, ceiling=1)
    _mail(store, episode, root)
    first = store.admit_chat_wake("project", "chat", "episode_mail", ["mail"])
    store.mark_agent_task_running(first.operation_id)
    store.complete_agent_task(first.operation_id, applied_revision=None, result={})
    _mail(store, episode, root, "second")
    with pytest.raises(ValueError, match="chat_wake_cooldown"):
        store.admit_chat_wake("project", "chat", "episode_mail", ["second"])
    later = (datetime.fromisoformat(store.now()) + timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(store, "now", lambda: later)
    with pytest.raises(ValueError, match="chat_wake_chain_limit"):
        store.admit_chat_wake("project", "chat", "episode_mail", ["second"])
    assert store.auto_research_message("second").delivered_at is None


def test_wake_caused_item_never_wakes_and_resolved_question_does_not_wake(tmp_path):
    store, episode, root = _mailbox(tmp_path)
    _mail(store, episode, root, item_kind="question", source_ref="resolved")
    assert store.admit_chat_wake("project", "chat", "episode_mail", ["mail"]) is None
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json,'$.chat_wake',json(?)) WHERE operation_id=?",
            ('{"kind":"episode_mail"}', root.operation_id),
        )
    _mail(store, episode, root, "second")
    with pytest.raises(ValueError, match="chat_wake_self_caused"):
        store.admit_chat_wake("project", "chat", "episode_mail", ["second"])
    assert store.auto_research_message("second").delivered_at is None


def test_frozen_database_keeps_old_mail_as_actor_messages(tmp_path):
    source = (
        Path(__file__).parent
        / "fixtures/server_upgrade/pre-plan-cards-v50-1358a21/data/rcp.sqlite3.gz"
    )
    database = tmp_path / "rcp.sqlite3"
    database.write_bytes(gzip.decompress(source.read_bytes()))
    with sqlite3.connect(database) as connection:
        episode_id = connection.execute(
            "SELECT episode_id FROM episodes WHERE mode='auto_research' LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO auto_research_messages(message_id,episode_id,sender_role,"
            "sender_task_id,recipient_task_id,body,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                "old",
                episode_id,
                "orchestrator",
                "sender",
                "recipient",
                "old",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='auto_research_messages'"
            )
        }
    upgraded = AppStore(database)
    old = upgraded.auto_research_message("old")
    assert (old.recipient_kind, old.recipient_task_id, old.item_kind) == (
        "actor",
        "recipient",
        "message",
    )
    upgraded.record_episode_message(
        AutoResearchMessageRecord(
            message_id="new",
            episode_id=episode_id,
            sender_role="orchestrator",
            sender_task_id="sender",
            recipient_kind="chat",
            recipient_chat_id="chat",
            body="New",
            created_at=upgraded.now(),
        )
    )
    assert [item.message_id for item in upgraded.pending_episode_mail("chat", "chat")] == ["new"]
    with upgraded.connection() as connection:
        assert triggers <= {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='auto_research_messages'"
            )
        }
    upgraded.close()


def test_auto_research_children_report_only_to_their_orchestrator(tmp_path):
    from tests.test_auto_research_children_storage import _experiment_route, _experiment_task

    store, episode, root = _mailbox(tmp_path)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json,"
            "'$.started_from_operation_id','origin') WHERE operation_id=?",
            (root.operation_id,),
        )
    child_task = _experiment_task(store, CHILD, episode.authorized_by, node_id="exp/child")
    child_task = child_task.model_copy(
        update={"request": {**child_task.request, "started_from_operation_id": "origin"}}
    )
    store.create_experiment_episode_with_invocation(
        child_task, auto_research_route=_experiment_route(store, episode, root, child_task)
    )
    store.end_episode_without_report(CHILD, ending="failed")
    assert store.pending_episode_mail("chat", "chat") == []

    store.end_episode_without_report(episode.episode_id, ending="failed")
    [ending] = store.pending_episode_mail("chat", "chat")
    assert (ending.episode_id, ending.item_kind, ending.sender_role) == (
        episode.episode_id,
        "ending",
        "orchestrator",
    )


def test_experiment_mail_reaches_the_continuation_and_stops_at_its_fence(tmp_path):
    from rcp.storage import EpisodeRecord
    from tests.test_auto_research_children_storage import _identity

    store = AppStore(tmp_path / "store.sqlite3")
    _answered(store)
    human = _identity(store)

    def episode(episode_id, continues=None, node="node-1"):
        now = store.now()
        return store.create_episode(
            EpisodeRecord(
                episode_id=episode_id,
                project_id="project",
                mode="experiment_loop",
                control_node_id=node,
                status="queued",
                invocation_ceiling=2,
                authorized_by=human,
                created_at=now,
                updated_at=now,
                continues_episode_id=continues,
                continuation_request_id="more" if continues else None,
            )
        )

    first = episode("first")
    store.record_episode_message(
        AutoResearchMessageRecord(
            message_id="late",
            episode_id=first.episode_id,
            sender_role="human",
            authorized_by=human,
            recipient_kind="episode",
            body="Use the smaller batch.",
            created_at=store.now(),
        )
    )
    store.end_episode_without_report("first", ending="exhausted")
    second = episode("second", continues="first")
    store.request_episode_stop(second.episode_id)
    assert store.claim_episode_mail("episode", "second", ["fenced"]) == []
    third = episode("third", node="node-2")
    assert store.claim_episode_mail("episode", "third", ["other"]) == []
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET stop_requested_at=NULL,status='running' WHERE episode_id='second'"
        )
    [claimed] = store.claim_episode_mail("episode", "second", ["invocation"])
    assert (claimed.message_id, claimed.delivery_operation_id) == ("late", "invocation")
    assert third.episode_id == "third"
