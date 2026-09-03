from __future__ import annotations

import os
import shlex
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
from rcp.transport.run_stage import RemoteRunStage
from rcp.transport.ssh import rsync_ssh_arguments, ssh_arguments


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
    with pytest.raises(ValueError, match="execution account home"):
        _reject_broad_repository_root(
            str(home_alias),
            account_home=str(home),
            app_data_dir=None,
            path_semantics=semantics,
        )
    with pytest.raises(ValueError, match="application data directory"):
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
        with pytest.raises(StateUnavailable, match="remote run stage"):
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

    expected = f"ControlPath={control_directory}/%C"
    assert expected in ssh_argv
    assert expected in rsync_shell
    info = control_directory.lstat()
    assert stat.S_ISDIR(info.st_mode)
    assert info.st_uid == os.geteuid()
    assert stat.S_IMODE(info.st_mode) == 0o700


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

    with pytest.raises(RuntimeError, match="control directory is unsafe"):
        ssh_arguments("research.example", "true")


@pytest.mark.parametrize(
    "host",
    ["-Ffoo", "-oProxyCommand=sh", "--", " host.example"],
)
def test_ssh_arguments_reject_option_shaped_destinations(host: str) -> None:
    with pytest.raises(ValueError, match="SSH destination contains unsupported characters"):
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
