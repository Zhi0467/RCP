from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from rcp.runs.shared import checkpoint_local_recovery_stages
from rcp.storage.models import (
    RunStageLifecycleRecord,
)

BASE_COMMIT = "a" * 40
CANDIDATE_COMMIT = "b" * 40
WEB_BUILD_ID = "sha256:" + ("c" * 64)


class _StageLifecycleStore:
    def __init__(self, *records: RunStageLifecycleRecord) -> None:
        self.records = records

    def run_stage_lifecycles(self) -> tuple[RunStageLifecycleRecord, ...]:
        return self.records


@pytest.mark.parametrize("parent", ["run-stage", "agent-threads/member/stages"])
def test_local_recovery_stage_inventory_uses_the_task_ledger_and_ignores_remote(
    tmp_path: Path,
    parent: str,
) -> None:
    data_dir = tmp_path / "data"
    local = data_dir / parent / "local-stage"
    local.mkdir(parents=True)
    local_operation = str(uuid.uuid4())
    local_episode = str(uuid.uuid4())
    remote_view = uuid.uuid4().hex[:24]
    store = _StageLifecycleStore(
        RunStageLifecycleRecord(
            stage_host="",
            stage_root=str(local),
            owner_refs=(
                f"experiment_episode_state:{local_episode}",
                f"graph_runs:{local_operation}",
            ),
            must_exist=True,
            protect_from_cleanup=True,
        ),
        RunStageLifecycleRecord(
            stage_host="gpu.example",
            stage_root="/tmp/rcp-run.remote-stage",
            owner_refs=(f"result_views:{remote_view}",),
            must_exist=False,
            protect_from_cleanup=True,
        ),
    )

    inventory = checkpoint_local_recovery_stages(store, data_dir)

    assert [item.root for item in inventory] == [local]
    assert inventory[0].owner_refs == (
        f"experiment_episode_state:{local_episode}",
        f"graph_runs:{local_operation}",
    )


def test_local_recovery_stage_inventory_ignores_retention_swept_history(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    missing_task = data_dir / "run-stage" / "old-task"
    missing_view = data_dir / "run-stage" / "old-result-view"
    store = _StageLifecycleStore(
        RunStageLifecycleRecord(
            stage_host="",
            stage_root=str(missing_task),
            owner_refs=(f"graph_runs:{uuid.uuid4()}",),
            must_exist=False,
            protect_from_cleanup=False,
        ),
        RunStageLifecycleRecord(
            stage_host="",
            stage_root=str(missing_view),
            owner_refs=(f"result_views:{uuid.uuid4().hex[:24]}",),
            must_exist=False,
            protect_from_cleanup=False,
        ),
    )

    assert checkpoint_local_recovery_stages(store, data_dir) == ()


def test_local_recovery_stage_inventory_requires_active_episode_stage(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    episode_id = str(uuid.uuid4())
    missing = data_dir / "run-stage" / "active-episode"
    store = _StageLifecycleStore(
        RunStageLifecycleRecord(
            stage_host="",
            stage_root=str(missing),
            owner_refs=(f"experiment_episode_state:{episode_id}",),
            must_exist=True,
            protect_from_cleanup=True,
        )
    )

    with pytest.raises(ValueError, match="recovery-critical local run stage"):
        checkpoint_local_recovery_stages(store, data_dir)


def test_thread_stage_sweep_preserves_live_stages_and_transcripts(tmp_path):
    import os

    from rcp.runs.shared import _swept_stage_root

    parent = tmp_path / "agent-threads/member/stages"
    stale, live = parent / "stale", parent / "live"
    for stage in (stale, live):
        stage.mkdir(parents=True)
        os.utime(stage, (1, 1))
    transcript = parent.parent / "thread.jsonl"
    transcript.write_text("private")

    class Store:
        def protected_run_stage_roots(self, stage_host: str) -> tuple[str, ...]:
            return (str(live),)

    _swept_stage_root(tmp_path, store=Store())
    assert not stale.exists()
    assert live.is_dir() and transcript.read_text() == "private"
