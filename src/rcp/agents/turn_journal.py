"""Stage and invoke the stdlib execution-host turn journal."""

from __future__ import annotations

import hashlib
from importlib.resources import files
from pathlib import PurePosixPath

from rcp.limits import (
    PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS,
    REMOTE_PROVIDER_STOP_POLL_SECONDS,
    REMOTE_PROVIDER_TERM_WAIT_SECONDS,
    TURN_JOURNAL_MAX_BYTES,
    TURN_JOURNAL_MAX_CONTROL_MESSAGES,
    TURN_JOURNAL_MAX_EVENT_BYTES,
    TURN_JOURNAL_MAX_PATCH_BYTES,
    TURN_JOURNAL_MAX_STDERR_BYTES,
    TURN_JOURNAL_MAX_UPLINK_BYTES,
)

STAGED_TURN_JOURNAL_NAME = "staged_turn_journal.py"


def staged_turn_journal_source() -> str:
    return files("rcp.agents").joinpath(STAGED_TURN_JOURNAL_NAME).read_text(encoding="utf-8")


def staged_turn_journal_label() -> str:
    digest = hashlib.sha256(staged_turn_journal_source().encode()).hexdigest()[:16]
    return f"turn-journal-{digest}.py"


def journal_command(
    command: list[str],
    *,
    pid_file: str,
    provider: str,
    runtime_id: str,
    provider_version: str | None,
    close_input_after_initial: bool,
) -> list[str]:
    stage = PurePosixPath(pid_file).parent
    result = [
        "python3",
        str(stage / "inputs" / staged_turn_journal_label()),
        "--pid-file",
        pid_file,
        "--provider",
        provider,
        "--runtime-id",
        runtime_id,
        "--patch-path",
        str(stage / "workspace" / "patch.json"),
        "--max-journal-bytes",
        str(TURN_JOURNAL_MAX_BYTES),
        "--max-event-bytes",
        str(TURN_JOURNAL_MAX_EVENT_BYTES),
        "--max-stderr-bytes",
        str(TURN_JOURNAL_MAX_STDERR_BYTES),
        "--max-patch-bytes",
        str(TURN_JOURNAL_MAX_PATCH_BYTES),
        "--max-uplink-bytes",
        str(TURN_JOURNAL_MAX_UPLINK_BYTES),
        "--max-control-messages",
        str(TURN_JOURNAL_MAX_CONTROL_MESSAGES),
        "--stop-hold-seconds",
        str(PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS),
        "--stop-grace-seconds",
        str(REMOTE_PROVIDER_TERM_WAIT_SECONDS),
        "--poll-seconds",
        str(REMOTE_PROVIDER_STOP_POLL_SECONDS),
    ]
    if provider_version:
        result.extend(["--provider-version", provider_version])
    if close_input_after_initial:
        result.append("--close-input-after-initial")
    return [*result, "--", *command]
