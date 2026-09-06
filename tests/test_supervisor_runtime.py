from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.runtime import Paths, SystemRuntime


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch):
    runtime = object.__new__(SystemRuntime)
    runtime.paths = Paths(service_home=tmp_path)
    runtime.uid, runtime.gid = os.getuid(), os.getgid()
    runtime.notify = lambda message: None
    runtime.startup = False
    runtime.restore_request = None
    runtime._deployment_lock_fd = None
    if sys.platform != "linux":
        # Production requires Linux. This local framing fixture explicitly
        # bypasses prctl only on hosts where its kernel proof cannot run.
        runtime.owned_argv = lambda argv: argv
    logs = tmp_path / "logs"
    logs.mkdir()
    counter = iter(range(20))

    @contextmanager
    def log(label):
        path = logs / f"{next(counter)}-{label}.log"
        with path.open("w+b") as output:
            yield output, str(path)

    runtime._log = log
    actual_popen = subprocess.Popen

    def popen(*args, **kwargs):
        # The Linux VM qualification proves the UID transition. These real local
        # subprocesses exercise framing, deadlines, output bounds and diagnostics.
        assert kwargs.pop("user") == os.getuid()
        assert kwargs.pop("group") == os.getgid()
        assert kwargs.pop("extra_groups") == []
        return actual_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return runtime


def test_application_stderr_does_not_corrupt_json_and_diagnostics_survive(runtime):
    result = runtime.service_json(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys,json; print('diagnostic',file=sys.stderr); print(json.dumps(json.load(sys.stdin)))",
        ],
        {"version": 1, "payload": "x" * 100_000},
    )
    assert result["payload"] == "x" * 100_000
    assert any(
        "diagnostic" in path.read_text() for path in (runtime.paths.service_home / "logs").iterdir()
    )


def test_subprocess_not_reading_stdin_cannot_defeat_deadline(runtime):
    start = time.monotonic()
    with pytest.raises(SupervisorError, match="time limit"):
        runtime.service_json(
            [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
            {"payload": "x" * 1_000_000},
            timeout=0.2,
        )
    assert time.monotonic() - start < 5


def test_stderr_output_is_bounded_too(runtime, monkeypatch):
    monkeypatch.setattr("rcp_supervisor.runtime.MAX_APP_OUTPUT_BYTES", 8192)
    with pytest.raises(SupervisorError, match="output|Output"):
        runtime.service_json(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys,time; sys.stderr.write('x'*10000); sys.stderr.flush(); time.sleep(60)",
            ],
            timeout=5,
        )


def test_backup_requires_complete_protected_receipt(runtime, monkeypatch):
    def output(status, count):
        return json.dumps(
            {
                "step": {
                    "fields": [
                        {"name": "backup_status", "value": status},
                        {"name": "uncaptured_projects", "value": count},
                    ]
                }
            }
        ).encode()

    legacy = {"release_directory": "/tmp/release", "commit": "a" * 40, "version_string": "0.3.4"}
    monkeypatch.setattr(runtime, "_service_output", lambda *args, **kwargs: output("protected", 0))
    runtime.protected_backup(legacy)
    for status, count in [("partial", 1), ("complete", 0), ("protected", 1)]:
        monkeypatch.setattr(
            runtime,
            "_service_output",
            lambda *args, _status=status, _count=count, **kwargs: output(_status, _count),
        )
        with pytest.raises(SupervisorError, match="complete verified"):
            runtime.protected_backup(legacy)


def test_probe_arms_parent_ownership_in_supervisor_code_after_service_uid_drop(
    runtime, monkeypatch
):
    from types import SimpleNamespace

    release = {"release_directory": "/isolated/selected"}
    operation = {"operation_id": "operation", "nonce": "boundary"}
    launched = []
    stopped = []
    runtime.owned_argv = SystemRuntime.owned_argv.__get__(runtime)
    runtime._deployment_lock_fd = 42
    monkeypatch.setattr(runtime, "require_capability", lambda _release: None)
    monkeypatch.setattr(runtime, "prepare_control_directory", lambda: None)
    monkeypatch.setattr(runtime, "environment", lambda _release: {})
    monkeypatch.setattr(runtime, "_stop_child", lambda process: stopped.append(process.pid))

    def popen(argv, **kwargs):
        launched.append((argv, kwargs))
        return SimpleNamespace(pid=123, poll=lambda: 1)

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(SupervisorError, match="exited before verification"):
        runtime.probe(release, operation, None)
    ((argv, kwargs),) = launched
    assert argv[:5] == [
        sys.executable,
        "-I",
        str(Path(sys.modules[SystemRuntime.__module__].__file__).with_name("child_exec.py")),
        str(os.getpid()),
        "/isolated/selected/.venv/bin/python",
    ]
    assert argv[5:9] == ["-I", "-m", "rcp", "serve"]
    assert kwargs["pass_fds"] == (42,)
    assert kwargs["user"] == runtime.uid and kwargs["group"] == runtime.gid
    assert kwargs["extra_groups"] == [] and kwargs["start_new_session"] is True
    assert stopped == [123]


@pytest.fixture
def control_runtime(runtime, tmp_path, monkeypatch):
    parent = tmp_path / "runtime-parent"
    parent.mkdir()
    runtime.paths = Paths(control_socket=parent / "rcp/control.sock", service_home=tmp_path)
    ancestors = set(runtime.paths.control_socket.parent.parents)
    lstat = Path.lstat
    fstat = os.fstat
    parent_identity = (parent.stat().st_dev, parent.stat().st_ino)

    def protected_ancestor(path):
        info = lstat(path)
        if path in ancestors:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        return info

    def protected_parent_fd(descriptor):
        info = fstat(descriptor)
        if (info.st_dev, info.st_ino) == parent_identity:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        return info

    # The local test account cannot own /run. Only ancestry ownership is modeled;
    # mkdir/open/chown/chmod/fsync and runtime entry validation operate on real files.
    monkeypatch.setattr(Path, "lstat", protected_ancestor)
    monkeypatch.setattr(os, "fstat", protected_parent_fd)
    return runtime


