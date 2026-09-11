from __future__ import annotations

import json

import pytest

from rcp.core.models import GraphState, Patch
from rcp.history import HistoryManager, PatchRejected
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
