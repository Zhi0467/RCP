from __future__ import annotations

from types import SimpleNamespace

import pytest

from rcp.core.models import Patch
from rcp.notifications import NotificationSender
from rcp.server_ops.maintenance import RuntimeAdmissionGate

from .helpers import append_fixture_patch, create_named_app
from .test_episode_api_serialization import _auto_episode


def _setup(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    device = store.register_notification_device(store.local_owner.user_id)
    return app, store, project_id, device


def _blocker_patch(identifier):
    return Patch(
        kind="refresh",
        author="agent",
        summary="fixture",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": identifier,
                        "type": "blocker",
                        "title": "fixture",
                        "description": "fixture",
                        "status": "open",
                    }
                ],
            }
        ],
    )


@pytest.mark.parametrize("nonempty", [False, True])
def test_graph_baseline_restart_reopen_and_no_watchers(manifest, tmp_path, nonempty):
    app, store, project_id, device = _setup(manifest, tmp_path)
    service = app.state.catalog.open(project_id)
    if nonempty:
        append_fixture_patch(service, _blocker_patch("blk/baseline"))
    sender = app.state.notification_sender
    sender.run_pass()
    assert store.notification_graph_marker(project_id) is not None
    assert store.notification_outbox() == []
    append_fixture_patch(service, _blocker_patch("blk/new"))
    sender.run_pass()
    assert [row["item_id"] for row in store.notification_outbox()] == ["blk/new"]
    # Canonical work accepted while the owner is down is replayed on restart.
    append_fixture_patch(service, _blocker_patch("blk/down"))
    restarted = NotificationSender(store, app.state.catalog, admission=RuntimeAdmissionGate())
    restarted.run_pass()
    restarted.run_pass()
    assert {row["item_id"] for row in store.notification_outbox()} == {"blk/new", "blk/down"}
    assert store.active_graph_watchers(project_id) == []
    assert len(restarted.pending_desktop(device["device_id"])) == 2


def test_unreachable_graph_does_not_advance_or_enqueue(manifest, tmp_path, monkeypatch):
    app, store, project_id, _ = _setup(manifest, tmp_path)
    sender = app.state.notification_sender
    sender.run_pass()
    marker = store.notification_graph_marker(project_id)
    monkeypatch.setattr(
        app.state.catalog, "open", lambda _: (_ for _ in ()).throw(OSError("offline"))
    )
    sender.run_pass()
    assert store.notification_graph_marker(project_id) == marker
    assert store.notification_outbox() == []


def test_episode_baseline_and_finishes_between_passes_or_while_down(tmp_path):
    from rcp.storage import AppStore

    from .test_episode_api_serialization import _project

    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    store.seat_project_member("project", store.local_owner.user_id)
    store.register_notification_device(store.local_owner.user_id)
    store.set_notification_preferences(
        "project", store.local_owner.user_id, {"episode_finished": True}
    )
    catalog = SimpleNamespace(open=lambda _: (_ for _ in ()).throw(OSError("offline")))
    sender = NotificationSender(store, catalog, admission=RuntimeAdmissionGate())
    old, _ = _auto_episode(store, "old")
    store.end_episode_without_report(old.episode_id, ending="completed")
    sender.run_pass()
    assert store.notification_outbox() == []
    between, _ = _auto_episode(store, "between")
    store.end_episode_without_report(between.episode_id, ending="completed")
    sender.run_pass()
    down, _ = _auto_episode(store, "down")
    sender.run_pass()
    store.end_episode_without_report(down.episode_id, ending="failed")
    restarted = NotificationSender(store, catalog, admission=RuntimeAdmissionGate())
    restarted.run_pass()
    restarted.run_pass()
    assert {row["item_id"] for row in store.notification_outbox()} == {
        between.episode_id,
        down.episode_id,
    }
    assert len(store.notification_outbox()) == 2


