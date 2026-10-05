from __future__ import annotations

import importlib.resources
import json
import os
import shlex
import signal
import subprocess
import tempfile
from pathlib import Path

import pytest

from rcp import ssh_agent
from rcp.compute_jobs.backend_context import BackendContext
from rcp.compute_jobs.backends.launchd import LaunchdBackend
from rcp.compute_jobs.backends.systemd_user import SystemdUserBackend
from rcp.transport import remote_ssh_agent


@pytest.fixture
def home():
    with tempfile.TemporaryDirectory(prefix="rcp-r-", dir="/tmp") as directory:
        yield Path(directory)


def make_key(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    return path


class LocalStage:
    host = "test-account"

    def __init__(self, environment):
        self.environment = environment

    def _ssh(self, argv):
        return subprocess.run(
            argv, env=self.environment, capture_output=True, text=True, timeout=20
        )

    def _ssh_bytes(self, argv, *, input_data, timeout_seconds):
        return subprocess.run(
            argv,
            env=self.environment,
            input=input_data,
            capture_output=True,
            timeout=timeout_seconds,
        )


@pytest.mark.parametrize(
    "state", ["confirmed", "missing_public", "stale_public", "wrong_agent", "sign_failed"]
)
def test_shipped_confirmation_uses_separate_agents_and_verified_pairs(home, monkeypatch, state):
    credentials = home / ".local/share/rcp/credentials"
    deploy = make_key(credentials / "projects/p/r/id_ed25519")
    user = make_key(home / ".ssh/id_ed25519")
    (home / ".ssh/config").write_text(f"IdentityFile {deploy}\n")
    agent = ssh_agent.BackendSSHAgent(credentials, home=home)
    agent.start()
    # A distinct user socket is essential: the deploy agent must not confirm it.
    user_socket = home / "user.sock"
    user_agent = subprocess.Popen(
        ["ssh-agent", "-D", "-a", str(user_socket)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        from tests.helpers import wait_until

        wait_until(lambda: user_socket.exists())
        environment = dict(os.environ, SSH_AUTH_SOCK=str(user_socket))
        subprocess.run(["ssh-add", str(user)], env=environment, check=True, capture_output=True)
        if state == "missing_public":
            Path(str(deploy) + ".pub").unlink()
        elif state == "stale_public":
            Path(str(deploy) + ".pub").write_text(Path(str(user) + ".pub").read_text())
        elif state == "wrong_agent":
            environment["SSH_AUTH_SOCK"] = str(agent.socket)
        elif state == "sign_failed":
            # Exercise the same shipped function with a failing signer only.
            original = remote_ssh_agent._run

            def run(argv, *args, **kwargs):
                if "sign" in argv:
                    return subprocess.CompletedProcess(argv, 1, "", "")
                return original(argv, *args, **kwargs)

            monkeypatch.setattr(remote_ssh_agent, "_run", run)
            result = remote_ssh_agent.confirm_keys(
                private_key_paths=(str(deploy),),
                kind="deploy_key",
                agent_socket=str(agent.socket),
                timeout=5,
            )
            assert result[0]["visibility"] == "readable"
            return
        evidence = ssh_agent.confirm_remote_key_evidence(
            remote_stage=LocalStage(environment), home=str(home)
        )
        by_path = {item.path: item for item in evidence}
        assert len(evidence) == len(by_path) == 2
        assert by_path[str(deploy)].agent_confirmed == (state in {"confirmed", "wrong_agent"})
        assert by_path[str(user)].agent_confirmed == (state != "wrong_agent")
        assert all(not item.path.endswith(".pub") for item in evidence)
    finally:
        user_agent.terminate()
        user_agent.wait(timeout=5)
        agent.stop()


@pytest.mark.parametrize("failure", ["owner_unavailable", "confirmation_unavailable", "started"])
@pytest.mark.parametrize(
    "platform,backend", [("linux", SystemdUserBackend), ("darwin", LaunchdBackend)]
)
def test_remote_failure_returns_readable_inventory(home, monkeypatch, failure, platform, backend):
    deploy = make_key(home / ".local/share/rcp/credentials/projects/p/r/id_ed25519")
    calls = []
    owners = []

    class Stage(LocalStage):
        def _ssh(self, argv):
            if "inventory" in argv:
                result = super()._ssh(argv)
                facts = json.loads(result.stdout)
                facts["platform"] = platform
                return subprocess.CompletedProcess(argv, 0, json.dumps(facts), "")
            if failure == "confirmation_unavailable":
                return subprocess.CompletedProcess(argv, 1, "", "")
            return super()._ssh(argv)

    def start(self, handle, argv, context):
        calls.append((handle, argv))
        if failure == "started":
            owners.append(
                subprocess.Popen(
                    argv,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        # Nonzero submission means the subsequent evidence still gets checked.

    monkeypatch.setattr(backend, "start_account_service", start)
    monkeypatch.setattr(ssh_agent, "AGENT_COMMAND_TIMEOUT_SECONDS", 0.2)
    try:
        stage = Stage(dict(os.environ))
        (evidence,) = ssh_agent.confirm_remote_key_evidence(remote_stage=stage, home=str(home))
        assert evidence.path == str(deploy)
        assert evidence.visibility == ("hidden" if failure == "started" else "readable")
        if failure == "started":
            assert ssh_agent.confirm_remote_key_evidence(remote_stage=stage, home=str(home)) == (
                evidence,
            )
            import fcntl

            with (
                (home / ".rcp/ssh-agent/owner.lock").open() as lock,
                pytest.raises(BlockingIOError),
            ):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert len(calls) == 1
        helper = Path(calls[0][1][1])
        assert helper.parent == home / ".rcp/ssh-agent"
        assert (
            helper.read_text()
            == importlib.resources.files("rcp.transport")
            .joinpath("remote_ssh_agent.py")
            .read_text()
        )
    finally:
        for owner in owners:
            os.killpg(owner.pid, signal.SIGTERM)
            owner.wait(timeout=5)


@pytest.mark.parametrize("backend", [SystemdUserBackend, LaunchdBackend])
def test_account_service_duplicate_never_cancels_owner(backend):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0 if "python3" in argv else 1, "", "already running"
        )

    context = BackendContext(
        execution_host="", execution_machine="test", compute=None, runner=runner, uid="123"
    )
    backend().start_account_service("/account/.rcp/ssh-agent", ["ssh-agent", "-D"], context)
    if backend is SystemdUserBackend:
        assert len(calls) == 1
        assert "systemd-run" in calls[0]
        assert "rcp-ssh-agent" in calls[0]
    else:
        assert len(calls) == 3
        assert calls[-1] == ["launchctl", "kickstart", "gui/123/rcp-ssh-agent"]
    assert not any(argument in {"stop", "bootout", "-k"} for call in calls for argument in call)


@pytest.mark.parametrize("running", [True, False])
def test_shipped_git_retains_private_key_and_remote_identity_agent(home, running):
    credentials = home / ".local/share/rcp/credentials"
    key = make_key(credentials / "projects/p/r/id_ed25519")
    agent = ssh_agent.BackendSSHAgent(credentials, home=home)
    if running:
        agent.start()
    try:
        repository = home / "checkout"
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        source = importlib.resources.files("rcp").joinpath("git_access.py").read_text()
        subprocess.run(
            ["python3", "-c", source, str(repository), str(key), "5"],
            env=dict(os.environ, HOME=str(home)),
            check=True,
            capture_output=True,
        )
        command = subprocess.check_output(
            ["git", "-C", str(repository), "config", "core.sshCommand"], text=True
        )
        argv = shlex.split(command)
        assert argv[argv.index("-i") + 1] == str(key)
        assert (f"IdentityAgent={agent.socket}" in argv) == running
        assert "IdentitiesOnly=yes" in argv
    finally:
        agent.stop()
