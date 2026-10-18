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


def test_merge_delivery_is_bound_to_parent_across_crash_and_later_history(history):
    from rcp.core.models import BranchMergeProvenance, BranchMergeReceipt
    from rcp.history import BranchMergeAlreadyCommitted
    from rcp.history.branches import read_branch_snapshots
    from rcp.runs.branch_merge import branch_merge_id

    destination = history.create_branch(fork_metadata(history, history.head_ref()))
    destination.append(_branch_patch("ev/destination"))
    source = history.create_branch(fork_metadata(history, destination.head_ref()))
    source.append(_branch_patch("ev/source"))
    metadata = source.branch_metadata()
    provenance = BranchMergeProvenance(
        merge_id=branch_merge_id(metadata),
        branch_id=metadata.branch_id,
        episode_id=metadata.episode_id,
        branch_base_head=metadata.effective_parent_head,
        branch_head=metadata.head,
        rebased_main_head=destination.head_ref(),
        merge_task_id="fork-merge",
    )
    patch = _branch_patch("ev/source").model_copy(
        update={
            "profile": "orchestrator",
            "task_id": provenance.merge_task_id,
            "authorized_by": metadata.authorized_by,
            "branch_merge": provenance,
        }
    )
    committed, _ = destination.append(patch)
    destination.append(_branch_patch("ev/later"))
    # Crash before receipt publication: reads prove delivery without writing a receipt.
    snapshot = read_branch_snapshots(history, [(source.branch_id, None, "project")])[
        source.branch_id
    ]
    assert snapshot is not None and len(snapshot.receipts) == 1
    assert source.merge_receipts() == []
    with pytest.raises(BranchMergeAlreadyCommitted):
        destination.append(patch)
    receipt = source.reconcile_merge_receipt(
        provenance.merge_id, destination=destination.graph_target
    )
    assert receipt == snapshot.receipts[0]
    assert receipt.result_main_head.target == destination.graph_target
    assert receipt.result_main_head.revision == committed.revision
    # A later parent-to-main merge and a descendant inheriting the Patch cannot redirect delivery.
    parent_metadata = destination.branch_metadata()
    parent_merge = BranchMergeProvenance(
        merge_id=branch_merge_id(parent_metadata),
        branch_id=destination.branch_id,
        episode_id=None,
        branch_base_head=parent_metadata.effective_parent_head,
        branch_head=parent_metadata.head,
        rebased_main_head=history.head_ref(),
        merge_task_id="parent-merge",
    )
    history.append(
        _branch_patch("ev/source").model_copy(
            update={
                "profile": "orchestrator",
                "task_id": parent_merge.merge_task_id,
                "authorized_by": parent_metadata.authorized_by,
                "branch_merge": parent_merge,
            }
        )
    )
    descendant = history.create_branch(fork_metadata(history, destination.head_ref()))
    assert descendant.validated_merge_receipts() == []
    assert source.validated_merge_receipts() == [receipt]
    with pytest.raises(ValueError):
        source.reconcile_merge_receipt(provenance.merge_id, destination=history.graph_target)
    with pytest.raises(ValueError):
        history.append(patch)
    redirected = provenance.model_copy(
        update={
            "branch_base_head": metadata.base_head,
            "rebased_main_head": history.head_ref(),
        }
    )
    with pytest.raises(ValueError):
        history.append(patch.model_copy(update={"branch_merge": redirected}))
    invalid = receipt.model_dump(mode="json")
    invalid["result_main_head"]["target"] = {"kind": "main", "branch_id": None}
    with pytest.raises(ValueError):
        BranchMergeReceipt.model_validate(invalid)


