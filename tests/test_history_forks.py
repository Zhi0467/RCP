from __future__ import annotations

import json
import uuid

import pytest

from rcp.core.models import GraphBranchMetadata
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history import HistoryManager
from rcp.history.engine import ProjectHistoryEngine
from rcp.history.queries import parse_head
from tests.helpers import seed_patch, unchecked_write_authority
from tests.test_branch_history import _authorizer, _branch_metadata, _branch_patch


def fork_metadata(history, source):
    branch_id = str(uuid.uuid4())
    base = (
        source
        if source.target.kind == "main"
        else history.branch(source.target.branch_id, initialize=False).branch_metadata().base_head
    )
    return GraphBranchMetadata(
        branch_id=branch_id,
        project_id="project",
        kind="fork",
        base_head=base,
        parent_head=source,
        created_by="human:fork-request",
        head=source.model_copy(
            update={"target": GraphTargetRef(kind="branch", branch_id=branch_id)}
        ),
        authorized_by=_authorizer(),
    )


@pytest.fixture
def history(manifest):
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    history.append(seed_patch())
    return history


def test_fork_at_past_head_excludes_later_revisions(history):
    source = history.head_ref()
    history.append(_branch_patch("ev/later"))
    branch = history.create_branch(fork_metadata(history, source))
    assert branch.state() == history.history_engine.state_at(source)
    assert "ev/later" not in json.loads((branch.root / "graph.json").read_text())["nodes"]
    assert history.create_branch(branch.branch_metadata()).head_ref() == branch.head_ref()


def test_existing_branch_reconciles_without_replaying_or_requiring_writable_main(
    history, monkeypatch
):
    metadata = fork_metadata(history, history.head_ref())
    branch = history.create_branch(metadata)

    def unexpected(*args, **kwargs):
        raise AssertionError("existing branch attempted parent replay or write admission")

    monkeypatch.setattr(history, "materialize", unexpected)
    monkeypatch.setattr(history, "require_writable", unexpected)
    monkeypatch.setattr("rcp.history.branches.branch_parent_snapshot", unexpected)
    assert history.create_branch(metadata).branch_metadata() == branch.branch_metadata()


def test_fork_creation_validates_parent_once(manifest, tmp_path, monkeypatch):
    from rcp.history import branches
    from rcp.runs.graph_forks import create_graph_fork
    from tests.helpers import authorized_human
    from tests.test_branch_merge_api import _create_branch_harness

    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    original = branches.branch_parent_snapshot
    calls = []

    def capture(history, parent_head, branch_id):
        calls.append(parent_head)
        return original(history, parent_head, branch_id)

    monkeypatch.setattr(branches, "branch_parent_snapshot", capture)
    parent = harness.branch.head_ref()
    branch = create_graph_fork(
        harness.service.history,
        harness.store,
        project_id=harness.project_id,
        branch_id=str(uuid.uuid4()),
        parent_head=parent,
        authorized_by=authorized_human(harness.app),
        created_by="human:request",
    )
    assert calls == [parent]
    assert branch.state() == harness.branch.state()


def test_nested_fork_matches_linear_replay_and_preserves_original_heads(history, tmp_path):
    first = history.create_branch(fork_metadata(history, history.head_ref()))
    first.append(_branch_patch("ev/ancestor"))
    source = first.head_ref()
    first.append(_branch_patch("ev/hidden"))
    child = history.create_branch(fork_metadata(history, source))
    child.append(_branch_patch("ev/child"))
    history.append(_branch_patch("ev/ancestor"))
    history.append(_branch_patch("ev/child"))
    assert child.state() == history.state()
    engine = history.history_engine
    engine.configure_store(tmp_path / "cache", "project")
    engine.checkpoint_interval = 1
    child_head = child.head_ref()
    steps = engine.steps(child_head, include_inherited=True)
    assert [step.after_head.target for step in steps] == [
        GraphTargetRef(),
        first.graph_target,
        child.graph_target,
    ]
    assert [step.inherited for step in steps] == [True, True, False]
    restarted = ProjectHistoryEngine(history)
    restarted.configure_store(tmp_path / "cache", "project")
    assert restarted.state_at(child_head) == child.state()
    assert "ev/hidden" not in child.state().nodes
    with pytest.raises(ValueError):
        engine.resolve(parse_head(f"{child.graph_target.key}@1"), default_target=GraphTargetRef())


