from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest

from rcp.agents.loop_overlap import render_loop_overlap
from rcp.api.episodes import serialize_episode
from rcp.core.models import EpisodeIsolation, EpisodeWorktreeBinding
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.limits import LOOP_OVERLAP_MAX_BYTES, LOOP_OVERLAP_MAX_ROWS, SPACE_RUNS_COMPLETED_TTL
from rcp.loop_status import (
    EpisodeStarter,
    compact_loop_status,
    episode_loop_metadata,
    episode_loop_metadata_from_snapshot,
    loop_status_projection,
    other_branch_loops,
)
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
    assert [row["episode_id"] for row in task["live_elsewhere"]["rows"]] == [loop.episode_id]
    assert loop.store.episode(loop.episode_id).stop_requested_at is None
    main = loop_status_projection(
        loop.store,
        loop.project_id,
        EXPERIMENT_ID,
        graph_target=GraphTargetRef(),
    )
    assert main.current is not None
    assert main.current.episode_id == loop.episode_id
    assert [row.episode_id for row in main.live_elsewhere.rows] == [task["episode_id"]]
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
    assert (
        empty.state == "none"
        and empty.current is None
        and empty.live_elsewhere.rows == []
        and empty.live_elsewhere.omitted == 0
    )
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
        ).rows
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
    snapshot = loop.store.episode_loop_metadata_snapshots([episode])[episode.episode_id]
    assert episode_loop_metadata_from_snapshot(episode, snapshot).checkout == checkout


def test_overlap_renderer_preserves_shared_row_data(loop):
    loop.start_episode()
    rows = other_branch_loops(
        loop.store,
        loop.project_id,
        graph_target=GraphTargetRef(kind="branch", branch_id="other"),
    )
    rendered = render_loop_overlap(rows)
    assert json.loads(rendered.split("\n", 1)[1]) == rows.model_dump(mode="json", exclude_none=True)


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


def test_loop_metadata_queries_only_root_receipts_when_serializing_many_turns(loop, monkeypatch):
    loop.start_episode()
    root = loop.store.agent_task("loop-root")
    assert root is not None
    episode = loop.store.episode(loop.episode_id)
    assert episode is not None
    loop.store.record_agent_task_receipt(
        root.operation_id,
        "agent_launch",
        {
            "execution_host": "",
            "canonical_repository_roots": ["/checkout/project"],
        },
    )
    reads = []
    original = loop.store.agent_task_receipts_by_category

    def receipts(operation_id, category):
        reads.append(operation_id)
        return original(operation_id, category)

    monkeypatch.setattr(loop.store, "agent_task_receipts_by_category", receipts)
    tasks = [root] + [root.model_copy(update={"operation_id": f"turn-{i}"}) for i in range(50)]
    metadata = episode_loop_metadata(loop.store, episode, tasks=tasks)
    assert reads == [root.operation_id]
    assert metadata.checkout.available
    assert metadata.checkout.repository_paths == ["/checkout/project"]
    snapshot = loop.store.episode_loop_metadata_snapshots([episode])[episode.episode_id]
    assert episode_loop_metadata_from_snapshot(episode, snapshot) == metadata


def test_overlap_skips_terminal_metadata_but_target_status_retains_old_loop(loop, monkeypatch):
    loop.start_episode()
    loop.stop()
    old = (
        datetime.fromisoformat(loop.store.now()) - SPACE_RUNS_COMPLETED_TTL - timedelta(days=1)
    ).isoformat()
    with loop.store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET ended_at = ? WHERE episode_id = ?", (old, loop.episode_id)
        )
    branch = GraphTargetRef(kind="branch", branch_id="elsewhere")
    episode = loop.store.episode(loop.episode_id)
    assert episode is not None
    loop.store.create_episode(
        episode.model_copy(
            update={
                "episode_id": "live-elsewhere",
                "graph_target": branch,
                "status": "queued",
                "invocations_used": 0,
                "graph_base_head": GraphHeadRef(revision=0),
                "wrapup_state": "not_started",
                "ending": None,
                "ended_at": None,
                "stop_requested_at": None,
                "stop_settled_at": None,
                "root_operation_id": None,
                "created_at": loop.store.now(),
            }
        )
    )
    import rcp.loop_status as status_module

    from .test_episode_storage import _episode

    loop.store.create_episode(
        _episode(
            loop.store,
            "terminal-elsewhere",
            project_id=loop.project_id,
            control_node_id="other-node",
        )
    )
    loop.store.end_episode_without_report("terminal-elsewhere", ending="completed")
    metadata_ids = []
    original = status_module.episode_loop_metadata

    def metadata(store, episode, **kwargs):
        metadata_ids.append(episode.episode_id)
        return original(store, episode, **kwargs)

    monkeypatch.setattr(status_module, "episode_loop_metadata", metadata)
    rows = other_branch_loops(loop.store, loop.project_id, graph_target=GraphTargetRef())
    assert [row.episode_id for row in rows.rows] == ["live-elsewhere"]
    assert metadata_ids == ["live-elsewhere"]
    status = loop_status_projection(
        loop.store, loop.project_id, EXPERIMENT_ID, graph_target=GraphTargetRef()
    )
    assert status.current is not None and status.current.episode_id == loop.episode_id
    assert status.state == "stopped"


