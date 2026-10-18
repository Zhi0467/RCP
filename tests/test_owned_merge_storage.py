from __future__ import annotations

import uuid

import pytest

from rcp.core.transition_models import GraphTargetRef
from rcp.runs.branch_merge_request import BranchMergeRunRequest, MergeBinding
from rcp.runs.graph_forks import create_graph_fork
from rcp.storage import AgentTaskRecord

from .test_branch_merge_api import _create_branch_harness, _orchestrator_authority


def _owned_merge(manifest, tmp_path, *, requires_agent=True):
    harness = _create_branch_harness(manifest, tmp_path, change="none", ended=False)
    store, episode = harness.store, harness.episode
    assert episode.authorized_by is not None
    assert store.mark_agent_task_running(harness.root.operation_id)
    fork = create_graph_fork(
        harness.service.history,
        store,
        project_id=harness.project_id,
        branch_id=str(uuid.uuid4()),
        parent_head=harness.branch.head_ref(),
        authorized_by=episode.authorized_by,
        created_by=f"orchestrator:{harness.root.operation_id}",
        owner_episode_id=episode.episode_id,
    )
    binding = MergeBinding(
        source=fork.graph_target,
        source_head=fork.head_ref(),
        destination=episode.graph_target,
        dispatcher_episode_id=episode.episode_id,
        dispatcher_operation_id=harness.root.operation_id,
        command_key="merge-test",
        requires_agent=requires_agent,
    )
    request = BranchMergeRunRequest(
        episode_id=episode.episode_id,
        merge_binding=binding,
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo-a"],
        mode="work",
        chat_scope="project",
    )
    task = AgentTaskRecord(
        operation_id=str(uuid.uuid4()),
        project_id=harness.project_id,
        episode_id=episode.episode_id,
        graph_target=fork.graph_target,
        kind="branch_merge",
        status="queued",
        request=request.model_dump(mode="json"),
        created_at=store.now(),
        updated_at=store.now(),
        status_message="Queued",
        authorized_by=episode.authorized_by,
        dispatch_authority=_orchestrator_authority(episode.episode_id),
    )
    return harness, task


@pytest.mark.parametrize("requires_agent", [False, True])
def test_owned_merge_admission_reserves_one_allocation_with_binding(
    manifest, tmp_path, requires_agent
):
    harness, task = _owned_merge(manifest, tmp_path, requires_agent=requires_agent)
    store = harness.store
    before = store.episode_budget_meter(harness.episode.episode_id).invocations_used
    admitted = store.create_branch_merge_task(task)
    assert store.create_branch_merge_task(task) == admitted
    assert store.episode_budget_meter(harness.episode.episode_id).invocations_used == before + int(
        requires_agent
    )
    allocations = store.episode_invocations(harness.episode.episode_id)
    assert sum(item.operation_id == task.operation_id for item in allocations) == int(
        requires_agent
    )
    assert store.agent_task_authority(task.project_id, task.operation_id).apply_target == (
        harness.episode.graph_target
    )
    if not requires_agent:
        upgraded = store.reserve_branch_merge_invocation(task.operation_id)
        assert store.reserve_branch_merge_invocation(task.operation_id) == upgraded
        assert store.episode_budget_meter(harness.episode.episode_id).invocations_used == before + 1
    store.set_branch_archived(task.project_id, harness.branch.branch_id, archived=True)
    with pytest.raises(ValueError, match="merge_destination_archived"):
        store.agent_task_authority(task.project_id, task.operation_id)


def test_owned_merge_exhausted_budget_rolls_back_task_and_intent(manifest, tmp_path):
    harness, task = _owned_merge(manifest, tmp_path)
    store = harness.store
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET invocations_used=invocation_ceiling WHERE episode_id=?",
            (task.episode_id,),
        )
    with pytest.raises(ValueError, match="merge_budget_exhausted"):
        store.create_branch_merge_task(task)
    assert store.agent_task(task.operation_id) is None
    assert all(
        row.operation_id != task.operation_id
        for row in store.episode_invocations(harness.episode.episode_id)
    )


