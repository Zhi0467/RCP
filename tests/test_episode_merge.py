from __future__ import annotations

from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.core.models import EpisodeIsolation, EpisodeWorktreeBinding
from rcp.transport import conversation_worktree

from .helpers import append_fixture_patch, wait_for_task
from .test_branch_merge_api import _branch_patch, _create_branch_harness
from .test_conversation_worktree_git import git


def _isolated(harness, tmp_path, *, graph=True, shared=None):
    shared = shared or tmp_path / "code"
    shared.mkdir(exist_ok=True)
    git(shared, "init", "--initial-branch=main")
    (shared / ".git" / "info" / "exclude").write_text(".research/\n")
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
    # The graph side still merges after its code side was discarded.
    merged = harness.client.post(
        route.replace("/cleanup", "/merge"),
        json={"remove_worktree": False, "delete_code_branch": False},
    )
    assert merged.status_code == 202, merged.text
    assert len(harness.branch.merge_receipts()) == 1
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert (state.status, state.merge_reservation) == ("removed", None)


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

    def fail_branch_delete(store, binding, operation, **values):
        if operation == "delete_branch":
            raise ValueError("fixture_delete_failure")
        return original(store, binding, operation, **values)

    monkeypatch.setattr(merge, "_git", fail_branch_delete)
    route = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}"
    assert harness.client.post(route + "/merge", json={}).status_code == 409
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert state.delivered_source_commit
    assert state.merge_attempt.phase == "cleanup"
    # A failed code cleanup keeps the episode listed, so its Merge panel can retry.
    assert not state.graph_archived
    assert state.status != "removed"
    assert "remove_worktree" in state.merge_attempt.cleanup_completed
    assert len(harness.branch.merge_receipts()) == 1
    # A preview after partial cleanup reads the delivery, not the removed worktree.
    response = harness.client.get(route + "/merge-preview")
    assert response.status_code == 200, response.text
    assert response.json()["code"]["status"] == "already_merged"
    monkeypatch.setattr(merge, "_git", original)
    response = harness.client.post(
        route + "/cleanup",
        json={"remove_worktree": True, "delete_code_branch": True, "archive_graph_branch": True},
    )
    assert response.status_code == 200, response.text
    assert not Path(binding.worktree_path).exists()
    assert (shared / "file").read_text() == "episode"
    assert len(harness.branch.merge_receipts()) == 1
    assert harness.store.episode_isolation_state(
        harness.project_id, harness.episode.episode_id
    ).graph_archived


def test_later_cleanup_keeps_the_branch_when_target_history_was_dropped(manifest, tmp_path):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _isolated(harness, tmp_path)
    (shared / "other").write_text("target work")
    git(shared, "add", "other")
    git(shared, "commit", "-m", "target work")
    (Path(binding.worktree_path) / "file").write_text("episode")
    route = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}"
    response = harness.client.post(
        route + "/merge",
        json={"remove_worktree": False, "delete_code_branch": False, "keep_branch_open": True},
    )
    assert response.status_code == 202, response.text
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert state.delivered_target_commit
    # The target is reset to the episode's commit, dropping the history it merged into.
    git(shared, "reset", "--hard", state.delivered_source_commit)
    response = harness.client.post(
        route + "/cleanup", json={"remove_worktree": True, "delete_code_branch": True}
    )
    assert response.status_code == 409
    assert git(shared, "rev-parse", binding.branch) == state.delivered_source_commit


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


@pytest.mark.parametrize(
    "phase,history_mode",
    [
        *[
            (phase, "merge")
            for phase in (
                "landing",
                "verified",
                "graph_receipt",
                "graph_committed",
                "cleanup",
                "done",
            )
        ],
        # Only re-landing and branch deletion read the squash commit.
        ("landing", "squash"),
        ("cleanup", "squash"),
    ],
)
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
        # "graph_receipt": the receipt is written, the phase that records it is not.
        if phase == "graph_receipt" and changes.get("phase") == "graph_committed":
            raise Crash()
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
    assert state.merge_attempt.phase == ("verified" if phase == "graph_receipt" else phase)
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


def _code_only_owner(harness):
    import uuid
    from dataclasses import replace

    from rcp.core.transition_models import GraphTargetRef

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
    return replace(harness, episode=owner)


def test_code_only_owner_merges_without_graph_task(manifest, tmp_path):
    harness = _code_only_owner(_create_branch_harness(manifest, tmp_path, change="none"))
    owner_id = harness.episode.episode_id
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