def test_rejected_receipt_remains_an_exact_fork_point(history):
    previous = history.head_ref()
    rejected = _branch_patch("ev/rejected").model_copy(
        update={"revision": 2, "admission": "rejected"}
    )
    history._atomic_text(history.patches_dir / "000002.json", rejected.model_dump_json())
    source = history.head_ref()
    assert source.revision == 2 and source.transition_id == previous.transition_id
    branch = history.create_branch(fork_metadata(history, source))
    assert "ev/rejected" not in branch.state().nodes
    patch, result = branch.append(_branch_patch("ev/accepted"))
    assert patch.revision == result.state.revision == 3


def test_fork_refuses_missing_range_prebase_and_depth(history, monkeypatch):
    from rcp import limits

    metadata = fork_metadata(history, history.head_ref())
    for parent in [
        GraphHeadRef(revision=100),
        GraphHeadRef(target=GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4())), revision=1),
    ]:
        invalid = metadata.model_copy(
            update={
                "parent_head": parent,
                "head": parent.model_copy(update={"target": metadata.head.target}),
            }
        )
        with pytest.raises((ValueError, KeyError)):
            history.create_branch(invalid)
    first = history.create_branch(metadata)
    prebase = GraphHeadRef(target=first.graph_target, revision=0)
    invalid = metadata.model_copy(update={"branch_id": str(uuid.uuid4()), "parent_head": prebase})
    invalid = invalid.model_copy(
        update={
            "head": prebase.model_copy(
                update={"target": GraphTargetRef(kind="branch", branch_id=invalid.branch_id)}
            )
        }
    )
    with pytest.raises(ValueError):
        history.create_branch(invalid)
    monkeypatch.setattr(limits, "GRAPH_BRANCH_MAX_DEPTH", 1)
    with pytest.raises(ValueError):
        history.create_branch(fork_metadata(history, first.head_ref()))


def test_cycle_refused_before_replay(history):
    first = history.create_branch(fork_metadata(history, history.head_ref()))
    child = history.create_branch(fork_metadata(history, first.head_ref()))
    raw = json.loads((first.root / "branch.json").read_text())
    raw["parent_head"] = child.head_ref().model_dump(mode="json")
    (first.root / "branch.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        ProjectHistoryEngine(history).state_at(child.head_ref())


def test_old_writer_metadata_reads_without_rewriting(history):
    metadata = _branch_metadata(history)
    branch = history.create_branch(metadata)
    path = branch.root / "branch.json"
    raw = metadata.model_dump(mode="json", exclude={"parent_head", "created_by"})
    content = json.dumps(raw)
    path.write_text(content)
    reopened = history.branch(metadata.branch_id, initialize=False)
    assert reopened.branch_metadata().effective_parent_head == metadata.base_head
    assert reopened.state() == history.state()
    assert path.read_text() == content


@pytest.mark.parametrize("kind", ["fork", "auto_research", "experiment_loop"])
def test_fork_before_home_transfer_retains_current_authorizer(manifest, kind):
    from tests.test_project_home_transfer import (
        PROJECT_ID,
        SOURCE_SPACE_ID,
        TARGET_SPACE_ID,
        _actor,
    )

    history = HistoryManager(
        manifest, expected_space_id=SOURCE_SPACE_ID, write_authority=unchecked_write_authority()
    )
    history.claim_project_identity("created", project_id=PROJECT_ID)
    past = history.head_ref()
    authorizer = _actor(TARGET_SPACE_ID, "Current owner")
    history.transfer_project_home(
        project_id=PROJECT_ID,
        previous_home_space_id=SOURCE_SPACE_ID,
        new_home_space_id=TARGET_SPACE_ID,
        source_released_by=_actor(SOURCE_SPACE_ID, "Previous owner"),
        target_admitted_by=authorizer,
    )
    current = HistoryManager(
        manifest, expected_space_id=TARGET_SPACE_ID, write_authority=unchecked_write_authority()
    )
    metadata = fork_metadata(current, past).model_copy(
        update={"project_id": PROJECT_ID, "authorized_by": authorizer}
    )
    branch = current.create_branch(metadata)
    if kind != "fork":
        path = branch.root / "branch.json"
        path.write_text(
            metadata.model_copy(
                update={"kind": kind, "episode_id": str(uuid.uuid4())}
            ).model_dump_json()
        )
        with pytest.raises(ValueError):
            current.branch(metadata.branch_id, initialize=False)
        return
    assert branch.state().revision == past.revision
    assert branch.branch_metadata().authorized_by == authorizer
