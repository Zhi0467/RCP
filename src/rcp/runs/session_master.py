"""The durable copy of the master contract a native session was given.

A session's master is recorded byte for byte on the operation that bootstrapped it, and
restored from that record into the selected stage on every launch that points to it.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath

from rcp.runs.shared import _stage_or_reuse_task_input
from rcp.storage import AppStore
from rcp.transport import RemoteRunStage

SESSION_MASTER_ROLE = "session_master"

_LABEL_DIGEST = re.compile(r"-([0-9a-f]{16})\.md\Z")


def session_master_label(prefix: str, content: str) -> str:
    """Name a master by its content, so one label always holds the same bytes."""

    return f"{prefix}-{_sha256(content)[:16]}.md"


def record_session_master(store: AppStore, operation_id: str, content: str) -> str:
    """Record the exact master bytes on the operation that sends them; return their digest."""

    digest = _sha256(content)
    store.record_agent_task_contract(operation_id, SESSION_MASTER_ROLE, content, digest)
    return digest


def stage_session_master(
    store: AppStore,
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    operation_id: str,
    sha256: str,
    path: str,
) -> str:
    """Restore a recorded master into the selected stage under its own label.

    The stage itself must already exist; this writes one immutable input inside it.
    """

    content = store.agent_task_contract(operation_id, SESSION_MASTER_ROLE)
    if content is None:
        raise ValueError("The native session's recorded master contract is unavailable.")
    if _sha256(content) != sha256:
        raise ValueError("The native session's recorded master contract is corrupt.")
    return _stage_or_reuse_task_input(local_stage, remote_stage, PurePosixPath(path).name, content)


def read_legacy_session_master(
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    path: str,
) -> str | None:
    """Read a pre-record master from this stage, only if its bytes match its own label."""

    label = PurePosixPath(path).name
    match = _LABEL_DIGEST.search(label)
    if match is None:
        return None
    if remote_stage is not None:
        try:
            content = remote_stage.read_input_text(label)
        except ValueError:
            return None
    else:
        assert local_stage is not None
        target = local_stage / "inputs" / label
        if target.is_symlink() or not target.is_file():
            return None
        content = target.read_text(encoding="utf-8")
    if _sha256(content)[:16] != match.group(1):
        return None
    return content


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
