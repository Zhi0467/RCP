from __future__ import annotations

import os
import shlex
import subprocess
import sys

import pytest

from rcp.transport import state_transfer
from rcp.transport.run_stage import RemoteStageTransportFailure


@pytest.fixture(autouse=True)
def isolated_transfer_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("RCP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(state_transfer, "_CACHE", {})
    monkeypatch.setattr(state_transfer, "_WARNED", set())


@pytest.mark.parametrize(
    "version, passes",
    [
        ("rsync  version 3.1.0  protocol version 31", True),
        ("rsync  version 3.4.1  protocol version 32", True),
        ("openrsync: protocol version 29", True),
        ("rsync  version 3.0.9  protocol version 30", True),
        ("rsync  version 2.6.9  protocol version 29", True),
        ("rsync  version 2.6.3  protocol version 28", False),
        ("openrsync: protocol version 28", False),
        ("unknown rsync", False),
    ],
)
def test_version_contract(version, passes):
    assert state_transfer.parse_version(version) is passes


def test_first_passing_path_candidate(tmp_path, monkeypatch):
    directories = [tmp_path / str(index) for index in range(3)]
    for directory, version in zip(
        directories,
        [
            "rsync  version 3.4.1  protocol version 32",
            "openrsync: protocol version 29",
            "rsync  version 3.4.1  protocol version 32",
        ],
        strict=True,
    ):
        directory.mkdir()
        script = directory / "rsync"
        reject_relative = (
            'for option do [ "$option" != "-R" ] || exit 1; done\n'
            if directory == directories[0]
            else ""
        )
        script.write_text(f"#!/bin/sh\n{reject_relative}printf '%s\\n' {shlex.quote(version)}\n")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join(map(str, directories)))
    binary, version = state_transfer._local_candidate()
    assert binary == str(directories[1] / "rsync")
    assert state_transfer.parse_version(version)


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    script = directory / "ssh"
    script.write_text(
        f"#!{sys.executable}\nimport os, sys\nos.execl('/bin/sh', 'sh', '-c', sys.argv[-1])\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    # Keep socket-directory setup inside this disposable fixture as well.
    monkeypatch.setattr("rcp.transport.ssh._require_control_directory", lambda: directory)
    return script


def test_tar_pull_mirrors_deletion_and_preserves_excludes(tmp_path, fake_ssh):
    remote = tmp_path / "remote"
    local = tmp_path / "local"
    remote.mkdir()
    local.mkdir()
    root_inode = local.stat().st_ino
    (remote / "current").write_text("new")
    (remote / "current").chmod(0o750)
    (remote / "alias").symlink_to("current")
    (remote / "external-alias").symlink_to("/outside-target")
    (remote / ".publish").mkdir()
    (remote / ".publish" / "remote-only").write_text("excluded")
    (local / "obsolete").write_text("old")
    (local / "removed-parent").mkdir()
    lock = local / "removed-parent" / ".refresh.lock"
    lock.write_text("keep")
    inode = lock.stat().st_ino
    (local / "removed-parent" / "obsolete").write_text("old")
    result = state_transfer.pull_tar("fixture", remote, local, [".publish", ".refresh.lock"])
    assert result.returncode == 0, result.stderr
    assert local.stat().st_ino == root_inode
    assert (local / "current").read_text() == "new"
    assert (local / "current").stat().st_mode & 0o777 == 0o750
    assert (local / "alias").is_symlink()
    assert os.readlink(local / "external-alias") == "/outside-target"
    assert not (local / "obsolete").exists()
    assert not (local / ".publish").exists()
    assert lock.read_text() == "keep"
    assert lock.stat().st_ino == inode
    assert not (local / "removed-parent" / "obsolete").exists()
    unchanged = {
        name: (local / name).lstat().st_ino for name in ("current", "alias", "external-alias")
    }
    result = state_transfer.pull_tar("fixture", remote, local, [".publish", ".refresh.lock"])
    assert result.returncode == 0, result.stderr
    assert {name: (local / name).lstat().st_ino for name in unchanged} == unchanged


@pytest.mark.parametrize("complete_file", [False, True])
def test_incomplete_archive_preserves_previous_tree(tmp_path, fake_ssh, complete_file):
    local = tmp_path / "local"
    local.mkdir()
    (local / "previous").write_text("intact")
    fake_ssh.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(b'partial tar stream')\n"
    )
    if complete_file:
        fake_ssh.write_text(
            f"#!{sys.executable}\nimport io, sys, tarfile\nstream = io.BytesIO()\nwith tarfile.open(fileobj=stream, mode='w') as archive:\n    member = tarfile.TarInfo('new')\n    member.size = 3\n    archive.addfile(member, io.BytesIO(b'new'))\nsys.stdout.buffer.write(stream.getvalue()[:1024])\n"
        )
    result = state_transfer.pull_tar("fixture", tmp_path, local, [])
    assert result.returncode != 0
    assert sorted(path.name for path in local.iterdir()) == ["previous"]
    assert (local / "previous").read_text() == "intact"


def test_failed_mid_apply_keeps_complete_files_and_converges(tmp_path, fake_ssh, monkeypatch):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    root_inode = local.stat().st_ino
    for name in ("first", "second"):
        (local / name).write_text(f"old {name}")
        (remote / name).write_text(f"new {name}")
    (local / "obsolete").write_text("keep until publication completes")
    replace = os.replace

    def fail_second(source, destination):
        if destination == local / "second":
            raise OSError("injected publication failure")
        return replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(state_transfer.os, "replace", fail_second)
        result = state_transfer.pull_tar("fixture", remote, local, [])
    assert result.returncode != 0
    assert local.stat().st_ino == root_inode
    assert (local / "first").read_text() == "new first"
    assert (local / "second").read_text() == "old second"
    assert (local / "obsolete").exists()
    result = state_transfer.pull_tar("fixture", remote, local, [])
    assert result.returncode == 0, result.stderr
    assert local.stat().st_ino == root_inode
    assert (local / "first").read_text() == "new first"
    assert (local / "second").read_text() == "new second"
    assert not (local / "obsolete").exists()


