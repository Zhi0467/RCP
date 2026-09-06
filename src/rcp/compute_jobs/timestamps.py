"""Convert the wrapper's epoch receipts to storage timestamps."""

from __future__ import annotations

from datetime import UTC, datetime


def epoch_timestamp(value: str) -> str:
    return datetime.fromtimestamp(int(value), tz=UTC).isoformat()
