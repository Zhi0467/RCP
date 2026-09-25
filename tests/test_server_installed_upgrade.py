from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rcp_supervisor import driver
from rcp_supervisor.checkpoint import create_stopped_snapshot, restore_checkpoint
from rcp_supervisor.operations import Coordinator
from rcp_supervisor.runtime import Paths, SystemRuntime

from tests import server_installed_upgrade as installed
from tests import server_installed_upgrade_hook as hook


@pytest.mark.parametrize("mask", [0o002, 0o077])
def test_permission_probe_observes_creation_modes_without_changing_policy(
    tmp_path, monkeypatch, mask
):
    monkeypatch.setattr(hook, "permission_state", lambda: {"umask": f"{mask:04o}"})
    if sys.platform != "linux":
        # The runner is Linux-only; still exercise real mkdir/umask locally.
        monkeypatch.setattr(os, "listxattr", lambda _path: [], raising=False)
    previous = os.umask(mask)
    try:
        result = hook.service_permissions(tmp_path)
        assert os.umask(mask) == mask
    finally:
        os.umask(previous)
    assert result["umask"] == f"{mask:04o}"
    assert result["directory_mode"] == oct(0o777 & ~mask)
    assert result["file_mode"] == oct(0o666 & ~mask)
    assert result["default_acls"] == {}
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(sys.platform != "linux", reason="Linux default ACL inheritance")
def test_permission_probe_distinguishes_default_acl_from_process_mask(tmp_path, monkeypatch):
    import struct

    # Linux POSIX ACL xattr: version 2, user::rwx, group::rwx, other::r-x.
    acl = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, mode, 0xFFFFFFFF) for tag, mode in ((1, 7), (4, 7), (32, 5))
    )
    os.setxattr(tmp_path, "system.posix_acl_default", acl)
    monkeypatch.setattr(hook, "permission_state", lambda: {"umask": "0077"})
    previous = os.umask(0o077)
    try:
        result = hook.service_permissions(tmp_path)
        assert os.umask(0o077) == 0o077
    finally:
        os.umask(previous)
    assert result["directory_mode"] == "0o775"
    assert result["file_mode"] == "0o664"
    assert result["default_acls"][str(tmp_path)] == acl.hex()
    assert not list(tmp_path.iterdir())


def test_permission_state_reads_kernel_without_mutating_mask(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda _path: "Name:\tpython\nUmask:\t0077\n")
    monkeypatch.setattr(os, "umask", lambda _: pytest.fail("observation changed umask"))
    assert hook.permission_state()["umask"] == "0077"


def test_uv_observation_does_not_disclose_arguments_or_environment(monkeypatch, capsys):
    monkeypatch.setattr(hook, "permission_state", lambda: {"umask": "0077"})
    hook.observe_uv_launch("subprocess.Popen", ("/usr/local/bin/uv", ["secret"], None, {}))
    hook.observe_uv_launch("subprocess.Popen", ("/usr/bin/other", [], None, {}))
    assert capsys.readouterr().err == 'Installed upgrade uv parent: {"umask": "0077"}\n'


