from __future__ import annotations

import json
import shutil
from contextlib import nullcontext
from pathlib import Path

import pytest
from rcp_supervisor.checkpoint import (
    Checkpoint,
    SnapshotRoot,
    create_checkpoint,
    restore_checkpoint,
)
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.operations import Coordinator, OperationBusy, OperationStore


class PowerLoss(BaseException):
    pass


class FakeRuntime:
    def __init__(self, root: Path):
        self.root = root
        self.data = root / "data"
        self.data.mkdir(mode=0o700)
        (self.data / "value").write_text("old")
        self.fail_target = False
        self.fail_previous = False
        self.start_failure = False
        self.accept_then_die = False
        self.stops = 0
        self.starts = 0
        self.restores = 0
        self.pointer = 100
        self.selected = 100

    def selected_release(self):
        return self.releases[self.selected]

    def protected_backup(self):
        pass

    def deployment_lock(self):
        return nullcontext()

    def enter_maintenance(self, operation):
        return {"closed": True}

    def abort_maintenance(self, operation):
        pass

    def stop_service(self):
        self.stops += 1

    def prepare(self, operation, capture):
        payload = self.root / "payload"
        shutil.copytree(self.data, payload)
        checkpoint = create_checkpoint(
            self.root / "checkpoint", (SnapshotRoot(self.data, payload),), boundary_sha256="b" * 64
        )
        identity = {
            "directory": str(checkpoint.directory),
            "sha256": checkpoint.sha256,
            "boundary_sha256": checkpoint.boundary_sha256,
        }
        proof = {"path": str(self.root / "proof.json"), "sha256": "c" * 64}
        return identity, proof, proof, None

    def restore_roots(self, checkpoint):
        self.restores += 1
        restore_checkpoint(
            Checkpoint(
                Path(checkpoint["directory"]), checkpoint["sha256"], checkpoint["boundary_sha256"]
            )
        )

    def switch_pointer(self, release, *, allowed):
        assert self.pointer in {item["build"] for item in allowed}
        self.pointer = release["build"]

    def select(self, release):
        assert self.pointer == release["build"]
        self.selected = release["build"]

    def probe(self, release, operation, proof):
        if release["build"] == 101:
            if (self.data / "value").read_text() == "old":
                (self.data / "value").write_text("migrated")
                (self.data / "candidate-only").write_text("retain in quarantine")
            if self.fail_target:
                raise SupervisorError("candidate health failed or reported the wrong build")
        else:
            assert (self.data / "value").read_text() == "old"
            assert not (self.data / "candidate-only").exists()
            if self.fail_previous:
                raise SupervisorError("previous release did not become healthy")

    def start_service(self):
        self.starts += 1
        if self.start_failure:
            raise SupervisorError("selected runtime failed to start")
        if self.accept_then_die:
            (self.data / "value").write_text("accepted work")
            (self.data / "accepted-new-file").write_text("must survive")
            raise PowerLoss


def _case(root: Path):
    root.mkdir(mode=0o700)
    operations = root / "operations"
    operations.mkdir(mode=0o700)
    releases = root / "releases"
    releases.mkdir(mode=0o700)

    def release(build, commit):
        return {
            "version": 1,
            "release_tag": "v0.3.4",
            "version_string": f"0.3.4+build.{build}.g{commit[:7]}",
            "build": build,
            "commit": commit,
            "manifest_sha256": "f" * 64,
            "release_directory": str(releases / str(build)),
            "supervisor_version": "0.1.1",
        }

    previous, target = release(100, "a" * 40), release(101, "b" * 40)
    runtime = FakeRuntime(root)
    runtime.releases = {100: previous, 101: target}
    store = OperationStore(operations, releases)
    return Coordinator(store, runtime), runtime, previous, target


def test_success_chooses_target_before_admission(tmp_path: Path):
    coordinator, runtime, previous, target = _case(tmp_path / "case")
    result = coordinator.deploy(previous, target)
    assert result["phase"] == "committed"
    assert runtime.selected == 101 and runtime.pointer == 101
    assert runtime.starts == 1 and runtime.restores == 0
    assert coordinator.recover() is None


