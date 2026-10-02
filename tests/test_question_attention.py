from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

import pytest

from rcp.api.episodes import serialize_episode, space_auto_research_episode_projection
from rcp.episode_health import load_episode_health
from rcp.notifications import NotificationSender
from rcp.server_ops.maintenance import RuntimeAdmissionGate
from rcp.storage import AppStore
from rcp.storage.question_models import QuestionOrigin

from .test_episode_api_serialization import _auto_episode, _branch_summary, _project


def _question(store, episode, root, *, key="first", chat=False):
    return store.create_or_get_question(
        origin=QuestionOrigin(
            owner_kind="chat" if chat else "episode",
            project_id=episode.project_id,
            owner_id="chat / one" if chat else episode.episode_id,
            operation_id=root.operation_id,
            provider="codex",
            native_session_id=root.native_session_id,
            stage_root=root.stage_root,
            capability="orchestrator",
            write_scope_fingerprint="scope",
            graph_target=episode.graph_target,
        ),
        key=key,
        question="Which measurement should lead?",
    )


def _setup(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    store.seat_project_member("project", store.local_owner.user_id)
    device = store.register_notification_device(store.local_owner.user_id)
    episode, root = _auto_episode(store, "questions")
    episode = store.episode(episode.episode_id)
    catalog = SimpleNamespace(open=lambda _: (_ for _ in ()).throw(OSError("offline")))
    sender = NotificationSender(store, catalog, admission=RuntimeAdmissionGate())
    sender.observe_episodes()
    return store, episode, root, sender, device


def test_question_health_is_shared_projection_without_mutating_episode(tmp_path):
    store, episode, root, _, _ = _setup(tmp_path)
    _question(store, episode, root)
    before = store.episode(episode.episode_id)
    assert before.status == "running"
    assert load_episode_health(store, [before])[episode.episode_id] == (
        "needs_action",
        "review",
        None,
        "question",
    )
    full = serialize_episode(store, "project", before, branch_summary=_branch_summary)
    assert (full.health, full.blocked_reason) == ("needs_action", "question")
    snapshot = store.auto_research_space_run_projection_snapshots(
        {"project"}, completed_since=store.now()
    )[0]
    assert space_auto_research_episode_projection(snapshot)[:2] == ("needs_action", "actionable")
    assert store.episode(episode.episode_id) == before
    store.set_episode_questions_withdrawn("project", episode.episode_id, withdrawn=True)
    assert load_episode_health(store, [before])[episode.episode_id][0] == "active"


@pytest.mark.parametrize("ending", ["completed", "failed", "stopped", "exhausted", "human_pause"])
def test_question_overlay_never_overrides_episode_ending(tmp_path, ending):
    store, episode, root, _, _ = _setup(tmp_path)
    _question(store, episode, root)
    if ending == "stopped":
        store.request_episode_stop(episode.episode_id)
        ended = store.mark_episode_stop_skipped(episode.episode_id)
    else:
        ended = store.end_episode_without_report(episode.episode_id, ending=ending)
    # Even a stale question snapshot cannot win over the persisted ending.
    store.set_episode_questions_withdrawn("project", episode.episode_id, withdrawn=False)
    projected = load_episode_health(store, [ended])[episode.episode_id]
    assert projected[0] == ("needs_action" if ending in {"exhausted", "human_pause"} else ending)
    assert projected[3] != "question"


@pytest.mark.parametrize("chat", [False, True])
def test_each_question_pushes_once_with_owner_link_and_no_health_duplicate(tmp_path, chat):
    store, episode, root, sender, device = _setup(tmp_path)
    first = _question(store, episode, root, chat=chat)
    sender.observe_episodes()
    second = _question(store, episode, root, key="second", chat=chat)
    sender.observe_episodes()
    assert {row["item_id"] for row in store.notification_outbox()} == {
        first.question_id,
        second.question_id,
    }
    assert all(row["kind"] == "episode_needs_action" for row in store.notification_outbox())
    expected_link = (
        "#/projects/project?view=chats&chat=chat%20%2F%20one"
        if chat
        else f"#/projects/project/targets/{quote(episode.graph_target.key, safe='')}/episode/{episode.episode_id}"
    )
    assert {row["deep_link"] for row in store.notification_outbox()} == {expected_link}
    from rcp.api.notifications import DesktopNotification

    delivered = sender.pending_desktop(device["device_id"])
    assert len(delivered) == 2
    assert all(
        DesktopNotification.model_validate(item).reason == "episode_needs_action"
        for item in delivered
    )
    # A durable creation event survives receipt expiry and restarting the sender.
    for row in store.notification_outbox():
        store.drop_notification(device["device_id"], row["notification_id"])
    restarted = NotificationSender(store, sender.catalog, admission=RuntimeAdmissionGate())
    restarted.observe_episodes()
    assert store.notification_outbox() == []
    store.dismiss_question(first.question_id, resolved_by=episode.authorized_by)
    store.set_episode_questions_withdrawn("project", episode.episode_id, withdrawn=True)
    restarted.observe_episodes()
    store.set_episode_questions_withdrawn("project", episode.episode_id, withdrawn=False)
    restarted.observe_episodes()
    assert store.notification_outbox() == []


def test_question_delivery_checks_resolution_and_needs_you_preference(tmp_path):
    store, episode, root, sender, device = _setup(tmp_path)
    first = _question(store, episode, root, chat=True)
    sender.observe_episodes()
    store.dismiss_question(first.question_id, resolved_by=episode.authorized_by)
    assert sender.pending_desktop(device["device_id"]) == []
    store.set_notification_preferences(
        "project", store.local_owner.user_id, {"episode_needs_action": False}
    )
    _question(store, episode, root, key="disabled")
    sender.observe_episodes()
    store.set_notification_preferences(
        "project", store.local_owner.user_id, {"episode_needs_action": True}
    )
    sender.observe_episodes()
    assert store.notification_outbox() == []


def test_human_experiment_questions_have_the_same_attention_overlay(tmp_path):
    from .test_episode_storage import _start_modern_experiment_episode

    store = AppStore(tmp_path / "experiment.sqlite3")
    _project(store)
    episode_id = str(uuid4())
    _start_modern_experiment_episode(store, episode_id, "turn", complete_task=True)
    store.checkpoint_agent_task("turn", native_session_id="session", stage_root="/stage")
    episode = store.episode(episode_id)
    root = store.agent_task("turn")
    _question(store, episode, root)
    assert episode.status == "running"
    assert load_episode_health(store, [episode])[episode.episode_id] == (
        "needs_action",
        "review",
        None,
        "question",
    )
    assert store.episode(episode_id).status == "running"
