from __future__ import annotations

import json
import uuid

import pytest

from rcp.agents.loop_overlap import render_loop_overlap
from rcp.api.episodes import serialize_episode
from rcp.core.models import EpisodeIsolation, EpisodeWorktreeBinding
from rcp.core.transition_models import GraphTargetRef
from rcp.loop_status import episode_loop_metadata, loop_status_projection, other_branch_loops
from rcp.storage import EpisodeRecord

from .helpers import create_named_app, wait_for_task
from .test_experiment_stop import EXPERIMENT_ID, NODE_PATH, _Loop


@pytest.fixture
def loop(manifest, tmp_path):
    return _Loop(create_named_app(str(manifest.path), data_dir=tmp_path / "data"))


def test_isolated_human_start_reports_overlap_and_preserves_main(loop):
    loop.start_episode()
    loop.record_answers()
    response = loop.client.post(
        f"/api/projects/{loop.project_id}/experiments/{NODE_PATH}/run",
        json={"chat_id": str(uuid.uuid4()), "graph_isolation": True},
    )
    assert response.status_code == 202, response.text
    task = response.json()
    wait_for_task(loop.app, task["operation_id"])
    assert task["graph_target"]["kind"] == "branch"
    assert [row["episode_id"] for row in task["live_elsewhere"]] == [loop.episode_id]
    assert loop.store.episode(loop.episode_id).stop_requested_at is None
    main = loop_status_projection(
        loop.store,
        loop.project_id,
        EXPERIMENT_ID,
        graph_target=GraphTargetRef(),
    )
    assert main.current is not None
    assert main.current.episode_id == loop.episode_id
    assert [row.episode_id for row in main.live_elsewhere] == [task["episode_id"]]
    refused = loop.client.post(
        f"/api/projects/{loop.project_id}/experiments/{NODE_PATH}/run",
        json={"chat_id": str(uuid.uuid4())},
    )
    assert refused.status_code == 409


@pytest.mark.parametrize("ending", ["stopped", "completed"])
def test_fresh_status_and_episode_response_share_lifecycle_metadata(loop, ending):
    empty = loop_status_projection(
        loop.store,
        loop.project_id,
        EXPERIMENT_ID,
        graph_target=GraphTargetRef(),
    )
    assert empty.state == "none" and empty.current is None and empty.live_elsewhere == []
    loop.start_episode()
    loop.store.record_agent_task_receipt(
        "loop-root",
        "agent_launch",
        {
            "execution_host": "",
            "canonical_repository_roots": ["/checkout/project"],
        },
    )
    if ending == "stopped":
        loop.stop()
    else:
        loop.store.end_episode_without_report(loop.episode_id, ending="completed")
    status = loop_status_projection(
        loop.store,
        loop.project_id,
        EXPERIMENT_ID,
        graph_target=GraphTargetRef(),
    )
    assert status.state == ending
    row = status.current
    assert row is not None
    episode = loop.store.episode(loop.episode_id)
    response = serialize_episode(loop.store, loop.project_id, episode)
    assert row.started_by == response.started_by
    assert row.checkout == response.checkout
    assert row.checkout.repository_paths == ["/checkout/project"]
    assert row.checkout.execution_host == "" and row.checkout.available
    assert row.started_by.kind == "human" and row.started_by.human == loop.authorizer
    assert row.stop_initiated_by == response.stop_initiated_by
    assert row.stop_settled_at == response.stop_settled_at
    if ending == "stopped":
        assert row.stop_initiated_by == f"human:{loop.authorizer.user_id}"
        assert row.stop_settled_at is not None
    assert (
        other_branch_loops(
            loop.store,
            loop.project_id,
            graph_target=GraphTargetRef(kind="branch", branch_id="other"),
        )
        == []
    )


def test_checkout_identity_uses_immutable_binding_and_explicit_unavailable_history(loop):
    loop.start_episode()
    episode = loop.store.episode(loop.episode_id)
    missing = episode_loop_metadata(loop.store, episode)
    assert missing.checkout.available is False
    assert missing.checkout.repository_paths == []
    binding = EpisodeWorktreeBinding(
        owner_episode_id=loop.episode_id,
        repository_alias="repo-a",
        machine="remote",
        execution_host="execution-host",
        shared_path="/shared/project",
        worktree_path="/worktrees/run",
        git_common_dir="/shared/project/.git",
        branch="run",
        starting_branch="main",
        starting_commit="a" * 40,
    )
    loop.store.create_episode_isolation(
        loop.project_id,
        EpisodeIsolation(
            owner_episode_id=loop.episode_id,
            worktree=binding,
        ),
    )
    checkout = episode_loop_metadata(loop.store, episode).checkout
    assert checkout.kind == "worktree" and checkout.available
    assert checkout.execution_host == binding.execution_host
    assert checkout.repository_paths == [binding.worktree_path]


def test_overlap_renderer_preserves_shared_row_data(loop):
    loop.start_episode()
    rows = other_branch_loops(
        loop.store,
        loop.project_id,
        graph_target=GraphTargetRef(kind="branch", branch_id="other"),
    )
    rendered = render_loop_overlap(rows)
    assert json.loads(rendered.split("\n", 1)[1]) == [row.model_dump(mode="json") for row in rows]


def test_historical_loop_without_its_task_is_explicitly_unavailable(loop):
    now = loop.store.now()
    episode = loop.store.create_episode(
        EpisodeRecord(
            episode_id=str(uuid.uuid4()),
            project_id=loop.project_id,
            mode="experiment_loop",
            control_node_id=EXPERIMENT_ID,
            status="queued",
            authorized_by=loop.authorizer,
            invocation_ceiling=1,
            created_at=now,
            updated_at=now,
        )
    )
    loop.store.end_episode_without_report(episode.episode_id, ending="completed")
    status = loop_status_projection(
        loop.store,
        loop.project_id,
        EXPERIMENT_ID,
        graph_target=GraphTargetRef(),
    )
    assert status.state == "unavailable"
    assert status.current is not None and status.current.diagnostic is not None
    assert not status.current.checkout.available