def _merge_turn(harness, shared, turns, *, lands=True):
    async def stream(_provider, _prompt, *, write_dirs, session_id=None, **_kwargs):
        turns.append(sorted(str(path) for path in write_dirs))
        state = harness.store.episode_isolation_state(
            harness.project_id, harness.episode.episode_id
        )
        if lands:
            source = state.merge_attempt.source_commit
            git(shared, "merge", "--no-ff", "-X", "theirs", "-m", "merge", source)
        yield AgentEvent(event="session", session_id="code-merge")
        yield AgentEvent(event="provider_exit", text='{"return_code":0}')
        yield AgentEvent(event="done")

    return stream


def _conflicting(harness, tmp_path, *, graph):
    # The task resolves the registered repository, so the worktree belongs to it.
    shared, binding = _isolated(harness, tmp_path, graph=graph, shared=tmp_path / "repo-a")
    (shared / "file").write_text("target change")
    git(shared, "commit", "-am", "target change")
    (Path(binding.worktree_path) / "file").write_text("episode change")
    return shared, binding


@pytest.mark.parametrize("case", ["lands", "skips", "squash"])
def test_code_conflict_runs_one_code_merge_task(manifest, tmp_path, monkeypatch, case):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _conflicting(harness, tmp_path, graph=True)
    target_commit = git(shared, "rev-parse", "HEAD")
    revision = harness.service.history.state().revision
    turns = []
    monkeypatch.setattr(
        harness.app.state.launcher,
        "stream",
        _merge_turn(harness, shared, turns, lands=case == "lands"),
    )
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge",
        json={"history_mode": "squash", "keep_branch_open": False} if case == "squash" else {},
    )
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    if case == "squash":
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "squash_needs_agentless_merge"
        assert state.merge_reservation is None
        assert git(shared, "rev-parse", "HEAD") == target_commit
        return
    assert response.status_code == 202, response.text
    wait_for_task(
        harness.store,
        state.merge_attempt.graph_task_id,
        expect="succeeded" if case == "lands" else "failed",
    )
    # One code turn inside Integrate's roots; the graph needed no provider turn.
    assert turns == [sorted([str(shared), binding.worktree_path])]
    if case == "skips":
        # Code that did not land blocks the graph commit.
        assert harness.service.history.state().revision == revision
        assert not harness.branch.merge_receipts()
        # Without reconciliation first (a click racing the task's end), nothing is cleaned.
        from rcp.runs.episodes import merge

        with pytest.raises(ValueError, match="episode_merge_reserved"):
            merge.merge_episode(
                harness.service,
                harness.store,
                harness.episode,
                merge.MergeEpisodeBody(),
                authorized_by=harness.episode.authorized_by,
                dispatch_graph=lambda *_args, **_kwargs: None,
            )
        assert Path(binding.worktree_path).exists()
        # The next Merge releases the failed attempt and starts a fresh one.
        again = harness.client.post(
            f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge",
            json={},
        )
        assert again.status_code == 202, again.text
        retried = harness.store.episode_isolation_state(
            harness.project_id, harness.episode.episode_id
        ).merge_attempt
        assert retried.attempt_id != state.merge_attempt.attempt_id
        wait_for_task(harness.store, retried.graph_task_id, expect="failed")
        return
    assert (shared / "file").read_text() == "episode change"
    assert harness.branch.merge_receipts()[-1].outcome == "committed"
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert state.delivered_source_commit == state.merge_attempt.source_commit


def test_code_merge_turn_launches_beside_an_unrelated_dirty_checkout(
    manifest, tmp_path, monkeypatch
):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _conflicting(harness, tmp_path, graph=True)
    git(shared, "branch", "release")
    # The target is not checked out, so the shared checkout's own work is untouched.
    (shared / "scratch.txt").write_text("human work\n")
    turns = []
    monkeypatch.setattr(
        harness.app.state.launcher, "stream", _merge_turn(harness, shared, turns, lands=False)
    )
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge",
        json={"target_branch": "release"},
    )
    assert response.status_code == 202, response.text
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    wait_for_task(harness.store, state.merge_attempt.graph_task_id, expect="failed")
    assert turns == [sorted([str(shared), binding.worktree_path])]
    assert (shared / "scratch.txt").read_text() == "human work\n"


def test_code_merge_that_leaves_the_worktree_off_its_branch_is_unverified(
    manifest, tmp_path, monkeypatch
):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _conflicting(harness, tmp_path, graph=True)
    git(shared, "branch", "release")
    revision = harness.service.history.state().revision
    worktree = Path(binding.worktree_path)

    async def stream(_provider, _prompt, **_kwargs):
        state = harness.store.episode_isolation_state(
            harness.project_id, harness.episode.episode_id
        )
        # Lands into release from the worktree, then never restores the episode branch.
        git(worktree, "checkout", "release")
        git(
            worktree,
            "merge",
            "--no-ff",
            "-X",
            "theirs",
            "-m",
            "merge",
            state.merge_attempt.source_commit,
        )
        yield AgentEvent(event="session", session_id="code-merge")
        yield AgentEvent(event="provider_exit", text='{"return_code":0}')
        yield AgentEvent(event="done")

    monkeypatch.setattr(harness.app.state.launcher, "stream", stream)
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge",
        json={"target_branch": "release"},
    )
    assert response.status_code == 202, response.text
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    wait_for_task(harness.store, state.merge_attempt.graph_task_id, expect="failed")
    assert harness.service.history.state().revision == revision
    assert not harness.branch.merge_receipts()


