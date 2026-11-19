"""A Bulletin mention wakes an idle Work chat or orchestrator once."""

from __future__ import annotations

from datetime import datetime, timedelta

from rcp.runs.episode_mailbox import wake_bulletin_mentions
from tests.test_auto_research_children_storage import _identity
from tests.test_episode_mail_storage import _mailbox
from tests.test_episode_mailbox_delivery import _Background


def _post(store, text, key):
    origin = store.agent_task("origin")
    if not store.is_project_member("project", _identity(store).user_id):
        store.seat_project_member("project", _identity(store).user_id)
    return store.mutate_bulletin_post(
        "project", origin.graph_target.key, user_id=_identity(store).user_id, key=key, text=text
    )["post_id"]


def _later(store, monkeypatch, seconds):
    later = (datetime.fromisoformat(store.now()) + timedelta(seconds=seconds)).isoformat()
    monkeypatch.setattr(store, "now", lambda: later)


def test_a_mention_wakes_the_idle_chat_once_and_respects_the_actor_rate(tmp_path, monkeypatch):
    store, _episode, _root = _mailbox(tmp_path)
    committed = []
    store.on_wake_input = lambda *args: committed.append(args)
    first = _post(store, "<@agent:chat:chat> the data moved", "first")
    assert committed == [("project", "bulletin_mention", first)]
    background = _Background(store)
    assert wake_bulletin_mentions(background, "project", first) == {"chat:chat": "running"}
    [wake] = background.launched
    assert store.agent_task(wake).request["chat_wake"] == {
        "kind": "bulletin_mention",
        "item_ids": [first],
        "chains": [],
    }
    assert wake_bulletin_mentions(background, "project", first) == {
        "chat:chat": "bulletin_wake_ineligible"
    }
    store.complete_agent_task(wake, applied_revision=None, result={})
    _later(store, monkeypatch, 60)
    assert wake_bulletin_mentions(background, "project", first) == {
        "chat:chat": "bulletin_wake_ineligible"
    }
    second = _post(store, "<@agent:chat:chat> and the config too", "second")
    assert wake_bulletin_mentions(background, "project", second) == {
        "chat:chat": "bulletin_wake_cooldown"
    }
    assert "bulletin:project:chat:chat" in background.deferred
    _later(store, monkeypatch, 400)
    assert wake_bulletin_mentions(background, "project", second) == {"chat:chat": "running"}
    assert store.agent_task(background.launched[-1]).request["chat_wake"]["item_ids"] == [second]


def test_a_post_from_a_woken_turn_wakes_nobody(tmp_path):
    store, _episode, _root = _mailbox(tmp_path)
    post = _post(store, "<@agent:chat:chat> see this", "woken")
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json,'$.wake_cause','message') "
            "WHERE operation_id='origin'"
        )
        connection.execute(
            "UPDATE bulletin_posts SET author_kind='agent',operation_id='origin',user_id=NULL "
            "WHERE post_id=?",
            (post,),
        )
    background = _Background(store)
    assert wake_bulletin_mentions(background, "project", post) == {
        "chat:chat": "bulletin_wake_ineligible"
    }
    assert background.launched == []


def test_a_mention_of_a_running_orchestrator_becomes_one_lifecycle_notice(tmp_path, monkeypatch):
    store, episode, _root = _mailbox(tmp_path)
    woken = []
    monkeypatch.setattr(
        "rcp.runs.auto_research_delivery.reconcile_pending_auto_research_lifecycle",
        lambda _background, *, episode_id: woken.append(episode_id),
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status='running' WHERE episode_id=?", (episode.episode_id,)
        )
    actor = f"episode:{episode.episode_id}"
    post = _post(store, f"<@agent:{actor}> the shared data moved", "orchestrator")
    background = _Background(store)
    assert wake_bulletin_mentions(background, "project", post)[actor] == "bulletin_wake_notice"
    assert woken == [episode.episode_id]
    with store.connection() as connection:
        notices = connection.execute(
            "SELECT source_kind,source_id FROM auto_research_lifecycle_notices "
            "WHERE episode_id=? AND source_kind='bulletin_mention'",
            (episode.episode_id,),
        ).fetchall()
    assert [tuple(row) for row in notices] == [("bulletin_mention", post)]
    assert wake_bulletin_mentions(background, "project", post)[actor] in {
        "bulletin_wake_ineligible",
        "bulletin_wake_cooldown",
    }
