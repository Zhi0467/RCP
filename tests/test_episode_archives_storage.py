from __future__ import annotations

import sqlite3
import uuid

import pytest

from rcp.storage import AppStore, EpisodeArchiveState, EpisodeRecord

from .test_auto_research_children_storage import (
    _admission,
    _auto_parent,
    _experiment_route,
    _experiment_task,
    _work_pair,
)
from .test_campaign_storage import _project
from .test_episode_storage import _authorizer, _episode, _operational_task, _start_wrapping
from .test_server_member_removal_storage import _authorized, _claimed_team


def _store(tmp_path) -> tuple[AppStore, str]:
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    actor = _authorizer(store)
    store.seat_project_member("project", actor.user_id)
    return store, actor.user_id


def _end(store: AppStore, episode_id: str, ending: str = "failed") -> None:
    if ending == "stopped":
        store.mark_episode_stop_skipped(episode_id)
    else:
        store.end_episode_without_report(episode_id, ending=ending)


@pytest.mark.parametrize("ending", ["completed", "exhausted", "stopped", "failed", "human_pause"])
def test_archive_is_reversible_persisted_and_does_not_change_episode_history(
    tmp_path, ending
) -> None:
    store, user_id = _store(tmp_path)
    store.create_episode(_episode(store, "episode"))
    store.allocate_episode_invocation(
        "episode", _operational_task(store, "turn", episode_id="episode")
    )
    store.fail_agent_task("turn", "Retained failure diagnostic")
    _end(store, "episode", ending)
    before = (
        store.episode("episode"),
        store.episode_tasks("episode"),
        store.episode_wrapup("episode"),
        store.episode_invocations("episode"),
    )

    assert store.episode_archive_states("project")["episode"] == EpisodeArchiveState(
        archived=False, can_archive=True
    )
    assert store.set_episode_archived("project", "episode", user_id, archived=True).archived
    with store.connection() as connection:
        first_archive = tuple(connection.execute("SELECT * FROM episode_archives").fetchone())
    store.set_episode_archived("project", "episode", user_id, archived=True)
    reopened = AppStore(store.path)
    with reopened.connection() as connection:
        assert (
            tuple(connection.execute("SELECT * FROM episode_archives").fetchone()) == first_archive
        )
    assert reopened.episode_archive_states("project")["episode"].archived
    assert [episode.episode_id for episode in reopened.archived_episodes("project")] == ["episode"]
    assert (
        reopened.episode("episode"),
        reopened.episode_tasks("episode"),
        reopened.episode_wrapup("episode"),
        reopened.episode_invocations("episode"),
    ) == before
    for _ in range(2):
        assert not reopened.set_episode_archived(
            "project", "episode", user_id, archived=False
        ).archived
    assert reopened.archived_episodes("project") == []


def test_archive_is_shared_and_retains_the_archiving_human_after_member_removal(tmp_path) -> None:
    store, alice, _token, _invitation, bob, _bob_token = _claimed_team(tmp_path)
    _project(store)
    for member in (alice, bob):
        store.seat_project_member("project", member.user_id)
    now = store.now()
    store.create_episode(
        EpisodeRecord(
            episode_id="episode",
            project_id="project",
            mode="experiment_loop",
            control_node_id="experiment",
            status="queued",
            invocation_ceiling=1,
            authorized_by=_authorized(store, alice),
            created_at=now,
            updated_at=now,
        )
    )
    _end(store, "episode")
    store.set_episode_archived("project", "episode", bob.user_id, archived=True)
    preview = store.member_removal_preview(bob.user_id)
    store.begin_member_removal(bob.user_id, expected_boundary_sha256=preview.boundary_sha256)
    store.complete_member_removal(bob.user_id)
    assert store.episode_archive_states("project")["episode"].archived
    with store.connection() as connection:
        archive = connection.execute("SELECT * FROM episode_archives").fetchone()
        assert archive["archived_user_id"] == bob.user_id
        assert archive["archived_display_name"] == "Bob"
    with pytest.raises(KeyError):
        store.set_episode_archived("project", "episode", bob.user_id, archived=False)
    assert not store.set_episode_archived(
        "project", "episode", alice.user_id, archived=False
    ).archived


