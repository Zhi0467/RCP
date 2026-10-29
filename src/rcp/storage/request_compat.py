from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Persisted request compatibility is deliberately closed. A field appears here
# only after RCP shipped it, later retired it, and documented why dropping that
# exact field preserves the request's meaning. Unknown fields are left in place
# so the request model rejects them rather than silently guessing a migration.
# Releases through 0.4.29 froze each turn's compute selection into every
# RunRequest. 0.4.31 gives every turn all project machines at launch, so the
# frozen selection carries no meaning a current launch would honor.
_RETIRED_COMPUTE_FIELDS: Final = frozenset({"active_compute_ids", "resolved_compute_context"})
_RETIRED_REQUEST_FIELDS: Final[dict[str, frozenset[str]]] = {
    "auto_research": frozenset({"ending"}),
    **dict.fromkeys(
        ("seed", "refresh", "node_chat", "project_chat", "branch_merge", "artifact_edit"),
        _RETIRED_COMPUTE_FIELDS,
    ),
}


def migrate_stored_task_request(
    kind: str,
    stored: Mapping[str, object],
    *,
    operation_id: str | None = None,
    warn: bool = True,
) -> dict[str, object]:
    """Apply the explicit compatibility allowlist to one persisted task request."""

    migrated = dict(stored)
    retired = _RETIRED_REQUEST_FIELDS.get(kind, frozenset())
    removed = sorted(set(migrated) & retired)
    if not removed:
        return migrated
    for field in removed:
        migrated.pop(field)
    if warn:
        logger.warning(
            "Dropped retired field(s) %s from stored %s task %s.",
            ", ".join(removed),
            kind,
            operation_id or "<unknown>",
        )
    return migrated


def persisted_request_roundtrips(request: BaseModel, stored: Mapping[str, object]) -> bool:
    """Return whether ``request`` is exactly what ``stored`` meant.

    This is the one rule every launch and finalization check applies. A row
    written before a field existed omits it, and the field's default is the
    legacy meaning. Every other difference fails.
    """

    dumped = request.model_dump(mode="json")
    fields = type(request).model_fields
    added = {
        key
        for key, value in dumped.items()
        if key not in stored
        and key in fields
        and not fields[key].is_required()
        and value == fields[key].get_default(call_default_factory=True)
    }
    return {key: value for key, value in dumped.items() if key not in added} == stored
