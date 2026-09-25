from __future__ import annotations

import os
import shlex
import socket
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

from rcp.agents.write_scope import (
    _ExecutionPathSemantics,
    _reject_broad_repository_root,
)
from rcp.transport import StateUnavailable
from rcp.transport.remote_compute_probe import probe_connection
from rcp.transport.run_stage import RemoteRunStage
from rcp.transport.ssh import (
    SHARED_CONTROL_PARTITION,
    control_partition_token,
    rsync_ssh_arguments,
    ssh_arguments,
    sweep_control_sockets,
)
from rcp.transport.state import _remote_advisory_lock_command


def test_local_path_semantics_use_real_filesystem_identity_for_authority_guards(
    tmp_path: Path,
) -> None:
    semantics = _ExecutionPathSemantics.for_execution(remote=False)
    home = tmp_path / "Research"
    repository = home / "Repo"
    nested = repository / "nested"
    data_root = tmp_path / "rcp-data"
    data_project = data_root / "projects"
    nested.mkdir(parents=True)
    data_project.mkdir(parents=True)
    home_alias = tmp_path / "home-alias"
    data_alias = tmp_path / "data-alias"
    home_alias.symlink_to(home, target_is_directory=True)
    data_alias.symlink_to(data_root, target_is_directory=True)

    assert semantics.equal(home, home_alias)
    assert semantics.overlaps(repository, home_alias / "Repo" / "nested")
    with pytest.raises(ValueError):
        _reject_broad_repository_root(
            str(home_alias),
            account_home=str(home),
            app_data_dir=None,
            path_semantics=semantics,
        )
    with pytest.raises(ValueError):
        _reject_broad_repository_root(
            str(data_alias / "projects"),
            account_home=str(home),
            app_data_dir=data_root,
            path_semantics=semantics,
        )


def test_remote_posix_path_semantics_remain_case_sensitive() -> None:
    semantics = _ExecutionPathSemantics.for_execution(remote=True)

    assert not semantics.overlaps("/srv/Research/Repo", "/srv/research/repo/nested")
    _reject_broad_repository_root(
        "/HOME/worker",
        account_home="/home/worker",
        app_data_dir=None,
        path_semantics=semantics,
    )


def test_remote_posix_path_semantics_keep_unicode_forms_distinct() -> None:
    import unicodedata

    semantics = _ExecutionPathSemantics.for_execution(remote=True)
    composed = "/srv/Résumé/Repo"
    decomposed = unicodedata.normalize("NFD", composed)

    assert not semantics.overlaps(composed, f"{decomposed}/nested")


@pytest.mark.parametrize("replacement", ["permissive-directory", "symlink"])
def test_reused_remote_stage_refuses_unsafe_existing_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    label = f"test-{uuid.uuid4().hex}"
    remote_root = Path("/tmp") / f"rcp-run.{label}"
    if replacement == "symlink":
        target = tmp_path / "replacement"
        target.mkdir()
        remote_root.symlink_to(target, target_is_directory=True)
    else:
        remote_root.mkdir(mode=0o700)
        remote_root.chmod(0o755)
    stage = RemoteRunStage("research.example")
    monkeypatch.setattr(stage, "sweep", lambda **_kwargs: None)
    monkeypatch.setattr(
        stage,
        "_ssh",
        lambda arguments: subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            check=False,
        ),
    )

    try:
        with pytest.raises(StateUnavailable):
            stage.open(label, reuse=True)
        assert stage.root is None
    finally:
        if remote_root.is_symlink():
            remote_root.unlink()
        elif remote_root.exists():
            remote_root.chmod(0o700)
            remote_root.rmdir()


def test_ssh_and_rsync_share_one_proven_private_control_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_directory = tmp_path / "ssh-control"
    monkeypatch.setattr("rcp.transport.ssh._control_directory_path", lambda: control_directory)

    ssh_argv = ssh_arguments("research.example", "true")
    rsync_shell = shlex.split(rsync_ssh_arguments()[1])

    expected = f"ControlPath={control_directory}/{SHARED_CONTROL_PARTITION}-%C"
    assert expected in ssh_argv
    assert expected in rsync_shell
    info = control_directory.lstat()
    assert stat.S_ISDIR(info.st_mode)
    assert info.st_uid == os.geteuid()
    assert stat.S_IMODE(info.st_mode) == 0o700


