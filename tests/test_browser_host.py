from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from rcp.browser.host import HostRuntime, UnavailableError, dispatch, pinned_config


def request(tmp_path: Path, owner: str = "first", **extra) -> dict:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return {
        "root": str(tmp_path / "browser"),
        "action": "ensure",
        "owner_token": owner,
        "workspace_dir": str(workspace),
        "controller_id": "controller",
        "controller_epoch": "epoch",
        "lease_id": owner,
        "limits": {
            "start": 10,
            "close": 10,
            "readiness": 10,
            "install": 10,
            "idle": 1800,
            "cap": 1,
        },
        **extra,
    }


class Host(HostRuntime):
    """Fake OS boundary; retain real ownership, disk state, and lease transitions."""

    live: set[str]
    starts: list[str]
    closes: list[str]

    def readiness(self):
        return {"status": "ready"}

    def executable(self):
        return "/owned/chromium"

    def alive(self, record):
        return record["owner_token"] in self.live

    def owner_status(self, record):
        return self.alive(record)

    def stop_owner(self, record):
        self.live.discard(record["owner_token"])

    def start(self, record, executable):
        self.live.add(record["owner_token"])
        self.starts.append(record["owner_token"])
        (self.record_path(record["owner_token"]).parent / "profile").mkdir(exist_ok=True)

    def cli_run(self, record, *arguments, check=True):
        assert arguments == ("close",)
        self.closes.append(record["owner_token"])
        self.live.discard(record["owner_token"])


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(Host, "live", set(), raising=False)
    monkeypatch.setattr(Host, "starts", [], raising=False)
    monkeypatch.setattr(Host, "closes", [], raising=False)
    return Host(request(tmp_path))


def test_ensure_reuses_live_session_and_adopts_after_controller_restart(host):
    first = host.ensure()
    again = host.ensure()
    assert first == again
    assert host.starts == ["first"]
    host.request.update(controller_epoch="restarted", lease_id="new")
    assert host.ensure() == first
    assert list(json.loads(host.record_path("first").read_text())["leases"]) == ["new"]
    assert host.starts == ["first"]


def test_busy_capacity_then_release_allows_idle_eviction(host, tmp_path):
    host.ensure()
    second = Host(request(tmp_path, "second"))
    with pytest.raises(UnavailableError) as error:
        second.ensure()
    assert error.value.code == "capacity"
    assert host.release()["alive"]
    second.ensure()
    assert host.closes == ["first"]
    assert host.live == {"second"}
    assert (host.record_path("first").parent / "profile").is_dir()


def test_lru_idle_selection(host, tmp_path):
    host.limits["cap"] = 2
    host.ensure()
    host.release()
    second = Host(request(tmp_path, "second"))
    second.limits["cap"] = 2
    second.ensure()
    second.release()
    third = Host(request(tmp_path, "third"))
    third.limits["cap"] = 2
    third.ensure()
    assert host.closes == ["first"]
    assert host.live == {"second", "third"}


def test_killed_daemon_is_lost_and_next_ensure_restarts(host):
    host.ensure()
    host.live.clear()
    assert host.release() == {"alive": False, "reason_code": "lost", "detail": None}
    host.ensure()
    assert host.starts == ["first", "first"]


def test_close_defers_active_lease_then_gracefully_deletes_profile(host):
    host.ensure()
    host.request["delete_profile"] = True
    host.close()
    assert host.live == {"first"}
    assert host.release()["alive"]
    assert host.closes == ["first"]
    assert not host.record_path("first").parent.exists()
    host.close()  # Idempotent deletion.


