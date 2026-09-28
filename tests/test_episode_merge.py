from __future__ import annotations

from pathlib import Path

import pytest

from rcp.core.models import EpisodeIsolation, EpisodeWorktreeBinding
from rcp.transport import conversation_worktree

from .test_branch_merge_api import _create_branch_harness
from .test_conversation_worktree_git import git


def _isolated(harness, tmp_path, *, graph=True):
    shared = tmp_path / "code"
    shared.mkdir()
    git(shared, "init", "--initial-branch=main")
    git(shared, "config", "user.name", "Fixture")
    git(shared, "config", "user.email", "fixture@example.invalid")
    (shared / "file").write_text("base")
    git(shared, "add", ".")
    git(shared, "commit", "-m", "base")
    values = conversation_worktree.execute(
        {
            "operation": "plan",
            "shared_path": str(shared),
            "owner_episode_id": harness.episode.episode_id,
            "timeout_seconds": 10,
        }
    )
    conversation_worktree.execute({"operation": "create", "binding": values, "timeout_seconds": 10})
    binding = EpisodeWorktreeBinding(
        repository_alias="repo-a", machine="laptop", execution_host="", **values
    )
    harness.store.create_episode_isolation(
        harness.project_id,
        EpisodeIsolation(
            owner_episode_id=harness.episode.episode_id,
            graph_branch_id=harness.episode.episode_id if graph else None,
            worktree=binding,
        ),
    )
    harness.store.set_episode_isolation_status(
        harness.project_id, harness.episode.episode_id, expected_status="creating", status="ready"
    )
    return shared, binding


def test_merge_lands_code_and_graph_without_provider(manifest, tmp_path, monkeypatch):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _isolated(harness, tmp_path)
    (Path(binding.worktree_path) / "file").write_text("episode")
    monkeypatch.setattr(
        harness.app.state.launcher,
        "stream",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("provider launched")),
    )
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge",
        json={"keep_branch_open": True},
    )
    assert response.status_code == 202, response.text
    assert (shared / "file").read_text() == "episode"
    assert len(harness.branch.merge_receipts()) == 1
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert state.merge_attempt.phase == "done"
    assert state.merge_reservation is None
    assert state.delivered_source_commit == git(Path(binding.worktree_path), "rev-parse", "HEAD")


def test_merge_preview_does_not_commit_leftovers(manifest, tmp_path):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    _, binding = _isolated(harness, tmp_path)
    (Path(binding.worktree_path) / "new").write_text("new")
    response = harness.client.get(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge-preview"
    )
    assert response.status_code == 200, response.text
    assert response.json()["code"]["leftover_files"]
    assert git(Path(binding.worktree_path), "rev-parse", "HEAD") == binding.starting_commit
    assert (
        harness.store.episode_isolation_state(
            harness.project_id, harness.episode.episode_id
        ).merge_attempt
        is None
    )


def test_unmerged_cleanup_requires_confirmation(manifest, tmp_path):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    _, binding = _isolated(harness, tmp_path)
    (Path(binding.worktree_path) / "file").write_text("episode")
    route = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/cleanup"
    response = harness.client.post(route, json={"remove_worktree": True})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "unmerged_cleanup_confirmation_required"
    response = harness.client.post(route, json={"remove_worktree": True, "confirm_discard": True})
    assert response.status_code == 200, response.text
    assert not Path(binding.worktree_path).exists()
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert state.status == "removed"
    assert state.merge_attempt.confirm_discard


def test_squash_records_commit_and_cleans_once(manifest, tmp_path):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _isolated(harness, tmp_path)
    (Path(binding.worktree_path) / "file").write_text("episode")
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge",
        json={"history_mode": "squash"},
    )
    assert response.status_code == 202, response.text
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert state.squash_commit == git(shared, "rev-parse", "HEAD")
    assert state.status == "removed"
    assert state.graph_archived
    assert set(state.merge_attempt.cleanup_completed) == {
        "remove_worktree",
        "delete_code_branch",
        "archive_graph_branch",
    }
    assert not Path(binding.worktree_path).exists()


