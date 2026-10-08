"""The one rule every reader applies to a stored task operation identity."""

from __future__ import annotations

import uuid


def canonical_operation_uuid(value: str, *, label: str) -> str:
    """Accept either lowercase spelling of a stored task operation identity.

    RCP mints ordinary task ids with UUID4 and deterministic Experiment-loop and
    Auto-research child ids with UUID5. Question follow-ups use bare hex; preserve
    the stored spelling because it is also the task lookup key.
    """

    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be a canonical UUID") from exc
    if parsed.version not in (4, 5) or value not in (str(parsed), parsed.hex):
        raise ValueError(f"{label} must be a lowercase UUID4 or UUID5, hyphenated or bare hex")
    return value


__all__ = ["canonical_operation_uuid"]
