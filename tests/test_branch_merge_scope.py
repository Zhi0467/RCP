from __future__ import annotations

import uuid

import pytest

from rcp.core.models import Patch
from rcp.core.validation import ValidationContext, validate_patch
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord

from . import test_branch_merge_api as merge_api
from .helpers import append_fixture_patch, authorized_human, wait_for_task


@pytest.fixture
def sourced_branch(manifest, tmp_path, monkeypatch, request):
    seed = merge_api._main_fixture_patch().model_dump(mode="python")
    seed["ops"][0]["nodes"].append(
        {
            "id": "rq/scope",
            "type": "research_question",
            "title": "Scope question",
            "question": "Original question?",
        }
    )
    monkeypatch.setattr(merge_api, "_main_fixture_patch", lambda: Patch.model_validate(seed))
    harness = merge_api._create_branch_harness(manifest, tmp_path, change="none")
    work_request = RunRequest(
        mode="work",
        chat_scope="project",
        chat_id=str(uuid.uuid4()),
        message="Research the non-default repository on this branch.",
        run_truth_scope=["repo-b"],
    )
    now = harness.store.now()
    work = harness.store.create_agent_task(
        AgentTaskRecord(
            operation_id=str(uuid.uuid4()),
            project_id=harness.project_id,
            graph_target=harness.episode.graph_target,
            kind="project_chat",
            status="queued",
            request=work_request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="Queued custom-scoped branch Work.",
            authorized_by=authorized_human(harness.app),
            dispatch_authority=resolve_dispatch_authority("project_chat", work_request),
        )
    )
    source_ref = {
        "machine": "laptop",
        "truth_repository": "repo-b",
        "source": "codex",
        "session_id": "non-default-repository-session",
        "record_uuid": "non-default-repository-record",
        "timestamp": harness.store.now(),
        "excerpt": "Research from the explicitly selected non-default repository.",
    }
    if request.param == "node":
        operation = {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "ev/non-default-source",
                    "type": "evidence",
                    "title": "Non-default repository result",
                    "observation": "The branch recorded this result.",
                    "origin": "internal_run",
                    "source_refs": [source_ref],
                }
            ],
        }
    else:
        operation = {
            "op": "create_proposals",
            "proposals": [
                {
                    "id": "prop/non-default-source",
                    "title": "Review the sourced question",
                    "card": {
                        "situation_cold": "The branch found new research context.",
                        "why_human_now": "The existing question needs human review.",
                        "consequences": "Approval records the question and its source.",
                        "decision_needed": "Approve or reject the revised question.",
                    },
                    "ops": [
                        {
                            "op": "update_nodes",
                            "intent": "content_change",
                            "nodes": [
                                {
                                    "id": "rq/scope",
                                    "changes": {
                                        "question": "Revised question?",
                                        "source_refs": [source_ref],
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    branch_patch = Patch(
        kind="work",
        author="agent",
        summary="Record research from a custom repository selection.",
        run_truth_scope=["repo-b"],
        repositories_read=["repo-b"],
        source_operation_id=work.operation_id,
        ops=[operation],
    )
    harness.branch.append(branch_patch)
    harness.store.complete_agent_task(work.operation_id, applied_revision=None, result={})
    return harness, operation, branch_patch


def _remove_source_repository(harness):
    append_fixture_patch(
        harness.service,
        Patch(
            kind="approval",
            author="human",
            summary="Remove repo-b from current project truth membership.",
            ops=[{"op": "set_project_truth_scope", "truth_scope": ["repo-a"]}],
        ),
        authorized_by=authorized_human(harness.app),
    )


def _plan(harness, operation):
    from rcp.runs.branch_merge import BranchMergeContext, BranchMergeEligibility
    from rcp.runs.branch_merge_admission import start_branch_merge
    from rcp.runs.branch_merge_request import BranchMergeRunRequest
    from tests.test_merge_patch import MISSING, change, plan_from_context

    history = harness.service.history
    scope = history.state().project_truth_scope
    request = BranchMergeRunRequest(
        episode_id=harness.episode.episode_id,
        provider="codex",
        model="gpt-5",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=scope,
        chat_scope="project",
        mode="work",
        trigger="human",
        patch_kind="work",
    )
    task = start_branch_merge(
        harness.app.state.background_tasks,
        harness.project_id,
        request,
        authorized_by=authorized_human(harness.app),
        launch=False,
    )
    context = BranchMergeContext.create(
        merge_task_id=task.operation_id,
        authorized_by=task.authorized_by,
        metadata=harness.branch.branch_metadata(),
        eligibility=BranchMergeEligibility(branch_head=harness.branch.head_ref()),
        base_graph=harness.branch.base_state(),
        branch_graph=harness.branch.state(),
        main_head=history.head_ref(),
        main_graph=history.state(),
        run_truth_scope=scope,
    )
    collection = "nodes" if operation["op"] == "create_nodes" else "proposals"
    entity_id = operation[collection][0]["id"]
    return plan_from_context(
        context,
        authority=history.write_authority,
        template_episode_id=harness.episode.episode_id,
        direct_changes=(
            change(
                collection,
                entity_id,
                None,
                MISSING,
                getattr(harness.branch.state(), collection)[entity_id],
                MISSING,
            ),
        ),
    )


@pytest.mark.parametrize("sourced_branch", ["node", "proposal"], indirect=True)
@pytest.mark.parametrize("removed_from_main", [False, True])
def test_merge_provenance_uses_current_project_membership(sourced_branch, removed_from_main):
    from rcp.runs.merge_contract import MergeRefused
    from rcp.runs.merge_patch import prepare_merge_candidate
    from tests.test_merge_patch import answers

    harness, operation, branch_patch = sourced_branch
    assert harness.service.manifest.agent.default_run_truth_scope == ["repo-a"]
    if removed_from_main:
        _remove_source_repository(harness)
    plan = _plan(harness, operation)
    before = harness.service.history.head_ref()

    if removed_from_main:
        with pytest.raises(MergeRefused) as failure:
            prepare_merge_candidate(plan, answers(plan), harness.service.history)
        assert failure.value.diagnostics
        assert harness.service.history.head_ref() == before
    else:
        prepared = prepare_merge_candidate(plan, answers(plan), harness.service.history)
        assert prepared.patch.run_truth_scope == ["repo-a", "repo-b"]
        assert prepared.patch.repositories_read == []
        main = prepared.projection.graph
        if operation["op"] == "create_nodes":
            assert main.nodes["ev/non-default-source"].source_refs[0].truth_repository == "repo-b"
        else:
            proposal = main.proposals["prop/non-default-source"]
            assert proposal.status == "pending"
            assert (
                proposal.ops[0].nodes[0].changes["source_refs"][0]["truth_repository"] == "repo-b"
            )
            assert main.nodes["rq/scope"].question == "Original question?"
        assert harness.service.history.head_ref() == before
    assert harness.branch.merge_receipts() == []

    # A run that writes only repo-a may still cite any project repository it read.
    report = validate_patch(
        harness.branch.base_state(),
        branch_patch.model_copy(update={"run_truth_scope": ["repo-a"]}),
        ValidationContext(["repo-a", "repo-b"]),
    )
    assert "source-outside-run-scope" not in {message.code for message in report.messages}


@pytest.mark.parametrize("sourced_branch", ["node", "proposal"], indirect=True)
def test_merge_fails_closed_when_membership_changes_after_plan(sourced_branch):
    from rcp.history import RevisionConflict
    from rcp.runs.merge_patch import prepare_merge_candidate
    from tests.test_merge_patch import answers

    harness, operation, _branch_patch = sourced_branch
    plan = _plan(harness, operation)
    _remove_source_repository(harness)
    before = harness.service.history.head_ref()

    with pytest.raises(RevisionConflict):
        prepare_merge_candidate(plan, answers(plan), harness.service.history)

    main = harness.service.history.state()
    assert main.project_truth_scope == ["repo-a"]
    assert "prop/non-default-source" not in main.proposals
    assert "ev/non-default-source" not in main.nodes
    assert harness.service.history.head_ref() == before
    assert harness.branch.merge_receipts() == []


@pytest.mark.parametrize("sourced_branch", ["proposal"], indirect=True)
@pytest.mark.parametrize("before_launch", [False, True])
def test_merge_fails_closed_when_main_membership_changes_after_dispatch(
    sourced_branch, monkeypatch, before_launch
):
    harness, _operation, _branch_patch = sourced_branch
    # A real conflict requires the checked agent turn whose live scope we exercise.
    for destination, title in ((harness.branch, "Branch title"), (None, "Main title")):
        state = (destination or harness.service.history).state()
        patch = Patch(
            kind="approval",
            author="human",
            summary="Choose a branch-specific title.",
            ops=[
                {
                    "op": "update_nodes",
                    "nodes": [
                        {
                            "id": "blk/merge-ready",
                            "base_updated_rev": state.nodes["blk/merge-ready"].updated_rev,
                            "changes": {"title": title},
                        }
                    ],
                }
            ],
        )
        if destination is not None:
            destination.append(patch, authorized_by=authorized_human(harness.app))
        else:
            append_fixture_patch(
                harness.service, patch, authorized_by=authorized_human(harness.app)
            )
    main_before = harness.service.history.head_ref()
    launcher = merge_api._AnswersWritingLauncher()

    async def remove_repository_during_turn(*args, **kwargs):
        _remove_source_repository(harness)
        async for event in launcher.stream(*args, **kwargs):
            yield event

    if before_launch:
        tasks = harness.app.state.background_tasks
        spawn_record = tasks._spawn_record

        def remove_repository_before_launch(*args, **kwargs):
            _remove_source_repository(harness)
            return spawn_record(*args, **kwargs)

        monkeypatch.setattr(tasks, "_spawn_record", remove_repository_before_launch)
        monkeypatch.setattr(harness.app.state.launcher, "stream", launcher.stream)
    else:
        monkeypatch.setattr(harness.app.state.launcher, "stream", remove_repository_during_turn)
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge", json={}
    )
    assert response.status_code == 202, response.text
    # A preflight refusal can settle before the response projection is returned.
    (admitted,) = [
        task
        for task in harness.store.agent_tasks(harness.project_id)
        if task.kind == "branch_merge"
    ]
    task = wait_for_task(harness.store, admitted.operation_id, expect="failed")
    assert task.error
    assert launcher.calls == (0 if before_launch else 1)
    main = harness.service.history.state()
    assert main.revision == main_before.revision + 1
    assert main.project_truth_scope == ["repo-a"]
    assert "prop/non-default-source" not in main.proposals
    assert harness.branch.merge_receipts() == []