@pytest.mark.parametrize(
    "boundary",
    [
        "preparing",
        "backup_ready",
        "entering_maintenance",
        "quiescent",
        "checkpoint_ready",
        "activating",
        "pointer_switched",
        "candidate_verified",
        "candidate_chosen",
        "committed",
    ],
)
def test_every_update_journal_boundary_recovers_without_operator_reentry(
    tmp_path: Path, boundary: str
):
    coordinator, runtime, previous, target = _case(tmp_path / "case")

    def power_loss(phase):
        if phase == boundary:
            raise PowerLoss

    coordinator.boundary = power_loss
    with pytest.raises(PowerLoss):
        coordinator.deploy(previous, target)
    coordinator.boundary = lambda _name: None
    stops, starts = runtime.stops, runtime.starts
    result = coordinator.recover(startup=True)
    assert runtime.stops == stops and runtime.starts == starts
    if boundary in {"candidate_chosen", "committed"}:
        assert runtime.selected == 101 and (runtime.data / "value").read_text() == "migrated"
    else:
        assert runtime.selected == 100 and (runtime.data / "value").read_text() == "old"
    assert result is None or result["phase"] in {"aborted", "rolled_back", "committed"}


@pytest.mark.parametrize(
    "boundary",
    [
        "rollback_started",
        "rollback_roots_complete",
        "previous_pointer_restored",
        "previous_verified",
        "previous_chosen",
        "rolled_back",
    ],
)
def test_repeated_interruption_during_forward_migration_rollback(tmp_path: Path, boundary: str):
    coordinator, runtime, previous, target = _case(tmp_path / "case")
    runtime.fail_target = True

    def power_loss(phase):
        if phase == boundary:
            raise PowerLoss

    coordinator.boundary = power_loss
    with pytest.raises(PowerLoss):
        coordinator.deploy(previous, target)
    coordinator.boundary = lambda _name: None
    coordinator.recover(startup=True)
    assert runtime.selected == 100 and (runtime.data / "value").read_text() == "old"
    assert not (runtime.data / "candidate-only").exists()
    assert any(
        path.read_text() == "retain in quarantine" for path in runtime.root.rglob("candidate-only")
    )


def test_failed_health_reports_both_failure_and_successful_rollback(tmp_path: Path):
    coordinator, runtime, previous, target = _case(tmp_path / "case")
    runtime.fail_target = True
    with pytest.raises(SupervisorError, match="rolled_back.*candidate health"):
        coordinator.deploy(previous, target)
    assert runtime.selected == 100 and runtime.restores == 1
    assert runtime.starts == 1


def test_failed_previous_probe_keeps_recovery_pending_then_retries(tmp_path: Path):
    coordinator, runtime, previous, target = _case(tmp_path / "case")
    runtime.fail_target = runtime.fail_previous = True
    with pytest.raises(SupervisorError, match="previous release"):
        coordinator.deploy(previous, target)
    assert coordinator.store.active()["phase"] == "previous_pointer_restored"
    assert runtime.starts == 0
    runtime.fail_previous = False
    assert coordinator.recover(startup=True)["phase"] == "rolled_back"
    assert runtime.restores == 1


def test_work_accepted_before_terminal_receipt_survives_recovery(tmp_path: Path):
    coordinator, runtime, previous, target = _case(tmp_path / "case")
    runtime.accept_then_die = True
    with pytest.raises(PowerLoss):
        coordinator.deploy(previous, target)
    assert coordinator.store.active()["phase"] == "candidate_chosen"
    runtime.accept_then_die = False
    coordinator.recover(startup=True)
    assert (runtime.data / "value").read_text() == "accepted work"
    assert (runtime.data / "accepted-new-file").read_text() == "must survive"
    assert runtime.restores == 0


def test_failed_selected_runtime_does_not_roll_back(tmp_path: Path):
    coordinator, runtime, previous, target = _case(tmp_path / "case")
    runtime.start_failure = True
    with pytest.raises(SupervisorError, match="preserve its data"):
        coordinator.deploy(previous, target)
    assert coordinator.store.active()["phase"] == "candidate_chosen"
    assert runtime.restores == 0
    runtime.start_failure = False
    coordinator.recover(startup=True)
    assert runtime.selected == 101


def test_unknown_journal_fails_closed_without_touching_service(tmp_path: Path):
    coordinator, runtime, previous, target = _case(tmp_path / "case")
    path = coordinator.store.directory / "unknown.json"
    path.write_text(json.dumps({"version": 999}))
    path.chmod(0o600)
    with pytest.raises(SupervisorError):
        coordinator.recover(startup=True)
    assert runtime.starts == runtime.stops == runtime.restores == 0


def test_operation_lock_uses_kernel_ownership(tmp_path: Path):
    coordinator, runtime, previous, target = _case(tmp_path / "case")
    with coordinator.store.locked(), pytest.raises(OperationBusy):
        coordinator.deploy(previous, target)
    assert coordinator.store.active() is None


