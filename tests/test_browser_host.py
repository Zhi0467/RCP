from __future__ import annotations

import json
import plistlib
import shlex
import shutil
import subprocess
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
        host.save({**record, "pending_close": False, "delete_profile": False})
        (host.record_path("first").parent / "profile").mkdir()
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
    assert host.record_path("first").parent.exists() == (stale_owner in {"live", "unknown"})
    if stale_owner in {"live", "unknown"}:
        with pytest.raises(UnavailableError) as error:
            host.ensure()
        assert error.value.code in {"workspace_missing", "owner_unavailable"}


def test_interrupted_install_reads_as_installable(tmp_path, monkeypatch):
    runtime = HostRuntime(request(tmp_path, action="readiness"))
    path = runtime.tools / "node_modules" / "@playwright" / "cli" / "package.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"version": "0.1')
    monkeypatch.setattr(runtime, "prerequisites", lambda: None)
    monkeypatch.setattr(runtime, "owner_ready", lambda: None)
    assert runtime.readiness()["status"] == "not_installed"


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
    monkeypatch.setattr(runtime, "owner_ready", lambda: None)
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


def hidden_scope(path="/selected/secret"):
    from rcp.agents.hidden_read import HIDDEN_READ_ENV_DENY_LIST
    from rcp.core.models import HiddenReadScope, HiddenReadStatus

    return HiddenReadScope(
        execution_machine="local",
        execution_host="",
        os_account="researcher",
        hidden_files=(path,),
        env_deny_list=HIDDEN_READ_ENV_DENY_LIST,
        enforcement=HiddenReadStatus(status="enforced"),
    )