@pytest.mark.parametrize("refusal", ["unowned", "archived", "main", "dispatcher"])
def test_owned_merge_admission_refuses_invalid_destination_or_dispatcher(
    manifest, tmp_path, refusal
):
    harness, task = _owned_merge(manifest, tmp_path)
    store = harness.store
    expected = {
        "unowned": "merge_branch_not_owned",
        "archived": "merge_destination_archived",
        "main": "merge_destination_not_source",
        "dispatcher": "merge_requires_owning_orchestrator",
    }[refusal]
    if refusal == "unowned":
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_branches SET owner_episode_id=NULL WHERE branch_id=?",
                (task.graph_target.branch_id,),
            )
    elif refusal == "archived":
        store.set_branch_archived(task.project_id, harness.branch.branch_id, archived=True)
    else:
        request = BranchMergeRunRequest.model_validate(task.request)
        binding = request.merge_binding
        assert binding is not None
        change = (
            {"destination": GraphTargetRef()}
            if refusal == "main"
            else {"dispatcher_operation_id": str(uuid.uuid4())}
        )
        task = task.model_copy(
            update={
                "request": request.model_copy(
                    update={"merge_binding": binding.model_copy(update=change)}
                ).model_dump(mode="json")
            }
        )
    with pytest.raises(ValueError, match=expected):
        store.create_branch_merge_task(task)
    assert store.agent_task(task.operation_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("residue", [False, True])
async def test_owned_merge_delivers_to_parent_and_reuses_key(
    manifest, tmp_path, monkeypatch, residue
):
    import asyncio
    import json

    from rcp.agents.hidden_read import host_facts
    from rcp.core.models import Patch
    from rcp.history.manager import HistoryManager
    from rcp.runs.branch_merge_admission import start_owned_branch_merge

    from .helpers import unchecked_write_authority, wait_for_task
    from .test_branch_merge_api import _PatchWritingLauncher

    monkeypatch.setattr("rcp.rcp_home.rcp_home", lambda: tmp_path / "rcp-home")
    monkeypatch.setattr(
        "rcp.agents.hidden_read.host_facts",
        lambda paths: {**host_facts(paths), "home": str(tmp_path / "execution-home")},
    )
    harness, fixture_task = _owned_merge(manifest, tmp_path)
    history = HistoryManager(
        harness.service.manifest,
        harness.service.history.workspace,
        write_authority=unchecked_write_authority(),
    )
    source = history.branch(fixture_task.graph_target.branch_id or "")
    ops = [
        {
            "op": "update_nodes",
            "nodes": [{"id": "blk/merge-ready", "changes": {"title": "Fork choice"}}],
        }
    ]
    source.append(
        Patch.model_validate(
            dict(
                kind="work",
                author="agent",
                producer="agent",
                summary="Fork change",
                run_truth_scope=["repo-a"],
                ops=ops,
            )
        )
    )
    if residue:
        history.branch(harness.branch.branch_id).append(
            Patch.model_validate(
                dict(
                    kind="work",
                    author="agent",
                    producer="agent",
                    summary="Parent change",
                    run_truth_scope=["repo-a"],
                    ops=[
                        {
                            "op": "update_nodes",
                            "nodes": [
                                {"id": "blk/merge-ready", "changes": {"title": "Parent choice"}}
                            ],
                        }
                    ],
                )
            )
        )
    launcher = _PatchWritingLauncher(json.dumps({"summary": "Resolve fork", "ops": ops}))
    monkeypatch.setattr(harness.app.state.launcher, "stream", launcher.stream)
    # This test exercises merge dispatch and Apply; delivery wakes have independent coverage.
    monkeypatch.setattr(harness.app.state.background_tasks, "on_task_settled", None)
    before = harness.store.episode_budget_meter(harness.episode.episode_id).invocations_used
    result = await start_owned_branch_merge(
        harness.app.state.background_tasks,
        harness.service,
        episode=harness.episode,
        dispatcher_operation_id=harness.root.operation_id,
        branch_id=source.branch_id,
        command_key="deliver",
    )
    operation_id = result["operation_id"]
    assert isinstance(operation_id, str)
    task = await asyncio.to_thread(wait_for_task, harness.store, operation_id, expect="succeeded")
    replay = await start_owned_branch_merge(
        harness.app.state.background_tasks,
        harness.service,
        episode=harness.episode,
        dispatcher_operation_id=harness.root.operation_id,
        branch_id=source.branch_id,
        command_key="deliver",
    )
    assert replay["operation_id"] == task.operation_id
    assert launcher.calls == int(residue)
    assert harness.store.episode_budget_meter(
        harness.episode.episode_id
    ).invocations_used == before + int(residue)
    assert harness.branch.state().nodes["blk/merge-ready"].title == "Fork choice"
    assert harness.service.history.state().nodes["blk/merge-ready"].title == "Merge gate"
    receipt = source.reconcile_merge_receipt(source.merge_receipts()[0].provenance.merge_id)
    assert receipt is not None
    assert receipt.result_main_head.target == harness.branch.graph_target


@pytest.mark.parametrize("paused_source_writer", [False, True])
def test_interrupted_merge_keeps_allocation_and_fences_wrapup(
    manifest, tmp_path, paused_source_writer
):
    from rcp.core.graph_targets import graph_target_json

    harness, task = _owned_merge(manifest, tmp_path)
    store = harness.store
    store.create_branch_merge_task(task)
    store.complete_agent_task(harness.root.operation_id, applied_revision=None, result={})
    assert not store.auto_research_is_quiescent(harness.episode.episode_id)
    store.fail_agent_task(task.operation_id, "Process restarted", status="interrupted")
    assert not store.auto_research_is_quiescent(harness.episode.episode_id)
    if paused_source_writer:
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status='paused', graph_target_json=? WHERE operation_id=?",
                (graph_target_json(task.graph_target), harness.root.operation_id),
            )
    recovered = store.requeue_owned_merge_task(task.operation_id)
    assert recovered.attempt == task.attempt + 1
    assert recovered.status == "queued"
    assert len(store.episode_invocations(harness.episode.episode_id)) == 2
    assert store.episode_budget_meter(harness.episode.episode_id).invocations_used == 2
    store.complete_agent_task(task.operation_id, applied_revision=None, result={})
    if paused_source_writer:
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status='succeeded', graph_target_json=? WHERE operation_id=?",
                (graph_target_json(harness.root.graph_target), harness.root.operation_id),
            )
    assert store.auto_research_is_quiescent(harness.episode.episode_id)