def test_no_change_fork_receipt_suppresses_later_destination_append(history):
    from rcp.core.models import BranchMergeProvenance, BranchMergeReceipt
    from rcp.history import BranchMergeAlreadyResolved
    from rcp.runs.branch_merge import branch_merge_id

    destination = history.create_branch(fork_metadata(history, history.head_ref()))
    source = history.create_branch(fork_metadata(history, destination.head_ref()))
    metadata = source.branch_metadata()
    provenance = BranchMergeProvenance(
        merge_id=branch_merge_id(metadata),
        branch_id=metadata.branch_id,
        episode_id=metadata.episode_id,
        branch_base_head=metadata.effective_parent_head,
        branch_head=metadata.head,
        rebased_main_head=destination.head_ref(),
        merge_task_id="no-change-fork",
    )
    receipt = source.write_merge_receipt(
        BranchMergeReceipt(
            outcome="no_change",
            provenance=provenance,
            result_main_head=destination.head_ref(),
            authorized_by=metadata.authorized_by,
        )
    )
    destination.append(_branch_patch("ev/later"))
    patch = _branch_patch("ev/duplicate").model_copy(
        update={
            "profile": "orchestrator",
            "task_id": provenance.merge_task_id,
            "authorized_by": metadata.authorized_by,
            "branch_merge": provenance,
        }
    )
    with pytest.raises(BranchMergeAlreadyResolved) as rejected:
        destination.append(patch)
    assert rejected.value.receipt == receipt
    assert "ev/duplicate" not in destination.state().nodes


@pytest.mark.parametrize("parent_branch", [False, True])
def test_first_no_change_receipt_checks_destination_after_acquiring_append_lock(
    history, monkeypatch, parent_branch
):
    from contextlib import contextmanager

    from rcp.core.models import BranchMergeProvenance, BranchMergeReceipt
    from rcp.history import RevisionConflict
    from rcp.runs.branch_merge import branch_merge_id

    destination = (
        history.create_branch(fork_metadata(history, history.head_ref()))
        if parent_branch
        else history
    )
    source = history.create_branch(fork_metadata(history, destination.head_ref()))
    metadata = source.branch_metadata()
    expected = destination.head_ref()
    receipt = BranchMergeReceipt(
        outcome="no_change",
        provenance=BranchMergeProvenance(
            merge_id=branch_merge_id(metadata),
            branch_id=metadata.branch_id,
            episode_id=metadata.episode_id,
            branch_base_head=metadata.effective_parent_head,
            branch_head=metadata.head,
            rebased_main_head=expected,
            merge_task_id="no-change-race",
        ),
        result_main_head=expected,
        authorized_by=metadata.authorized_by,
    )
    original_lock = history._append_lock

    @contextmanager
    def moved_before_lock():
        monkeypatch.setattr(history, "_append_lock", original_lock)
        destination.append(_branch_patch("ev/concurrent"))
        with original_lock():
            yield

    monkeypatch.setattr(history, "_append_lock", moved_before_lock)
    # Main's public writer delegates to the same guarded source-branch writer.
    writer = source.write_merge_receipt if parent_branch else history.write_merge_receipt
    with pytest.raises(RevisionConflict):
        writer(receipt)
    assert source.merge_receipts() == []

    current = destination.head_ref()
    fresh = receipt.model_copy(
        update={
            "provenance": receipt.provenance.model_copy(update={"rebased_main_head": current}),
            "result_main_head": current,
        }
    )
    assert writer(fresh) == fresh
    retained_bytes = (source.merges_dir / f"{fresh.provenance.merge_id}.json").read_bytes()
    destination.append(_branch_patch("ev/later"))
    assert writer(fresh) == fresh
    assert source.validated_merge_receipts() == [fresh]
    assert (source.merges_dir / f"{fresh.provenance.merge_id}.json").read_bytes() == retained_bytes


