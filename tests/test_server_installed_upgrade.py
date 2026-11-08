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
    """The runner is Linux-only; this still exercises real mkdir and umask locally."""
    monkeypatch.setattr(hook, "permission_state", lambda: {"umask": f"{mask:04o}"})
    if sys.platform != "linux":
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
    """The xattr is a Linux POSIX ACL: version 2, user::rwx, group::rwx, other::r-x."""
    import struct

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
    """Regression: the oracle cannot silently accept the reported empty-patches bug."""
    monkeypatch.setattr(hook, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(driver, "followed_release", driver.followed_release)
    monkeypatch.setattr(driver, "prepare_release", driver.prepare_release)
    monkeypatch.setattr(Coordinator, "__init__", Coordinator.__init__)
    receipt = tmp_path / "sqlite-capture.json"
    receipt.write_text('{"projects": []}')
    monkeypatch.setattr(
        SystemRuntime,
        "enter_maintenance",
        lambda self, operation: {"receipt_path": str(receipt)},
    )
    monkeypatch.setattr(
        SystemRuntime, "__init__", lambda self, paths, **kwargs: setattr(self, "paths", paths)
    )
    hook.install_hooks()
    runtime = SystemRuntime(Paths(data_dir=tmp_path / "data"))
    assert runtime.enter_maintenance({}) == {"receipt_path": str(receipt)}
    assert (tmp_path / "maintenance-capture.json").read_bytes() == receipt.read_bytes()
    assert runtime.paths.port == installed.PORT and runtime.paths.port != 8421
    data = runtime.paths.data_dir
    data.mkdir()
    sentinel = data / "installed-upgrade-fault-sentinel"
    sentinel.write_text("original fixture sentinel\n")
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
    assert (data / "installed-upgrade-fault-created").exists()
    restore_checkpoint(saved)
    coordinator.boundary("rollback_roots_complete")
    assert json.loads((tmp_path / "rollback-exact.json").read_text())["roots"] == 2
    assert sentinel.read_text() == "original fixture sentinel\n"
    assert not (data / "installed-upgrade-fault-created").exists()
    assert (research / "patches").is_dir()
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


def test_added_codex_home_upgrade_preserves_old_writer_store(tmp_path, monkeypatch):
    from tests.server_upgrade_codex import check_codex_home, seed_codex_home

    monkeypatch.setattr("rcp.providers.codex.homes._legacy_home_idle", lambda home: True)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "machine"))
    fixture = seed_codex_home(tmp_path, "member")
    assert fixture is not None
    home = Path(fixture["home"])
    assert (home / "sessions").is_dir() and not (home / "sessions").is_symlink()
    assert (home / "state_5.sqlite").is_file()
    check_codex_home(tmp_path, fixture, migrated=True)
    check_codex_home(tmp_path, fixture, migrated=True)
    check_codex_home(tmp_path, fixture, migrated=False)


@pytest.mark.parametrize("wrong", ["token", "owner", "mode", "symlink", "hardware"])
def test_private_guest_requires_exact_owned_marker(tmp_path, monkeypatch, wrong):
    marker = tmp_path / "marker"
    marker.write_text("owned-token")
    hardware = tmp_path / "hardware"
    hardware.write_text("QEMU" if wrong != "hardware" else "host hardware")
    monkeypatch.setattr(
        installed, "Path", lambda name: marker if name.endswith("qualification") else hardware
    )
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: SimpleNamespace(
            st_uid=501 if wrong == "owner" else 0,
            st_mode=(stat.S_IFLNK if wrong == "symlink" else stat.S_IFREG)
            | (0o644 if wrong == "mode" else 0o600),
        ),
    )
    with pytest.raises(RuntimeError, match="exact owned QEMU guest token"):
        installed.require_guest("wrong-token" if wrong == "token" else "owned-token")


def test_owned_guest_still_refuses_existing_host_installation(monkeypatch):
    monkeypatch.setattr(installed, "require_guest", lambda token: None)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(Path, "is_dir", lambda path: True)
    monkeypatch.setattr(os.path, "lexists", lambda path: path == installed.ROOT)
    with pytest.raises(RuntimeError, match="Refusing an existing installation"):
        installed.preflight(guest_token="owned")


def test_prepared_installation_requires_bound_guest_token(tmp_path, monkeypatch):
    monkeypatch.setattr(installed, "ROOT", tmp_path)
    monkeypatch.setattr(installed, "require_guest", lambda token: None)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(Path, "is_dir", lambda path: True)
    state = tmp_path / "state/prepared.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"guest_token": "original"}))
    monkeypatch.setattr(
        Path, "lstat", lambda path: SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600)
    )
    with pytest.raises(RuntimeError, match="another guest"):
        installed.preflight(guest_token="different", prepared=True)
    installed.preflight(guest_token="original", prepared=True)