def test_graph_already_delivered_does_not_block_new_code(manifest, tmp_path):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _isolated(harness, tmp_path)
    route = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge"
    first = harness.client.post(route, json={"keep_branch_open": True})
    assert first.status_code == 202, first.text
    revision = harness.service.history.state().revision
    (Path(binding.worktree_path) / "file").write_text("second")
    second = harness.client.post(route, json={"keep_branch_open": True})
    assert second.status_code == 202, second.text
    assert (shared / "file").read_text() == "second"
    assert harness.service.history.state().revision == revision
    assert len(harness.branch.merge_receipts()) == 1
    assert all(
        task.status == "succeeded"
        for task in harness.store.episode_tasks(harness.episode.episode_id)
        if task.kind == "branch_merge"
    )


def test_cleanup_failure_keeps_delivery_and_retries(manifest, tmp_path, monkeypatch):
    from rcp.runs.episodes import merge

    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _isolated(harness, tmp_path)
    (Path(binding.worktree_path) / "file").write_text("episode")
    original = merge._git

    def fail_removal(store, binding, operation, **values):
        if operation == "remove_episode_worktree":
            raise ValueError("fixture_removal_failure")
        return original(store, binding, operation, **values)

    monkeypatch.setattr(merge, "_git", fail_removal)
    route = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}"
    response = harness.client.post(route + "/merge")
    assert response.status_code == 409
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert state.delivered_source_commit
    assert state.merge_attempt.phase == "cleanup"
    assert state.graph_archived
    assert len(harness.branch.merge_receipts()) == 1
    monkeypatch.setattr(merge, "_git", original)
    response = harness.client.post(
        route + "/cleanup", json={"remove_worktree": True, "delete_code_branch": True}
    )
    assert response.status_code == 200, response.text
    assert not Path(binding.worktree_path).exists()
    assert (shared / "file").read_text() == "episode"
    assert len(harness.branch.merge_receipts()) == 1


def test_uncertain_landing_reconciles_before_new_merge(manifest, tmp_path, monkeypatch):
    from rcp.runs.episodes import merge

    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _isolated(harness, tmp_path)
    (Path(binding.worktree_path) / "file").write_text("episode")
    original = merge._git

    def lose_response(store, binding, operation, **values):
        result = original(store, binding, operation, **values)
        if operation == "land":
            raise ValueError("fixture_uncertain_ssh")
        return result

    monkeypatch.setattr(merge, "_git", lose_response)
    route = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge"
    response = harness.client.post(route, json={"keep_branch_open": True})
    assert response.status_code == 409
    landed = git(shared, "rev-parse", "HEAD")
    assert not harness.branch.merge_receipts()
    monkeypatch.setattr(merge, "_git", original)
    response = harness.client.post(route, json={"keep_branch_open": True})
    assert response.status_code == 202, response.text
    assert git(shared, "rev-parse", "HEAD") == landed
    assert len(harness.branch.merge_receipts()) == 1
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert state.merge_reservation is None


@pytest.mark.parametrize("phase", ["landing", "verified", "graph_committed", "cleanup", "done"])
@pytest.mark.parametrize("history_mode", ["merge", "squash"])
def test_interrupted_phase_reconciles_exact_delivery(
    manifest, tmp_path, monkeypatch, phase, history_mode
):
    from rcp.api.episode_routes import _resolved_branch_merge_request
    from rcp.runs.branch_merge_admission import start_branch_merge
    from rcp.runs.episodes import merge

    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _isolated(harness, tmp_path)
    (Path(binding.worktree_path) / "file").write_text("episode")

    class Crash(BaseException):
        pass

    original = merge._save

    def crash_after_save(store, owner, attempt, **changes):
        result = original(store, owner, attempt, **changes)
        if changes.get("phase") == phase:
            raise Crash()
        return result

    monkeypatch.setattr(merge, "_save", crash_after_save)
    with pytest.raises(Crash):
        merge.merge_episode(
            harness.service,
            harness.store,
            harness.episode,
            merge.MergeEpisodeBody(history_mode=history_mode),
            authorized_by=harness.episode.authorized_by,
            dispatch_graph=lambda operation_id, launch=True: start_branch_merge(
                harness.app.state.background_tasks,
                harness.project_id,
                _resolved_branch_merge_request(harness.service, harness.episode.episode_id),
                authorized_by=harness.episode.authorized_by,
                operation_id=operation_id,
                launch=launch,
            ),
        )
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    attempt_id = state.merge_attempt.attempt_id
    assert state.merge_attempt.phase == phase
    monkeypatch.setattr(merge, "_save", original)
    harness.app.state.background_tasks.recover_at_startup()
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge",
        json={"history_mode": history_mode},
    )
    assert response.status_code == 202, response.text
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert state.merge_reservation is None
    assert state.merge_attempt.phase == "done"
    assert len(harness.branch.merge_receipts()) == 1
    delivered_task = harness.branch.merge_receipts()[0].provenance.merge_task_id
    assert harness.store.agent_task(delivered_task).status == "succeeded"
    assert harness.store.agent_task(attempt_id).status == (
        "interrupted" if phase == "verified" else "succeeded"
    )
    assert (shared / "file").read_text() == "episode"


