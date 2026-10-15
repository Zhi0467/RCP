from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rcp.storage.models import ProjectTransferTargetAdmissionReceipt


def is_admitter(admission: ProjectTransferTargetAdmissionReceipt | None, user_id: str) -> bool:
    """Only the member who admitted a target transfer may finish its native steps."""

    return admission is not None and admission.admitted_by.user_id == user_id


def is_restore_admitter(
    admission: ProjectTransferTargetAdmissionReceipt | None, *, space_id: str, user_id: str
) -> bool:
    """A restored transfer re-enters only through its exact admitter, space included."""

    return (
        admission is not None
        and admission.admitted_by.space_id == space_id
        and admission.admitted_by.user_id == user_id
    )