@pytest.mark.parametrize("task_status", ["queued", "running", "pausing", "paused", "failed"])
def test_unended_or_recoverable_episode_cannot_be_archived(tmp_path, task_status) -> None:
    store, user_id = _store(tmp_path)
    store.create_episode(_episode(store, "episode"))
    store.allocate_episode_invocation(
        "episode", _operational_task(store, "turn", episode_id="episode")
    )
    with store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status = ?", (task_status,))
    assert not store.episode_archive_states("project")["episode"].can_archive
    with pytest.raises(ValueError, match="settled work"):
        store.set_episode_archived("project", "episode", user_id, archived=True)
    if task_status != "failed":
        _end(store, "episode")
        assert not store.episode_archive_states("project")["episode"].can_archive
        with pytest.raises(ValueError, match="settled work"):
            store.set_episode_archived("project", "episode", user_id, archived=True)


def test_pending_report_blocks_archive_until_final_failure_and_preserves_report_history(
    tmp_path,
) -> None:
    store, user_id = _store(tmp_path)
    _start_wrapping(store, "episode")
    store.complete_agent_task("episode-operation", applied_revision=None, result={})
    attempt = store.allocate_episode_report_attempt("episode")
    assert not store.episode_archive_states("project")["episode"].can_archive
    with pytest.raises(ValueError, match="settled work"):
        store.set_episode_archived("project", "episode", user_id, archived=True)
    store.finish_episode_report_error(attempt.attempt_id, "No report")
    before = (
        store.episode("episode"),
        store.episode_wrapup("episode"),
        store.episode_report_attempts("episode"),
        store.episode_tasks("episode", include_hidden=True),
    )
    store.set_episode_archived("project", "episode", user_id, archived=True)
    assert (
        store.episode("episode"),
        store.episode_wrapup("episode"),
        store.episode_report_attempts("episode"),
        store.episode_tasks("episode", include_hidden=True),
    ) == before


def test_archive_mutation_checks_project_and_member_under_the_write_lock(tmp_path) -> None:
    store, user_id = _store(tmp_path)
    _project(store, "other")
    store.create_episode(_episode(store, "episode"))
    _end(store, "episode")
    for project_id, episode_id, actor in (
        ("other", "episode", user_id),
        ("missing", "episode", user_id),
        ("project", "missing", user_id),
        ("project", "episode", "missing"),
    ):
        with pytest.raises(KeyError):
            store.set_episode_archived(project_id, episode_id, actor, archived=True)
    store.seat_project_member("other", user_id)
    with pytest.raises(KeyError):
        store.set_episode_archived("other", "episode", user_id, archived=True)
    assert not store.episode_archive_states("project")["episode"].archived


def test_archived_auto_research_does_not_hide_a_fresh_reauthorization(tmp_path) -> None:
    store, user_id = _store(tmp_path)
    store.create_episode(_episode(store, "old", mode="auto_research"))
    _end(store, "old", "exhausted")
    store.set_episode_archived("project", "old", user_id, archived=True)
    store.create_episode(_episode(store, "new", mode="auto_research"))
    states = store.episode_archive_states("project")
    assert states["old"].archived
    assert states["new"] == EpisodeArchiveState(archived=False, can_archive=False)
    snapshots = store.auto_research_space_run_projection_snapshots(
        {"project"}, completed_since=store.now()
    )
    assert snapshots
    assert all(snapshot.episode.authorized_by == _authorizer(store) for snapshot in snapshots)


