"""RCP keeps its own files under `~/.rcp`, never `/tmp`, on the machine that makes them."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from rcp import rcp_home
from rcp.agents import staged_command_broker, staged_command_client
from rcp.agents.write_scope import rcp_owned_paths
from rcp.runs.tasks import auto_research_stream
from rcp.transport import StateMissing, StateUnavailable, ssh
from rcp.transport.run_stage import RemoteRunStage

# Captured before conftest swaps it out for a per-test directory.
_CONTROL_DIRECTORY_PATH = ssh._control_directory_path


def _local_stage(monkeypatch: pytest.MonkeyPatch, home: Path) -> RemoteRunStage:
    """A stage whose "remote" commands run here, with `home` as the remote account home."""

    monkeypatch.setenv("HOME", str(home))
    stage = RemoteRunStage("research.example")
    monkeypatch.setattr(
        stage,
        "_ssh",
        lambda arguments: subprocess.run(
            [sys.executable, *arguments[1:]] if arguments[0] == "python3" else arguments,
            capture_output=True,
            text=True,
            check=False,
        ),
    )
    return stage


def test_new_remote_stages_land_under_the_remote_rcp_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    stage = _local_stage(monkeypatch, home)
    monkeypatch.setattr(stage, "sweep", lambda **_kwargs: None)

    for label in ("op-one", None):
        stage.open(label)
        assert stage.root is not None
        root = Path(str(stage.root))
        assert root.parent == home / ".rcp" / "stages"
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stage.close()


def test_a_stage_parent_others_can_write_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".rcp").mkdir(parents=True)
    (home / ".rcp").chmod(0o777)
    stage = _local_stage(monkeypatch, home)
    monkeypatch.setattr(stage, "sweep", lambda **_kwargs: None)

    with pytest.raises(StateUnavailable):
        stage.open("op-one")
    assert not (home / ".rcp" / "stages" / "rcp-run.op-one").exists()


def test_a_reused_stage_keeps_its_saved_legacy_tmp_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    label = f"test-{uuid.uuid4().hex}"
    legacy = Path("/tmp") / f"rcp-run.{label}"
    legacy.mkdir(mode=0o700)
    (tmp_path / "home").mkdir()
    stage = _local_stage(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(stage, "sweep", lambda **_kwargs: None)
    try:
        stage.open(label, reuse=True)
        assert stage.root == PurePosixPath(str(legacy))
    finally:
        shutil.rmtree(legacy, ignore_errors=True)


def test_attach_refuses_a_stage_outside_this_accounts_rcp_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other" / ".rcp" / "stages" / "rcp-run.op"
    other.mkdir(parents=True, mode=0o700)
    other.chmod(0o700)
    stage = _local_stage(monkeypatch, tmp_path / "home")

    with pytest.raises(StateMissing):
        stage.attach(str(other))


def test_sweep_ages_out_new_and_legacy_stages_but_keeps_protected_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    new_parent = home / ".rcp" / "stages"
    # Stands in for /tmp, so the test never sweeps real stages on this machine.
    legacy_parent = tmp_path / "legacy-tmp"
    stale_new, kept_new = new_parent / "rcp-run.stale", new_parent / "rcp-run.kept"
    stale_legacy = legacy_parent / "rcp-run.stale"
    old = time.time() - 30 * 86400
    for root in (stale_new, kept_new, stale_legacy):
        root.mkdir(parents=True)
        os.utime(root, (old, old))

    stage = RemoteRunStage("research.example")
    calls: list[list[str]] = []
    monkeypatch.setattr(stage, "_ssh", lambda arguments: calls.append(arguments))
    stage.sweep(retain_days=7, protected_roots=[])
    script = calls[0][2]
    assert script.count("'/tmp/rcp-run.*'") == 1
    script = script.replace("'/tmp/rcp-run.*'", repr(f"{legacy_parent}/rcp-run.*"))

    swept = subprocess.run(
        [sys.executable, "-c", script, "7", json.dumps([str(kept_new)])],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert swept.returncode == 0, swept.stderr
    assert not stale_new.exists()
    assert not stale_legacy.exists()
    assert kept_new.exists()


def test_legacy_stage_roots_lists_only_tmp_stage_directories(monkeypatch) -> None:
    label = f"test-{uuid.uuid4().hex}"
    legacy = Path("/tmp") / f"rcp-run.{label}"
    legacy.mkdir(mode=0o700)
    stage = _local_stage(monkeypatch, Path("/nonexistent-home"))
    try:
        roots = stage.legacy_stage_roots()
    finally:
        legacy.rmdir()

    assert os.path.realpath(legacy) in roots
    assert roots == sorted(roots)


@pytest.mark.parametrize(
    "saved_root",
    [
        "/home/worker/.rcp/stages/rcp-run.auto_research-worker-a",
        "/tmp/rcp-run.auto_research-worker-a",
    ],
)
def test_auto_research_resumes_a_new_or_legacy_saved_remote_stage(
    monkeypatch: pytest.MonkeyPatch, saved_root: str
) -> None:
    monkeypatch.setattr(
        RemoteRunStage,
        "_directory_probe",
        lambda _stage, root: subprocess.CompletedProcess([], 0, "", ""),
    )
    machine = SimpleNamespace(host="worker.example", provider_paths={})
    service = SimpleNamespace(manifest=SimpleNamespace(machine_map={"gpu": machine}))
    turn = SimpleNamespace(
        request=SimpleNamespace(provider="codex", model="m", reasoning="high", run_on="gpu"),
        binding=SimpleNamespace(stage_root=saved_root, stage_host="worker.example"),
    )

    def open_stage(stage_name: str):
        return auto_research_stream._open_auto_research_actor_stage(
            service,  # type: ignore[arg-type]
            Path("/unused"),
            SimpleNamespace(),  # type: ignore[arg-type]
            turn,  # type: ignore[arg-type]
            stage_name=stage_name,
            actor_label="worker",
        )

    stage = open_stage("auto_research-worker-a")
    assert stage.remote is not None and str(stage.remote.root) == saved_root
    with pytest.raises(ValueError):
        open_stage("auto_research-worker-b")


@pytest.fixture
def short_home():
    # Not tmp_path: a socket path has to fit sun_path, and tmp_path does not.
    home = Path("/tmp") / f"rcp-h{uuid.uuid4().hex[:8]}"
    home.mkdir()
    try:
        yield home
    finally:
        shutil.rmtree(home)


def test_command_sockets_resolve_under_the_account_rcp_home(short_home: Path, monkeypatch) -> None:
    account = SimpleNamespace(pw_dir=str(short_home))
    monkeypatch.setattr(staged_command_broker.pwd, "getpwuid", lambda _uid: account)
    monkeypatch.setattr(staged_command_client.pwd, "getpwuid", lambda _uid: account)
    name = f"~/.rcp/sockets/rcp-command-{'a' * 32}.sock"

    broker_path = staged_command_broker._safe_socket_path(name)

    expected = str(short_home / ".rcp" / "sockets" / f"rcp-command-{'a' * 32}.sock")
    assert broker_path == expected
    assert stat.S_IMODE((short_home / ".rcp" / "sockets").stat().st_mode) == 0o700
    assert staged_command_client._broker_socket_path(name) == expected
    for outside in (f"/tmp/rcp-command-{'a' * 32}.sock", "~/.rcp/sockets/../x.sock"):
        with pytest.raises(staged_command_broker.BrokerError):
            staged_command_broker._safe_socket_path(outside)
        with pytest.raises(staged_command_client.ClientInputError):
            staged_command_client._broker_socket_path(outside)


def test_a_long_home_moves_sockets_to_one_protected_short_folder(monkeypatch) -> None:
    long_home = "/home/" + "u" * 80
    short_root = rcp_home.short_socket_root(long_home)
    name = f"rcp-command-{'a' * 32}.sock"
    for module in (staged_command_broker, staged_command_client):
        monkeypatch.setattr(module.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=long_home))
    created: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        staged_command_broker,
        "_private_directory",
        lambda path, *, exact=False: created.append((path, exact)),
    )

    broker = staged_command_broker._safe_socket_path(f"~/.rcp/sockets/{name}")
    assert broker == staged_command_client._broker_socket_path(f"~/.rcp/sockets/{name}")
    assert broker == f"{rcp_home.command_socket_directory(long_home)}/{name}"
    assert broker.startswith(f"{short_root}/") and len(broker.encode()) < 100
    assert created[0] == (short_root, True)
    assert short_root in rcp_owned_paths(account_home=long_home, app_data_dir=None, remote=True)
    short_home = "/home/worker"
    assert rcp_home.command_socket_directory(short_home) == "/home/worker/.rcp/sockets"
    assert rcp_home.short_socket_root(short_home) not in rcp_owned_paths(
        account_home=short_home, app_data_dir=None, remote=True
    )

    monkeypatch.setattr(ssh, "rcp_home", lambda: Path("/home/worker/.rcp"))
    monkeypatch.setattr(ssh.Path, "home", classmethod(lambda _cls: Path(long_home)))
    monkeypatch.setattr(ssh.os, "geteuid", lambda: 99999)  # no /run/user/99999
    assert _CONTROL_DIRECTORY_PATH() == Path("/home/worker/.rcp/ssh")
    monkeypatch.setattr(ssh, "rcp_home", lambda: Path(long_home) / ".rcp")
    assert _CONTROL_DIRECTORY_PATH() == Path(short_root) / "ssh"