def test_code_only_owner_merges_without_graph_task(manifest, tmp_path):
    import uuid
    from dataclasses import replace

    from rcp.core.transition_models import GraphTargetRef

    harness = _create_branch_harness(manifest, tmp_path, change="none")
    owner_id = str(uuid.uuid4())
    owner = harness.episode.model_copy(
        update={
            "episode_id": owner_id,
            "mode": "experiment_loop",
            "control_node_id": "blk/merge-ready",
            "graph_target": GraphTargetRef(),
            "graph_base_head": None,
            "graph_isolation": False,
            "code_worktree": True,
            "isolation_owner_episode_id": owner_id,
            "root_operation_id": None,
            "status": "queued",
            "invocations_used": 0,
            "ending": None,
            "wrapup_state": "not_started",
            "report_attempts_used": 0,
            "ended_at": None,
            "stop_requested_at": None,
            "stop_settled_at": None,
            "stop_initiated_by": None,
        }
    )
    harness.store.create_episode(owner)
    harness = replace(harness, episode=owner)
    shared, binding = _isolated(harness, tmp_path, graph=False)
    (Path(binding.worktree_path) / "file").write_text("code only")
    revision = harness.service.history.state().revision
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{owner_id}/merge",
        json={"keep_branch_open": True},
    )
    assert response.status_code == 202, response.text
    assert (shared / "file").read_text() == "code only"
    assert harness.service.history.state().revision == revision
    assert harness.store.episode_tasks(owner_id) == []


def test_target_moved_after_preparation_blocks_graph_commit(manifest, tmp_path, monkeypatch):
    from rcp.runs.episodes import merge

    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _isolated(harness, tmp_path)
    (Path(binding.worktree_path) / "file").write_text("episode")
    original = merge.prepare_branch_merge_with_history

    def move_target(*args, **kwargs):
        prepared = original(*args, **kwargs)
        git(shared, "update-ref", "refs/heads/main", binding.starting_commit)
        return prepared

    monkeypatch.setattr(merge, "prepare_branch_merge_with_history", move_target)
    revision = harness.service.history.state().revision
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge",
        json={"keep_branch_open": True},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "code_landing_unverified"
    assert harness.service.history.state().revision == revision
    assert not harness.branch.merge_receipts()


@pytest.mark.parametrize(
    "body",
    [
        {"history_mode": "squash", "keep_branch_open": True},
        {"remove_worktree": False, "delete_code_branch": True},
        {"remove_worktree": False, "delete_code_branch": True, "keep_branch_open": True},
    ],
)
def test_invalid_merge_choices_fail_request_validation(manifest, tmp_path, body):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge", json=body
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "value_error"
    assert (
        harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
        is None
    )
    assert not harness.branch.merge_receipts()


def test_code_residue_refuses_without_graph_delivery_or_provider(manifest, tmp_path, monkeypatch):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _isolated(harness, tmp_path)
    (shared / "file").write_text("target change")
    git(shared, "add", "file")
    git(shared, "commit", "-m", "target change")
    target_commit = git(shared, "rev-parse", "HEAD")
    (Path(binding.worktree_path) / "file").write_text("episode change")
    monkeypatch.setattr(
        harness.app.state.launcher,
        "stream",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("provider launched")),
    )
    task_ids = {
        task.operation_id for task in harness.store.episode_tasks(harness.episode.episode_id)
    }
    revision = harness.service.history.state().revision
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge"
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "code_residue_needs_merge_task"
    assert git(shared, "rev-parse", "HEAD") == target_commit
    assert harness.service.history.state().revision == revision
    assert not harness.branch.merge_receipts()
    assert {
        task.operation_id for task in harness.store.episode_tasks(harness.episode.episode_id)
    } == task_ids
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert state.merge_reservation is None
    assert state.delivered_source_commit is None
