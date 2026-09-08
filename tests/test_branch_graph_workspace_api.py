from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from rcp.core.models import Patch

from .test_branch_chats import _app_branch
from .test_branch_merge_api import _admit_held_merge_task, _create_branch_harness

QUESTION = "rq/learning-after-shift"
HYPOTHESIS = "hyp/replanning-restores-plasticity"


def test_branch_graph_workspace_sync_isolated_with_delta_and_human_history(manifest, tmp_path):
    app, main, episode, _root = _app_branch(manifest, tmp_path)
    client = TestClient(app)
    base = f"/api/projects/{episode.project_id}"
    params = {"branch_id": episode.episode_id}
    main_before = main.history.state()
    opened = client.get(base, params=params)
    assert opened.status_code == 200, opened.text
    snapshot = opened.json()
    assert snapshot["graph_target"] == episode.graph_target.model_dump(mode="json")
    assert snapshot["graph_head"] == snapshot["graph_changes"]["head"]
    assert snapshot["graph_changes"]["nodes"] == []
    assert snapshot["graph_mutation"]["available"] is True
    draft = {
        "base_revision": snapshot["revision"],
        "nodes": [
            {
                "node_id": QUESTION,
                "base_updated_rev": main_before.nodes[QUESTION].updated_rev,
                "changes": {"question": "Can adaptation remain effective after repeated shifts?"},
                "standing": "accepted",
            }
        ],
    }
    preview = client.post(f"{base}/sync/preview", params=params, json=draft)
    assert preview.status_code == 200, preview.text
    assert preview.json()["projection"]["head"]["target"] == snapshot["graph_target"]
    assert main.history.state() == main_before
    synced = client.post(f"{base}/sync", params=params, json=draft)
    assert synced.status_code == 200, synced.text
    assert synced.json()["head"]["target"] == snapshot["graph_target"]
    assert main.history.state() == main_before
    after = client.get(base, params=params).json()
    change = after["graph_changes"]["nodes"][0]
    assert change["node_id"] == QUESTION
    assert change["before"]["question"] == main_before.nodes[QUESTION].question
    assert change["after"]["standing"] == "accepted"
    assert change["history"][0]["producer"] == "human"
    assert change["history"][0]["episode_id"] is None
    assert set(after["graph_changes"]["context_node_ids"]) == {QUESTION, HYPOTHESIS}
    assert client.get(f"{base}/graph/changes", params=params).json() == after["graph_changes"]
    assert client.get(f"{base}/revision", params=params).json()["revision"] == after["revision"]
    assert (
        client.get(f"{base}/cached/revision", params=params).json()["revision"] == after["revision"]
    )
    history = client.get(f"{base}/history/summaries", params=params).json()
    assert history[-1]["producer"] == "human"
    assert history[-1]["episode_id"] is None
    branch_patch = main.for_graph_target(episode.graph_target).history.load_patches()[-1]
    assert branch_patch.transition.pre_head.target == episode.graph_target
    stale = client.post(f"{base}/sync", params=params, json=draft)
    assert stale.status_code == 409, stale.text

    removed = client.post(
        f"{base}/sync",
        params=params,
        json={
            "base_revision": after["revision"],
            "removed_node_ids": [HYPOTHESIS],
        },
    )
    assert removed.status_code == 200, removed.text
    changes = client.get(f"{base}/graph/changes", params=params).json()
    deleted = next(item for item in changes["nodes"] if item["node_id"] == HYPOTHESIS)
    assert deleted["change"] == "removed"
    assert deleted["before"]["id"] == HYPOTHESIS and deleted["after"] is None
    assert changes["edges"][0]["change"] == "removed"
    assert main.history.state() == main_before


