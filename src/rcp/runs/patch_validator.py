from __future__ import annotations

import asyncio
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rcp.background import AgentTaskExecution
from rcp.limits import (
    PATCH_SELF_CHECK_MAX_COUNT,
    PATCH_SELF_CHECK_MAX_REQUEST_BYTES,
    PATCH_SELF_CHECK_POLL_SECONDS,
)
from rcp.transport import RemoteRunStage, StateUnavailable

_MAILBOX_ID = r"[a-f0-9]{32}"
_REQUEST_ID = r"[a-f0-9]{32}"


class PatchValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["valid", "invalid", "unavailable"]
    messages: list[str] = Field(default_factory=list)
    live_revision: int | None = Field(default=None, ge=0)
    candidate_revision: int | None = Field(default=None, ge=0)


@dataclass
class PatchValidationBudget:
    count: int = 0


class _PatchValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    mailbox_id: str = Field(pattern=rf"^{_MAILBOX_ID}$")
    request_id: str = Field(pattern=rf"^{_REQUEST_ID}$")
    patch: str


class _PatchValidationResponse(PatchValidationResult):
    version: Literal[1] = 1
    request_id: str = Field(pattern=rf"^{_REQUEST_ID}$")


VALIDATOR_CLIENT_SOURCE = r"""from __future__ import print_function

import json
import os
import sys
import tempfile
import time
import uuid


VALID = 0
INVALID = 1
UNAVAILABLE = 2


def _atomic_json(path, value):
    directory = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".rcp-validator-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    if len(sys.argv) != 5:
        print("usage: rcp-validator-client.py PATCH_PATH MAILBOX_ID TIMEOUT_SECONDS WORKSPACE")
        return UNAVAILABLE
    patch_path, mailbox_id, timeout_text, workspace = sys.argv[1:]
    try:
        timeout = float(timeout_text)
    except ValueError:
        print("RCP validator timeout is invalid.")
        return UNAVAILABLE
    workspace = os.path.abspath(workspace)
    patch_path = os.path.abspath(patch_path)
    if os.path.dirname(patch_path) != workspace or os.path.basename(patch_path) != "patch.json":
        print("Patch validation accepts only this run workspace's patch.json.")
        return INVALID
    try:
        with open(patch_path, "r", encoding="utf-8") as stream:
            patch = stream.read()
    except (OSError, UnicodeError) as exc:
        print("patch.json is unavailable or not UTF-8: {0}".format(exc))
        return INVALID

    request_id = uuid.uuid4().hex
    prefix = "rcp-validator-{0}-{1}".format(mailbox_id, request_id)
    request_path = os.path.join(workspace, prefix + ".request.json")
    response_path = os.path.join(workspace, prefix + ".response.json")
    try:
        _atomic_json(
            request_path,
            {
                "version": 1,
                "mailbox_id": mailbox_id,
                "request_id": request_id,
                "patch": patch,
            },
        )
    except OSError as exc:
        print("RCP validator request could not be written: {0}".format(exc))
        return UNAVAILABLE

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(response_path, "r", encoding="utf-8") as stream:
                response = json.load(stream)
        except FileNotFoundError:
            time.sleep(0.1)
            continue
        except (OSError, UnicodeError, ValueError) as exc:
            print("RCP validator response could not be read: {0}".format(exc))
            return UNAVAILABLE
        if response.get("request_id") != request_id:
            print("RCP validator returned a mismatched response.")
            return UNAVAILABLE
        rendered = json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True)
        print(rendered)
        if response.get("status") == "valid":
            return VALID
        if response.get("status") == "invalid":
            return INVALID
        return UNAVAILABLE
    print("RCP validator did not answer before the timeout.")
    return UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
"""


async def serve_patch_validation_mailbox(
    *,
    mailbox_id: str,
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    execution: AgentTaskExecution | None,
    validate: Callable[[str], PatchValidationResult],
    stop: asyncio.Event,
    budget: PatchValidationBudget,
) -> None:
    """Answer bounded patch checks while one provider process owns the workspace."""

    if re.fullmatch(_MAILBOX_ID, mailbox_id) is None:
        raise ValueError("validator mailbox id is malformed")
    seen: set[str] = set()
    while True:
        try:
            names = await asyncio.to_thread(_workspace_files, workspace, remote_stage)
        except (OSError, StateUnavailable, ValueError) as exc:
            _record_mailbox_unavailable(execution, str(exc))
            return
        request_names = sorted(
            name
            for name in names
            if name.startswith(f"rcp-validator-{mailbox_id}-")
            and name.endswith(".request.json")
            and name not in seen
        )
        for name in request_names:
            seen.add(name)
            budget.count += 1
            count = budget.count
            request_id = _request_id_from_name(name, mailbox_id)
            if request_id is None:
                continue
            if count > PATCH_SELF_CHECK_MAX_COUNT:
                result = PatchValidationResult(
                    status="unavailable",
                    messages=["This task has reached its bounded RCP validator self-check limit."],
                )
            else:
                result = await _answer_request(
                    name,
                    mailbox_id=mailbox_id,
                    request_id=request_id,
                    workspace=workspace,
                    remote_stage=remote_stage,
                    validate=validate,
                )
            response = _PatchValidationResponse(request_id=request_id, **result.model_dump())
            response_name = name.removesuffix(".request.json") + ".response.json"
            try:
                await asyncio.to_thread(
                    _write_workspace_text,
                    workspace,
                    remote_stage,
                    response_name,
                    response.model_dump_json(indent=2) + "\n",
                )
            except (OSError, StateUnavailable, ValueError) as exc:
                _record_mailbox_unavailable(execution, str(exc))
                return
            _record_self_check(execution, count, result)

        if stop.is_set():
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=PATCH_SELF_CHECK_POLL_SECONDS)


