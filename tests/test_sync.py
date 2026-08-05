from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from rcp.api import create_app
from rcp.core.models import Patch, ValidationMessage
from rcp.history import HistoryManager
from rcp.service import GraphSyncNodeChange, GraphSyncRequest, ReviewRequest
from rcp.storage import AgentTaskRecord
from tests.helpers import seed_patch


def ontology_payload() -> dict[str, object]:
    return {
        "types": [
            {
                "name": "mechanism_hypothesis",
                "definition": "A hypothesis about the mechanism responsible for an effect.",
                "base_type": "hypothesis",
                "layer": "epistemic",
                "deprecated": False,
            }
        ],
        "fields": [
            {
                "owner_type": "mechanism_hypothesis",
                "name": "mechanism",
                "definition": "The proposed causal mechanism.",
                "kind": "text",
                "required": False,
                "agent_writable": True,
                "deprecated": False,
            }
        ],
        "relations": [],
    }


def custom_hypothesis_payload() -> dict[str, object]:
    return {
        "id": "mechanism_hypothesis/custom-mechanism",
        "type": "hypothesis",
        "extension_type": "mechanism_hypothesis",
        "extension_fields": {"mechanism": "Replanning restores unused update directions."},
        "title": "Replanning mechanism",
        "statement": "Periodic replanning preserves future plasticity.",
    }