def test_proposal_and_reopened_attention_have_distinct_occurrences(manifest, tmp_path):
    app, store, project_id, _ = _setup(manifest, tmp_path)
    sender = app.state.notification_sender
    service = app.state.catalog.open(project_id)
    sender.run_pass()
    proposal_patch = Patch(
        kind="refresh",
        author="agent",
        summary="fixture",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "hyp/proposed",
                        "type": "hypothesis",
                        "title": "fixture",
                        "statement": "fixture",
                    }
                ],
            },
            {
                "op": "create_proposals",
                "proposals": [
                    {
                        "id": "prop/new",
                        "title": "fixture",
                        "card": {"decision_needed": "fixture"},
                        "ops": [
                            {
                                "op": "remove_nodes",
                                "intent": "removal",
                                "node_ids": ["hyp/proposed"],
                            }
                        ],
                    }
                ],
            },
        ],
    )
    append_fixture_patch(service, proposal_patch.model_copy(update={"ops": proposal_patch.ops[:1]}))
    append_fixture_patch(service, proposal_patch.model_copy(update={"ops": proposal_patch.ops[1:]}))
    append_fixture_patch(service, _blocker_patch("blk/reopen"))
    sender.run_pass()
    for status in ("resolved", "open"):
        append_fixture_patch(
            service,
            Patch(
                kind="refresh",
                author="agent",
                summary="fixture",
                run_truth_scope=["repo-a"],
                repositories_read=["repo-a"],
                ops=[
                    {
                        "op": "update_nodes",
                        "nodes": [{"id": "blk/reopen", "changes": {"status": status}}],
                    }
                ],
            ),
        )
        sender.run_pass()
    rows = store.notification_outbox()
    assert len([row for row in rows if row["item_id"] == "prop/new"]) == 1
    reopened = [row for row in rows if row["item_id"] == "blk/reopen"]
    assert len(reopened) == 2
    assert len({row["notification_id"] for row in reopened}) == 2


def test_wrapup_sign_in_changes_observation_without_changing_health(tmp_path, monkeypatch):
    from rcp.api.episodes import serialize_episode
    from rcp.storage import AppStore

    from .test_episode_api_serialization import _begin_report, _branch_summary, _project

    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    store.seat_project_member("project", store.local_owner.user_id)
    store.register_notification_device(store.local_owner.user_id)
    catalog = SimpleNamespace(open=lambda _: (_ for _ in ()).throw(OSError("offline")))
    sender = NotificationSender(store, catalog, admission=RuntimeAdmissionGate())
    episode, root = _auto_episode(store, "wrapup")
    _begin_report(store, episode, root, ending="completed")
    blocked = [False]

    def refusal(*args):
        return "blocked" if blocked[0] else None

    monkeypatch.setattr("rcp.agents.provider_accounts.account_login_refusal", refusal)
    monkeypatch.setattr("rcp.api.episodes.account_login_refusal", refusal)
    observed_at = store.now()
    monkeypatch.setattr(store, "now", lambda: observed_at)
    sender.run_pass()
    assert (
        store.notification_episode_observations("project")[episode.episode_id]["health"]
        == "wrapping_up"
    )
    blocked[0] = True
    sender.run_pass()
    sender.run_pass()
    response = serialize_episode(
        store, "project", store.episode(episode.episode_id), branch_summary=_branch_summary
    )
    assert (response.health, response.blocked_reason) == ("wrapping_up", "sign_in")
    rows = store.notification_outbox()
    assert [(row["item_id"], row["kind"]) for row in rows] == [
        (episode.episode_id, "episode_needs_action")
    ]
    blocked[0] = False
    sender.run_pass()
    blocked[0] = True
    sender.run_pass()
    assert len({row["notification_id"] for row in store.notification_outbox()}) == 2


def test_delivery_drops_changed_observation_even_within_same_kind(tmp_path):
    from rcp.storage import AppStore

    from .test_episode_api_serialization import _project

    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    store.seat_project_member("project", store.local_owner.user_id)
    device = store.register_notification_device(store.local_owner.user_id)
    catalog = SimpleNamespace(open=lambda _: (_ for _ in ()).throw(OSError("offline")))
    sender = NotificationSender(store, catalog, admission=RuntimeAdmissionGate())
    sender.run_pass()
    episode, root = _auto_episode(store, "changed-observation", root_status="failed")
    sender.run_pass()
    before = store.notification_outbox()
    assert len(before) == 1
    assert (before[0]["observed_health"], before[0]["observed_blocked_reason"]) == (
        "needs_action",
        None,
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET failure_kind='provider_auth' WHERE operation_id=?",
            (root.operation_id,),
        )
    # Delivery itself checks current health, before the next reconciliation pass.
    assert sender.pending_desktop(device["device_id"]) == []
    assert store.notification_outbox() == []
    sender.run_pass()
    after = store.notification_outbox()
    assert len(after) == 1
    assert (after[0]["observed_health"], after[0]["observed_blocked_reason"]) == (
        "needs_action",
        "sign_in",
    )
    assert after[0]["notification_id"] != before[0]["notification_id"]
    assert [row["notification_id"] for row in sender.pending_desktop(device["device_id"])] == [
        after[0]["notification_id"]
    ]
