from __future__ import annotations

import json

import pytest

from rcp.core.models import GraphState, Patch
from rcp.history import HistoryManager, PatchRejected
from rcp.runs.tasks.graph import _read_prepared_graph_context, _stage_prepared_graph_context
from rcp.service import RunRequest
from rcp.storage import AgentTaskRecord
from tests.helpers import create_named_app as create_app
from tests.helpers import seed_patch


@pytest.mark.parametrize("kind", ["seed", "refresh", "work"])
def test_new_reading_reports_are_rejected(manifest, kind: str) -> None:
    history = HistoryManager(manifest)
    patch = Patch.model_validate(
        {
            "kind": kind,
            "author": "agent",
            "summary": "Claimed reading coverage.",
            "run_truth_scope": ["repo-a"],
            "repositories_read": ["repo-a"],
            "ops": [{"op": "set_coverage", "coverage": {"note": "Everything was read."}}],
        }
    )

    with pytest.raises(PatchRejected) as caught:
        history.append(patch)

    assert any(message.code == "legacy-only-operation" for message in caught.value.report.messages)
    assert all(patch.admission == "rejected" for patch in history.load_patches())
    assert history.materialize().state.nodes == {}


def test_historical_reading_report_replays_without_outputs_or_history_rewrite(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    # A pre-transition historical seed contains real graph operations and a report.
    raw = seed_patch().model_dump(mode="json")
    raw["revision"] = 1
    raw["ops"].append(
        {
            "op": "set_coverage",
            "coverage": {
                "sessions_read": ["repo-a/laptop/codex/session-a"] * 2,
                "sessions_skipped": ["repo-a/laptop/codex/session-b"],
                "repositories_never_seen": ["repo-b"],
                "earliest_timestamp": "2026-01-01T00:00:00Z",
                "note": "All authorized sources were read.",
            },
        }
    )
    patch_dir = manifest.research_dir / "patches"
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_path = patch_dir / "000001.json"
    patch_path.write_text(json.dumps(raw), encoding="utf-8")
    original = patch_path.read_bytes()

    state = history.materialize().state

    assert state.replay_status == "complete", state.replay_failure
    assert state.revision == 1
    assert "rq/learning-after-shift" in state.nodes
    assert "coverage" not in state.model_dump()
    assert "coverage" not in json.loads((manifest.research_dir / "graph.json").read_text())
    assert not (manifest.research_dir / "coverage.json").exists()
    assert patch_path.read_bytes() == original


def test_old_graph_snapshot_discards_reading_report_without_mutating_input() -> None:
    raw = {"revision": 3, "coverage": {"note": "Old report", "sessions_skipped": ["session-a"]}}

    state = GraphState.model_validate(raw)

    assert state.revision == 3
    assert "coverage" not in state.model_dump()
    assert raw["coverage"]["note"] == "Old report"


def test_retained_stage_from_before_the_retirement_still_reuses_its_context(
    manifest, tmp_path
) -> None:
    # An upgrade can land while a Seed or Refresh keeps a retained stage, and that
    # stage's version-2 context still names `coverage_path`. `RunContext` forbids
    # extras, so without the retirement allowlist the checkpoint would fail a
    # native continuation and quietly force a retry to rebuild the context it
    # promised to reuse.
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    context = service.assemble_run(RunRequest(run_truth_scope=["repo-a"]), surface="refresh")
    stage = tmp_path / "stage"
    stage.mkdir()
    _stage_prepared_graph_context(
        stage,
        None,
        project_id="project",
        kind="refresh",
        graph_revision=context.graph_revision,
        execution_host="",
        original_contract_path=str(stage / "inputs/task.md"),
        context=context,
    )
    prepared_path = stage / "inputs/prepared-context.json"
    payload = json.loads(prepared_path.read_text(encoding="utf-8"))
    payload["context"]["coverage_path"] = str(manifest.research_dir / "coverage.json")
    # Staged inputs are written read-only; rewrite as the previous version would have.
    prepared_path.chmod(0o600)
    prepared_path.write_text(json.dumps(payload), encoding="utf-8")

    now = "2026-08-04T12:00:00+00:00"
    record = AgentTaskRecord(
        operation_id="parent",
        project_id="project",
        kind="refresh",
        status="failed",
        request={"provider": "codex"},
        created_at=now,
        updated_at=now,
        status_message="failed",
        attempt=1,
        stage_root=str(stage),
    )

    prepared = _read_prepared_graph_context(record)

    assert prepared.graph_revision == context.graph_revision
    assert prepared.context.research_md_path == context.research_md_path
    assert "coverage_path" not in prepared.context.model_dump()
    # The file on disk is evidence of the prior attempt, not a migration target.
    assert json.loads(prepared_path.read_text(encoding="utf-8"))["context"]["coverage_path"]

    # The allowlist stays closed: only the retired key is dropped.
    payload["context"]["unrecognized_path"] = "/tmp/unknown"
    prepared_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unrecognized_path"):
        _read_prepared_graph_context(record)