def test_ended_parent_cannot_hide_running_child_and_archiving_does_not_cascade(tmp_path) -> None:
    store, user_id = _store(tmp_path)
    parent, root = _auto_parent(store)
    child_id = str(uuid.uuid4())
    child_task = _experiment_task(store, child_id, parent.authorized_by, node_id="experiment")
    route = _experiment_route(store, parent, root, child_task)
    store.create_experiment_episode_with_invocation(child_task, auto_research_route=route)
    store.fail_agent_task(root.operation_id, "Parent failed")
    _end(store, parent.episode_id)
    assert not store.episode_archive_states("project")[parent.episode_id].can_archive
    with pytest.raises(ValueError, match="settled work"):
        store.set_episode_archived("project", parent.episode_id, user_id, archived=True)
    store.fail_agent_task(child_task.operation_id, "Child failed")
    _end(store, child_id)
    store.set_episode_archived("project", parent.episode_id, user_id, archived=True)
    states = store.episode_archive_states("project")
    assert states[parent.episode_id].archived
    assert states[child_id] == EpisodeArchiveState(archived=False, can_archive=True)


def test_ended_parent_cannot_hide_running_child_work_or_unsettled_admission(tmp_path) -> None:
    store, user_id = _store(tmp_path)
    parent, root = _auto_parent(store)
    route, task = _work_pair(store, parent, root, worker_id="worker")
    store.create_auto_research_child_work(route, task)
    admission = _admission(
        store, parent, admission_id="pending", child_kind="work", child_id="other-worker"
    )
    store.record_auto_research_child_admission(admission)
    store.fail_agent_task(root.operation_id, "Parent failed")
    _end(store, parent.episode_id)
    store.cancel_auto_research_child_admission("pending")
    assert not store.episode_archive_states("project")[parent.episode_id].can_archive
    store.fail_agent_task(task.operation_id, "Child failed")
    assert store.episode_archive_states("project")[parent.episode_id].can_archive
    with store.connection() as connection:
        connection.execute(
            "UPDATE auto_research_child_admissions SET state = 'accepted' "
            "WHERE admission_id = 'pending'"
        )
    assert not store.episode_archive_states("project")[parent.episode_id].can_archive
    with pytest.raises(ValueError, match="settled work"):
        store.set_episode_archived("project", parent.episode_id, user_id, archived=True)


def test_archived_history_outlives_recent_window_and_never_hides_newly_unsettled_work(tmp_path):
    store, user_id = _store(tmp_path)
    store.create_episode(_episode(store, "old"))
    _end(store, "old")
    store.set_episode_archived("project", "old", user_id, archived=True)
    for number in range(51):
        episode_id = f"new-{number}"
        store.create_episode(_episode(store, episode_id))
        _end(store, episode_id)
    assert "old" not in {episode.episode_id for episode in store.episodes("project")}
    assert [episode.episode_id for episode in store.archived_episodes("project")] == ["old"]
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status = 'running', ending = NULL, ended_at = NULL "
            "WHERE episode_id = 'old'"
        )
    assert store.episode_archive_states("project")["old"] == EpisodeArchiveState(
        archived=False, can_archive=False
    )
    assert store.archived_episodes("project") == []
    assert not store.set_episode_archived("project", "old", user_id, archived=False).archived


def test_archive_migration_defaults_legacy_episodes_visible_and_project_deletion_cleans_up(
    tmp_path,
):
    store, user_id = _store(tmp_path)
    store.create_episode(_episode(store, "episode"))
    _end(store, "episode")
    before = store.episode("episode")
    with store.connection() as connection:
        connection.execute("DROP TABLE episode_archives")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version = 13")
    snapshot = AppStore.open_read_only(store.path)
    assert snapshot.check_storage_schema_migrations()[2] == ("episode_archives_v1",)
    migrated = AppStore(store.path)
    assert migrated.episode("episode") == before
    assert migrated.episode_archive_states("project")["episode"] == EpisodeArchiveState(
        archived=False, can_archive=True
    )
    migrated.set_episode_archived("project", "episode", user_id, archived=True)
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir(mode=0o700)
    backup_path = backup_dir / "snapshot.sqlite3"
    migrated.online_snapshot(backup_path)
    backup = AppStore.open_read_only_snapshot(backup_path)
    assert backup.episode_archive_states("project")["episode"].archived
    assert migrated.delete_project_records("project")["episode_archives"] == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM episode_archives").fetchone() == (0,)