def test_overlap_caps_compact_rows_and_reports_omitted(loop, monkeypatch):
    loop.start_episode()
    original = loop.store.episode(loop.episode_id)
    assert original is not None
    for index in range(LOOP_OVERLAP_MAX_ROWS + 3):
        loop.store.create_episode(
            original.model_copy(
                update={
                    "episode_id": f"overlap-{index:03}",
                    "status": "queued",
                    "invocations_used": 0,
                    "graph_target": GraphTargetRef(kind="branch", branch_id=f"branch-{index:03}"),
                    "graph_base_head": GraphHeadRef(revision=0),
                    "root_operation_id": None,
                }
            )
        )
    overlap = other_branch_loops(loop.store, loop.project_id, graph_target=GraphTargetRef())
    assert len(overlap.rows) == LOOP_OVERLAP_MAX_ROWS
    assert overlap.omitted == 3
    payload = overlap.model_dump(mode="json", exclude_none=True)
    assert len(json.dumps(payload).encode()) <= LOOP_OVERLAP_MAX_BYTES
    assert set(payload["rows"][0]) == {
        "node_id",
        "episode_id",
        "graph_target",
        "state",
        "started_by",
        "checkout",
    }
    assert payload["rows"][0]["started_by"] == {
        "kind": "human",
        "id": loop.authorizer.user_id,
        "display_name": loop.authorizer.display_name,
    }
    assert set(payload["rows"][0]["checkout"]) == {"kind", "repository_paths"}

    import rcp.loop_status as status_module

    original_metadata = status_module.episode_loop_metadata

    def oversized_metadata(store, episode, **kwargs):
        metadata = original_metadata(store, episode, **kwargs)
        metadata.checkout.repository_paths = ["/" + "x" * LOOP_OVERLAP_MAX_BYTES]
        return metadata

    monkeypatch.setattr(status_module, "episode_loop_metadata", oversized_metadata)
    oversized = other_branch_loops(loop.store, loop.project_id, graph_target=GraphTargetRef())
    assert oversized.rows == []
    assert oversized.omitted == LOOP_OVERLAP_MAX_ROWS + 3
    assert len(json.dumps(oversized.model_dump(mode="json")).encode()) <= LOOP_OVERLAP_MAX_BYTES


@pytest.mark.parametrize("kind", ["human", "auto_research", "unknown"])
def test_compact_loop_starter_retains_recorded_human_name_only(loop, kind):
    loop.start_episode()
    current = loop_status_projection(
        loop.store, loop.project_id, EXPERIMENT_ID, graph_target=GraphTargetRef()
    ).current
    assert current is not None
    parent_id = str(uuid.uuid4()) if kind == "auto_research" else None
    current.started_by = EpisodeStarter(
        kind=kind,
        human=loop.authorizer if kind == "human" else None,
        auto_research_episode_id=parent_id,
    )
    starter = compact_loop_status(current).started_by
    assert starter.kind == kind
    assert starter.id == (loop.authorizer.user_id if kind == "human" else parent_id)
    assert starter.display_name == (loop.authorizer.display_name if kind == "human" else None)
    assert "display_name" in starter.model_dump(mode="json")
