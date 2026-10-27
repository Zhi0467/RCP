from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from rcp.attachments import NodeReferenceSelector, PaperReferenceSelector
from rcp.paper.coach_seam import SavedPaperFile, SavedPaperSnapshot
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


def test_paper_reference_retains_saved_tree_paths_ids_and_revision(branch_services, monkeypatch):
    snapshot = SavedPaperSnapshot(
        revision=9,
        files=[
            SavedPaperFile(file_id="tex", path="main.tex", kind="text", text="include{sections/a}"),
            SavedPaperFile(
                file_id="section", path="sections/a.tex", kind="text", text="Saved draft"
            ),
            SavedPaperFile(
                file_id="figure", path="figures/a.png", kind="binary", blob_sha256="a" * 64
            ),
        ],
    )

    def materialize(snapshot, dest):
        for item in snapshot.files:
            path = dest / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item.text.encode() if item.text is not None else b"binary")

    branch_services[0].paper_live = SimpleNamespace(
        snapshot=lambda project_id: snapshot, materialize=materialize
    )
    items = _resolve(branch_services, [PaperReferenceSelector(kind="paper")])
    assert [item.reference.path for item in items] == [
        "figures/a.png",
        "main.tex",
        "sections/a.tex",
    ]
    assert {item.reference.version for item in items} == {"9"}
    one = _resolve(branch_services, [PaperReferenceSelector(kind="paper", file_id="section")])
    assert len(one) == 1 and one[0].content == b"Saved draft"
    assert one[0].reference.source_id == "section"
    with pytest.raises(ValueError):
        _resolve(branch_services, [PaperReferenceSelector(kind="paper", file_id="missing")])
    monkeypatch.setattr("rcp.project_references.CHAT_ATTACHMENT_MAX_FILE_BYTES", 4)
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


def test_expired_artifact_is_refused(branch_services):
    from datetime import datetime, timedelta

    from rcp.attachments import ArtifactReferenceSelector
    from rcp.storage import Artifact

    store = branch_services[0].paper.store
    artifact = store.create_artifact(
        Artifact(
            artifact_id="d" * 24,
            project_id="project",
            supplier="turn",
            supplier_id="origin",
            source_name="old.txt",
            media_type="text/plain",
            created_at=store.now(),
            expires_at=(datetime.fromisoformat(store.now()) - timedelta(seconds=1)).isoformat(),
        ),
        data=b"stale",
    )
    with pytest.raises(ValueError):
        _resolve(
            branch_services,
            [ArtifactReferenceSelector(kind="artifact", artifact_id=artifact.artifact_id)],
        )
