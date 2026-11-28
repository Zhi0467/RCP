from __future__ import annotations

import json
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


class LimitEvidence(BaseModel):
    exhausted: bool
    resets_at: datetime | None = None


def _exhausted_windows(windows: list[tuple[bool, datetime | None]]) -> LimitEvidence:
    return LimitEvidence(
        exhausted=any(exhausted for exhausted, _ in windows),
        resets_at=max(
            (reset for exhausted, reset in windows if exhausted and reset is not None),
            default=None,
        ),
    )


def _codex(snapshot: dict[str, object]) -> LimitEvidence:
    windows: list[tuple[bool, datetime | None]] = []
    for name in ("primary", "secondary"):
        window = snapshot.get(name)
        if isinstance(window, dict):
            used = window.get("usedPercent")
            windows.append(
                (
                    isinstance(used, (int, float)) and not isinstance(used, bool) and used >= 100,
                    _timestamp(window.get("resetsAt")),
                )
            )
    return _exhausted_windows(windows)


def _claude(snapshot: dict[str, object]) -> LimitEvidence:
    windows = snapshot.get("unifiedWindows")
    values = list(windows.values()) if isinstance(windows, dict) else []
    values.append(snapshot)
    exhausted_windows: list[tuple[bool, datetime | None]] = []
    for window in values:
        if not isinstance(window, dict):
            continue
        used = window.get("utilization")
        exhausted = window.get("status") == "rejected" or (
            isinstance(used, (int, float)) and not isinstance(used, bool) and used >= 1
        )
        exhausted_windows.append((exhausted, _timestamp(window.get("resetsAt"))))
    return _exhausted_windows(exhausted_windows)


def _iso_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _agy(snapshot: dict[str, object]) -> LimitEvidence:
    groups = snapshot.get("groups")
    if not isinstance(groups, list):
        raise ValueError("Antigravity usage has no groups list")
    windows: list[tuple[bool, datetime | None]] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("buckets"), list):
            raise ValueError("Antigravity usage group has no buckets list")
        for bucket in group["buckets"]:
            remaining = bucket.get("remaining_fraction") if isinstance(bucket, dict) else None
            if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
                raise ValueError("Antigravity usage bucket has no remaining_fraction")
            windows.append((remaining <= 0, _iso_timestamp(bucket.get("reset_time"))))
    return _exhausted_windows(windows)


def parse_agy_usage(stdout: str) -> dict[str, object]:
    payload = json.loads(stdout)
    command = payload.get("command") if isinstance(payload, dict) else None
    data = command.get("data") if isinstance(command, dict) else None
    if not isinstance(data, dict):
        raise ValueError("Antigravity usage has no command.data")
    _agy(data)
    return data


def limit_evidence(provider: str, snapshot: dict[str, object]) -> LimitEvidence:
    if provider == "codex":
        return _codex(snapshot)
    if provider == "claude":
        return _claude(snapshot)
    if provider == "agy":
        return _agy(snapshot)
    raise ValueError(f"No usage-limit evidence parser for {provider}")


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
    if snapshot is not None:
        evidence = limit_evidence(provider, snapshot)
    else:
        evidence = LimitEvidence(exhausted=True, resets_at=_text_reset(provider, error, failed_at))
    return reset_from_evidence(evidence, failed_at=failed_at)
