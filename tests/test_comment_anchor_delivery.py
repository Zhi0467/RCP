from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from rcp.agents import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.comments.models import COMMENT_MEDIA_TYPES
from rcp.runs.artifact_edit_admission import admit_chat_artifact_comment
from rcp.runs.chat import stage_artifact_context
from rcp.service import ArtifactContextRequest, RunRequest

from .test_artifact_edit_admission import edit_origin as edit_origin
from .test_unified_artifacts import _publish_artifact, _stored_artifact

ANCHORS = [
    {"kind": "whole"},
    {"kind": "text", "text": "quote"},
    {"kind": "element", "path": "p"},
    {"kind": "pdf", "page": 1},
    {
        "kind": "box",
        "rect": {"x": 0, "y": 0, "width": 1, "height": 1},
        "viewport": {"width": 10, "height": 10},
        "elements": [],
    },
]


@pytest.mark.parametrize("anchor", ANCHORS)
@pytest.mark.parametrize(
    "media_type", sorted(COMMENT_MEDIA_TYPES["whole"] | {"application/octet-stream"})
)
def test_comment_admission_obeys_kind_media_contract(edit_origin, anchor, media_type):
    app, store, task = edit_origin
    artifact = _stored_artifact(app, task.operation_id, "source.txt", b"source")
    with store.connection() as connection:
        connection.execute(
            "UPDATE artifacts SET metadata=json_set(metadata, '$.media_type', ?) WHERE artifact_id=?",
            (media_type, artifact.artifact_id),
        )
    request = RunRequest(
        chat_id=task.request["chat_id"],
        artifact_context=ArtifactContextRequest.model_validate(
            {
                "operation_id": task.operation_id,
                "artifact_id": artifact.artifact_id,
                "selections": [{**anchor, "comment": "Explain"}],
            }
        ),
    )
    if media_type in COMMENT_MEDIA_TYPES[anchor["kind"]]:
        admitted = admit_chat_artifact_comment(store, app.state.service, task.project_id, request)
        assert admitted.artifact_context is not None
        assert admitted.artifact_context.selections[0].kind == anchor["kind"]
    else:
        with pytest.raises(ValueError):
            admit_chat_artifact_comment(store, app.state.service, task.project_id, request)


def _png(color):
    stream = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(stream, format="PNG")
    return stream.getvalue()


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("continuation", ["resume", "retry"])
def test_crops_use_drawn_versions_and_recover_without_old_bytes(
    edit_origin, tmp_path, monkeypatch, remote, continuation
):
    app, store, task = edit_origin
    store.record_agent_task_receipt(
        task.operation_id,
        "operation_created",
        {
            "kind": task.kind,
            "attempt": 1,
            "has_parent": False,
            "resumed": False,
        },
    )
    artifact = _stored_artifact(app, task.operation_id, "plot.png", _png("red"))
    first = store.artifact(artifact.artifact_id).current_version
    _publish_artifact(store, artifact.artifact_id, _png("blue"))
    current = store.artifact(artifact.artifact_id).current_version
    request = RunRequest(
        artifact_context=ArtifactContextRequest.model_validate(
            {
                "operation_id": task.operation_id,
                "artifact_id": artifact.artifact_id,
                "base_version": current,
                "selections": [
                    {
                        **ANCHORS[-1],
                        "elements": None,
                        "comment": "Explain",
                        "drawn_version": version,
                    }
                    for version in [first, current, first, "missing"]
                ],
            }
        ),
    )
    reads = []
    read = store.read_artifact_bytes

    def tracked_read(artifact_id, version_id=None):
        reads.append(version_id)
        return read(artifact_id, version_id)

    monkeypatch.setattr(store, "read_artifact_bytes", tracked_read)
    execution = AgentTaskExecution(
        operation_id=task.operation_id, store=store, control=AgentProcessControl()
    )

    class RemoteStage:
        root = tmp_path / "remote-stage"

        def finalize_inputs(self):
            pass

        def put_directory(self, source, label, *, reuse):
            destination = tmp_path / "remote-stage" / "inputs" / label
            shutil.copytree(source, destination)
            return str(destination)

    remote_stage = RemoteStage() if remote else None

    def stage():
        return stage_artifact_context(
            app.state.service,
            request,
            execution,
            local_stage=None if remote else tmp_path / "crop-stage",
            remote_stage=cast(Any, remote_stage),
            edit_paths={artifact.artifact_id: str(tmp_path / "tracked.png")},
        )

    staged = stage()
    assert staged is not None
    selections = staged.pointer["selections"]
    assert isinstance(selections, list)
    for selection, color in zip(
        selections[:3], [(255, 0, 0), (0, 0, 255), (255, 0, 0)], strict=True
    ):
        with Image.open(Path(selection["crop_path"])) as crop:
            assert crop.convert("RGB").getpixel((0, 0)) == color
    assert "crop_path" not in selections[3]
    assert selections[3]["drawn_version_missing"] is True
    assert reads.count(first) == reads.count(current) == reads.count("missing") == 1
    for version in store.artifact_versions(artifact.artifact_id):
        if version.version_id == first:
            store.artifact_file_path(version).unlink()
    resumed = task.model_copy(
        update={
            "operation_id": "crop-recovery",
            "parent_operation_id": task.operation_id,
            "attempt": 2,
            "status": "running",
        }
    )
    store.create_agent_task(resumed)
    store.record_agent_task_receipt(
        resumed.operation_id,
        "operation_created",
        {
            "kind": task.kind,
            "attempt": 2,
            "has_parent": True,
            "resumed": True,
        },
    )
    execution = AgentTaskExecution(
        operation_id=resumed.operation_id,
        store=store,
        control=AgentProcessControl(),
        continuation=continuation,
    )
    _publish_artifact(store, artifact.artifact_id, _png("green"), "new-current")
    recovered = stage()
    assert recovered is not None
    assert recovered.pointer == {
        **staged.pointer,
        "current_version": store.artifact(artifact.artifact_id).current_version,
    }


@pytest.mark.parametrize("continuation", ["resume", "retry"])
def test_revoking_edit_resume_reuses_staged_context(manifest, tmp_path, continuation):
    from .test_artifact_edit_runtime import _edit_execution, _setup, _stage

    app, request, execution, source, workspace = _setup(manifest, tmp_path)
    _, original = _stage(app, request, execution, workspace)
    assert original is not None
    execution.store.pause_agent_task(execution.operation_id)
    resumed = _edit_execution(
        execution, request, "edit-resumed", parent_operation_id=execution.operation_id, attempt=2
    )
    resumed.continuation = continuation
    _, recovered = _stage(app, request, resumed, workspace)
    assert recovered is not None
    assert recovered.pointer["selections"] == original.pointer["selections"]
    artifact = execution.store.artifact(source.artifact_id)
    assert artifact is not None
    assert recovered.pointer["current_version"] == artifact.current_version
