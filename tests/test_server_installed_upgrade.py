from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest
from rcp_supervisor import driver
from rcp_supervisor.checkpoint import create_stopped_snapshot, restore_checkpoint
from rcp_supervisor.operations import Coordinator
from rcp_supervisor.runtime import Paths, SystemRuntime

from tests import server_installed_upgrade as installed
from tests import server_installed_upgrade_hook as hook


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
