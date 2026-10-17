from __future__ import annotations

import json
from pathlib import Path

import pytest

from rcp.core.materialize import materialize_patches
from rcp.core.models import GraphState, Patch
from rcp.core.operations import UpsertGlossaryOperation
from rcp.core.validation import build_validation_context
from rcp.history import HistoryManager, ReplayHalted
from rcp.history.engine import ProjectHistoryEngine
from rcp.history.queries import HeadSpec
from tests.helpers import seed_patch, unchecked_write_authority
from tests.test_branch_history import _branch_metadata, _branch_patch
from tests.test_proposals import proposal_patch


@pytest.fixture
def history(manifest):
    owner = HistoryManager(manifest, write_authority=unchecked_write_authority())
    owner.append(seed_patch())
    return owner


@pytest.mark.parametrize("scope", [None, "{", "{}", '{"truth_scope":1}', "[]"])
def test_scope_failures_are_the_same_degraded_read_for_every_owner(history, scope):
    branch = history.create_branch(_branch_metadata(history))
    path = history.root / "scope-base.json"
    if scope is None:
        path.unlink()
    else:
        path.write_text(scope)
    result = history.current_materialization()
    assert result.state.revision == 0
    assert result.state.replay_status == "degraded"
    assert result.state.replay_failure.code == (
        "scope-provenance-missing" if scope is None else "scope-provenance-invalid"
    )
    assert history.history_engine.snapshot(history.graph_target).result == result
    assert branch.current_materialization().state == result.state
    assert branch.base_state() == result.state
    with pytest.raises(ValueError, match="precedes the branch base"):
        history.history_engine.resolve(HeadSpec(revision=0), default_target=branch.graph_target)
    with pytest.raises(ReplayHalted):
        history.append(_branch_patch("ev/refused"))
    with pytest.raises(ReplayHalted):
        branch.append(_branch_patch("ev/refused"))


@pytest.mark.parametrize("later_failure", ["schema", "chain"])
@pytest.mark.parametrize("target", ["main", "branch"])
def test_first_replay_halt_wins_over_later_structural_failure(history, target, later_failure):
    owner = history if target == "main" else history.create_branch(_branch_metadata(history))
    first, _ = owner.append(_branch_patch("ev/first"))
    second, _ = owner.append(_branch_patch("ev/second"))
    third, _ = owner.append(_branch_patch("ev/third"))
    second_path = owner.patches_dir / f"{second.revision:06d}.json"
    second_path.write_text(second_path.read_text().replace("ev/second", "ev/first"))
    third_path = owner.patches_dir / f"{third.revision:06d}.json"
    if later_failure == "schema":
        third_path.write_text("{")
    else:
        document = json.loads(third_path.read_text())
        document["transition"]["pre_head"]["transition_id"] = "diverged"
        third_path.write_text(json.dumps(document))
    result = owner.current_materialization()
    assert result.state.replay_status == "degraded"
    assert result.state.revision == first.revision
    assert result.state.replay_failure.revision == second.revision
    assert result.state.replay_failure.code not in {
        "patch-schema-invalid",
        "transition-head-mismatch",
    }
    assert "ev/second" not in result.state.nodes
    repeated = owner.history_engine.snapshot(owner.graph_target).result
    assert repeated.state == result.state
    assert repeated.patches == result.patches
    assert {rev: report.messages for rev, report in repeated.reports.items()} == {
        rev: report.messages for rev, report in result.reports.items()
    }
    with pytest.raises(ReplayHalted):
        owner.append(_branch_patch("ev/refused"))


def test_pending_tail_is_not_published_or_retained(history, tmp_path):
    prepared, report, _ = history.validate_candidate(_branch_patch("ev/pending"))
    assert not report.rejected
    prepared = prepared.model_copy(update={"admission": "accepted"})
    pending = tmp_path / f"{prepared.revision:06d}.json"
    pending.write_text(prepared.model_dump_json())
    before = {path: path.read_bytes() for path in history.root.rglob("*") if path.is_file()}
    canonical = history.current_materialization()
    result = history.materialize(write_outputs=False, pending_patch_paths=[pending])
    assert result.state.revision == prepared.revision
    assert "ev/pending" in result.state.nodes
    assert history.current_materialization() == canonical
    assert before == {path: path.read_bytes() for path in history.root.rglob("*") if path.is_file()}
    # Publication makes the same bytes visible to the shared engine's next inventory.
    pending.rename(history.patches_dir / pending.name)
    published = history.current_materialization()
    assert published.state == result.state
    assert published.patches == result.patches
    assert {rev: report.messages for rev, report in published.reports.items()} == {
        rev: report.messages for rev, report in result.reports.items()
    }


@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize("target", ["main", "branch"])
def test_append_never_calls_optional_store(history, tmp_path, monkeypatch, cold, target):
    owner = history if target == "main" else history.create_branch(_branch_metadata(history))
    if cold:
        history._history_engine = ProjectHistoryEngine(history)
    engine = history.history_engine
    engine.configure_store(tmp_path / "cache", "project")
    engine.checkpoint_interval = 2

    def forbidden(*args, **kwargs):
        raise AssertionError("canonical append must not wait on optional cache I/O")

    for name in ("load_checkpoint", "load_step_segments", "save_checkpoint", "save_step_segments"):
        monkeypatch.setattr(engine.store, name, forbidden)
    patch, result = owner.append(_branch_patch("ev/new"))
    assert result.state.replay_status == "complete"
    assert result.state.revision == patch.revision
    assert "ev/new" in result.state.nodes
    assert owner.current_materialization() == result


