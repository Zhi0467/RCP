from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from rcp.core.models import Patch
from rcp.core.validation import validate_patch
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


@pytest.mark.parametrize("sourced_branch", ["node", "proposal"], indirect=True)
@pytest.mark.parametrize("removed_from_main", [False, True])
def test_merge_provenance_uses_current_project_membership(
    sourced_branch, monkeypatch, removed_from_main
):
    harness, operation, branch_patch = sourced_branch
    assert harness.service.manifest.agent.default_run_truth_scope == ["repo-a"]
    if removed_from_main:
        _remove_source_repository(harness)
    main_before = harness.service.history.head_ref()
    launcher = merge_api._PatchWritingLauncher(
        json.dumps({"summary": "Carry source provenance.", "ops": [operation]})
    )
    monkeypatch.setattr(harness.app.state.launcher, "stream", launcher.stream)

    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge"
    )
    assert response.status_code == 202, response.text
    (admitted,) = [
        task
        for task in harness.store.agent_tasks(harness.project_id)
        if task.kind == "branch_merge"
    ]
    operation_id = admitted.operation_id
    task = wait_for_task(
        harness.store, operation_id, expect="failed" if removed_from_main else "succeeded"
    )

    main = harness.service.history.state()
    if removed_from_main:
        assert launcher.calls == 0
        assert harness.service.history.head_ref() == main_before
        assert task.stage_root is not None
        diagnostics = [task.error] + [
            json.loads(path.read_text())["problem"]
            for path in Path(task.stage_root).rglob("*-branch-merge-correction-*.json")
        ]
        assert any(
            "Source reference uses 'repo-b' outside this run scope" in d for d in diagnostics
        )
        assert harness.branch.merge_receipts() == []
    else:
        merged = harness.service.history.load_patches()[-1]
        assert merged.run_truth_scope == ["repo-a", "repo-b"]
        assert merged.repositories_read == []
        if operation["op"] == "create_nodes":
            assert main.nodes["ev/non-default-source"].source_refs[0].truth_repository == "repo-b"
        else:
            proposal = main.proposals["prop/non-default-source"]
            assert proposal.status == "pending"
            assert (
                proposal.ops[0].nodes[0].changes["source_refs"][0]["truth_repository"] == "repo-b"
            )
            assert main.nodes["rq/scope"].question == "Original question?"
        assert harness.branch.merge_receipts()[-1].outcome == "committed"

    # Ordinary Work keeps its selected run scope even though the project is wider.
    report = validate_patch(
        harness.branch.base_state(),
        branch_patch.model_copy(update={"run_truth_scope": ["repo-a"]}),
        ["repo-a", "repo-b"],
    )
    assert "source-outside-run-scope" in {message.code for message in report.messages}


@pytest.mark.parametrize("sourced_branch", ["proposal"], indirect=True)
@pytest.mark.parametrize("before_launch", [False, True])
def test_merge_fails_closed_when_main_membership_changes_after_dispatch(
    sourced_branch, monkeypatch, before_launch
):
    harness, operation, _branch_patch = sourced_branch
    main_before = harness.service.history.head_ref()
    launcher = merge_api._PatchWritingLauncher(
        json.dumps({"summary": "Carry source provenance.", "ops": [operation]})
    )

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
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge"
    )
    assert response.status_code == 202, response.text
    # A preflight refusal can settle before the response projection is returned.
    (admitted,) = [
        task
        for task in harness.store.agent_tasks(harness.project_id)
        if task.kind == "branch_merge"
    ]
    task = wait_for_task(harness.store, admitted.operation_id, expect="failed")
    assert "Project truth membership changed" in task.error
    assert launcher.calls == (0 if before_launch else 1)
    main = harness.service.history.state()
    assert main.revision == main_before.revision + 1
    assert main.project_truth_scope == ["repo-a"]
    assert "prop/non-default-source" not in main.proposals
    assert harness.branch.merge_receipts() == []
