from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from rcp.limits import LIMIT_RESET_MIN_SECONDS, PROVIDER_LOGIN_LIMIT_HINT_SECONDS


class LimitReset(BaseModel):
    resets_at: datetime
    estimated: bool


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (ValueError, OverflowError, OSError):
        return None


class LimitEvidence(BaseModel):
    exhausted: bool
    resets_at: datetime | None = None


def _exhausted_windows(windows: list[tuple[bool, datetime | None]]) -> LimitEvidence:
    """The latest reset of the exhausted windows, unknown if any of them names none."""
    resets = [reset for exhausted, reset in windows if exhausted]
    return LimitEvidence(
        exhausted=bool(resets),
        resets_at=None if None in resets else max(resets, default=None),
    )


def _iso_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def reset_from_evidence(evidence: LimitEvidence, *, failed_at: datetime) -> LimitReset:
    reset = evidence.resets_at
    if reset is not None and reset >= failed_at + timedelta(seconds=LIMIT_RESET_MIN_SECONDS):
        return LimitReset(resets_at=reset.astimezone(UTC), estimated=False)
    return LimitReset(
        resets_at=(failed_at + timedelta(seconds=PROVIDER_LOGIN_LIMIT_HINT_SECONDS)).astimezone(
            UTC
        ),
        estimated=True,
    )
