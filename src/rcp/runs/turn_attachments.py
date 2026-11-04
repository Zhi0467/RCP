"""One claim binds a chat turn's uploads and project references to its operation."""

from __future__ import annotations

from typing import TypeVar

from rcp.attachments import ChatAttachmentStore
from rcp.project_references import resolve_project_references
from rcp.projects import ProjectCatalog
from rcp.service import CoachRequest, RunRequest
from rcp.storage import AppStore

_Request = TypeVar("_Request", bound=RunRequest | CoachRequest)


def claim_turn_attachments(
    store: AppStore,
    catalog: ProjectCatalog,
    attachment_store: ChatAttachmentStore,
    project_id: str,
    request: _Request,
    *,
    operation_id: str,
    viewer_id: str,
) -> tuple[_Request, str | None]:
    """Copy references into the set, bind it to the operation and name it on the request.

    Returns the request and the claimed batch id, or None when it carries no files.
    Claiming an already resolved set again for the same operation changes nothing.
    """
    if not (request.attachment_set_id or request.references):
        return request, None
    scope_id = request.attachment_scope_id if isinstance(request, CoachRequest) else request.chat_id
    if scope_id is None:
        raise ValueError("Attachments and references require an attachment scope.")
    claimed = attachment_store.claim(
        project_id=project_id,
        chat_id=scope_id,
        client_id=request.attachment_client_id,
        attachment_set_id=request.attachment_set_id,
        operation_id=operation_id,
        reference_files=resolve_project_references(
            store, catalog, project_id, request.references, viewer_id=viewer_id
        ),
    )
    return (
        request.model_copy(
            update={
                "attachment_set_id": None,
                "attachment_client_id": None,
                "attachment_batch_id": claimed.attachment_batch_id,
                "attachments": claimed.attachments,
                "references": [],
            }
        ),
        claimed.attachment_batch_id,
    )
