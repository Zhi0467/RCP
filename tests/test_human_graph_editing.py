from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rcp.core.models import RELATION_SPEC
from rcp.core.validation.constants import NODE_PREFIXES
from rcp.core.validation.ops import ASSESSMENT_REQUIRED_FOR
from tests.helpers import create_named_app


def _draft():
    return {
        "base_revision": 1,
        "custom_nodes": [
            {
                "id": "rq/example",
                "type": "research_question",
                "title": "Question",
                "question": "Does it work?",
            },
            {
                "id": "hyp/example",
                "type": "hypothesis",
                "title": "Hypothesis",
                "statement": "It works.",
            },
        ],
        "added_edges": [
            {
                "id": "rq/example::has_hypothesis::hyp/example",
                "source": "rq/example",
                "target": "hyp/example",
                "relation": "has_hypothesis",
            }
        ],
    }


def test_human_nodes_edges_preview_sync_remove_preserve_history(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    base = f"/api/projects/{app.state.default_project_id}"
    history = app.state.service.history
    before = history.load_patches()
    draft = _draft()
    draft["base_revision"] = history.state().revision
    preview = client.post(f"{base}/sync/preview", json=draft)
    assert preview.status_code == 200, preview.text
    assert history.load_patches() == before
    committed = client.post(f"{base}/sync", json=draft)
    assert committed.status_code == 200, committed.text
    graph = committed.json()
    edge_id = draft["added_edges"][0]["id"]
    assert graph["edges"][edge_id]["layer"] == "epistemic"
    assert len(history.load_patches()) == len(before) + 1
    prefix = [patch.model_dump(mode="json") for patch in history.load_patches()]
    deleted = client.post(
        f"{base}/sync",
        json={"base_revision": graph["revision"], "removed_node_ids": ["hyp/example"]},
    )
    assert deleted.status_code == 200, deleted.text
    assert "hyp/example" not in deleted.json()["nodes"]
    assert edge_id not in deleted.json()["edges"]
    assert [patch.model_dump(mode="json") for patch in history.load_patches()][:-1] == prefix


def test_edge_replacement_stale_draft_and_invalid_endpoint_are_atomic(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    base = f"/api/projects/{app.state.default_project_id}/sync"
    draft = _draft()
    draft["base_revision"] = app.state.service.history.state().revision
    created = client.post(base, json=draft)
    assert created.status_code == 200, created.text
    edge = {**draft["added_edges"][0], "explanation": "Human explanation"}
    replacement = {
        "base_revision": created.json()["revision"],
        "removed_edge_ids": [edge["id"]],
        "added_edges": [edge],
    }
    result = client.post(base, json=replacement)
    assert result.status_code == 200, result.text
    assert result.json()["edges"][edge["id"]]["explanation"] == "Human explanation"
    assert client.post(base, json=replacement).status_code == 409
    count = len(app.state.service.history.load_patches())
    bad = {
        "base_revision": result.json()["revision"],
        "removed_edge_ids": [edge["id"]],
        "added_edges": [{**edge, "id": "missing", "target": "hyp/missing"}],
    }
    assert client.post(f"{base}/preview", json=bad).status_code == 422
    assert client.post(base, json=bad).status_code == 422
    assert len(app.state.service.history.load_patches()) == count
    assert app.state.service.history.state().edges[edge["id"]].explanation == "Human explanation"


def test_graph_edit_options_are_backend_owned(manifest, tmp_path, monkeypatch):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)

    def unexpected_materialization(*args, **kwargs):
        raise AssertionError("static editing options must not replay history")

    monkeypatch.setattr(
        app.state.service.history, "current_materialization", unexpected_materialization
    )
    response = client.get(f"/api/projects/{app.state.default_project_id}/graph-edit-options")
    assert response.status_code == 200
    assert {item["name"] for item in response.json()["relations"]} == set(RELATION_SPEC)
    assert response.json()["node_prefixes"] == NODE_PREFIXES
    for item in response.json()["relations"]:
        assert set(item) == {"name", "assessment_required_for"}
        assert {
            (pair["source_type"], pair["target_type"]) for pair in item["assessment_required_for"]
        } == set(ASSESSMENT_REQUIRED_FOR.get(item["name"], ()))


def test_sync_new_edges_accept_generated_ids_and_reject_duplicate_effective_ids(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    base = f"/api/projects/{app.state.default_project_id}/sync"
    draft = _draft()
    draft["base_revision"] = app.state.service.history.state().revision
    edge = draft["added_edges"][0]
    edge_id = edge.pop("id")
    draft["added_edges"].append({**edge, "id": edge_id})
    for suffix in ("/preview", ""):
        response = client.post(f"{base}{suffix}", json=draft)
        assert response.status_code == 422, response.text
        assert "duplicate added edge" in response.text
    draft["added_edges"].pop()
    response = client.post(base, json=draft)
    assert response.status_code == 200, response.text
    assert edge_id in response.json()["edges"]


def test_sync_rejects_connection_to_node_removed_by_same_draft(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    base = f"/api/projects/{app.state.default_project_id}/sync"
    draft = _draft()
    draft["base_revision"] = app.state.service.history.state().revision
    created = client.post(base, json=draft)
    assert created.status_code == 200, created.text
    before = app.state.service.history.load_patches()
    for suffix in ("/preview", ""):
        response = client.post(
            f"{base}{suffix}",
            json={
                "base_revision": created.json()["revision"],
                "removed_node_ids": ["hyp/example"],
                "added_edges": draft["added_edges"],
            },
        )
        assert response.status_code == 422, response.text
        assert "cannot connect a node it removes" in response.text
    assert app.state.service.history.load_patches() == before


@pytest.mark.parametrize(
    ("node_type", "prefix", "content", "defaults"),
    [
        ("research_question", "rq", {"question": "Why?"}, {"status": "open", "motivation": ""}),
        (
            "hypothesis",
            "hyp",
            {"statement": "It works."},
            {"status": "proposed", "predictions": []},
        ),
        ("experiment", "exp", {"objective": "Measure it."}, {"status": "proposed", "attempts": []}),
        (
            "evidence",
            "ev",
            {"observation": "Observed it.", "origin": "analytic"},
            {"validity": "valid", "artifact_refs": []},
        ),
        ("decision", "dec", {"question": "Which path?"}, {"status": "open", "options": []}),
        (
            "blocker",
            "blk",
            {"description": "Missing input."},
            {"status": "open", "blocker_type": "unknown"},
        ),
    ],
)
def test_minimal_human_nodes_preview_and_sync_receive_backend_defaults(
    manifest, tmp_path, node_type, prefix, content, defaults
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    history = app.state.service.history
    base = f"/api/projects/{app.state.default_project_id}/sync"
    node_id = f"{prefix}/minimal"
    request = {
        "base_revision": history.state().revision,
        "custom_nodes": [{"id": node_id, "type": node_type, "title": "Minimal node", **content}],
    }
    before = history.load_patches()
    preview = client.post(f"{base}/preview", json=request)
    assert preview.status_code == 200, preview.text
    assert history.load_patches() == before
    synced = client.post(base, json=request)
    assert synced.status_code == 200, synced.text
    node = synced.json()["nodes"][node_id]
    assert preview.json()["projection"]["graph"]["nodes"][node_id] == node
    assert {field: node[field] for field in defaults} == defaults
    assert {field: node[field] for field in content} == content
    assert node["standing"] == "asserted"
    assert history.state().nodes[node_id].model_dump(mode="json") == node


@pytest.mark.parametrize("legacy_strength", [None, "supporting"])
def test_human_evidence_cannot_supply_compatibility_strength(manifest, tmp_path, legacy_strength):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    history = app.state.service.history
    base = f"/api/projects/{app.state.default_project_id}/sync"
    request = {
        "base_revision": history.state().revision,
        "custom_nodes": [
            {
                "id": "ev/forbidden",
                "type": "evidence",
                "title": "Forbidden compatibility field",
                "observation": "Observed it.",
                "origin": "analytic",
                "legacy_strength": legacy_strength,
            }
        ],
    }
    before = history.load_patches()
    for suffix in ("/preview", ""):
        response = client.post(f"{base}{suffix}", json=request)
        assert response.status_code == 422, response.text
        assert "live-legacy-evidence-strength" in response.text
    assert history.load_patches() == before