def test_probe_recreates_runtime_directory_removed_by_systemd(control_runtime, monkeypatch):
    runtime = control_runtime
    directory = runtime.paths.control_socket.parent
    runtime.prepare_control_directory()
    directory.rmdir()  # RuntimeDirectoryPreserve=no removes this on systemctl stop.
    monkeypatch.setattr(runtime, "require_capability", lambda _release: None)
    monkeypatch.setattr(runtime, "environment", lambda _release: {})

    def launch(argv, **kwargs):
        assert directory.is_dir()
        info = directory.stat()
        assert (info.st_uid, info.st_gid) == (runtime.uid, runtime.gid)
        assert stat.S_IMODE(info.st_mode) == 0o700
        return SimpleNamespace(pid=123, poll=lambda: 1)

    monkeypatch.setattr(subprocess, "Popen", launch)
    with pytest.raises(SupervisorError, match="exited before verification"):
        runtime.probe(
            {"release_directory": "/prepared"},
            {"operation_id": "operation", "nonce": "boundary"},
            None,
        )


def test_control_directory_preparation_preserves_existing_entries(control_runtime):
    runtime = control_runtime
    runtime.prepare_control_directory()
    directory = runtime.paths.control_socket.parent
    entry = directory / "retained-entry"
    entry.write_bytes(b"do not alter")
    entry.chmod(0o400)
    before = entry.stat()
    runtime.prepare_control_directory()
    assert entry.read_bytes() == b"do not alter"
    after = entry.stat()
    assert (after.st_ino, after.st_mode, after.st_uid, after.st_gid) == (
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
    )


@pytest.mark.parametrize("unsafe", ["symlink", "permissions", "regular_file", "owner"])
def test_control_directory_preparation_refuses_unsafe_existing_path(control_runtime, unsafe):
    directory = control_runtime.paths.control_socket.parent
    if unsafe == "symlink":
        directory.symlink_to(directory.parent, target_is_directory=True)
    elif unsafe == "permissions":
        directory.mkdir(mode=0o755)
    elif unsafe == "owner":
        directory.mkdir(mode=0o700)
        control_runtime.uid += 1
    else:
        directory.write_bytes(b"retain")
    with pytest.raises((SupervisorError, OSError)):
        control_runtime.prepare_control_directory()


@pytest.mark.parametrize("held", [False, True])
def test_service_worker_receives_parent_ownership_and_only_held_lock(runtime, monkeypatch, held):
    from types import SimpleNamespace

    wrapped = []

    def owned_argv(argv):
        wrapped.append(argv)
        return ["owned-child", *argv]

    monkeypatch.setattr(runtime, "owned_argv", owned_argv)
    runtime._deployment_lock_fd = 42 if held else None
    launched = []

    def popen(argv, **kwargs):
        launched.append((argv, kwargs))
        return SimpleNamespace(pid=123, poll=lambda: 0, returncode=0, stdin=None)

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert runtime._service_output(["/installed/worker"], timeout=5) == b""
    assert wrapped == [["/installed/worker"]]
    ((argv, kwargs),) = launched
    assert argv == ["owned-child", "/installed/worker"]
    assert kwargs["pass_fds"] == ((42,) if held else ())


def test_deployment_and_existing_backup_share_one_kernel_lock(runtime, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from rcp.server_ops.backup import BackupRunRefused, backup_run_lock

    runtime.paths = replace(runtime.paths, data_dir=runtime.paths.service_home / "data")
    layout = SimpleNamespace(server_root=runtime.paths.service_home)
    monkeypatch.setattr("rcp_supervisor.runtime.MAINTENANCE_TIMEOUT_SECONDS", 0.01)
    with (
        backup_run_lock(layout),
        pytest.raises(SupervisorError, match="running protected backup"),
        runtime.deployment_lock(),
    ):
        pytest.fail("deployment raced the backup")
    with runtime.deployment_lock():
        assert runtime._deployment_lock_fd is not None
        with pytest.raises(BackupRunRefused), backup_run_lock(layout):
            pytest.fail("backup raced deployment")
    with backup_run_lock(layout):
        assert runtime._deployment_lock_fd is None


@pytest.mark.parametrize("matches", [False, True])
def test_selected_release_requires_receipt_and_current_pointer_agreement(monkeypatch, matches):
    from rcp_supervisor import driver
    from rcp_supervisor import runtime as runtime_module

    selected = {"release_directory": "/releases/100"}
    pointer = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=stat.S_IFLNK, st_uid=0))
    paths = SimpleNamespace(selected="receipt", releases_root="/releases", current=pointer)
    runtime = object.__new__(SystemRuntime)
    runtime.paths = paths
    monkeypatch.setattr(runtime_module, "read_selected_receipt", lambda *args, **kwargs: selected)
    monkeypatch.setattr(
        runtime_module.os, "readlink", lambda _: "/releases/100" if matches else "/releases/101"
    )
    for read in (runtime.selected_release, lambda: driver.selected_pointer(paths)):
        if matches:
            assert read() == selected
        else:
            with pytest.raises(SupervisorError, match="current pointer.*disagree"):
                read()