@pytest.mark.parametrize("target", ["main", "branch"])
def test_shared_materialization_preserves_output_bytes_and_observer_boundaries(history, target):
    owner = history if target == "main" else history.create_branch(_branch_metadata(history))
    owner.append(proposal_patch())
    owner.append(
        Patch(
            kind="work",
            author="agent",
            producer="agent",
            summary="Define a term and retain an ingestion cursor.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            processed_cursors={"session": "record-2"} if target == "main" else {},
            ops=[
                UpsertGlossaryOperation.model_validate(
                    {
                        "op": "upsert_glossary",
                        "terms": [{"term": "Plasticity", "plain_definition": "Ability to learn."}],
                    }
                )
            ],
        )
    )
    expected = materialize_patches(
        history.load_patches()
        if target == "main"
        else [*history.load_patches(), *owner.load_patches()],
        context=build_validation_context(
            history.manifest,
            GraphState(project_truth_scope=history.manifest.project.truth_scope),
            mode="replay",
        ),
    )
    owner._write_materialized_outputs(expected)
    outputs = ["graph.json", "glossary.json", "proposals.json", "cursors.json", "research.md"]
    if target == "branch":
        outputs.remove("cursors.json")
    before = {name: (owner.root / name).read_bytes() for name in outputs}
    observed = []
    actual = owner.materialize(
        accepted_patch_observer=lambda before, patch, after: observed.append(
            (before.revision, patch.revision, after.revision)
        )
    )
    assert actual.state == expected.state
    assert actual.patches == expected.patches
    assert actual.processed_cursors == expected.processed_cursors
    assert actual.repository_descriptors == expected.repository_descriptors
    assert {rev: report.messages for rev, report in actual.reports.items()} == {
        rev: report.messages for rev, report in expected.reports.items()
    }
    assert observed == [(0, 1, 1), (1, 2, 2), (2, 3, 3)]
    assert before == {name: (owner.root / name).read_bytes() for name in outputs}
    history._history_engine = ProjectHistoryEngine(history)
    assert owner.materialize().state == expected.state
    assert before == {name: (owner.root / name).read_bytes() for name in outputs}


def test_branch_replays_its_base_when_main_halts_after_that_base(history):
    branch = history.create_branch(_branch_metadata(history))
    branch.append(_branch_patch("ev/branch"))
    patch, _ = history.append(_branch_patch("ev/main"))
    (history.patches_dir / f"{patch.revision:06d}.json").write_text("{")
    result = branch.current_materialization()
    assert result.state.replay_status == "complete"
    assert "ev/branch" in result.state.nodes
    assert "ev/main" not in result.state.nodes
    branch.append(_branch_patch("ev/next"))
    assert branch.base_state().replay_status == "complete"


def test_transient_patch_read_failure_keeps_prefix_and_recovers(history, monkeypatch):
    patch, _ = history.append(_branch_patch("ev/later"))
    path = history.patches_dir / f"{patch.revision:06d}.json"
    history._history_engine = ProjectHistoryEngine(history)
    read_bytes = Path.read_bytes

    def unavailable(candidate):
        if candidate == path:
            raise OSError("temporary Patch read failure")
        return read_bytes(candidate)

    with monkeypatch.context() as blocked:
        blocked.setattr(Path, "read_bytes", unavailable)
        result = history.current_materialization()
    assert result.state.revision == 1
    assert result.state.replay_status == "degraded"
    assert result.state.replay_failure.code == "patch-read-failed"
    recovered = history.current_materialization()
    assert recovered.state.replay_status == "complete"
    assert recovered.state.revision == patch.revision
    assert "ev/later" in recovered.state.nodes


def test_branch_revision_gap_and_zero_head_stay_degraded(history):
    branch = history.create_branch(_branch_metadata(history))
    branch.append(_branch_patch("ev/one"))
    branch.append(_branch_patch("ev/two"))
    first, second = sorted((branch.root / "patches").glob("*.json"))
    first.unlink()
    result = branch.current_materialization()
    assert result.state.replay_status == "degraded"
    assert result.state.replay_failure.code == "patch-revision-discontinuous"
    assert "ev/two" not in result.state.nodes
    from rcp.history.branches import read_branch_snapshots

    metadata = branch.branch_metadata()
    summary = read_branch_snapshots(
        history, [(metadata.branch_id, metadata.episode_id, metadata.project_id)]
    )[metadata.branch_id]
    assert summary is not None and summary.state.replay_status == "degraded"

    (history.root / "scope-base.json").unlink()
    engine = history.history_engine
    zero = engine.resolve(HeadSpec(revision=0), default_target=history.graph_target)
    assert engine.state_at(zero).replay_status == "degraded"