@pytest.mark.parametrize("parent_branch", [False, True])
@pytest.mark.parametrize("revoked", ["membership", "task"])
def test_no_change_dry_preparation_preserves_head_and_checks_live_authority(
    history, parent_branch, revoked
):
    from datetime import UTC, datetime

    from rcp.core.authority import (
        AgentAuthorizerDeparted,
        AgentDispatchAuthority,
        AgentDispatchScope,
        AgentTaskAuthority,
        AgentTaskAuthorityRefused,
        WriteAuthority,
    )
    from rcp.runs.branch_merge import (
        BranchMergeContext,
        BranchMergeEligibility,
        merge_planning_context,
    )
    from rcp.runs.merge_contract import MergeAnswers, MergeRefused
    from rcp.runs.merge_patch import build_merge_patch, prepare_merge_candidate
    from rcp.runs.merge_plan import plan_merge

    destination = (
        history.create_branch(fork_metadata(history, history.head_ref()))
        if parent_branch
        else history
    )
    source = history.create_branch(fork_metadata(history, destination.head_ref()))
    metadata = source.branch_metadata()
    state = destination.state()
    head = destination.head_ref()
    task = AgentTaskAuthority(
        operation_id="empty-merge",
        project_id=metadata.project_id,
        apply_target=destination.graph_target,
        authorized_by=metadata.authorized_by,
        dispatch_authority=AgentDispatchAuthority(
            profile="orchestrator",
            task_contract="orchestrate",
            scope=AgentDispatchScope(run_truth_scope=state.project_truth_scope, patch_kind="work"),
        ),
        merge_source=source.graph_target,
        merge_dispatcher="member",
    )
    live = {"membership": True, "task": True}

    def resolve_task(_project_id, operation_id):
        if not live["task"]:
            raise KeyError(operation_id)
        return task

    history.project_id = metadata.project_id
    history.write_authority = WriteAuthority(
        agent_authority_resolver=resolve_task,
        project_membership_check=lambda _project_id, _user_id: live["membership"],
    )
    context = BranchMergeContext.create(
        merge_task_id=task.operation_id,
        authorized_by=metadata.authorized_by,
        metadata=metadata,
        eligibility=BranchMergeEligibility(branch_head=metadata.head),
        base_graph=source.base_state(),
        branch_graph=source.state(),
        main_head=head,
        main_graph=state,
        run_truth_scope=state.project_truth_scope,
        member_merge=True,
    )
    plan = plan_merge(merge_planning_context(context, destination, created_at=datetime.now(UTC)))
    answers = MergeAnswers(answers={}, cards={})
    candidate = build_merge_patch(plan, answers)
    retained = {path: path.read_bytes() for path in history.root.rglob("*.json") if path.is_file()}

    validated, report, current = destination.validate_candidate(candidate)
    assert not report.rejected
    assert validated.ops == [] and validated.transition is None
    assert current == state
    prepared = prepare_merge_candidate(plan, answers, destination)
    assert prepared.patch.ops == [] and prepared.patch.transition is None
    assert prepared.projection.graph == state
    assert prepared.projection.head == head
    assert prepared.projection.base_head == head
    assert prepared.projection.canonical is False

    live[revoked] = False
    refusal = AgentAuthorizerDeparted if revoked == "membership" else AgentTaskAuthorityRefused
    with pytest.raises(refusal):
        destination.validate_candidate(candidate)
    with pytest.raises(MergeRefused) as rejected:
        prepare_merge_candidate(plan, answers, destination)
    assert rejected.value.diagnostics[0].code == refusal.code
    assert destination.head_ref() == head
    assert source.merge_receipts() == []
    assert {
        path: path.read_bytes() for path in history.root.rglob("*.json") if path.is_file()
    } == retained


def test_interrupted_fork_initialization_retries_with_leftover_directory(history, monkeypatch):
    from rcp.history.branches import BranchHistoryManager

    metadata = fork_metadata(history, history.head_ref())
    interrupted_roots = []

    def interrupt(branch, _metadata):
        interrupted_roots.append(branch.root)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(BranchHistoryManager, "_write_metadata", interrupt)
        with pytest.raises(KeyboardInterrupt):
            history.create_branch(metadata)
    leftover = interrupted_roots[0]
    assert leftover.is_dir()
    assert not (leftover / "branch.json").exists()

    branch = history.create_branch(metadata)
    assert branch.root != leftover
    assert branch.branch_metadata() == metadata
    assert branch.state() == history.state()
    assert branch.patches_dir.is_dir() and branch.merges_dir.is_dir()
    assert branch._outputs_coherent(branch.materialize(write_outputs=False))
    assert history.create_branch(metadata).head_ref() == branch.head_ref()