def test_graph_sync_commits_staged_wording_and_judgment_once(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    node = service.history.state().nodes["rq/learning-after-shift"]
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{app.state.default_project_id}/sync",
        json={
            "base_revision": 1,
            "nodes": [
                {
                    "node_id": node.id,
                    "base_updated_rev": node.updated_rev,
                    "changes": {"title": "Learning after a task shift"},
                    "standing": "accepted",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert response.json()["nodes"][node.id]["standing"] == "accepted"
    assert response.json()["nodes"][node.id]["title"] == "Learning after a task shift"
    assert len(service.history.load_patches()) == 2
    assert "Learning after a task shift" in (manifest.research_dir / "research.md").read_text(
        encoding="utf-8"
    )


def test_graph_sync_builds_from_the_single_in_lock_current_replay(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    node = service.history.state().nodes["rq/learning-after-shift"]
    calls: list[tuple[bool, bool]] = []
    materialize = service.history.materialize

    def counted_materialize(*, write_outputs=True, pending_patch_paths=None):
        calls.append((write_outputs, pending_patch_paths is not None))
        return materialize(
            write_outputs=write_outputs,
            pending_patch_paths=pending_patch_paths,
        )

    monkeypatch.setattr(service.history, "materialize", counted_materialize)

    state = service.sync_graph(
        GraphSyncRequest(
            base_revision=1,
            nodes=[
                GraphSyncNodeChange(
                    node_id=node.id,
                    base_updated_rev=node.updated_rev,
                    standing="accepted",
                )
            ],
        ),
        active_control_node_ids=set(),
    )

    assert state.revision == 2
    assert calls == [(False, False), (False, True)]


def test_project_service_coalesces_concurrent_index_builds(manifest, tmp_path, monkeypatch) -> None:
    service = create_app(str(manifest.path), data_dir=tmp_path / "data").state.service
    builds = 0
    rendezvous = threading.Barrier(2)
    snapshot = object()

    def build(**_kwargs):
        nonlocal builds
        builds += 1
        with suppress(threading.BrokenBarrierError):
            rendezvous.wait(timeout=0.2)
        return snapshot

    monkeypatch.setattr(service.indexer, "build", build)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _item: service.index_snapshot(), range(2)))

    assert builds == 1
    assert results == [snapshot, snapshot]


def test_graph_sync_withdraws_to_asserted_and_rewrites_research_once(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    node = service.history.state().nodes["rq/learning-after-shift"]
    service.review_node(node.id, ReviewRequest(standing="accepted"))
    accepted = service.history.state().nodes[node.id]
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{app.state.default_project_id}/sync",
        json={
            "base_revision": 2,
            "nodes": [
                {
                    "node_id": node.id,
                    "base_updated_rev": accepted.updated_rev,
                    "standing": "asserted",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 3
    assert response.json()["nodes"][node.id]["standing"] == "asserted"
    assert (manifest.research_dir / "research.md").read_text(encoding="utf-8") == ""


def test_graph_sync_no_net_change_writes_no_patch(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    node = service.history.state().nodes["rq/learning-after-shift"]
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{app.state.default_project_id}/sync",
        json={
            "base_revision": 1,
            "nodes": [
                {
                    "node_id": node.id,
                    "base_updated_rev": node.updated_rev,
                    "standing": "asserted",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert len(service.history.load_patches()) == 1


def test_graph_sync_removes_node_and_its_incident_edges(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{app.state.default_project_id}/sync",
        json={
            "base_revision": 1,
            "removed_node_ids": ["rq/learning-after-shift"],
        },
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert "rq/learning-after-shift" not in response.json()["nodes"]
    assert response.json()["edges"] == {}
    stored = service.history.load_patches()[-1]
    assert stored.kind == "approval"
    assert stored.author == "human"
    assert stored.ops == [{"op": "remove_nodes", "node_ids": ["rq/learning-after-shift"]}]


def test_graph_sync_removal_preserves_base_revision_conflict(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    service.history.append(
        Patch(
            kind="approval",
            author="human",
            summary="Moved the graph after the draft opened.",
            ops=[
                {
                    "op": "set_standing",
                    "node_id": "hyp/replanning-restores-plasticity",
                    "standing": "contested",
                }
            ],
        )
    )
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{app.state.default_project_id}/sync",
        json={
            "base_revision": 1,
            "removed_node_ids": ["rq/learning-after-shift"],
        },
    )

    assert response.status_code == 409
    assert "graph changed after this draft began" in response.json()["detail"]
    assert "rq/learning-after-shift" in service.history.state().nodes
    assert service.history.state().revision == 2


@pytest.mark.parametrize("same_draft", [False, True])
@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_graph_sync_staged_decision_withdraws_proposal_made_stale_by_node_removal(
    manifest, tmp_path, same_draft, decision
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    service.history.append(
        Patch(
            kind="refresh",
            author="agent",
            summary="Proposed activating the replanning hypothesis.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[
                {
                    "op": "create_proposals",
                    "proposals": [
                        {
                            "id": "prop/activate-replanning-hypothesis",
                            "title": "Treat replanning as the active hypothesis",
                            "card": {
                                "situation_cold": "The causal explanation is only proposed.",
                                "why_human_now": "Activation changes experiment interpretation.",
                                "consequences": "Evidence will be organized around it.",
                                "decision_needed": "Decide whether it should become active.",
                            },
                            "ops": [
                                {
                                    "op": "update_nodes",
                                    "nodes": [
                                        {
                                            "id": "hyp/replanning-restores-plasticity",
                                            "changes": {"status": "active"},
                                            "cause": {
                                                "kind": "evidence_edge",
                                                "ref_id": "edge/replanning-activation",
                                            },
                                        }
                                    ],
                                }
                            ],
                            "related_node_ids": ["hyp/replanning-restores-plasticity"],
                            "base_rev": 1,
                        }
                    ],
                },
                {
                    "op": "create_nodes",
                    "nodes": [
                        {
                            "id": "ev/replanning-activation",
                            "type": "evidence",
                            "title": "Replanning activation evidence",
                            "observation": "The observed behavior warrants activation testing.",
                            "origin": "analytic",
                        }
                    ],
                },
                {
                    "op": "create_edges",
                    "edges": [
                        {
                            "id": "edge/replanning-activation",
                            "source": "ev/replanning-activation",
                            "target": "hyp/replanning-restores-plasticity",
                            "relation": "supports",
                        }
                    ],
                },
            ],
        )
    )
    project_id = app.state.default_project_id
    client = TestClient(app)
    if same_draft:
        decided = client.post(
            f"/api/projects/{project_id}/sync",
            json={
                "base_revision": 2,
                "removed_node_ids": ["hyp/replanning-restores-plasticity"],
                "proposals": [
                    {
                        "proposal_id": "prop/activate-replanning-hypothesis",
                        "decision": decision,
                    }
                ],
            },
        )
    else:
        removed = client.post(
            f"/api/projects/{project_id}/sync",
            json={
                "base_revision": 2,
                "removed_node_ids": ["hyp/replanning-restores-plasticity"],
            },
        )
        assert removed.status_code == 200
        assert removed.json()["proposals"]["prop/activate-replanning-hypothesis"]["status"] == (
            "pending"
        )

        decided = client.post(
            f"/api/projects/{project_id}/sync",
            json={
                "base_revision": 3,
                "proposals": [
                    {
                        "proposal_id": "prop/activate-replanning-hypothesis",
                        "decision": decision,
                    }
                ],
            },
        )

    assert decided.status_code == 200
    assert decided.json()["revision"] == 4
    assert (
        decided.json()["proposals"]["prop/activate-replanning-hypothesis"]["status"] == "withdrawn"
    )
    assert "hyp/replanning-restores-plasticity" not in decided.json()["nodes"]
    assert service.history.load_patches()[-1].ops == [
        {
            "op": "resolve_proposals",
            "resolutions": [{"id": "prop/activate-replanning-hypothesis", "status": "withdrawn"}],
        }
    ]
    if same_draft:
        assert service.history.load_patches()[-1].change_summary == [
            "The proposal “Treat replanning as the active hypothesis” became stale because a related "
            "research concept was removed in this Sync."
        ]


def test_graph_sync_refuses_removing_an_accepted_node(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    service.review_node(
        "rq/learning-after-shift",
        ReviewRequest(standing="accepted"),
    )
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{app.state.default_project_id}/sync",
        json={
            "base_revision": 2,
            "removed_node_ids": ["rq/learning-after-shift"],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Accepted node rq/learning-after-shift cannot be removed; withdraw its acceptance "
        "and Sync before removing it."
    )
    accepted = service.history.state().nodes["rq/learning-after-shift"]
    combined = client.post(
        f"/api/projects/{app.state.default_project_id}/sync",
        json={
            "base_revision": 2,
            "nodes": [
                {
                    "node_id": accepted.id,
                    "base_updated_rev": accepted.updated_rev,
                    "standing": "asserted",
                }
            ],
            "removed_node_ids": [accepted.id],
        },
    )
    assert combined.status_code == 422
    assert "cannot both change and remove the same node" in combined.text
    assert service.history.state().revision == 2


def test_graph_sync_request_rejects_duplicate_and_conflicting_removals() -> None:
    with pytest.raises(ValueError, match="duplicate removed node targets"):
        GraphSyncRequest(
            base_revision=1,
            removed_node_ids=["hyp/one", "hyp/one"],
        )

    with pytest.raises(ValueError, match="both change and remove the same node: hyp/one"):
        GraphSyncRequest(
            base_revision=1,
            nodes=[
                GraphSyncNodeChange(
                    node_id="hyp/one",
                    base_updated_rev=1,
                    standing="contested",
                )
            ],
            removed_node_ids=["hyp/one"],
        )


def test_graph_sync_route_passes_active_experiment_loop_to_removal_guard(
    manifest, tmp_path
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    initial = seed_patch()
    initial.ops[0]["nodes"].append(
        {
            "id": "exp/active-loop",
            "type": "experiment",
            "title": "Active loop",
            "objective": "Exercise the bounded loop removal guard.",
            "status": "running",
        }
    )
    service.history.append(initial)
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    now = datetime.now(UTC).isoformat()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="active-experiment-loop",
            project_id=project_id,
            kind="node_chat",
            status="queued",
            request={
                "trigger": "experiment_run",
                "patch_kind": "experiment_loop",
                "control_node_id": "exp/active-loop",
                "control_revision": 1,
                "control_episode_id": str(uuid.uuid4()),
                "control_invocation": 1,
                "control_invocation_ceiling": 5,
                "control_decision_bundle": [],
                "control_completion_criteria": [],
            },
            created_at=now,
            updated_at=now,
            status_message="Queued bounded experiment loop.",
        )
    )
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{project_id}/sync",
        json={"base_revision": 1, "removed_node_ids": ["exp/active-loop"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Experiment exp/active-loop cannot be removed while its bounded experiment loop is active."
    )
    assert service.history.state().revision == 1


def test_graph_sync_commits_ontology_as_human_approval_patch(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{app.state.default_project_id}/sync",
        json={"base_revision": 1, "ontology": ontology_payload()},
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert response.json()["ontology"] == ontology_payload()
    stored = service.history.load_patches()[-1]
    assert stored.kind == "approval"
    assert stored.author == "human"
    assert stored.ops == [{"op": "set_ontology", "ontology": ontology_payload()}]


def test_graph_sync_unchanged_ontology_writes_no_patch(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    client = TestClient(app)
    project_id = app.state.default_project_id
    assert (
        client.post(
            f"/api/projects/{project_id}/sync",
            json={"base_revision": 1, "ontology": ontology_payload()},
        ).status_code
        == 200
    )

    response = client.post(
        f"/api/projects/{project_id}/sync",
        json={"base_revision": 2, "ontology": ontology_payload()},
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert len(service.history.load_patches()) == 2


def test_graph_sync_refuses_stale_ontology_draft(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    service.review_node("rq/learning-after-shift", ReviewRequest(standing="accepted"))
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{app.state.default_project_id}/sync",
        json={"base_revision": 1, "ontology": ontology_payload()},
    )

    assert response.status_code == 409
    assert "graph changed" in response.json()["detail"].lower()


def test_graph_sync_refuses_defining_and_using_a_type_in_one_draft(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{app.state.default_project_id}/sync",
        json={
            "base_revision": 1,
            "ontology": ontology_payload(),
            "custom_nodes": [custom_hypothesis_payload()],
        },
    )

    assert response.status_code == 422
    assert "defines and uses a new ontology type" in response.json()["detail"]
    assert "sync the ontology first" in response.json()["detail"].lower()
    assert service.history.state().revision == 1


def test_graph_sync_does_not_offer_direct_base_node_authoring(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{app.state.default_project_id}/sync",
        json={
            "base_revision": 1,
            "custom_nodes": [
                {
                    "id": "hyp/human-base-node",
                    "type": "hypothesis",
                    "title": "Human base node",
                    "statement": "This must not create a new base node.",
                }
            ],
        },
    )

    assert response.status_code == 422
    assert "base-node authoring is not available" in response.json()["detail"]
    assert service.history.state().revision == 1


def test_graph_sync_creates_an_asserted_node_of_an_active_custom_type(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    client = TestClient(app)
    project_id = app.state.default_project_id
    assert (
        client.post(
            f"/api/projects/{project_id}/sync",
            json={"base_revision": 1, "ontology": ontology_payload()},
        ).status_code
        == 200
    )

    node = custom_hypothesis_payload()
    node["standing"] = "accepted"
    response = client.post(
        f"/api/projects/{project_id}/sync",
        json={"base_revision": 2, "custom_nodes": [node]},
    )

    assert response.status_code == 200
    created = response.json()["nodes"]["mechanism_hypothesis/custom-mechanism"]
    assert created["extension_type"] == "mechanism_hypothesis"
    assert created["extension_fields"] == {
        "mechanism": "Replanning restores unused update directions."
    }
    assert created["standing"] == "asserted"
    stored = service.history.load_patches()[-1]
    assert stored.kind == "approval"
    assert stored.ops[0]["op"] == "create_nodes"


def test_graph_sync_replaces_active_extension_fields_on_an_existing_custom_node(
    manifest, tmp_path
) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    client = TestClient(app)
    project_id = app.state.default_project_id
    assert (
        client.post(
            f"/api/projects/{project_id}/sync",
            json={"base_revision": 1, "ontology": ontology_payload()},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/projects/{project_id}/sync",
            json={"base_revision": 2, "custom_nodes": [custom_hypothesis_payload()]},
        ).status_code
        == 200
    )

    response = client.post(
        f"/api/projects/{project_id}/sync",
        json={
            "base_revision": 3,
            "nodes": [
                {
                    "node_id": "mechanism_hypothesis/custom-mechanism",
                    "base_updated_rev": 3,
                    "changes": {
                        "extension_fields": {"mechanism": "Replanning refreshes update directions."}
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["nodes"]["mechanism_hypothesis/custom-mechanism"][
        "extension_fields"
    ] == {"mechanism": "Replanning refreshes update directions."}


def test_graph_sync_preserves_an_unchanged_deprecated_extension_field(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    client = TestClient(app)
    project_id = app.state.default_project_id
    ontology = ontology_payload()
    ontology["fields"].append(
        {
            "owner_type": "mechanism_hypothesis",
            "name": "legacy_note",
            "definition": "A field retained for old nodes.",
            "kind": "text",
            "required": False,
            "agent_writable": True,
            "deprecated": False,
        }
    )
    assert (
        client.post(
            f"/api/projects/{project_id}/sync",
            json={"base_revision": 1, "ontology": ontology},
        ).status_code
        == 200
    )
    node = custom_hypothesis_payload()
    node["extension_fields"]["legacy_note"] = "Keep this old value."
    assert (
        client.post(
            f"/api/projects/{project_id}/sync",
            json={"base_revision": 2, "custom_nodes": [node]},
        ).status_code
        == 200
    )
    ontology["fields"][1]["deprecated"] = True
    assert (
        client.post(
            f"/api/projects/{project_id}/sync",
            json={"base_revision": 3, "ontology": ontology},
        ).status_code
        == 200
    )

    omitted = client.post(
        f"/api/projects/{project_id}/sync",
        json={
            "base_revision": 4,
            "nodes": [
                {
                    "node_id": "mechanism_hypothesis/custom-mechanism",
                    "base_updated_rev": 3,
                    "changes": {
                        "extension_fields": {"mechanism": "Replanning refreshes update directions."}
                    },
                }
            ],
        },
    )
    assert omitted.status_code == 422
    assert "legacy_note" in omitted.json()["detail"]

    response = client.post(
        f"/api/projects/{project_id}/sync",
        json={
            "base_revision": 4,
            "nodes": [
                {
                    "node_id": "mechanism_hypothesis/custom-mechanism",
                    "base_updated_rev": 3,
                    "changes": {
                        "extension_fields": {
                            "mechanism": "Replanning refreshes update directions.",
                            "legacy_note": "Keep this old value.",
                        }
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["nodes"]["mechanism_hypothesis/custom-mechanism"][
        "extension_fields"
    ] == {
        "mechanism": "Replanning refreshes update directions.",
        "legacy_note": "Keep this old value.",
    }


def test_batch_overwrites_forged_admission_receipts(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    forged = ValidationMessage(level="reject", code="forged", message="forged")
    raw = Patch(
        kind="approval",
        author="human",
        summary="Agree with the question.",
        ops=[
            {
                "op": "set_standing",
                "node_id": "rq/learning-after-shift",
                "standing": "accepted",
            }
        ],
        admission="rejected",
        admission_messages=[forged],
    )

    prepared, result = history.append_batch([raw], expected_revision=1)

    assert result.state.revision == 2
    assert prepared[0].admission == "accepted"
    assert not prepared[0].admission_messages
    stored = history.load_patches()[-1]
    assert stored.admission == "accepted"
    assert not stored.admission_messages


def test_batch_reuses_pending_replay_for_committed_outputs(manifest, monkeypatch) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    raw = Patch(
        kind="approval",
        author="human",
        summary="Agree with the question.",
        ops=[
            {
                "op": "set_standing",
                "node_id": "rq/learning-after-shift",
                "standing": "accepted",
            }
        ],
    )
    calls: list[tuple[bool, bool]] = []
    materialize = history.materialize

    def counted_materialize(*, write_outputs=True, pending_patch_paths=None):
        calls.append((write_outputs, pending_patch_paths is not None))
        return materialize(
            write_outputs=write_outputs,
            pending_patch_paths=pending_patch_paths,
        )

    monkeypatch.setattr(history, "materialize", counted_materialize)

    prepared, result = history.append_batch([raw], expected_revision=1)

    assert [patch.revision for patch in prepared] == [2]
    assert result.state.revision == 2
    assert calls == [(False, False), (False, True)]
    stored = json.loads((manifest.research_dir / "graph.json").read_text(encoding="utf-8"))
    assert stored["revision"] == 2


def test_batch_builder_receives_fresh_state_under_append_lock(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    lock_attempts: list[bool] = []

    def build(state):
        assert state.revision == 1

        def contend_for_lock() -> None:
            acquired = history._process_lock.acquire(timeout=0.05)
            lock_attempts.append(acquired)
            if acquired:
                history._process_lock.release()

        contender = threading.Thread(target=contend_for_lock)
        contender.start()
        contender.join()
        return [
            Patch(
                kind="approval",
                author="human",
                summary="Agree with the question.",
                ops=[
                    {
                        "op": "set_standing",
                        "node_id": "rq/learning-after-shift",
                        "standing": "accepted",
                    }
                ],
            )
        ]

    prepared, result = history.append_batch_from_state(build, expected_revision=1)

    assert lock_attempts == [False]
    assert [patch.revision for patch in prepared] == [2]
    assert result.state.nodes["rq/learning-after-shift"].standing == "accepted"


def test_graph_sync_refuses_stale_project_draft(manifest, tmp_path) -> None:
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    service.history.append(seed_patch())
    node = service.history.state().nodes["rq/learning-after-shift"]
    service.review_node(node.id, ReviewRequest(standing="accepted"))
    client = TestClient(app)

    response = client.post(
        f"/api/projects/{app.state.default_project_id}/sync",
        json={
            "base_revision": 1,
            "nodes": [
                {
                    "node_id": node.id,
                    "base_updated_rev": node.updated_rev,
                    "standing": "contested",
                }
            ],
        },
    )

    assert response.status_code == 409
    assert "graph changed" in response.json()["detail"].lower()


def test_interrupted_batch_write_exposes_none_of_the_sync(manifest, monkeypatch) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    before_graph = (manifest.research_dir / "graph.json").read_bytes()
    patches = [
        Patch(
            kind="approval",
            author="human",
            summary="Agree with the question.",
            ops=[
                {
                    "op": "set_standing",
                    "node_id": "rq/learning-after-shift",
                    "standing": "accepted",
                }
            ],
        ),
        Patch(
            kind="approval",
            author="human",
            summary="Disagree with the hypothesis.",
            ops=[
                {
                    "op": "set_standing",
                    "node_id": "hyp/replanning-restores-plasticity",
                    "standing": "contested",
                }
            ],
        ),
    ]
    original_atomic_text = history._atomic_text
    staged_writes = 0

    def fail_second_staged_patch(path, content):
        nonlocal staged_writes
        if path.parent.name.startswith(".batch-"):
            staged_writes += 1
            if staged_writes == 2:
                raise OSError("simulated disk failure")
        original_atomic_text(path, content)

    monkeypatch.setattr(history, "_atomic_text", fail_second_staged_patch)

    with pytest.raises(OSError, match="simulated disk failure"):
        history.append_batch(patches, expected_revision=1)

    assert [patch.revision for patch in history.load_patches()] == [1]
    assert history.state().revision == 1
    assert (manifest.research_dir / "graph.json").read_bytes() == before_graph
    assert not list((manifest.research_dir / "patches").glob(".batch-*"))
    assert not list((manifest.research_dir / "patches").glob("batch-*"))


def test_replay_ignores_an_uncommitted_hidden_batch(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    hidden = manifest.research_dir / "patches" / ".batch-interrupted"
    hidden.mkdir()
    (hidden / "000002.json").write_text(
        Patch(
            revision=2,
            kind="approval",
            author="human",
            summary="This transaction never committed.",
            ops=[
                {
                    "op": "set_standing",
                    "node_id": "rq/learning-after-shift",
                    "standing": "accepted",
                }
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )

    assert [patch.revision for patch in history.load_patches()] == [1]
    assert history.state().nodes["rq/learning-after-shift"].standing == "asserted"