def test_confirmed_unfinished_jobs_go_to_the_code_merge_agent(manifest, tmp_path, monkeypatch):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _isolated(harness, tmp_path, shared=tmp_path / "repo-a")
    (Path(binding.worktree_path) / "file").write_text("episode change")
    with harness.store.connection() as connection:
        connection.execute(
            "INSERT INTO watchers (watcher_id, project_id, origin_operation_id, "
            "origin_task_kind, chat_id, episode_id, execution_host, check_command, log_path, "
            "cwd, continuation_json, status, created_at) VALUES ('w', ?, 'origin', 'node_chat', "
            "'chat', ?, '', 'squeue -j 7', '/tmp/log', '/tmp', '{}', 'active', ?)",
            (harness.project_id, harness.episode.episode_id, harness.store.now()),
        )
    prompts = []

    async def stream(_provider, prompt, **_kwargs):
        # The staged contract carries the confirmed job for the agent to stop.
        prompts.append(Path(prompt.split("\n")[1].strip()).read_text())
        state = harness.store.episode_isolation_state(
            harness.project_id, harness.episode.episode_id
        )
        git(shared, "merge", "--no-ff", "-m", "merge", state.merge_attempt.source_commit)
        yield AgentEvent(event="session", session_id="code-merge")
        yield AgentEvent(event="provider_exit", text='{"return_code":0}')
        yield AgentEvent(event="done")

    monkeypatch.setattr(harness.app.state.launcher, "stream", stream)
    route = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge"
    # Merge pauses on the unfinished job instead of refusing it.
    paused = harness.client.post(route, json={})
    assert paused.status_code == 409
    assert paused.json()["detail"]["code"] == "unfinished_jobs_confirmation_required"
    assert [job["id"] for job in paused.json()["detail"]["jobs"]] == ["w"]
    # Confirmed, a merge that would land agentless goes to the agent with the job.
    response = harness.client.post(route, json={"confirm_unfinished_jobs": True})
    assert response.status_code == 202, response.text
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert state.merge_attempt.code_by_agent
    wait_for_task(harness.store, state.merge_attempt.graph_task_id, expect="succeeded")
    assert len(prompts) == 1 and "squeue -j 7" in prompts[0]
    assert (shared / "file").read_text() == "episode change"
    # The job still reads as unfinished, so Merge lands but keeps the worktree listed.
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert state.merge_attempt.worktree_kept and not state.graph_archived
    assert Path(binding.worktree_path).exists()
    with harness.store.connection() as connection:
        connection.execute(
            "UPDATE watchers SET status = 'completed', completed_at = ? WHERE watcher_id = 'w'",
            (harness.store.now(),),
        )
    # Once the job finished, Merge again removes what the first Merge kept.
    assert harness.client.post(route, json={}).status_code == 202
    assert not Path(binding.worktree_path).exists()


def test_code_only_conflict_runs_one_code_merge_turn(manifest, tmp_path, monkeypatch):
    harness = _code_only_owner(_create_branch_harness(manifest, tmp_path, change="none"))
    owner_id = harness.episode.episode_id
    shared, binding = _conflicting(harness, tmp_path, graph=False)
    revision = harness.service.history.state().revision
    turns = []
    monkeypatch.setattr(harness.app.state.launcher, "stream", _merge_turn(harness, shared, turns))
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{owner_id}/merge", json={}
    )
    assert response.status_code == 202, response.text
    state = harness.store.episode_isolation_state(harness.project_id, owner_id)
    wait_for_task(harness.store, state.merge_attempt.graph_task_id, expect="succeeded")
    assert turns == [sorted([str(shared), binding.worktree_path])]
    assert (shared / "file").read_text() == "episode change"
    assert harness.service.history.state().revision == revision
    state = harness.store.episode_isolation_state(harness.project_id, owner_id)
    assert state.delivered_source_commit == state.merge_attempt.source_commit
    assert state.merge_reservation is None
    # The Experiment's own reconciliation never mistakes the merge for an invocation.
    import logging

    from rcp.runs.episodes.reconcile import EpisodeReconciler

    reconciler = EpisodeReconciler(
        harness.store, harness.app.state.background_tasks, logger=logging.getLogger(__name__)
    )
    assert reconciler.latest_experiment_leaf(owner_id) is None


