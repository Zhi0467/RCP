from fastapi.testclient import TestClient

from rcp.core.models import RELATION_SPEC
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
        "added_edges": [{**edge, "id": "missing", "target": "hyp/missing"}],
    }
    assert client.post(f"{base}/preview", json=bad).status_code == 422
    assert client.post(base, json=bad).status_code == 422
    assert len(app.state.service.history.load_patches()) == count


def test_graph_edit_options_are_backend_owned(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    response = client.get(f"/api/projects/{app.state.default_project_id}/graph-edit-options")
    assert response.status_code == 200
    assert {item["name"] for item in response.json()["relations"]} == set(RELATION_SPEC)