def test_partitioned_ssh_and_rsync_keep_a_master_of_their_own(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_directory = tmp_path / "ssh-control"
    monkeypatch.setattr("rcp.transport.ssh._control_directory_path", lambda: control_directory)
    partition = "research.example:/tmp/rcp-run.op-one"

    ssh_argv = ssh_arguments("research.example", "true", partition=partition)
    rsync_shell = shlex.split(rsync_ssh_arguments(partition=partition)[1])
    shared_argv = ssh_arguments("research.example", "true")
    sibling_argv = ssh_arguments(
        "research.example",
        "true",
        partition="research.example:/tmp/rcp-run.op-two",
    )

    expected = f"ControlPath={control_directory}/{control_partition_token(partition)}-%C"
    assert expected in ssh_argv
    assert expected in rsync_shell
    assert expected not in shared_argv
    assert expected not in sibling_argv
    # Same directory, so the one proof of a private 0700 owner still covers it.
    assert all(
        item.startswith(f"ControlPath={control_directory}/")
        for argv in (ssh_argv, shared_argv, sibling_argv)
        for item in argv
        if item.startswith("ControlPath=")
    )


def test_control_partition_token_fits_a_unix_socket_path() -> None:
    """A partition that overflows the socket path would exit 255 like a lost link.

    The identities callers partition by are remote paths, so the token has to
    be a fixed short width rather than the name itself. The ceiling is not
    sun_path: a master binds `<path>.<16 random chars>` and renames it into
    place, so the real budget is sizeof(sun_path) - 1 - 17, and sun_path is
    104 on macOS. %C spends 40 of it before the token is added.
    """

    master_bind_ceiling = 104 - 1 - 17
    long_partition = "research.example:/tmp/rcp-run." + ("x" * 240)

    token = control_partition_token(long_partition)

    assert len(token) == len(control_partition_token("short"))
    # The widest user id this can be asked for, so no operator is the exception.
    widest = f"/tmp/rcp-ssh-{2**32 - 1}/{token}-{'c' * 40}"
    assert len(widest) <= master_bind_ceiling


def test_control_socket_sweep_keeps_every_master_that_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The directory is shared by user id, so a live master may not be ours."""

    # Not tmp_path: a socket may only be bound under a short path, which is the
    # same ceiling the partition token is sized against.
    control_directory = Path("/tmp") / f"rcp-t{uuid.uuid4().hex[:8]}"
    control_directory.mkdir(mode=0o700)
    monkeypatch.setattr("rcp.transport.ssh._control_directory_path", lambda: control_directory)

    listening = control_directory / "live"
    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    abandoned = control_directory / "dead"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # The shared path is reused, so OpenSSH heals it on the next connection and
    # sweeping it could only delete a socket a master just bound.
    reused = control_directory / f"{SHARED_CONTROL_PARTITION}-abc"
    shared = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unrelated = control_directory / "other"
    try:
        live.bind(os.fspath(listening))
        live.listen(1)
        dead.bind(os.fspath(abandoned))
        dead.close()
        shared.bind(os.fspath(reused))
        shared.close()
        unrelated.write_text("")

        sweep_control_sockets()

        assert listening.exists()
        assert unrelated.exists()
        assert reused.exists()
        assert not abandoned.exists()
    finally:
        live.close()
        for leftover in control_directory.iterdir():
            leftover.unlink()
        control_directory.rmdir()


@pytest.mark.parametrize("replacement", ["permissive-directory", "symlink"])
def test_ssh_control_directory_refuses_unsafe_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    control_directory = tmp_path / "ssh-control"
    if replacement == "symlink":
        target = tmp_path / "replacement"
        target.mkdir()
        control_directory.symlink_to(target, target_is_directory=True)
    else:
        control_directory.mkdir(mode=0o755)
        control_directory.chmod(0o755)
    monkeypatch.setattr("rcp.transport.ssh._control_directory_path", lambda: control_directory)

    with pytest.raises(RuntimeError):
        ssh_arguments("research.example", "true")


@pytest.mark.parametrize(
    "host",
    ["-Ffoo", "-oProxyCommand=sh", "--", " host.example"],
)
def test_ssh_arguments_reject_option_shaped_destinations(host: str) -> None:
    with pytest.raises(ValueError):
        ssh_arguments(host, "true")


def test_strict_host_key_ssh_never_reuses_a_multiplexed_connection(monkeypatch) -> None:
    monkeypatch.setattr(
        "rcp.transport.ssh._control_directory_path",
        lambda: (_ for _ in ()).throw(AssertionError("strict transport must be direct")),
    )

    argv = ssh_arguments("research.example", "true", strict_host_key_checking=True)

    assert argv[argv.index("-S") : argv.index("-S") + 2] == ["-S", "none"]
    assert "StrictHostKeyChecking=yes" in argv
    assert not any("ControlMaster=" in item or "ControlPath=" in item for item in argv)


def test_compute_probe_never_reuses_a_multiplexed_connection() -> None:
    commands: list[list[str]] = []

    def runner(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    probe_connection("ssh", "alice@gpu.example", runner=runner)

    argv = commands[0]
    assert argv[argv.index("-S") : argv.index("-S") + 2] == ["-S", "none"]
    assert "StrictHostKeyChecking=yes" in argv


def test_each_run_stage_keeps_a_master_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """One lost link must end one run, not every run sharing the host."""

    captured: list[list[str]] = []

    def run(arguments, **_kwargs):
        captured.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("rcp.transport.run_stage.subprocess.run", run)

    stages = [
        RemoteRunStage("research.example"),
        RemoteRunStage("research.example").attach("/tmp/rcp-run.op-one"),
        RemoteRunStage("research.example").attach("/tmp/rcp-run.op-two"),
    ]
    paths = []
    for stage in stages:
        captured.clear()
        stage._ssh(["true"])
        paths.append(next(item for item in captured[0] if item.startswith("ControlPath=")))

    assert len(set(paths)) == 3
    # A stage with no root owns no run yet, so it has nothing to isolate.
    assert paths[0].endswith(f"/{SHARED_CONTROL_PARTITION}-%C")
    assert paths[1].endswith(f"/{control_partition_token(stages[1].transport_partition)}-%C")


def test_each_advisory_lock_holder_keeps_a_master_of_its_own() -> None:
    """A dropped provider link must leave the run's lock still held."""

    run_lock = _remote_advisory_lock_command("research.example", "/srv/state/.agent-run.lock")
    refresh_lock = _remote_advisory_lock_command("research.example", "/srv/state/.refresh.lock")
    ordinary = ssh_arguments("research.example", "true")

    paths = [
        next(item for item in argv if item.startswith("ControlPath="))
        for argv in (run_lock, refresh_lock, ordinary)
    ]
    assert len(set(paths)) == 3


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(255, False), (1, True), (2, True)],
)
def test_attaching_tells_a_host_that_cannot_answer_from_one_that_did(
    monkeypatch, returncode, expected
) -> None:
    """Both are unusable state; only one is worth asking again about.

    255 is ssh reporting it could not reach the host, which says nothing about
    the stage. Any other status is the host answering that this stage is gone,
    replaced, or not ours -- a verdict, not silence, and callers that retry
    silence must not retry it.
    """

    from rcp.transport import StateMissing

    stage = RemoteRunStage("research.example")
    monkeypatch.setattr(
        stage,
        "_directory_probe",
        lambda _root: subprocess.CompletedProcess([], returncode, stdout="", stderr=""),
    )

    with pytest.raises(StateUnavailable) as caught:
        stage.attach("/tmp/rcp-run.op-one")

    assert isinstance(caught.value, StateMissing) is expected
    assert stage.root is None