@pytest.mark.parametrize(
    ("system", "ready", "reason"),
    [
        ("Linux", True, None),
        ("Linux", False, "userns_blocked"),
        ("Darwin", True, "browser_unwrapped_macos"),
    ],
)
def test_daemon_job_policy_preserves_browser_capability(
    tmp_path, monkeypatch, system, ready, reason
):
    secret = tmp_path / "secret"
    secret.write_text("private")
    monkeypatch.setattr("rcp.browser.host.platform.system", lambda: system)
    monkeypatch.setattr(
        "rcp.browser.host.probe_hidden_read_wrapper",
        lambda **kw: {"ready": ready, "reason": reason},
    )
    monkeypatch.setattr("rcp.browser.host.shutil.which", lambda name, **kw: "/usr/bin/" + name)
    runtime = HostRuntime(
        request(tmp_path, hidden_read_scope=hidden_scope(str(secret)).model_dump(mode="json"))
    )
    runtime.env["HTTPS_PROXY"] = "http://proxy.example"
    runtime.browser_policy()
    record = {
        "owner_token": "first",
        "handle": "job",
        "session_name": "session",
        "workspace_dir": str(tmp_path / "workspace"),
    }
    calls = []
    monkeypatch.setattr(
        runtime,
        "run",
        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(runtime, "alive", lambda record: True)
    monkeypatch.setattr(runtime, "stop_owner", lambda record: None)
    monkeypatch.setattr(runtime, "cli_run", lambda *a, **kw: None)
    runtime.start(record, "/owned/chromium")
    owner_dir = runtime.record_path("first").parent
    if system == "Linux":
        job = calls[0]
        assert job[0] == "systemd-run"
        assert job[job.index("--") + 1 :] == ["/bin/sh", str(owner_dir / "daemon.sh")]
        argv = shlex.split((owner_dir / "daemon.sh").read_text().split("exec ", 1)[1])
        assert "HTTPS_PROXY=http://proxy.example" in argv
        if ready:
            offset = argv.index("/usr/bin/bwrap")
            assert argv[offset : offset + 4] == ["/usr/bin/bwrap", "--dev-bind", "/", "/"]
            assert argv[argv.index("--ro-bind") + 1 : argv.index("--ro-bind") + 3] == [
                "/dev/null",
                str(secret),
            ]
            daemon = shlex.split(argv[-1])
            assert daemon[:2] == ["exec", runtime.node]
            assert daemon[2] == str(runtime.core / "lib/entry/cliDaemon.js")
        else:
            assert "/usr/bin/bwrap" not in argv
            assert argv[argv.index(runtime.node) + 1] == str(
                runtime.core / "lib/entry/cliDaemon.js"
            )
    else:
        plist = plistlib.loads((owner_dir / "owner.plist").read_bytes())
        assert plist["ProgramArguments"][:3] == [
            runtime.node,
            str(runtime.core / "lib/entry/cliDaemon.js"),
            "session",
        ]
        assert plist["EnvironmentVariables"] == runtime.daemon_env()
    assert runtime.hidden_read_enforcement == {
        "status": "unhidden" if reason else "enforced",
        "reasons": [reason] if reason else [],
    }


@pytest.mark.parametrize("interrupted", [False, True])
def test_policy_restart_keeps_profile_and_all_active_leases(host, monkeypatch, interrupted):
    monkeypatch.setattr("rcp.browser.host.platform.system", lambda: "Linux")
    host.backend = "systemd_user"
    monkeypatch.setattr(
        "rcp.browser.host.probe_hidden_read_wrapper",
        lambda **kw: {"ready": False, "reason": "userns_blocked"},
    )
    host.request["hidden_read_scope"] = hidden_scope().model_dump(mode="json")
    host.ensure()
    path = host.record_path("first")
    profile = path.parent / "profile" / "login"
    profile.write_text("retained")
    previous = json.loads(path.read_text())
    previous["leases"]["other"] = {"controller_id": "other", "controller_epoch": "old"}
    host.save(previous)
    host.request.update(
        lease_id="next", hidden_read_scope=hidden_scope("/changed/secret").model_dump(mode="json")
    )
    if interrupted:
        close = host.cli_run
        monkeypatch.setattr(
            host, "cli_run", lambda *a, **kw: (_ for _ in ()).throw(OSError("interrupted"))
        )
        with pytest.raises(OSError):
            host.ensure()
        assert json.loads(path.read_text())["leases"] == previous["leases"]
        monkeypatch.setattr(host, "cli_run", close)
    host.ensure()
    current = json.loads(path.read_text())
    assert current["hidden_read_fingerprint"] != previous["hidden_read_fingerprint"]
    assert set(current["leases"]) == {"first", "other", "next"}
    assert profile.read_text() == "retained"
    assert host.starts == ["first", "first"]
    host.ensure()
    assert host.starts == ["first", "first"]


@pytest.mark.parametrize("failure", ["probe", "start", "timeout"])
def test_enforcement_failure_admits_unhidden_browser(host, monkeypatch, failure):
    host.backend = "systemd_user"
    host.request["hidden_read_scope"] = hidden_scope().model_dump(mode="json")
    monkeypatch.setattr("rcp.browser.host.shutil.which", lambda *a, **kw: "/usr/bin/bwrap")

    def probe(**kwargs):
        if failure == "probe":
            raise OSError("probe unavailable")
        return {"ready": True, "reason": None}

    monkeypatch.setattr("rcp.browser.host.probe_hidden_read_wrapper", probe)
    start = host.start
    attempts = []

    def launch(record, executable):
        attempts.append(bool(host.hidden_read_command))
        if host.hidden_read_command:
            host.live.add(record["owner_token"])
            if failure == "timeout":
                raise subprocess.TimeoutExpired("bwrap", 1)
            raise UnavailableError("start_failed", "mount failed")
        start(record, executable)

    monkeypatch.setattr(host, "start", launch)
    result = host.ensure()
    assert result["hidden_read_enforcement"] == {
        "status": "unhidden",
        "reasons": ["wrapper_unavailable"],
    }
    assert attempts == ([False] if failure == "probe" else [True, False])
    assert host.release()["alive"]


@pytest.mark.parametrize("permitted", [True, False])
def test_missing_linger_is_its_own_reason_and_the_member_can_enable_it(
    tmp_path, monkeypatch, permitted
):
    runtime = HostRuntime(request(tmp_path, action="enable_linger"))
    runtime.backend = "systemd_user"
    linger = {"value": "no"}
    calls = []

    def run(argv, check=True):
        calls.append(argv)
        if argv[:2] == ["loginctl", "enable-linger"]:
            if not permitted:
                return subprocess.CompletedProcess(argv, 1, "", "Access denied")
            linger["value"] = "yes"
        out = linger["value"] + "\n" if argv[:2] == ["loginctl", "show-user"] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(runtime, "run", run)
    monkeypatch.setattr(shutil, "which", lambda tool, path=None: f"/usr/bin/{tool}")
    with pytest.raises(UnavailableError) as error:
        runtime.owner_ready()
    assert error.value.code == "linger_disabled"

    monkeypatch.setattr(runtime, "readiness", lambda: runtime.owner_ready() or {"status": "ready"})
    result = runtime.enable_linger()
    # The account enables its own linger; nothing is run as another user.
    assert ["loginctl", "enable-linger"] in calls
    if permitted:
        assert result["status"] == "ready"
    else:
        assert result["status"] == "linger_disabled"
        assert shlex.split(result["admin_command"])[:3] == ["sudo", "loginctl", "enable-linger"]


def test_agent_cli_runs_with_the_checked_node_not_the_first_on_path(tmp_path, monkeypatch):
    runtime = HostRuntime(request(tmp_path))
    checked = tmp_path / "checked-node"
    checked.write_text('#!/bin/sh\necho checked "$@"\n')
    checked.chmod(0o700)
    runtime.node = str(checked)
    stale = tmp_path / "stale-bin"
    stale.mkdir()
    (stale / "node").write_text("#!/bin/sh\necho stale\n")
    (stale / "node").chmod(0o700)
    prefix = runtime.cli_launcher()
    result = subprocess.run(
        [str(Path(prefix) / "playwright-cli"), "snapshot"],
        env={"PATH": f"{stale}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.split() == ["checked", str(runtime.cli), "snapshot"]
