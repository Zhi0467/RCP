"""Host routing and durable remote cleanup for the browser runtime."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pwd
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path

from pydantic import ValidationError

from rcp import limits
from rcp.browser.models import BrowserReadiness, SessionCheck, SessionLease, Unavailable
from rcp.transport.run_stage import RemoteRunStage
from rcp.transport.ssh import ssh_arguments

logger = logging.getLogger(__name__)

_EPOCH = uuid.uuid4().hex
_PENDING_LOCK = threading.RLock()


def _data_dir(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from rcp.api.app import default_data_dir

        data_dir = default_data_dir()
    return Path(data_dir).expanduser().resolve()


def _limits() -> dict[str, int]:
    return {
        "idle": limits.BROWSER_SESSION_IDLE_SECONDS,
        "start": limits.BROWSER_SESSION_START_TIMEOUT_SECONDS,
        "close": limits.BROWSER_SESSION_CLOSE_TIMEOUT_SECONDS,
        "readiness": limits.BROWSER_READINESS_TIMEOUT_SECONDS,
        "install": limits.BROWSER_INSTALL_TIMEOUT_SECONDS,
        "cap": limits.BROWSER_MAX_SESSIONS_PER_HOST,
    }


def _invoke(request: dict, *, host: str, partition: str | None, data_dir: Path) -> dict:
    root = Path(__file__).parent
    request = {
        **request,
        "root": None if host else str(data_dir / "browser"),
        "limits": _limits(),
        "controller_id": hashlib.sha256(str(data_dir).encode()).hexdigest(),
        "controller_epoch": _EPOCH,
    }
    timeout_key = {"ensure": "start", "release": "close"}.get(request["action"], request["action"])
    timeout = request["limits"][timeout_key]
    # Let the worker finish cancellation and return its diagnostic before its transport ends.
    request["limits"][timeout_key] = max(0.1, timeout - 1)
    if not host:
        from rcp.browser.host import dispatch

        started = time.monotonic()
        try:
            environment = subprocess.run(
                [pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh", "-lc", "/usr/bin/env -0"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
            request["environment"] = dict(
                item.split("=", 1) for item in environment.stdout.split("\0") if "=" in item
            )
            request["limits"][timeout_key] = max(0.1, timeout - (time.monotonic() - started))
            return dispatch(request)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {"reason_code": "runtime_failed", "detail": str(exc)[-2000:]}
    try:
        sources = {
            "rcp.browser.libraries": (root / "libraries.py").read_text(),
            "rcp.transport.compute_process_owner": (
                root.parent / "transport" / "compute_process_owner.py"
            ).read_text(),
            "rcp.browser.host": (root / "host.py").read_text(),
        }
        command = shlex.join(["python3", "-c", (root / "worker.py").read_text()])
        command = 'exec "${SHELL:-/bin/sh}" -lc ' + shlex.quote(command)
        arguments = ssh_arguments(host, command, partition=partition)
        result = subprocess.run(
            arguments,
            input=json.dumps({"sources": sources, "request": request}),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            return {
                "reason_code": "host_unreachable" if host else "runtime_failed",
                "detail": (result.stderr or result.stdout).strip()[-2000:],
            }
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("Browser host returned a non-object result")
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {
            "reason_code": "host_unreachable" if host else "runtime_failed",
            "detail": str(exc)[-2000:],
        }


def _pending_path(data_dir: Path, host: str, owner: str) -> Path:
    token = hashlib.sha256(json.dumps([host, owner]).encode()).hexdigest()
    return data_dir / "browser" / "pending" / f"{token}.json"


def _save_pending(path: Path, *, host: str, request: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps({"host": host, "request": request}))
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _retry_pending(*, host: str, partition: str | None, data_dir: Path) -> bool:
    """Retry this host's queued closures before admitting any new lease."""
    try:
        return _retry_pending_checked(host=host, partition=partition, data_dir=data_dir)
    except (OSError, ValueError) as exc:
        logger.warning("Browser cleanup journal could not be processed: %s", exc)
        return False


def _read_pending(path: Path) -> dict:
    pending = json.loads(path.read_text())
    if not isinstance(pending, dict) or not isinstance(pending.get("host"), str):
        raise ValueError("Invalid browser cleanup journal")
    request = pending.get("request")
    if (
        not isinstance(request, dict)
        or request.get("action") not in ("close", "release")
        or not isinstance(request.get("owner_token"), str)
        or (request["action"] == "close" and not isinstance(request.get("delete_profile"), bool))
        or (request["action"] == "release" and not isinstance(request.get("lease_id"), str))
    ):
        raise ValueError("Invalid browser cleanup request")
    return pending