@pytest.mark.parametrize(
    "fault",
    [None, "short_commit", "wrong_version", "missing_rollback", "uncommitted", "wrong_supervisor"],
)
def test_full_report_requires_observed_committed_retry(tmp_path, monkeypatch, fault):
    source = {
        "manifest_sha256": "a" * 64,
        "commit": "a" * 40,
        "version": "0.4.40",
        "supervisor_version": "0.1.14",
    }
    target = {
        "manifest_sha256": "b" * 64,
        "commit": "b" * 40,
        "version": "0.4.42",
        "supervisor_version": "0.1.15",
    }
    monkeypatch.setattr(installed, "ROOT", tmp_path)
    monkeypatch.setattr(installed, "OPERATIONS", tmp_path / "operations")
    installed.OPERATIONS.mkdir()
    monkeypatch.setattr(installed, "SELECTED", tmp_path / "selected.json")
    installed.SELECTED.write_text(json.dumps(target))
    (tmp_path / "rollback-exact.json").write_text(
        json.dumps({"operation_id": "rollback", "roots": 2})
    )
    for operation_id, phase in (("rollback", "rolled_back"), ("retry", "committed")):
        if fault == "missing_rollback" and operation_id == "rollback":
            continue
        if fault == "uncommitted" and operation_id == "retry":
            phase = "aborted"
        (installed.OPERATIONS / f"{operation_id}.json").write_text(
            json.dumps(
                {
                    "operation_id": operation_id,
                    "phase": phase,
                    "previous": source,
                    "target": target,
                }
            )
        )
    monkeypatch.setattr(
        installed, "bundle_identity", lambda bundle: source if bundle.name == "base" else target
    )
    monkeypatch.setattr(
        installed,
        "selected_supervisor_version",
        lambda: "0.1.14" if fault == "wrong_supervisor" else "0.1.15",
    )
    monkeypatch.setattr(
        installed,
        "wait_health",
        lambda: {
            "status": "ok",
            "running_commit": "b" * (7 if fault == "short_commit" else 40),
            "commit": "b" * 7,
            "version": "wrong" if fault == "wrong_version" else "0.4.42",
        },
    )
    if fault:
        with pytest.raises(AssertionError):
            installed.completed_report(
                tmp_path / "base", tmp_path / "target", "c" * 64, "stopped-source"
            )
    else:
        report = installed.completed_report(
            tmp_path / "base", tmp_path / "target", "c" * 64, "stopped-source"
        )
        assert report["serving"]["commit"] == "b" * 40
        assert report["rollback_exact"] is True and report["retry_committed"] is True
        assert report["journal"]["phase"] == "committed"
        assert report["input_sha256"] == "c" * 64


def test_required_ci_matrix_keeps_every_release_and_adds_production_corpus(monkeypatch, capsys):
    import re
    import textwrap

    from tests import server_upgrade_harness

    monkeypatch.setattr(
        server_upgrade_harness, "published_release_tags", lambda: {"v0.4.40", "v0.4.39"}
    )
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    code = re.search(r"python -c '\n(.*?)\n          ' >>", workflow, re.DOTALL)
    assert code is not None
    exec(textwrap.dedent(code.group(1)))
    cases = json.loads(capsys.readouterr().out.removeprefix("releases="))["include"]
    assert [case["tag"] for case in cases if not case["corpus"]] == ["v0.4.39", "v0.4.40"]
    production = [case for case in cases if case["corpus"]]
    assert production == [
        {
            "tag": "v0.4.40",
            "corpus": "terminal-transfer",
            "artifact": "installed-upgrade-v0.4.40-terminal-transfer",
        }
    ]
    assert len({case["artifact"] for case in cases}) == len(cases)


@pytest.mark.parametrize(
    "profile",
    [
        ["--private-state", "manifest.json", "--production-corpus", "terminal-transfer"],
        ["--prepare-guest", "--production-corpus", "terminal-transfer"],
        ["--private-state", "manifest.json"],
        ["--guest-token", "owned"],
    ],
)
def test_private_profile_rejects_unbound_or_synthetic_input_before_host_effects(
    monkeypatch, profile
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "driver",
            "--base",
            "base",
            "--candidate",
            "target",
            "--output",
            "out",
            "--tag",
            "v0.4.40",
            *profile,
        ],
    )
    monkeypatch.setattr(
        installed,
        "preflight",
        lambda **kwargs: pytest.fail("invalid profile reached host preflight"),
    )
    with pytest.raises(SystemExit) as failure:
        installed.main()
    assert failure.value.code == 2
