import uuid

import pytest
from fastapi.testclient import TestClient

from .helpers import append_fixture_patch, seed_patch
from .helpers import create_named_app as create_app


@pytest.mark.parametrize(
    "route,body",
    [
        # One kind reaches task validation; the other is refused before its 405.
        ("tasks/seed", {}),
        ("tasks/branch_merge", {}),
        ("experiments/missing/run", {}),
        ("episodes", {"mode": "auto_research", "invocation_ceiling": 1}),
        ("episodes/missing/continue", {"invocation_ceiling": 1, "request_id": str(uuid.uuid4())}),
        ("episodes/missing/messages", {"body": "hello"}),
        ("episodes/missing/merge", {}),
        ("artifacts/missing/comments", {"comments": [{"text": "hello"}]}),
        (
            "tasks/missing/steer",
            {
                "message_id": str(uuid.uuid4()),
                "attempt": 1,
                "expected_turn_id": "turn",
                "message": "hello",
            },
        ),
    ],
)
def test_reference_refused_routes(manifest, tmp_path, route, body):
    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{app.state.default_project_id}/{route}",
            json={**body, "references": [{"kind": "paper"}]},
        )
    assert response.status_code == 422
    if isinstance(response.json()["detail"], list):
        assert any(item["loc"][-1] == "references" for item in response.json()["detail"])


def test_admission_retains_reference_bytes_after_source_edit_and_expiry(manifest, tmp_path):
    from datetime import datetime, timedelta

    from rcp.attachments import ChatAttachmentDescriptor
    from rcp.storage import Artifact

    app = create_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    store = app.state.services.store
    project_id = app.state.default_project_id
    expiry = datetime.fromisoformat(store.now()) + timedelta(hours=1)
    artifact = store.create_artifact(
        Artifact(
            artifact_id="a" * 24,
            project_id=project_id,
            supplier="turn",
            supplier_id="origin",
            source_name="report.txt",
            display_title="Report",
            media_type="text/plain",
            created_at=store.now(),
            expires_at=expiry.isoformat(),
        ),
        data=b"frozen report",
    )
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project_id}/tasks/project_chat",
            json={
                "chat_id": str(uuid.uuid4()),
                "message": "Read this",
                "run_truth_scope": ["repo-a"],
                "references": [{"kind": "artifact", "artifact_id": artifact.artifact_id}],
                "attachments": [{"reference": {"kind": "forged"}}],
            },
        )
        assert response.status_code == 202
        admitted = response.json()["request"]
        assert admitted["references"] == []
        descriptors = [
            ChatAttachmentDescriptor.model_validate(item) for item in admitted["attachments"]
        ]
        assert len(descriptors) == 1
        assert descriptors[0].reference.version == artifact.current_version
        store.publish_artifact_version(
            artifact.artifact_id,
            base_version=artifact.current_version,
            operation_id="edit",
            data=b"edited report",
        )
        assert store.expire_artifacts(as_of=expiry + timedelta(days=1)) == 1
        assert store.artifact(artifact.artifact_id) is None
        from pathlib import Path

        for stage in ("initial", "recovery"):
            pointers = app.state.services.attachment_store.stage(
                admitted["attachment_batch_id"],
                descriptors,
                local_stage=tmp_path / stage,
                remote_stage=None,
            )
            assert Path(pointers[0]["path"]).read_bytes() == b"frozen report"
            assert Path(pointers[0]["path"]).suffix == ".txt"
            assert descriptors[0].name == "Report"