def _retry_pending_checked(*, host: str, partition: str | None, data_dir: Path) -> bool:
    with _PENDING_LOCK:
        for path in (data_dir / "browser" / "pending").glob("*.json"):
            pending = _read_pending(path)
            if pending["host"] != host:
                continue
            result = _invoke(pending["request"], host=host, partition=partition, data_dir=data_dir)
            if result.get("reason_code") not in (None, "lost"):
                return False
            path.unlink()
            # One retry per call bounds cleanup latency even after a long outage.
            return not any(
                _read_pending(other)["host"] == host for other in path.parent.glob("*.json")
            )
    return True


def ensure_session(
    owner_token: str, *, execution: RemoteRunStage | None, workspace_dir: str
) -> SessionLease | Unavailable:
    try:
        data_dir = _data_dir()
        host = execution.host if execution else ""
        partition = execution.transport_partition if execution else None
        if not _retry_pending(host=host, partition=partition, data_dir=data_dir):
            return Unavailable(
                reason_code="cleanup_pending", detail="Browser cleanup is still pending."
            )
        lease_id = uuid.uuid4().hex
        result = _invoke(
            {
                "action": "ensure",
                "owner_token": owner_token,
                "workspace_dir": workspace_dir,
                "lease_id": lease_id,
            },
            host=host,
            partition=partition,
            data_dir=data_dir,
        )
        if result.get("reason_code"):
            return Unavailable.model_validate(result)
        return SessionLease.model_validate(
            {
                **result,
                "owner_token": owner_token,
                "lease_id": lease_id,
                "host": host,
                "partition": partition,
                "data_dir": str(data_dir),
            }
        )

    except (OSError, ValidationError) as exc:
        return Unavailable(reason_code="runtime_failed", detail=str(exc)[-2000:])


def release_session(lease: SessionLease) -> SessionCheck:
    request = {"action": "release", "owner_token": lease.owner_token, "lease_id": lease.lease_id}
    try:
        result = _invoke(
            request,
            host=lease.host,
            partition=lease.partition,
            data_dir=Path(lease.data_dir),
        )
        try:
            checked = SessionCheck.model_validate(
                {"alive": False, **result} if result.get("reason_code") else result
            )
        except ValidationError as exc:
            checked = SessionCheck(
                alive=False, reason_code="runtime_failed", detail=str(exc)[-2000:]
            )
        if checked.reason_code not in (None, "lost"):
            # A transport failure must not leave a lease busy forever after reconnection.
            with _PENDING_LOCK:
                _save_pending(
                    _pending_path(Path(lease.data_dir), lease.host, lease.lease_id),
                    host=lease.host,
                    request=request,
                )
        return checked

    except (OSError, ValidationError) as exc:
        logger.warning("Browser release could not be recorded: %s", exc)
        return SessionCheck(alive=False, reason_code="runtime_failed", detail=str(exc)[-2000:])


def close_owner(
    owner_token: str, *, execution: RemoteRunStage | None, delete_profile: bool
) -> None:
    try:
        data_dir = _data_dir()
        host = execution.host if execution else ""
        partition = execution.transport_partition if execution else None
        request = {"action": "close", "owner_token": owner_token, "delete_profile": delete_profile}
        with _PENDING_LOCK:
            path = _pending_path(data_dir, host, owner_token)
            # Persist before contacting the host: interruption cannot forget a delete.
            if path.exists():
                existing = _read_pending(path)["request"]
                if existing["action"] != "close":
                    raise ValueError("Unexpected browser cleanup action")
                request["delete_profile"] |= existing["delete_profile"]
            _save_pending(path, host=host, request=request)
            result = _invoke(request, host=host, partition=partition, data_dir=data_dir)
            if not result.get("reason_code"):
                path.unlink()

    except (OSError, ValueError) as exc:
        logger.error("Browser close could not be durably recorded; cleanup requires retry: %s", exc)


def _host_status(
    action: str, *, host: str, os_account: str, data_dir: Path | None
) -> BrowserReadiness:
    try:
        directory = _data_dir(data_dir)
        if action == "install" and not _retry_pending(
            host=host, partition=None, data_dir=directory
        ):
            return BrowserReadiness(
                status="cleanup_pending", detail="Browser cleanup is still pending."
            )
        result = _invoke(
            {"action": action, "expected_account": os_account},
            host=host,
            partition=None,
            data_dir=directory,
        )
        if "status" not in result:
            result = {
                "status": result.get("reason_code", "runtime_failed"),
                "detail": result.get("detail"),
            }
        return BrowserReadiness.model_validate(result)

    except (OSError, ValidationError) as exc:
        return BrowserReadiness(status="runtime_failed", detail=str(exc)[-2000:])


def readiness(
    *, host: str = "", os_account: str = "", data_dir: Path | None = None
) -> BrowserReadiness:
    return _host_status("readiness", host=host, os_account=os_account, data_dir=data_dir)


def install_browser(
    *, host: str = "", os_account: str = "", data_dir: Path | None = None
) -> BrowserReadiness:
    return _host_status("install", host=host, os_account=os_account, data_dir=data_dir)
