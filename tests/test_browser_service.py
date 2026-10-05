from __future__ import annotations

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rcp.browser import service
from rcp.browser.models import Unavailable
from rcp.transport.run_stage import RemoteRunStage
from tests.test_browser_host import host as host


def test_lease_releases_on_its_original_host_and_reports_loss(tmp_path, monkeypatch):
    calls = []

    def invoke(request, **kwargs):
        calls.append((request, kwargs))
        if request["action"] == "ensure":
            return {
                "session_name": "owner",
                "invocation_dir": "/workspace",
                "path_prefix": "/tools/bin",
                "env": {"PLAYWRIGHT_CLI_SESSION": "owner"},
            }
        return {"reason_code": "host_unreachable", "detail": "offline"}

    monkeypatch.setattr(service, "_invoke", invoke)
    execution = RemoteRunStage("gpu.example")
    lease = service.ensure_session(
        "owner", execution=execution, workspace_dir="/workspace", data_dir=tmp_path
    )
    assert not isinstance(lease, Unavailable)
    execution.host = "other.example"
    result = service.release_session(
        lease.owner_token,
        lease_id=lease.lease_id,
        execution=RemoteRunStage(lease.host),
        data_dir=tmp_path,
    )
    assert not result.alive and result.reason_code == "host_unreachable"
    assert calls[1][1]["host"] == "gpu.example"
    assert calls[1][0]["lease_id"] == lease.lease_id
    assert "host" not in lease.model_dump()


def test_failed_cleanup_is_durable_retried_and_blocks_ensure(tmp_path, monkeypatch):
    calls = []
    reachable = False

    def invoke(request, **kwargs):
        calls.append(request.copy())
        return {} if reachable else {"reason_code": "host_unreachable", "detail": "offline"}

    monkeypatch.setattr(service, "_invoke", invoke)
    execution = RemoteRunStage("gpu.example")
    service.close_owner("owner", execution=execution, delete_profile=True, data_dir=tmp_path)
    service.close_owner("owner", execution=execution, delete_profile=False, data_dir=tmp_path)
    pending = list((tmp_path / "browser" / "pending").glob("*.json"))
    assert len(pending) == 1
    assert json.loads(pending[0].read_text())["request"]["delete_profile"] is True
    result = service.ensure_session(
        "owner", execution=execution, workspace_dir="/workspace", data_dir=tmp_path
    )
    assert isinstance(result, Unavailable) and result.reason_code == "cleanup_pending"
    assert all(call["action"] == "close" for call in calls)
    # Another owner's stuck cleanup never blocks this one.
    other = service.ensure_session(
        "other", execution=execution, workspace_dir="/other", data_dir=tmp_path
    )
    assert isinstance(other, Unavailable) and other.reason_code == "host_unreachable"
    reachable = True
    assert service._retry_pending(
        host=execution.host, partition=None, data_dir=tmp_path, owner_token="owner"
    )
    assert not pending[0].exists()
    assert service._retry_pending(host=execution.host, partition=None, data_dir=tmp_path)


def test_remote_transport_ships_source_and_uses_login_environment(tmp_path, monkeypatch):
    run_worker = subprocess.run
    calls = []
    monkeypatch.setattr(service, "_limits", lambda: {"readiness": 60})
    monkeypatch.setattr(service, "ssh_arguments", lambda host, command, **kw: [host, command])

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, '{"status":"ready"}', "")

    monkeypatch.setattr(service.subprocess, "run", run)
    result = service.readiness(host="gpu.example", os_account="researcher", data_dir=tmp_path)
    assert result.status == "ready"
    arguments, kwargs = calls[0]
    assert arguments[0] == "gpu.example"
    assert '"${SHELL:-/bin/sh}" -lc' in arguments[1]
    payload = json.loads(kwargs["input"])
    assert payload["request"]["root"] is None
    assert payload["request"]["expected_account"] == "researcher"
    assert (
        payload["sources"]["rcp.browser.host"]
        == (Path(service.__file__).parent / "host.py").read_text()
    )
    assert kwargs["timeout"] == 60
    root = Path(service.__file__).parent
    assert (
        payload["sources"]["rcp.agents.staged_hidden_read"]
        == (root.parent / "agents" / "staged_hidden_read.py").read_text()
    )
    payload["request"] = {
        "action": "close",
        "owner_token": "absent",
        "root": str(tmp_path / "worker"),
        "limits": {"close": 5},
    }
    completed = run_worker(
        [sys.executable, "-c", (root / "worker.py").read_text()],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(completed.stdout) == {}


def test_local_dispatch_uses_login_environment_without_a_python_child(tmp_path, monkeypatch):
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, "PATH=/account/tools/bin\0HOME=/account\0", ""
        )

    def dispatch(request):
        assert request["environment"]["PATH"] == "/account/tools/bin"
        assert request["root"] == str(tmp_path / "browser")
        return {"status": "ready"}

    monkeypatch.setattr(service.subprocess, "run", run)
    monkeypatch.setattr("rcp.browser.host.dispatch", dispatch)
    assert service.readiness(data_dir=tmp_path).status == "ready"
    assert len(calls) == 1
    assert calls[0][-1] == "/usr/bin/env -0"