class RestoreRuntime(FakeRuntime):
    def __init__(self, root, *, fresh=False):
        super().__init__(root)
        self.fresh = fresh
        if fresh:
            (self.data / "value").unlink()
        self.old_probes = 0
        self.archive_proof = {"path": str(self.root / "archive-proof.json"), "sha256": "d" * 64}

    def protected_backup(self):
        assert not self.fresh, "A fresh host has no live application to back up"

    def enter_maintenance(self, operation):
        assert not self.fresh, "A fresh host has no live maintenance endpoint"
        return super().enter_maintenance(operation)

    def prepare(self, operation, capture):
        previous, old_proof, _, _ = super().prepare(operation, capture)
        payload = self.root / "archive-payload"
        payload.mkdir(mode=0o700)
        (payload / "value").write_text("archived")
        (payload / "archive-only").write_text("restored archive")
        candidate = create_checkpoint(
            self.root / "candidate", (SnapshotRoot(self.data, payload),), boundary_sha256="d" * 64
        )
        self.archive_proof = {"path": str(self.root / "archive-proof.json"), "sha256": "d" * 64}
        return (
            previous,
            old_proof,
            self.archive_proof,
            {
                "directory": str(candidate.directory),
                "sha256": candidate.sha256,
                "boundary_sha256": candidate.boundary_sha256,
            },
        )

    def prepare_fresh_restore(self, operation):
        assert not list(self.data.iterdir())
        old, _, target, candidate = self.prepare(operation, {})
        return old, None, target, candidate

    def probe(self, release, operation, proof):
        if proof == self.archive_proof or (
            proof is None and operation["phase"] == "candidate_chosen"
        ):
            assert (self.data / "value").read_text() in {"archived", "accepted work"}
            if self.fail_target:
                raise SupervisorError("restored candidate failed its live proof")
        else:
            self.old_probes += 1
            assert not self.fresh
            assert (self.data / "value").read_text() == "old"
            assert not (self.data / "archive-only").exists()


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize(
    "boundary",
    [
        "preparing",
        "backup_ready",
        "entering_maintenance",
        "quiescent",
        "checkpoint_ready",
        "activating",
        "pointer_switched",
        "candidate_verified",
        "candidate_chosen",
        "committed",
    ],
)
def test_every_restore_journal_boundary_recovers_exact_candidate_or_previous(
    tmp_path, fresh, boundary
):
    coordinator, initial, previous, _ = _case(tmp_path / "case")
    shutil.rmtree(initial.data)
    runtime = RestoreRuntime(initial.root, fresh=fresh)
    runtime.releases = initial.releases
    coordinator.runtime = runtime

    def power_loss(phase):
        if phase == boundary:
            raise PowerLoss

    coordinator.boundary = power_loss
    with pytest.raises(PowerLoss):
        coordinator.deploy(previous, previous, kind="restore", previous_uninitialized=fresh)
    coordinator.boundary = lambda phase: None
    starts, stops = runtime.starts, runtime.stops
    coordinator.recover(startup=True)
    assert (runtime.starts, runtime.stops) == (starts, stops)
    if boundary in {"candidate_chosen", "committed"}:
        assert (runtime.data / "value").read_text() == "archived"
    elif fresh:
        assert not list(runtime.data.iterdir())
        assert runtime.old_probes == 0
    else:
        assert (runtime.data / "value").read_text() == "old"


def test_fresh_restore_failed_candidate_returns_to_uninitialized_stopped_host(tmp_path):
    coordinator, initial, previous, _ = _case(tmp_path / "case")
    shutil.rmtree(initial.data)
    runtime = RestoreRuntime(initial.root, fresh=True)
    runtime.fail_target = True
    runtime.releases = initial.releases
    coordinator.runtime = runtime
    with pytest.raises(SupervisorError, match="rolled_back"):
        coordinator.deploy(previous, previous, kind="restore", previous_uninitialized=True)
    assert runtime.starts == runtime.old_probes == 0
    assert not list(runtime.data.iterdir())
    assert coordinator.store.active() is None
    assert any(
        path.read_text() == "restored archive" for path in runtime.root.rglob("archive-only")
    )


def test_stale_previous_is_refused_before_journal_or_admission(tmp_path, monkeypatch):
    coordinator, runtime, previous, target = _case(tmp_path / "case")
    runtime.selected = runtime.pointer = target["build"]
    monkeypatch.setattr(runtime, "enter_maintenance", lambda _: pytest.fail("admission closed"))
    monkeypatch.setattr(runtime, "protected_backup", lambda: pytest.fail("backup started"))
    with pytest.raises(SupervisorError, match="selected release changed before deployment"):
        coordinator.deploy(previous, target)
    assert not list(coordinator.store.directory.glob("*.json"))
    assert runtime.stops == runtime.starts == 0
