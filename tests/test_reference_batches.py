import io
import uuid

import pytest

from rcp.attachments import ChatAttachmentStore, ProjectReferenceSource
from rcp.project_references import ResolvedProjectReference


def test_reference_caps_and_admission_rollback(tmp_path, monkeypatch):
    store = ChatAttachmentStore(tmp_path / "attachments")
    chat, client, operation = (str(uuid.uuid4()) for _ in range(3))
    scope = dict(project_id="project", chat_id=chat, client_id=client)
    uploaded = store.add(
        **scope, filename="upload.txt", media_type="text/plain", source=io.BytesIO(b"upload")
    )
    reference = ResolvedProjectReference(
        "result.bin",
        "Result",
        "application/octet-stream",
        b"frozen",
        ProjectReferenceSource(kind="artifact", source_id="a" * 24, version="1"),
    )
    admission = dict(**scope, attachment_set_id=uploaded.attachment_set_id, operation_id=operation)
    for limit, value in [
        ("CHAT_ATTACHMENT_MAX_COUNT", 1),
        ("CHAT_ATTACHMENT_MAX_FILE_BYTES", 5),
        ("CHAT_ATTACHMENT_MAX_TOTAL_BYTES", 10),
    ]:
        with monkeypatch.context() as patch:
            patch.setattr(f"rcp.attachments.{limit}", value)
            with pytest.raises(ValueError):
                store.claim(**admission, reference_files=[reference])
    batch = store.claim(**admission, reference_files=[reference])
    store.release(batch.attachment_batch_id, operation)
    reclaimed = store.claim(**admission)
    assert [item.attachment_id for item in reclaimed.attachments] == [
        uploaded.attachment.attachment_id
    ]
    server_batch = store.claim(**scope, operation_id=operation, reference_files=[reference])
    store.release(server_batch.attachment_batch_id, operation)
    assert not (store.root / server_batch.attachment_batch_id).exists()