def test_corrupt_pending_journal_blocks_browser_without_failing_turn(tmp_path, monkeypatch):
    path = tmp_path / "browser" / "pending" / "corrupt.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"host": "", "request": {"action": []}}')
    result = service.ensure_session(
        "owner", execution=None, workspace_dir=str(tmp_path), data_dir=tmp_path
    )
    assert isinstance(result, Unavailable)
    assert result.reason_code == "cleanup_pending"
    assert service.install_browser(data_dir=tmp_path).status == "cleanup_pending"
    assert path.exists()


@pytest.mark.parametrize("reply", [{"reason_code": "host_unreachable", "detail": "Lost reply"}, {}])
def test_lost_ensure_reply_releases_exact_lease_before_profile_deletion(
    host, tmp_path, monkeypatch, reply
):
    calls = []

    def invoke(request, **kwargs):
        calls.append(request.copy())
        host.request.update(request)
        if request["action"] == "ensure":
            pending = list((tmp_path / "browser" / "pending").glob("*.json"))
            assert len(pending) == 1
            assert json.loads(pending[0].read_text())["request"] == {
                "action": "release",
                "owner_token": "first",
                "lease_id": request["lease_id"],
            }
            host.ensure()
            return reply
        return getattr(host, request["action"])()

    monkeypatch.setattr(service, "_invoke", invoke)
    remote = RemoteRunStage("host.example")
    result = service.ensure_session(
        "first", execution=remote, workspace_dir=str(tmp_path / "workspace"), data_dir=tmp_path
    )
    assert isinstance(result, Unavailable)
    pending = list((tmp_path / "browser" / "pending").glob("*.json"))
    assert len(pending) == 1
    recovery = json.loads(pending[0].read_text())["request"]
    assert recovery == {
        "action": "release",
        "owner_token": "first",
        "lease_id": calls[0]["lease_id"],
    }
    service.close_owner("first", execution=remote, delete_profile=True, data_dir=tmp_path)
    assert service._retry_pending(host=remote.host, partition=None, data_dir=tmp_path)
    assert calls[-1] == recovery
    assert not (host.record_path("first").parent / "profile").exists()
    assert not list((tmp_path / "browser" / "pending").glob("*.json"))
    assert host.release()["reason_code"] == "lost"


def test_old_close_reply_keeps_a_newer_delete_request(host, tmp_path, monkeypatch):
    host.ensure()
    host.release()
    old_close_reached, finish_old_close = threading.Event(), threading.Event()
    online = False

    def invoke(request, **kwargs):
        if not online or request["delete_profile"]:
            return {"reason_code": "host_unreachable", "detail": "offline"}
        host.request.update(request)
        result = host.close()
        old_close_reached.set()
        assert finish_old_close.wait(5)
        return result

    monkeypatch.setattr(service, "_invoke", invoke)
    remote = RemoteRunStage("host.example")
    service.close_owner("first", execution=remote, delete_profile=False, data_dir=tmp_path)
    online = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        retry = pool.submit(
            service._retry_pending,
            host=remote.host,
            partition=None,
            data_dir=tmp_path,
            owner_token="first",
        )
        assert old_close_reached.wait(5)
        service.close_owner("first", execution=remote, delete_profile=True, data_dir=tmp_path)
        finish_old_close.set()
        assert not retry.result(timeout=5)
    [pending] = (tmp_path / "browser" / "pending").glob("*.json")
    assert json.loads(pending.read_text())["request"]["delete_profile"] is True
