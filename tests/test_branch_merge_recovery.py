from __future__ import annotations

import pytest

from rcp.core.models import BranchMergeReceipt
from rcp.runs.branch_merge import branch_merge_provenance
from rcp.runs.branch_merge_admission import recover_branch_merge_task
from rcp.runs.branch_merge_context import load_branch_merge_context
from rcp.runs.branch_merge_request import BranchMergeRunRequest
from tests.test_owned_merge_storage import _owned_merge


@pytest.mark.parametrize("no_change_receipt", [False, True])
def test_failed_bound_merge_never_retries_without_a_destination_patch(
    manifest, tmp_path, monkeypatch, no_change_receipt
):
    harness, task = _owned_merge(manifest, tmp_path)
    store = harness.store
    store.create_branch_merge_task(task)
    binding = BranchMergeRunRequest.model_validate(task.request).merge_binding
    assert binding is not None and task.authorized_by is not None
    assert binding.source.branch_id is not None
    if no_change_receipt:
        context = load_branch_merge_context(
            harness.service,
            store,
            harness.episode,
            operation_id=task.operation_id,
            authorized_by=task.authorized_by,
            merge_binding=binding,
        )
        harness.service.history.branch(binding.source.branch_id).write_merge_receipt(
            BranchMergeReceipt(
                outcome="no_change",
                provenance=branch_merge_provenance(context),
                result_main_head=context.main_head,
                authorized_by=task.authorized_by,
            )
        )
    store.fail_agent_task(task.operation_id, "Semantic merge failure")
    failed = store.agent_task(task.operation_id)
    assert failed is not None
    meter = store.episode_budget_meter(harness.episode.episode_id)
    monkeypatch.setattr(
        harness.app.state.background_tasks,
        "launch_admitted",
        lambda *_args: pytest.fail("Failed merge launched without canonical delivery proof"),
    )
    result = recover_branch_merge_task(harness.app.state.background_tasks, harness.service, failed)
    assert result["status"] == "failed"
    assert store.agent_task(task.operation_id) == failed
    assert store.episode_budget_meter(harness.episode.episode_id) == meter