def cleanup_patch_validation_mailbox(
    *,
    mailbox_id: str,
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    execution: AgentTaskExecution | None,
) -> None:
    """Remove one pass's request/response receipts from reusable conversation scratch."""

    try:
        names = _workspace_files(workspace, remote_stage)
        for name in names:
            if not name.startswith(f"rcp-validator-{mailbox_id}-"):
                continue
            if not name.endswith((".request.json", ".response.json")):
                continue
            if remote_stage is not None:
                remote_stage.remove_workspace_file(name)
            else:
                path = workspace / name
                if path.is_symlink() or not path.is_file():
                    continue
                path.unlink()
    except (OSError, StateUnavailable, ValueError) as exc:
        _record_mailbox_unavailable(execution, f"mailbox cleanup failed: {exc}")


def prepare_patch_validation_mailbox(
    *,
    mailbox_id: str,
    workspace: Path,
    remote_stage: RemoteRunStage | None,
) -> None:
    """Fail closed unless one stable mailbox prefix is clean before provider launch."""

    if re.fullmatch(_MAILBOX_ID, mailbox_id) is None:
        raise ValueError("validator mailbox id is malformed")
    for name in _workspace_files(workspace, remote_stage):
        if not name.startswith(f"rcp-validator-{mailbox_id}-"):
            continue
        if not name.endswith((".request.json", ".response.json")):
            continue
        if remote_stage is not None:
            remote_stage.remove_workspace_file(name)
            continue
        path = workspace / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"validator mailbox entry is unsafe: {name}")
        path.unlink()


async def _answer_request(
    name: str,
    *,
    mailbox_id: str,
    request_id: str,
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    validate: Callable[[str], PatchValidationResult],
) -> PatchValidationResult:
    try:
        text = await asyncio.to_thread(_read_workspace_text, workspace, remote_stage, name)
        if len(text.encode("utf-8")) > PATCH_SELF_CHECK_MAX_REQUEST_BYTES:
            raise ValueError("Patch validator request exceeds the configured size limit.")
        request = _PatchValidationRequest.model_validate_json(text)
        if request.mailbox_id != mailbox_id or request.request_id != request_id:
            raise ValueError("Patch validator request identity does not match its file name.")
    except (FileNotFoundError, OSError, StateUnavailable) as exc:
        return PatchValidationResult(status="unavailable", messages=[str(exc)])
    except (UnicodeError, ValueError, ValidationError) as exc:
        return PatchValidationResult(status="invalid", messages=[str(exc)])
    return await asyncio.to_thread(validate, request.patch)


def _request_id_from_name(name: str, mailbox_id: str) -> str | None:
    match = re.fullmatch(
        rf"rcp-validator-{re.escape(mailbox_id)}-({_REQUEST_ID})\.request\.json",
        name,
    )
    return match.group(1) if match else None


def _workspace_files(workspace: Path, remote_stage: RemoteRunStage | None) -> list[str]:
    if remote_stage is not None:
        return remote_stage.list_workspace_files()
    if workspace.is_symlink() or not workspace.is_dir():
        raise StateUnavailable(f"run workspace {workspace} is unavailable")
    return sorted(
        entry.name for entry in os.scandir(workspace) if entry.is_file(follow_symlinks=False)
    )


def _read_workspace_text(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    name: str,
) -> str:
    if remote_stage is not None:
        return remote_stage.read_workspace_text(name)
    if Path(name).name != name:
        raise ValueError("workspace file name must be a plain base name")
    path = workspace / name
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"workspace file is absent: {name}")
    return path.read_text(encoding="utf-8")


def _write_workspace_text(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    name: str,
    content: str,
) -> None:
    if remote_stage is not None:
        remote_stage.write_workspace_text(name, content)
        return
    if re.fullmatch(r"[A-Za-z0-9._-]+", name) is None or Path(name).name != name:
        raise ValueError("workspace file name contains unsupported characters")
    target = workspace / name
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError(f"workspace target is not a regular file: {name}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".rcp-validator-", dir=workspace)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _record_self_check(
    execution: AgentTaskExecution | None,
    count: int,
    result: PatchValidationResult,
) -> None:
    if execution is None:
        return
    execution.store.record_agent_task_event(
        execution.operation_id,
        (
            f"Patch self-check {count}/{PATCH_SELF_CHECK_MAX_COUNT}: "
            f"{result.status}"
            + (
                f" against live graph revision {result.live_revision}"
                if result.live_revision is not None
                else ""
            )
            + "."
        ),
        level="info" if result.status == "valid" else "warning",
    )
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "patch_self_check",
        {
            "count": count,
            "limit": PATCH_SELF_CHECK_MAX_COUNT,
            **result.model_dump(mode="json"),
        },
        tier="diagnostic",
    )


def _record_mailbox_unavailable(execution: AgentTaskExecution | None, detail: str) -> None:
    if execution is None:
        return
    execution.store.record_agent_task_event(
        execution.operation_id,
        f"Patch validator became unavailable: {' '.join(detail.split())[:400]}",
        level="warning",
    )
