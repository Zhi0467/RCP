from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from rcp import ssh_agent
from rcp.git_access import deploy_key_ssh_command


@pytest.fixture
def agent_home():
    # macOS's pytest temp paths exceed sun_path. Use a disposable short home.
    with tempfile.TemporaryDirectory(prefix="rcp-a-", dir="/tmp") as directory:
        yield Path(directory)


def key(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    return path


@pytest.mark.parametrize("state", ["loaded", "missing", "stale", "no_agent"])
def test_identity_requires_matching_pair_and_agent_signature(agent_home, state):
    private = key(agent_home / "credentials/projects/p/r/id_ed25519")
    agent = ssh_agent.BackendSSHAgent(agent_home / "credentials", home=agent_home)
    agent.start()
    try:
        assert ssh_agent.agent_status(agent_home) == "running"
        socket = str(agent.socket)
        if state == "missing":
            Path(str(private) + ".pub").unlink()
        elif state == "stale":
            other = key(agent_home / "other")
            Path(str(private) + ".pub").write_text(Path(str(other) + ".pub").read_text())
        elif state == "no_agent":
            socket = None
        (evidence,) = ssh_agent.confirm_key_evidence(
            private_key_paths=(str(private),),
            kind="deploy_key",
            agent_socket=socket,
        )
        assert evidence.agent_confirmed == (state == "loaded")
        assert evidence.visibility == ("hidden" if state == "loaded" else "readable")
    finally:
        agent.stop()


def test_account_lock_and_verified_orphan_recovery(agent_home):
    owner = ssh_agent.BackendSSHAgent(agent_home / "credentials", home=agent_home)
    contender = ssh_agent.BackendSSHAgent(agent_home / "credentials", home=agent_home)
    owner.start()
    try:
        pid = owner._pid
        assert pid is not None
        contender.start()
        assert contender._pid is None
        assert ssh_agent.agent_status(agent_home) == "running"
        # Simulate backend death, retaining only the separately owned daemon.
        os.close(owner._lock)
        owner._lock = None
        owner._pid = None
        contender.start()
        assert contender._pid is not None
        assert contender._pid != pid
    finally:
        owner.stop()
        contender.stop()


def test_identity_candidates_exclude_public_and_transport_files(agent_home):
    root = agent_home / ".ssh"
    root.mkdir()
    for name in ("id_ed25519", "id_ed25519.pub", "known_hosts", "custom"):
        (root / name).touch()
    (root / "config").write_text("IdentityFile ~/.ssh/custom\nIdentityFile ~/.ssh/known_hosts\n")
    assert ssh_agent.user_ssh_identity_candidates(agent_home) == (
        str(root / "custom"),
        str(root / "id_ed25519"),
    )


def test_git_transport_retains_file_fallback_and_separate_agent(monkeypatch):
    monkeypatch.setattr(ssh_agent, "running_agent_socket", lambda: "/stable/agent.sock")
    command = deploy_key_ssh_command("/keys/key", str(Path.home()))
    import shlex

    arguments = shlex.split(command)
    assert arguments[arguments.index("-i") + 1] == "/keys/key"
    assert "IdentityAgent=/stable/agent.sock" in arguments
    assert "IdentitiesOnly=yes" in arguments


@pytest.mark.parametrize("mode", ["ordinary", "fenced", "failure"])
def test_lifespan_orders_agent_around_recovery_and_workers(tmp_path, monkeypatch, mode):
    from unittest.mock import Mock

    from fastapi.testclient import TestClient

    from rcp.api.app import create_app
    from rcp.background import StartupEffectFence

    order = []
    agent = Mock()
    agent.start.side_effect = lambda: order.append("agent_start")
    agent.stop_after_workers_drained.side_effect = lambda idle: (
        order.append("agent_stop") if idle() else None
    )
    monkeypatch.setattr("rcp.api.app.BackendSSHAgent", lambda *args: agent)
    fence = StartupEffectFence("agent lifecycle") if mode == "fenced" else None
    app = create_app(data_dir=tmp_path, startup_effect_fence=fence)
    original_recovery = app.state.background_tasks.recover_at_startup
    original_shutdown = app.state.background_tasks.shutdown

    def recover():
        order.append("recovery")
        if mode == "failure":
            raise RuntimeError("recovery failed")
        original_recovery()

    def shutdown():
        original_shutdown()
        order.append("workers_drained")

    monkeypatch.setattr(app.state.background_tasks, "recover_at_startup", recover)
    monkeypatch.setattr(app.state.background_tasks, "shutdown", shutdown)
    client = TestClient(app, base_url="http://127.0.0.1:8421")
    if mode == "failure":
        with pytest.raises(RuntimeError), client:
            pass
    else:
        with client:
            if fence is not None:
                assert order == []
                fence.release()
                assert app.state.startup_effect_runtime_event.wait(timeout=5)
            assert order == ["agent_start", "recovery"]
    assert order == ["agent_start", "recovery", "workers_drained", "agent_stop"]


def test_signing_survives_shutdown_timeout_until_workers_drain(agent_home, monkeypatch):
    import threading

    agent = ssh_agent.BackendSSHAgent(agent_home / "credentials", home=agent_home)
    agent.start()
    idle = threading.Event()
    stopped = threading.Event()
    original_stop = agent.stop

    def stop():
        original_stop()
        stopped.set()

    monkeypatch.setattr(agent, "stop", stop)
    try:
        agent.stop_after_workers_drained(idle.is_set)
        assert ssh_agent.agent_status(agent_home) == "running"
        assert not stopped.is_set()
        idle.set()
        assert stopped.wait(timeout=5)
        assert ssh_agent.agent_status(agent_home) == "unavailable"
    finally:
        idle.set()
        agent.stop()