def test_installed_drive_refuses_without_disposable_root_opt_in(monkeypatch):
    monkeypatch.setenv("RCP_RUN_INSTALLED_UPGRADE", "1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    with pytest.raises(RuntimeError, match="disposable GitHub"):
        installed.preflight()


def test_installed_fault_observes_real_snapshot_and_exact_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    # Restore instrumentation after the test; these are the only production hooks.
    monkeypatch.setattr(driver, "followed_release", driver.followed_release)
    monkeypatch.setattr(Coordinator, "__init__", Coordinator.__init__)
    monkeypatch.setattr(
        SystemRuntime, "__init__", lambda self, paths, **kwargs: setattr(self, "paths", paths)
    )
    hook.install_hooks()
    runtime = SystemRuntime(Paths(data_dir=tmp_path / "data"))
    assert runtime.paths.port == installed.PORT and runtime.paths.port != 8421
    data = runtime.paths.data_dir
    for relative in (
        "run-stage/real-tools/repo",
        "run-stage/real-tools/empty",
        "providers/claude/qualification",
    ):
        (data / relative).mkdir(parents=True)
    (data / "run-stage/real-tools/repo/result.txt").write_text("original")
    (data / "providers/claude/qualification/setup-token").write_text("synthetic")
    research = tmp_path / "project/.research"
    (research / "patches").mkdir(parents=True)
    saved = create_stopped_snapshot(
        tmp_path / "checkpoint", (data, research), boundary_sha256="a" * 64
    )
    operation = {"operation_id": "test", "checkpoint": {"directory": str(saved.directory)}}
    coordinator = Coordinator(SimpleNamespace(active=lambda: operation), runtime)
    (tmp_path / "selection.json").write_text(json.dumps({"observe": True, "fail": True}))
    coordinator.boundary("snapshot_ready")
    with pytest.raises(RuntimeError, match="intentional candidate failure"):
        coordinator.boundary("candidate_verified")
    assert (data / "candidate-only").exists()
    restore_checkpoint(saved)
    coordinator.boundary("rollback_roots_complete")
    assert json.loads((tmp_path / "rollback-exact.json").read_text())["roots"] == 2
    assert (data / "run-stage/real-tools/repo/result.txt").read_text() == "original"
    assert (data / "providers/claude/qualification/setup-token").read_text() == "synthetic"
    assert (research / "patches").is_dir()
    # The oracle cannot silently accept the reported empty-patches regression.
    (research / "patches").rmdir()
    with pytest.raises(AssertionError, match="rollback changed"):
        coordinator.boundary("rollback_roots_complete")


@pytest.mark.parametrize("unsafe", ["executable_owner", "ancestor_owner", "ancestor_mode"])
def test_installed_toolchain_refuses_runner_writable_uv(monkeypatch, unsafe):
    executable = Path("/usr/local/bin/uv")
    monkeypatch.setattr(Path, "resolve", lambda self, *, strict: self)

    def metadata(path):
        regular = path == executable
        mode = (stat.S_IFREG if regular else stat.S_IFDIR) | 0o755
        uid = 0
        if unsafe == "executable_owner" and regular:
            uid = 1000
        if path == executable.parent:
            if unsafe == "ancestor_owner":
                uid = 1000
            if unsafe == "ancestor_mode":
                mode |= 0o020
        return SimpleNamespace(st_mode=mode, st_uid=uid)

    monkeypatch.setattr(Path, "stat", metadata)
    with pytest.raises(RuntimeError, match="root-owned uv"):
        installed.prepare_toolchain()


def test_installed_toolchain_drops_runner_python_and_cache_settings(monkeypatch):
    executable = Path("/usr/local/bin/uv")
    monkeypatch.setattr(Path, "resolve", lambda self, *, strict: self)
    monkeypatch.setattr(
        Path,
        "stat",
        lambda path: SimpleNamespace(
            st_uid=0, st_mode=(stat.S_IFREG if path == executable else stat.S_IFDIR) | 0o755
        ),
    )
    environment = {
        "PATH": "/runner/bin",
        "UV_PYTHON_INSTALL_DIR": "/runner/python",
        "UV_CACHE_DIR": "/runner/cache",
        "PIP_TARGET": "/runner/packages",
        "PYTHONPATH": "/runner/source",
        "VIRTUAL_ENV": "/runner/venv",
        "RCP_RUN_INSTALLED_UPGRADE": "1",
        "GITHUB_ACTIONS": "true",
    }
    monkeypatch.setattr(os, "environ", environment)
    installed.prepare_toolchain()
    assert environment == {
        "PATH": "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "RCP_RUN_INSTALLED_UPGRADE": "1",
        "GITHUB_ACTIONS": "true",
    }