def test_owned_merge_refuses_live_source_writer(manifest, tmp_path):
    from rcp.core.graph_targets import graph_target_json

    harness, task = _owned_merge(manifest, tmp_path)
    # Give the existing live orchestrator the fork as its target; admission must
    # fence any graph writer independently of its role or owning episode.
    with harness.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET graph_target_json=? WHERE operation_id=?",
            (graph_target_json(task.graph_target), harness.root.operation_id),
        )
    with pytest.raises(ValueError):
        harness.store.create_branch_merge_task(task)
    assert harness.store.agent_task(task.operation_id) is None
    assert harness.store.episode_budget_meter(harness.episode.episode_id).invocations_used == 1


@pytest.mark.asyncio
async def test_committed_merge_recovers_after_destination_archived(manifest, tmp_path, monkeypatch):
    from rcp.core.models import Patch
    from rcp.history.branches import BranchHistoryManager
    from rcp.history.manager import HistoryManager
    from rcp.runs.branch_merge_admission import start_owned_branch_merge

    from .helpers import unchecked_write_authority

    harness, task = _owned_merge(manifest, tmp_path)
    branch_id = task.graph_target.branch_id
    assert branch_id is not None
    history = HistoryManager(
        harness.service.manifest,
        harness.service.history.workspace,
        write_authority=unchecked_write_authority(),
    )
    history.branch(branch_id).append(
        Patch.model_validate(
            dict(
                kind="work",
                author="agent",
                producer="agent",
                summary="Fork change",
                run_truth_scope=["repo-a"],
                ops=[
                    {
                        "op": "update_nodes",
                        "nodes": [{"id": "blk/merge-ready", "changes": {"title": "Recovered"}}],
                    }
                ],
            )
        )
    )
    original = BranchHistoryManager.write_merge_receipt

    def crash(self, receipt):
        raise OSError("Injected crash after commit")

    monkeypatch.setattr(BranchHistoryManager, "write_merge_receipt", crash)
    tasks = harness.app.state.background_tasks
    monkeypatch.setattr(tasks, "on_task_settled", None)

    async def merge():
        return await start_owned_branch_merge(
            tasks,
            harness.service,
            episode=harness.episode,
            dispatcher_operation_id=harness.root.operation_id,
            branch_id=branch_id,
            command_key="crash",
        )

    with pytest.raises(OSError):
        await merge()
    operation_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"rcp:merge:{harness.root.operation_id}:crash")
    )
    harness.store.fail_agent_task(operation_id, "Process restarted", status="interrupted")
    committed_head = harness.branch.head_ref()
    harness.store.set_branch_archived(harness.project_id, harness.branch.branch_id, archived=True)
    monkeypatch.setattr(BranchHistoryManager, "write_merge_receipt", original)
    result = await merge()
    assert result["status"] == "succeeded"
    assert harness.branch.head_ref() == committed_head
    assert harness.branch.state().nodes["blk/merge-ready"].title == "Recovered"
    assert harness.store.episode_budget_meter(harness.episode.episode_id).invocations_used == 1