def test_failed_close_keeps_intent_and_profile(host, monkeypatch):
    host.ensure()
    host.release()
    monkeypatch.setattr(host, "cli_run", lambda *a: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(OSError):
        host.close_record(json.loads(host.record_path("first").read_text()), delete=True)
    state = json.loads(host.record_path("first").read_text())
    assert state["pending_close"] and state["delete_profile"]
    assert (host.record_path("first").parent / "profile").exists()


def test_pinned_config_and_environment_override_ambient_attachment(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_MCP_CDP_ENDPOINT", "http://ambient")
    monkeypatch.setenv("PLAYWRIGHT_MCP_HEADLESS", "false")
    monkeypatch.setenv("NODE_OPTIONS", "--inspect")
    runtime = HostRuntime(request(tmp_path))
    assert "PLAYWRIGHT_MCP_CDP_ENDPOINT" not in runtime.env
    assert "PLAYWRIGHT_MCP_HEADLESS" not in runtime.env
    assert "NODE_OPTIONS" not in runtime.env
    config = pinned_config("/owned/chromium", tmp_path / "profile", tmp_path / "out", 7)
    browser = config["browser"]
    assert config["extension"] is False
    assert browser["cdpEndpoint"] is browser["remoteEndpoint"] is None
    assert browser["contextOptions"]["storageState"] is None
    assert browser["launchOptions"]["headless"] is True
    assert browser["launchOptions"]["executablePath"] == "/owned/chromium"
    assert browser["launchOptions"]["args"] == []
    assert config["timeouts"]["idle"] == 7000


def test_owner_tokens_cannot_escape_storage(tmp_path):
    result = dispatch(request(tmp_path, "../escape", action="close", delete_profile=True))
    assert result["reason_code"] == "runtime_error"
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("stale_owner", [None, "stopped", "live", "unknown"])
def test_deleted_workspace_does_not_block_other_owner(host, tmp_path, monkeypatch, stale_owner):
    host.ensure()
    host.release()
    record = json.loads(host.record_path("first").read_text())
    host.close_record(record, delete=True)
    assert not host.record_path("first").exists()
    if stale_owner:
        host.save(record)
    fresh_workspace = tmp_path / "next-workspace"
    fresh_workspace.mkdir()
    runtime = Host(request(tmp_path, "next", workspace_dir=str(fresh_workspace)))
    shutil.rmtree(tmp_path / "workspace")
    original_alive = Host.alive

    def alive(self, value):
        assert Path(value["workspace_dir"]).is_dir()
        return original_alive(self, value)

    def owner_status(self, value):
        if value["owner_token"] == "first":
            if stale_owner == "unknown":
                raise UnavailableError("owner_unavailable", "offline")
            return stale_owner == "live"
        return original_alive(self, value)

    monkeypatch.setattr(Host, "alive", alive)
    monkeypatch.setattr(Host, "owner_status", owner_status)
    assert runtime.ensure()["invocation_dir"] == str(fresh_workspace)
    assert host.record_path("first").exists() == (stale_owner in {"live", "unknown"})
    if stale_owner in {"live", "unknown"}:
        with pytest.raises(UnavailableError) as error:
            host.ensure()
        assert error.value.code in {"workspace_missing", "owner_unavailable"}


@pytest.mark.parametrize("owner_state", ["stopped", "live", "unknown"])
@pytest.mark.parametrize("broken_cli", ["missing", "corrupt"])
def test_install_repairs_broken_cli_only_when_os_owners_are_stopped(
    tmp_path, monkeypatch, owner_state, broken_cli
):
    import subprocess

    runtime = HostRuntime(request(tmp_path, action="install"))
    runtime.save(
        {
            "owner_token": "retained",
            "session_name": "retained",
            "handle": "retained",
            "workspace_dir": str(tmp_path / "workspace"),
            "leases": {},
        }
    )
    if broken_cli == "corrupt":
        runtime.core.mkdir(parents=True)
        (runtime.core / "package.json").write_text("broken")
    monkeypatch.setattr(runtime, "prerequisites", lambda: None)
    monkeypatch.setattr(runtime, "readiness", lambda **kw: {"status": "ready"})
    monkeypatch.setattr(runtime, "executable", lambda: "/owned/chromium")
    monkeypatch.setattr(runtime, "start", lambda *a: None)
    monkeypatch.setattr(runtime, "close_record", lambda *a, **kw: None)
    installed = []
    probe_run = runtime.run

    def run(command, **kwargs):
        if command[1] == "-e":
            return probe_run(command, **kwargs)
        installed.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    def owner_status(record):
        if owner_state == "unknown":
            raise UnavailableError("owner_unavailable", "Cannot inspect process owner")
        return owner_state == "live"

    monkeypatch.setattr(runtime, "run", run)
    monkeypatch.setattr(runtime, "owner_status", owner_status)
    if owner_state == "unknown":
        with pytest.raises(UnavailableError) as error:
            runtime.install()
        assert error.value.code == "owner_unavailable"
    else:
        assert runtime.install()["status"] == ("ready" if owner_state == "stopped" else "busy")
    assert len(installed) == (2 if owner_state == "stopped" else 0)
