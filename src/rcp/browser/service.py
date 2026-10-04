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
# Journal entries a thread is sending to a host right now. The lock covers only the
# journal, never a host call, so one slow host cannot stall another.
_INFLIGHT_ENSURES: set[Path] = set()
_INFLIGHT_RETRIES: set[Path] = set()


def _data_dir(data_dir: Path) -> Path:
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


def _retry_pending(
    *, host: str, partition: str | None, data_dir: Path, owner_token: str | None = None
) -> bool:
    """Retry one queued cleanup; report whether this owner (or, for None, the host) is clear."""
    try:
        return _retry_pending_checked(
            host=host, partition=partition, data_dir=data_dir, owner_token=owner_token
        )
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


def _queued(data_dir: Path, host: str) -> list[tuple[Path, dict]]:
    # An acquisition's own journaled release is not cleanup until its reply is lost.
    return [
        (path, pending)
        for path in (data_dir / "browser" / "pending").glob("*.json")
        if path not in _INFLIGHT_ENSURES and (pending := _read_pending(path))["host"] == host
    ]


def _acknowledge(path: Path, request: dict) -> None:
    """Drop a journal entry only if it still holds the request the host confirmed."""
    with _PENDING_LOCK:
        if path.exists() and _read_pending(path)["request"] == request:
            path.unlink()


def _retry_pending_checked(
    *, host: str, partition: str | None, data_dir: Path, owner_token: str | None
) -> bool:
    with _PENDING_LOCK:
        queued = _queued(data_dir, host)
        # This owner's own cleanup goes first; a stuck entry of another owner never blocks it.
        queued.sort(key=lambda item: item[1]["request"]["owner_token"] != owner_token)
        claimed = next((item for item in queued if item[0] not in _INFLIGHT_RETRIES), None)
        if claimed:
            _INFLIGHT_RETRIES.add(claimed[0])
    if claimed:
        path, pending = claimed
        try:
            request = pending["request"]
            if request["action"] == "close":
                # A queued snapshot cannot authorize pruning after another restart.
                request = {**request, "retained_lease_ids": None}
            result = _invoke(request, host=host, partition=partition, data_dir=data_dir)
            # One retry per call bounds cleanup latency even after a long outage.
            if result.get("reason_code") in (None, "lost"):
                _acknowledge(path, pending["request"])
        finally:
            with _PENDING_LOCK:
                _INFLIGHT_RETRIES.discard(path)
    with _PENDING_LOCK:
        return not any(
            owner_token is None or pending["request"]["owner_token"] == owner_token
            for _path, pending in _queued(data_dir, host)
        )


def ensure_session(
    owner_token: str,
    *,
    execution: RemoteRunStage | None,
    workspace_dir: str,
    data_dir: Path,
    retained_lease_ids: tuple[str, ...] | list[str] = (),
) -> SessionLease | Unavailable:
    try:
        data_dir = _data_dir(data_dir)
        host = execution.host if execution else ""
        partition = execution.transport_partition if execution else None
        if not _retry_pending(
            host=host, partition=partition, data_dir=data_dir, owner_token=owner_token
        ):
            return Unavailable(
                reason_code="cleanup_pending", detail="Browser cleanup is still pending."
            )
        lease_id = uuid.uuid4().hex
        path = _pending_path(data_dir, host, lease_id)
        release = {"action": "release", "owner_token": owner_token, "lease_id": lease_id}
        # Journal the release first, so a lost reply cannot strand a busy lease.
        with _PENDING_LOCK:
            _save_pending(path, host=host, request=release)
            _INFLIGHT_ENSURES.add(path)
        try:
            result = _invoke(
                {
                    "action": "ensure",
                    "owner_token": owner_token,
                    "workspace_dir": workspace_dir,
                    "lease_id": lease_id,
                    "retained_lease_ids": list(retained_lease_ids),
                },
                host=host,
                partition=partition,
                data_dir=data_dir,
            )
            if result.get("reason_code"):
                return Unavailable.model_validate(result)
            lease = SessionLease.model_validate(
                {
                    **result,
                    "owner_token": owner_token,
                    "lease_id": lease_id,
                    "host": host,
                    "partition": partition,
                    "data_dir": str(data_dir),
                }
            )
            _acknowledge(path, release)
            return lease
        finally:
            with _PENDING_LOCK:
                _INFLIGHT_ENSURES.discard(path)

    except (OSError, ValidationError) as exc:
        return Unavailable(reason_code="runtime_failed", detail=str(exc)[-2000:])


def release_session(
    owner_token: str, *, lease_id: str, execution: RemoteRunStage | None, data_dir: Path
) -> SessionCheck:
    host = execution.host if execution else ""
    partition = execution.transport_partition if execution else None
    request = {"action": "release", "owner_token": owner_token, "lease_id": lease_id}
    try:
        result = _invoke(
            request,
            host=host,
            partition=partition,
            data_dir=data_dir,
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
                    _pending_path(data_dir, host, lease_id),
                    host=host,
                    request=request,
                )
        return checked

    except (OSError, ValidationError) as exc:
        logger.warning("Browser release could not be recorded: %s", exc)
        return SessionCheck(alive=False, reason_code="runtime_failed", detail=str(exc)[-2000:])


def close_owner(
    owner_token: str,
    *,
    execution: RemoteRunStage | None,
    delete_profile: bool,
    data_dir: Path,
    retained_lease_ids: tuple[str, ...] | list[str] = (),
) -> None:
    try:
        data_dir = _data_dir(data_dir)
        host = execution.host if execution else ""
        partition = execution.transport_partition if execution else None
        request = {
            "action": "close",
            "owner_token": owner_token,
            "delete_profile": delete_profile,
            "retained_lease_ids": list(retained_lease_ids),
        }
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
        # A concurrent close may have widened the entry to a delete meanwhile.
        if not result.get("reason_code"):
            _acknowledge(path, request)

    except (OSError, ValueError) as exc:
        logger.error("Browser close could not be durably recorded; cleanup requires retry: %s", exc)


def _host_status(action: str, *, host: str, os_account: str, data_dir: Path) -> BrowserReadiness:
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


def readiness(*, host: str = "", os_account: str = "", data_dir: Path) -> BrowserReadiness:
    return _host_status("readiness", host=host, os_account=os_account, data_dir=data_dir)


def install_browser(*, host: str = "", os_account: str = "", data_dir: Path) -> BrowserReadiness:
    return _host_status("install", host=host, os_account=os_account, data_dir=data_dir)
