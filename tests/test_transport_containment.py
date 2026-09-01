from __future__ import annotations

import os
import shlex
import stat
import subprocess
import unicodedata
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


def test_local_macos_path_semantics_cover_case_insensitive_authority_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rcp.agents.write_scope.sys.platform", "darwin")
    semantics = _ExecutionPathSemantics.for_execution(remote=False)

    assert semantics.overlaps("/Users/Research/Repo", "/users/research/repo/nested")
    with pytest.raises(ValueError, match="execution account home"):
        _reject_broad_repository_root(
            "/USERS/RESEARCH",
            account_home="/Users/Research",
            app_data_dir=None,
            path_semantics=semantics,
        )
    with pytest.raises(ValueError, match="application data directory"):
        _reject_broad_repository_root(
            "/USERS/RESEARCH/RCP-DATA/PROJECTS",
            account_home="/Users/Research",
            app_data_dir=Path("/Users/Research/rcp-data"),
            path_semantics=semantics,
        )


def test_remote_posix_path_semantics_remain_case_sensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rcp.agents.write_scope.sys.platform", "darwin")
    semantics = _ExecutionPathSemantics.for_execution(remote=True)

    assert not semantics.overlaps("/srv/Research/Repo", "/srv/research/repo/nested")
    _reject_broad_repository_root(
        "/HOME/worker",
        account_home="/home/worker",
        app_data_dir=None,
        path_semantics=semantics,
    )


def test_local_macos_path_semantics_normalize_unicode_for_authority_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rcp.agents.write_scope.sys.platform", "darwin")
    semantics = _ExecutionPathSemantics.for_execution(remote=False)
    composed_home = "/Users/Résumé"
    decomposed_home = unicodedata.normalize("NFD", composed_home)

    assert semantics.overlaps(
        f"{composed_home}/Repo",
        f"{decomposed_home}/repo/nested",
    )
    with pytest.raises(ValueError, match="execution account home"):
        _reject_broad_repository_root(
            decomposed_home,
            account_home=composed_home,
            app_data_dir=None,
            path_semantics=semantics,
        )
    with pytest.raises(ValueError, match="application data directory"):
        _reject_broad_repository_root(
            f"{decomposed_home}/RCP-DATA/projects",
            account_home="/Users/Other",
            app_data_dir=Path(f"{composed_home}/rcp-data"),
            path_semantics=semantics,
        )


def test_remote_posix_path_semantics_keep_unicode_forms_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rcp.agents.write_scope.sys.platform", "darwin")
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
    monkeypatch.setattr(stage, "sweep", lambda: None)
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