def test_diff_paths_carry_the_merge_builders_classification(manifest, tmp_path):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    competing = _branch_patch(harness.root.operation_id, "evidence")
    competing.ops[0].nodes[0].observation = "Main recorded a different result."
    append_fixture_patch(harness.service, competing)
    url = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge-preview"

    graph = harness.client.get(url).json()["graph"]
    flagged = {path["residue_reason"] for path in graph["paths"] if path["residue_reason"]}
    assert flagged == {item["reason"] for item in graph["residue"]}
    assert any(path["conflict"] for path in graph["paths"])
    assert not any(path["delivered"] for path in graph["paths"])


def test_delivered_paths_stay_listed_after_a_merge(manifest, tmp_path):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    url = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}"
    before = harness.client.get(f"{url}/merge-preview").json()["graph"]["paths"]
    assert before and not any(path["delivered"] for path in before)
    response = harness.client.post(f"{url}/merge", json={"keep_branch_open": True})
    assert response.status_code == 202, response.text

    after = harness.client.get(f"{url}/merge-preview").json()["graph"]["paths"]
    assert [(path["id"], path["field_path"]) for path in after] == [
        (path["id"], path["field_path"]) for path in before
    ]
    assert all(path["delivered"] for path in after)


def test_merge_task_failing_after_its_receipt_completes_on_the_next_merge(
    manifest, tmp_path, monkeypatch
):
    from rcp.runs.tasks import branch_merge as task_module

    harness = _create_branch_harness(manifest, tmp_path, change="status")
    shared, binding = _conflicting(harness, tmp_path, graph=True)
    monkeypatch.setattr(harness.app.state.launcher, "stream", _merge_turn(harness, shared, []))

    def crash(*_args):
        raise ValueError("crash after receipt")

    monkeypatch.setattr(task_module, "complete_graph_merge", crash)
    url = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge"
    assert harness.client.post(url, json={}).status_code == 202
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    wait_for_task(harness.store, state.merge_attempt.graph_task_id, expect="failed")
    assert len(harness.branch.merge_receipts()) == 1

    # The next Merge finishes the delivered attempt instead of dropping its cleanup.
    assert harness.client.post(url, json={}).status_code == 202
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert (state.merge_attempt.phase, state.merge_reservation) == ("done", None)
    assert not Path(binding.worktree_path).exists()
    assert len(harness.branch.merge_receipts()) == 1


def test_graph_residue_merges_after_the_worktree_was_discarded(manifest, tmp_path, monkeypatch):
    from .test_branch_merge_api import _candidate_for, _PatchWritingLauncher

    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    _, binding = _isolated(harness, tmp_path)
    url = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}"
    discarded = harness.client.post(
        f"{url}/cleanup", json={"remove_worktree": True, "confirm_discard": True}
    )
    assert discarded.status_code == 200, discarded.text
    competing = _branch_patch(harness.root.operation_id, "evidence")
    competing.ops[0].nodes[0].observation = "Main recorded a different result."
    append_fixture_patch(harness.service, competing)
    launcher = _PatchWritingLauncher(_candidate_for("evidence"))
    monkeypatch.setattr(harness.app.state.launcher, "stream", launcher.stream)

    response = harness.client.post(
        f"{url}/merge", json={"remove_worktree": False, "delete_code_branch": False}
    )
    assert response.status_code == 202, response.text
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert not state.merge_attempt.code_by_agent
    task = wait_for_task(harness.store, state.merge_attempt.graph_task_id)
    # The graph-only task reaches its provider with no repository roots.
    assert launcher.calls >= 1, task.error


def test_a_later_graph_only_merge_keeps_the_code_delivery(manifest, tmp_path):
    harness = _create_branch_harness(manifest, tmp_path, change="status")
    _, binding = _isolated(harness, tmp_path)
    (Path(binding.worktree_path) / "file").write_text("episode")
    url = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}"
    assert harness.client.post(f"{url}/merge", json={"keep_branch_open": True}).status_code == 202
    assert harness.client.post(f"{url}/cleanup", json={"remove_worktree": True}).status_code == 200
    delivered = harness.store.episode_isolation_state(
        harness.project_id, harness.episode.episode_id
    ).delivered_source_commit
    assert delivered
    harness.branch.append(
        _branch_patch(harness.root.operation_id, "evidence", suffix="-later"),
        expected_revision=harness.branch.head_ref().revision,
    )

    later = harness.client.post(
        f"{url}/merge", json={"remove_worktree": False, "delete_code_branch": False}
    )
    assert later.status_code == 202, later.text
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert len(harness.branch.merge_receipts()) == 2
    assert (state.status, state.delivered_source_commit) == ("removed", delivered)