def test_branch_inbox_approval_has_human_authority_only_on_selected_branch(manifest, tmp_path):
    app, main, episode, root = _app_branch(manifest, tmp_path)
    branch = main.for_graph_target(episode.graph_target)
    branch.history.append(
        Patch(
            kind="work",
            author="agent",
            source_operation_id=root.operation_id,
            summary="Propose a clearer research question.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_proposals",
                    "proposals": [
                        {
                            "id": "prop/clarify-question",
                            "title": "Clarify the research question",
                            "card": {
                                "situation_cold": "The branch narrows the repeated-shift setting.",
                                "why_human_now": "The research question is a protected belief.",
                                "consequences": "The branch will state the more precise question.",
                                "decision_needed": "Use this question?",
                            },
                            "ops": [
                                {
                                    "op": "update_nodes",
                                    "intent": "content_change",
                                    "nodes": [
                                        {
                                            "id": QUESTION,
                                            "changes": {
                                                "question": "Does adaptation survive repeated task changes?"
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        )
    )
    client = TestClient(app)
    response = client.post(
        f"/api/projects/{episode.project_id}/sync",
        params={"branch_id": episode.episode_id},
        json={
            "base_revision": branch.history.state().revision,
            "proposals": [
                {
                    "proposal_id": "prop/clarify-question",
                    "decision": "approved",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert branch.history.state().proposals["prop/clarify-question"].status == "approved"
    assert (
        branch.history.state().nodes[QUESTION].question
        == "Does adaptation survive repeated task changes?"
    )
    assert not main.history.state().proposals
    assert (
        main.history.state().nodes[QUESTION].question
        != branch.history.state().nodes[QUESTION].question
    )
    assert branch.history.load_patches()[-1].producer == "human"


def test_graph_target_selection_never_returns_or_overwrites_main_cache(manifest, tmp_path):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    base = f"/api/projects/{harness.project_id}"
    main = harness.client.get(base).json()
    branch = harness.client.get(base, params={"branch_id": harness.episode.episode_id}).json()
    assert "ev/branch-result" in branch["graph"]["nodes"]
    assert "ev/branch-result" not in main["graph"]["nodes"]
    assert harness.client.get(f"{base}/cached").json()["graph_target"]["kind"] == "main"
    assert (
        harness.client.get(
            f"{base}/cached", params={"branch_id": harness.episode.episode_id}
        ).status_code
        == 404
    )
    for branch_id in (str(uuid.uuid4()), "invalid"):
        for route in (
            "",
            "/graph",
            "/graph/changes",
            "/history",
            "/cached",
            "/transition-manifest",
        ):
            response = harness.client.get(f"{base}{route}", params={"branch_id": branch_id})
            assert response.status_code == 404, (route, response.text)
    assert harness.client.get(base).json()["graph_changes"] is None


def test_branch_merge_fences_manual_sync_and_projects_unavailability(
    manifest, tmp_path, monkeypatch
):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    _admit_held_merge_task(harness, monkeypatch)
    base = f"/api/projects/{harness.project_id}"
    params = {"branch_id": harness.episode.episode_id}
    snapshot = harness.client.get(base, params=params).json()
    assert snapshot["graph_mutation"] == {
        "available": False,
        "reason": "Wait for the graph merge to finish before editing.",
    }
    assert (
        harness.client.get(f"{base}/cached/revision", params=params).json()["graph_mutation"]
        == snapshot["graph_mutation"]
    )
    for route in ("sync", "sync/preview"):
        response = harness.client.post(
            f"{base}/{route}", params=params, json={"base_revision": snapshot["revision"]}
        )
        assert response.status_code == 409, response.text
    assert harness.client.get(base).json()["graph_mutation"]["available"] is True


def test_merge_refuses_work_before_creating_its_repository_worktree(
    manifest, tmp_path, monkeypatch
):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    _admit_held_merge_task(harness, monkeypatch)

    def unexpected_preparation(*args, **kwargs):
        raise AssertionError("Refused Work must not prepare or create a worktree")

    monkeypatch.setattr(
        "rcp.runs.chat_admission.admit_conversation_worktree", unexpected_preparation
    )
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/tasks/project_chat",
        params={"branch_id": harness.episode.episode_id},
        json={
            "chat_id": str(uuid.uuid4()),
            "mode": "work",
            "message": "Revise the branch.",
            "worktree": True,
        },
    )
    assert response.status_code == 409, response.text


def test_graph_changes_project_from_the_immutable_base_after_a_rejected_first_patch(
    manifest, tmp_path
):
    app, main, episode, root = _app_branch(manifest, tmp_path)
    client = TestClient(app)
    base = f"/api/projects/{episode.project_id}"
    params = {"branch_id": episode.episode_id}
    branch = main.for_graph_target(episode.graph_target).history
    rejected, _result = branch.append(
        Patch(
            kind="work",
            author="agent",
            summary="Retain a rejected first branch revision.",
            run_truth_scope=["repo-a"],
            source_operation_id=root.operation_id,
            ops=[
                {
                    "op": "create_edges",
                    "edges": [
                        {"source": QUESTION, "target": HYPOTHESIS, "relation": "not_a_relation"}
                    ],
                }
            ],
        ),
        raise_on_reject=False,
    )
    assert rejected.admission == "rejected"
    snapshot = client.get(base, params=params).json()
    assert snapshot["graph_changes"]["base_head"] == branch.branch_metadata().base_head.model_dump(
        mode="json"
    )
    synced = client.post(
        f"{base}/sync",
        params=params,
        json={
            "base_revision": snapshot["revision"],
            "nodes": [
                {
                    "node_id": QUESTION,
                    "base_updated_rev": main.history.state().nodes[QUESTION].updated_rev,
                    "changes": {"question": "Does the rejected revision hide this change?"},
                }
            ],
        },
    )
    assert synced.status_code == 200, synced.text
    changes = client.get(f"{base}/graph/changes", params=params)
    assert changes.status_code == 200, changes.text
    assert changes.json()["base_head"] == snapshot["graph_changes"]["base_head"]
    assert [item["node_id"] for item in changes.json()["nodes"]] == [QUESTION]
    assert changes.json()["nodes"][0]["history"][0]["producer"] == "human"


def test_branch_revision_heartbeat_replays_once_until_the_branch_changes(
    manifest, tmp_path, monkeypatch
):
    from rcp.api import project_state
    from rcp.history.branches import BranchHistoryManager

    app, main, episode, _root = _app_branch(manifest, tmp_path)
    client = TestClient(app)
    base = f"/api/projects/{episode.project_id}"
    params = {"branch_id": episode.episode_id}
    replays = []
    materialize = BranchHistoryManager.materialize

    def counting(self, **kwargs):
        replays.append(kwargs)
        return materialize(self, **kwargs)

    monkeypatch.setattr(BranchHistoryManager, "materialize", counting)
    project_state._BRANCH_HEARTBEATS.clear()
    first = client.get(f"{base}/cached/revision", params=params).json()
    second = client.get(f"{base}/cached/revision", params=params).json()
    assert first == second
    assert first["graph_mutation"] == {"available": True, "reason": None}
    assert len(replays) == 1
    snapshot = client.get(base, params=params).json()
    synced = client.post(
        f"{base}/sync",
        params=params,
        json={
            "base_revision": snapshot["revision"],
            "nodes": [
                {
                    "node_id": QUESTION,
                    "base_updated_rev": main.history.state().nodes[QUESTION].updated_rev,
                    "changes": {"question": "Does the heartbeat notice this?"},
                }
            ],
        },
    )
    assert synced.status_code == 200, synced.text
    heartbeat_replays = len(replays)
    third = client.get(f"{base}/cached/revision", params=params).json()
    assert third["revision"] == synced.json()["head"]["revision"] > first["revision"]
    assert len(replays) == heartbeat_replays + 1
    client.get(f"{base}/cached/revision", params=params)
    assert len(replays) == heartbeat_replays + 1
    monkeypatch.setattr(project_state, "active_merge", lambda *args: True)
    fenced = client.get(f"{base}/cached/revision", params=params).json()
    assert fenced["revision"] == third["revision"]
    assert fenced["graph_mutation"]["available"] is False
    assert "merge" in fenced["graph_mutation"]["reason"]
