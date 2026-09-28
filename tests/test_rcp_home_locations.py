"""RCP keeps its own files under `~/.rcp`, never `/tmp`, on the machine that makes them."""

from __future__ import annotations

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
from rcp.terminals import profile as terminal_profile
from rcp.transport import (
    StateMissing,
    StateUnavailable,
    remote_repository_browser,
    remote_stage_root,
    ssh,
)
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

    # Operation labels may carry dots; the stage opens under the same name.
    for label in ("op-one", "op.two", None):
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


def test_the_sweep_also_ages_out_legacy_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stands in for /tmp, so the test never sweeps real stages on this machine.
    legacy_parent = tmp_path / "legacy-tmp"
    monkeypatch.setattr(remote_stage_root, "LEGACY_PARENT", str(legacy_parent))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    stale, kept = legacy_parent / "rcp-run.stale", legacy_parent / "rcp-run.kept"
    old = time.time() - 30 * 86400
    for root in (stale, kept):
        root.mkdir(parents=True)
        os.utime(root, (old, old))

    remote_stage_root.sweep_stages(7, {str(kept)})

    assert not stale.exists() and kept.exists()


def test_legacy_stage_roots_lists_only_tmp_stage_directories(monkeypatch) -> None:
    label = f"test-{uuid.uuid4().hex}"
    legacy = Path("/tmp") / f"rcp-run.{label}"
    legacy.mkdir(mode=0o700)
    # Operation labels may carry dots.
    dotted = Path("/tmp") / f"rcp-run.{label}.turn"
    dotted.mkdir(mode=0o700)
    # Anyone who can write /tmp can make this; no mount can carry its colon.
    stray = Path("/tmp") / f"rcp-run.{label}:stray"
    stray.mkdir(mode=0o700)
    stage = _local_stage(monkeypatch, Path("/nonexistent-home"))
    try:
        roots = stage.legacy_stage_roots()
        terminal_roots = terminal_profile.legacy_stage_roots()
        picker_roots = remote_repository_browser.legacy_stage_roots()
    finally:
        legacy.rmdir()
        dotted.rmdir()
        stray.rmdir()

    for listed in (roots, terminal_roots, picker_roots):
        assert os.path.realpath(legacy) in listed
        assert os.path.realpath(dotted) in listed
        assert os.path.realpath(stray) not in listed
    assert roots == sorted(roots)


def test_the_sweep_removes_only_old_unprotected_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stages = home / ".rcp" / "stages"
    stages.mkdir(parents=True)
    old = time.time() - 10 * 86400
    made = {}
    for name in ("rcp-run.old", "rcp-run.kept", "rcp-run.fresh", "rcp-run.odd:name"):
        made[name] = stages / name
        (made[name] / "workspace").mkdir(parents=True)
        # A read-only result must not stop its stage being removed.
        (made[name] / "workspace" / "result").write_text("", encoding="utf-8")
        (made[name] / "workspace" / "result").chmod(0o400)
        (made[name] / "workspace").chmod(0o500)
        if name != "rcp-run.fresh":
            os.utime(made[name], (old, old))
    stage = _local_stage(monkeypatch, home)

    stage.sweep(retain_days=7, protected_roots=[str(made["rcp-run.kept"])])

    assert sorted(path.name for path in stages.iterdir()) == [
        "rcp-run.fresh",
        "rcp-run.kept",
        "rcp-run.odd:name",
    ]
    for path in stages.iterdir():
        (path / "workspace").chmod(0o700)


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


def test_a_private_folder_needs_a_parent_no_one_else_can_rename_it_in(tmp_path: Path) -> None:
    # Ours but group-writable, as a 0002 umask leaves `~/.rcp`: closed, then used.
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o775)
    rcp_home.private_directory(loose / "tmp", "test folder")
    assert stat.S_IMODE(loose.stat().st_mode) & 0o022 == 0
    # A symlinked parent puts the folder wherever the link points.
    (tmp_path / "linked").symlink_to(loose)
    with pytest.raises(RuntimeError):
        rcp_home.private_directory(tmp_path / "linked" / "tmp", "test folder")
    # /tmp is shared but sticky, so a folder directly inside it is still ours.
    shared = Path("/tmp") / f"rcp-test-{uuid.uuid4().hex[:8]}"
    try:
        rcp_home.private_directory(shared, "test folder")
    finally:
        shared.rmdir()


def test_a_socket_parent_another_user_can_write_is_refused(tmp_path: Path) -> None:
    parent = tmp_path / "rcp"
    parent.mkdir(mode=0o700)
    staged_command_broker._private_directory(str(parent))
    # Such a user could swap the socket folder under it.
    parent.chmod(0o722)
    with pytest.raises(staged_command_broker.BrokerError):
        staged_command_broker._private_directory(str(parent))


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