def test_tar_push_sends_only_requested_paths(tmp_path, fake_ssh):
    local = tmp_path / "local"
    stage = tmp_path / "stage"
    (local / "patches").mkdir(parents=True)
    stage.mkdir()
    (local / "patches" / "one.json").write_text("one")
    (local / "patches" / "two.json").write_text("two")
    result = state_transfer.push_tar("fixture", stage, local, ["patches/one.json"])
    assert result.returncode == 0, result.stderr
    assert (stage / "patches" / "one.json").read_text() == "one"
    assert not (stage / "patches" / "two.json").exists()


@pytest.mark.parametrize(
    "code, stderr", [(127, "missing"), (12, "protocol version mismatch -- is your shell clean?")]
)
def test_fallback_warning_once_per_host(monkeypatch, caplog, code, stderr):
    monkeypatch.setattr(
        state_transfer, "_local_candidate", lambda: (None, "rsync not found on PATH")
    )
    monkeypatch.setattr(
        state_transfer, "ssh_arguments", lambda host, command: ["ssh", host, command]
    )
    probes = []

    def probe(argv, **_kwargs):
        probes.append(argv)
        return True, "rsync  version 3.2.7  protocol version 31"

    monkeypatch.setattr(state_transfer, "_probe", probe)
    events = []
    with state_transfer.warning_context(events.append):
        assert state_transfer.get_engine("fixture").engine == "tar"
        state_transfer.get_engine("fixture")
        state_transfer.transfer_result("fixture", subprocess.CompletedProcess([], code, "", stderr))
    assert len(events) == 1
    assert len(caplog.records) == 1
    assert len(probes) == 2
    assert state_transfer.diagnostics("fixture")["engine"] == "tar"


@pytest.mark.parametrize("replacement", ["symlink", "file"])
def test_remote_parent_conflict_cannot_redirect_local_exclusions(tmp_path, fake_ssh, replacement):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    outside = tmp_path / "outside"
    (local / "directory").mkdir(parents=True)
    remote.mkdir()
    outside.mkdir()
    lock = local / "directory" / ".refresh.lock"
    lock.write_text("protected")
    if replacement == "symlink":
        (remote / "directory").symlink_to(outside, target_is_directory=True)
    else:
        (remote / "directory").write_text("replacement")
    result = state_transfer.pull_tar("fixture", remote, local, [".refresh.lock"])
    assert result.returncode != 0
    assert lock.read_text() == "protected"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("failure", ["ssh_exit", "timeout", "os_error"])
def test_probe_transport_failure_is_not_cached_or_warned(monkeypatch, caplog, failure):
    from rcp.transport import StateUnavailable, StateUnreachable

    monkeypatch.setattr(
        state_transfer,
        "_local_candidate",
        lambda: ("/fixture/rsync", "openrsync: protocol version 29"),
    )
    attempts = []

    def run(argv, **_kwargs):
        attempts.append(argv)
        if len(attempts) == 1:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 1)
            if failure == "os_error":
                raise OSError("cannot start ssh")
            return subprocess.CompletedProcess(argv, 255, "", "connection lost")
        return subprocess.CompletedProcess(argv, 0, "openrsync: protocol version 29", "")

    monkeypatch.setattr(state_transfer.subprocess, "run", run)
    events = []
    with state_transfer.warning_context(events.append):
        with pytest.raises(StateUnavailable) as error:
            state_transfer.get_engine("fixture")
        assert type(error.value) is (
            StateUnreachable if failure == "ssh_exit" else RemoteStageTransportFailure
        )
        assert state_transfer.diagnostics("fixture") is None
        assert not state_transfer._WARNED
        assert events == []
        assert caplog.records == []
        engine = state_transfer.get_engine("fixture")
        assert engine.engine == "rsync"
        assert state_transfer.get_engine("fixture") is engine
    assert len(attempts) == 2
    assert events == []
    assert caplog.records == []


@pytest.mark.parametrize(
    "failures, code, stderr, attempts, final",
    [
        (1, 255, "Connection reset by peer", 2, 0),
        (1, 12, "rsync: unexpected end of file", 2, 0),
        (9, 255, "Corrupted MAC on input", 3, 255),
        (9, 23, "some files could not be transferred", 1, 23),
        (9, 127, "rsync: command not found", 1, 127),
    ],
)
def test_dropped_transfer_retries_boundedly(monkeypatch, failures, code, stderr, attempts, final):
    monkeypatch.setattr(state_transfer, "_CACHE", {"fixture": object()})
    monkeypatch.setattr(state_transfer.time, "sleep", lambda _seconds: None)
    calls = []

    def run(argv, **_kwargs):
        if argv[0] != "rsync":
            return subprocess.CompletedProcess(argv, 127, "", "not found")
        calls.append(argv)
        if len(calls) <= failures:
            return subprocess.CompletedProcess(argv, code, "", stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(state_transfer.subprocess, "run", run)
    result = state_transfer.run_rsync("fixture", ["rsync", "a", "b"], phase="push")
    assert len(calls) == attempts
    assert result.returncode == final
    if final and attempts > 1:
        assert result.stderr.count(stderr) == attempts
