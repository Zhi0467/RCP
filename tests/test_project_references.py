from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from rcp.attachments import NodeReferenceSelector, PaperReferenceSelector
from rcp.project_references import resolve_project_references
from tests.test_branch_context import branch_services as branch_services


def _resolve(services, selectors):
    main, _branch = services
    catalog = SimpleNamespace(open=lambda project_id: main)
    return resolve_project_references(main.paper.store, catalog, "project", selectors)


def test_node_snapshot_keeps_source_branch_and_coherent_head(branch_services, monkeypatch):
    main, branch = branch_services
    selector = NodeReferenceSelector(
        kind="node", node_id="ev/branch-only", branch_id=branch.history.branch_id
    )
    with pytest.raises(ValueError):
        _resolve(branch_services, [selector, selector])
    item = _resolve(branch_services, [selector])[0]
    snapshot = json.loads(item.content)
    assert snapshot["node"]["id"] == selector.node_id
    assert snapshot["relations"] == []
    assert snapshot["graph_head"] == branch.history.head_ref().model_dump(mode="json")
    assert item.reference.graph_head.target == branch.history.graph_target
    assert selector.node_id not in main.history.state().nodes
    related = _resolve(
        branch_services, [selector.model_copy(update={"node_id": "rq/learning-after-shift"})]
    )[0]
    relation = json.loads(related.content)["relations"][0]
    assert relation["other_node_id"] == "hyp/replanning-restores-plasticity"
    assert relation["relation"] == "has_hypothesis"
    assert relation["direction"] == "outgoing"
    with pytest.raises(ValueError):
        _resolve(branch_services, [selector.model_copy(update={"branch_id": None})])
    with pytest.raises(ValueError):
        _resolve(branch_services, [selector.model_copy(update={"branch_id": "missing"})])

    monkeypatch.setattr("rcp.project_references.PROJECT_REFERENCE_NODE_MAX_BYTES", 1)
    with pytest.raises(ValueError):
        _resolve(branch_services, [selector])


def test_paper_reference_reads_saved_canonical_after_confirmed_refresh(
    branch_services, monkeypatch
):
    paper = branch_services[0].paper
    paper.create()
    paper._save_draft("unsaved draft", None, None)
    paper.workspace.remote = True
    refreshes = []

    def refresh():
        refreshes.append(True)
        paper.canonical_path.write_bytes(b"Fresh canonical paper\r\n")
        return True

    monkeypatch.setattr(paper.workspace, "refresh", refresh)
    item = _resolve(branch_services, [PaperReferenceSelector(kind="paper")])[0]
    assert refreshes == [True]
    assert item.content == b"Fresh canonical paper\r\n"
    assert item.reference.version == hashlib.sha256(item.content).hexdigest()
    monkeypatch.setattr(paper.workspace, "refresh", lambda: False)
    with pytest.raises(ValueError):
        _resolve(branch_services, [PaperReferenceSelector(kind="paper")])


def test_oversized_artifact_is_refused_before_its_bytes_are_read(branch_services, monkeypatch):
    from rcp.attachments import ArtifactReferenceSelector
    from rcp.storage import Artifact

    store = branch_services[0].paper.store
    artifact = store.create_artifact(
        Artifact(
            artifact_id="c" * 24,
            project_id="project",
            supplier="turn",
            supplier_id="origin",
            source_name="large.txt",
            media_type="text/plain",
            created_at=store.now(),
        ),
        data=b"12345",
    )
    selector = ArtifactReferenceSelector(kind="artifact", artifact_id=artifact.artifact_id)
    monkeypatch.setattr("rcp.project_references.CHAT_ATTACHMENT_MAX_FILE_BYTES", 4)
    monkeypatch.setattr(store, "read_artifact_bytes", lambda *_: pytest.fail("bytes read"))
    with pytest.raises(ValueError):
        _resolve(branch_services, [selector])
