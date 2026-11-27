from __future__ import annotations

import re
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


def _codex(snapshot: dict[str, object]) -> datetime | None:
    resets = []
    for name in ("primary", "secondary"):
        window = snapshot.get(name)
        if not isinstance(window, dict):
            continue
        used = window.get("usedPercent")
        reset = _timestamp(window.get("resetsAt"))
        if isinstance(used, (int, float)) and used >= 100 and reset is not None:
            resets.append(reset)
    return max(resets, default=None)


def _claude(snapshot: dict[str, object]) -> datetime | None:
    windows = snapshot.get("unifiedWindows")
    resets = []
    if isinstance(windows, dict):
        for window in windows.values():
            if not isinstance(window, dict):
                continue
            used = window.get("utilization")
            exhausted = window.get("status") == "rejected" or (
                isinstance(used, (int, float)) and used >= 1
            )
            reset = _timestamp(window.get("resetsAt"))
            if exhausted and reset is not None:
                resets.append(reset)
    if resets:
        return max(resets)
    return _timestamp(snapshot.get("resetsAt")) if snapshot.get("status") == "rejected" else None


def _text_reset(provider: str, error: str, failed_at: datetime) -> datetime | None:
    if provider == "agy":
        match = re.search(r"resets in\s+(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", error, re.I)
        if match and any(match.groups()):
            hours, minutes, seconds = (int(value or 0) for value in match.groups())
            return failed_at + timedelta(hours=hours, minutes=minutes, seconds=seconds)
    if provider == "codex":
        match = re.search(
            r"try again at\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2}| UTC))",
            error,
            re.I,
        )
        if match:
            try:
                return datetime.fromisoformat(
                    match[1].replace(" UTC", "+00:00").replace("Z", "+00:00")
                )
            except ValueError:
                return None
    return None


def limit_reset(
    provider: str,
    snapshot: dict[str, object] | None,
    error: str,
    *,
    failed_at: datetime,
) -> LimitReset:
    reset = None
    if snapshot is not None:
        if provider == "codex":
            reset = _codex(snapshot)
        elif provider == "claude":
            reset = _claude(snapshot)
    if reset is None:
        reset = _text_reset(provider, error, failed_at)
    if reset is not None and reset >= failed_at + timedelta(seconds=LIMIT_RESET_MIN_SECONDS):
        return LimitReset(resets_at=reset.astimezone(UTC), estimated=False)
    return LimitReset(
        resets_at=(failed_at + timedelta(seconds=PROVIDER_LOGIN_LIMIT_HINT_SECONDS)).astimezone(
            UTC
        ),
        estimated=True,
    )
