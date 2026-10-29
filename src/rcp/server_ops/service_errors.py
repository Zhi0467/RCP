"""Group the service journal's application errors by a secret-free signature.

Doctor reads `journalctl --output=json` for its own unit. Each stderr line of a
log record is one journal entry, so a record and its traceback arrive as
consecutive entries. A signature is the logger name, the exception type and the
function of the traceback's final frame. Message text never enters a signature
because it can carry ids, paths and secrets.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

# The `rcp` package handler (`%(levelname)s %(name)s: %(message)s`) and uvicorn's
# default handler (`%(levelprefix)s %(message)s`, its errors log as uvicorn.error).
_RECORD = re.compile(r"(DEBUG|INFO|WARNING|ERROR|CRITICAL) ([A-Za-z_][A-Za-z0-9_.-]*): ")
_UVICORN_RECORD = re.compile(r"(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s")
_TRACEBACK = "Traceback (most recent call last):"
_FRAME = re.compile(r'\s+File "[^"\n]*", line \d+, in (\S+)')
_EXCEPTION = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)(?::\s.*|:)?")
_FUNCTION = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|<[a-z]+>")
_ERROR_LEVELS = frozenset({"ERROR", "CRITICAL"})


@dataclass(frozen=True)
class ErrorSignature:
    logger: str
    exception: str | None
    function: str | None


@dataclass(frozen=True)
class ErrorGroup:
    signature: ErrorSignature
    count: int
    first_seen: datetime


def journal_entries(output: str, *, max_chars: int) -> list[tuple[datetime, str]]:
    """Oldest-first timestamped messages from `journalctl --output=json --reverse`.

    The input is newest first: with `--since`, journalctl 249 applies `--lines` from
    the oldest end, so only `--reverse` keeps the newest entries. Only the newest
    `max_chars` are read. An entry whose MESSAGE journalctl prints as null or a byte
    array is skipped.
    """

    if len(output) > max_chars:
        output = output[:max_chars].rpartition("\n")[0]
    entries: list[tuple[datetime, str]] = []
    for line in reversed(output.splitlines()):
        try:
            entry = json.loads(line)
            message = entry["MESSAGE"]
            micros = int(entry["__REALTIME_TIMESTAMP"])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(message, str):
            entries.append((datetime.fromtimestamp(micros / 1_000_000, UTC), message))
    return entries


def group_errors(entries: Iterable[tuple[datetime, str]]) -> tuple[ErrorGroup, ...]:
    """Count ERROR and CRITICAL records by signature, most frequent, then earliest, first."""

    counts: dict[ErrorSignature, int] = {}
    first_seen: dict[ErrorSignature, datetime] = {}
    record: tuple[datetime, str] | None = None
    exception: str | None = None
    function: str | None = None
    in_frames = False

    def close() -> None:
        if record is None:
            return
        signature = ErrorSignature(record[1], exception, function)
        counts[signature] = counts.get(signature, 0) + 1
        first_seen.setdefault(signature, record[0])

    for seen_at, message in entries:
        if header := _record_header(message):
            close()
            level, logger = header
            record = (seen_at, logger) if level in _ERROR_LEVELS else None
            exception = function = None
            in_frames = False
        elif record is None:
            continue
        elif message == _TRACEBACK:
            # A chained exception prints the cause first; the last one raised wins.
            in_frames = True
        elif in_frames and (frame := _FRAME.fullmatch(message)):
            function = frame[1] if _FUNCTION.fullmatch(frame[1]) else None
        elif in_frames and not message[:1].isspace():
            if raised := _EXCEPTION.fullmatch(message):
                exception = raised[1]
            in_frames = False
    close()
    return tuple(
        ErrorGroup(signature, count, first_seen[signature])
        for signature, count in sorted(counts.items(), key=lambda item: -item[1])
    )


def _record_header(message: str) -> tuple[str, str] | None:
    """The level and logger of a line that starts a log record."""

    if header := _RECORD.match(message):
        return header[1], header[2]
    if header := _UVICORN_RECORD.match(message):
        return header[1], "uvicorn.error"
    return None


__all__ = ["ErrorGroup", "ErrorSignature", "group_errors", "journal_entries"]
