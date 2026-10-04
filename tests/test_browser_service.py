from __future__ import annotations

import json
import subprocess
from pathlib import Path

from rcp.browser import service
from rcp.browser.models import Unavailable
from rcp.transport.run_stage import RemoteRunStage


def test_lease_releases_on_its_original_host_and_reports_loss(tmp_path, monkeypatch):
    monkeypatch.setenv("RCP_DATA_DIR", str(tmp_path))
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
    lease = service.ensure_session("owner", execution=execution, workspace_dir="/workspace")
    assert not isinstance(lease, Unavailable)
    execution.host = "other.example"
    result = service.release_session(lease)
    assert not result.alive and result.reason_code == "host_unreachable"
    assert calls[1][1]["host"] == "gpu.example"
    assert calls[1][0]["lease_id"] == lease.lease_id
    assert "host" not in lease.model_dump()


def test_failed_cleanup_is_durable_retried_and_blocks_ensure(tmp_path, monkeypatch):
    monkeypatch.setenv("RCP_DATA_DIR", str(tmp_path))
    calls = []
    reachable = False

    def invoke(request, **kwargs):
        calls.append(request.copy())
        return {} if reachable else {"reason_code": "host_unreachable", "detail": "offline"}

    monkeypatch.setattr(service, "_invoke", invoke)
    execution = RemoteRunStage("gpu.example")
    service.close_owner("owner", execution=execution, delete_profile=True)
    service.close_owner("owner", execution=execution, delete_profile=False)
    pending = list((tmp_path / "browser" / "pending").glob("*.json"))
    assert len(pending) == 1
    assert json.loads(pending[0].read_text())["request"]["delete_profile"] is True
    result = service.ensure_session("owner", execution=execution, workspace_dir="/workspace")
    assert isinstance(result, Unavailable) and result.reason_code == "cleanup_pending"
    assert all(call["action"] == "close" for call in calls)
    # Another owner's stuck cleanup never blocks this one.
    other = service.ensure_session("other", execution=execution, workspace_dir="/other")
    assert isinstance(other, Unavailable) and other.reason_code == "host_unreachable"
    reachable = True
    assert service._retry_pending(host=execution.host, partition=None, data_dir=tmp_path)
    assert not pending[0].exists()


def test_remote_transport_ships_source_and_uses_login_environment(tmp_path, monkeypatch):
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
    monkeypatch.setenv("RCP_DATA_DIR", str(tmp_path))
    path = tmp_path / "browser" / "pending" / "corrupt.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"host": "", "request": {"action": []}}')
    result = service.ensure_session("owner", execution=None, workspace_dir=str(tmp_path))
    assert isinstance(result, Unavailable)
    assert result.reason_code == "cleanup_pending"
    assert service.install_browser(data_dir=tmp_path).status == "cleanup_pending"
    assert path.exists()
