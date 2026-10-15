from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rcp.storage.models import ProjectProvisioningRequestRecord


def is_requester(record: ProjectProvisioningRequestRecord, user_id: str) -> bool:
    """Only the member who authorized a preparation may cancel or confirm it."""

    return record.authorized_by.user_id == user_id
