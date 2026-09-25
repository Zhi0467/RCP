from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from rcp_supervisor import driver
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.limits import (
    RETAINED_CHECKPOINTS,
    RETAINED_RELEASES,
)
from rcp_supervisor.operations import OperationStore
from rcp_supervisor.retention import plan_retention, prune_retained, remove_retained_tree

NOW = 1_800_000_000.0


def _release(releases: Path, build: int) -> dict:
    commit = f"{build:040x}"
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


def _record(checkpoints: Path, previous: dict, target: dict, *, phase: str = "committed") -> dict:
    operation_id = str(uuid.uuid4())
    workspace = checkpoints / operation_id
    return {
        "version": 1,
        "operation_id": operation_id,
        "kind": "update",
        "phase": phase,
        "previous": previous,
        "target": target,
        "nonce": "0" * 64,
        "checkpoint": {
            "directory": str(workspace / "checkpoint"),
            "sha256": "a" * 64,
            "boundary_sha256": "b" * 64,
        },
        "candidate_checkpoint": None,
        "previous_uninitialized": False,
        "previous_proof": {"path": str(workspace / "previous.json"), "sha256": "c" * 64},
        "target_proof": {"path": str(workspace / "target.json"), "sha256": "d" * 64},
        "error": None,
    }


def _roots(tmp_path: Path, builds: range) -> tuple[Path, Path]:
    checkpoints = tmp_path / "update-checkpoints"
    releases = tmp_path / "releases"
    checkpoints.mkdir(mode=0o700)
    releases.mkdir(mode=0o700)
    for build in builds:
        (releases / str(build)).mkdir(mode=0o700)
        (releases / str(build) / "installed.json").write_text("{}")
    return checkpoints, releases


def _workspace(checkpoints: Path, operation_id: str, *, age: float = 0.0) -> Path:
    workspace = checkpoints / operation_id
    workspace.mkdir(mode=0o700)
    (workspace / "checkpoint").mkdir(mode=0o700)
    (workspace / "checkpoint" / "payload").write_bytes(b"x" * 16)
    os.utime(workspace, (NOW - age, NOW - age))
    return workspace


def test_plan_removes_every_finished_checkpoint_and_keeps_the_newest_releases(tmp_path):
    checkpoints, releases = _roots(tmp_path, range(100, 105))
    chain = [
        _record(checkpoints, _release(releases, build), _release(releases, build + 1))
        for build in range(100, 104)
    ]
    for record in chain:
        _workspace(checkpoints, record["operation_id"])
    records = [(record, float(index)) for index, record in enumerate(chain)]
    # Two newer journals aborted after preparing a candidate but before any
    # checkpoint was recorded; terminal and recorded, they are reclaimed too.
    aborted_ids = set()
    for offset in (1, 2):
        aborted = _record(
            checkpoints, _release(releases, 104), _release(releases, 105), phase="aborted"
        )
        aborted["checkpoint"] = None
        (checkpoints / aborted["operation_id"] / "prepared").mkdir(parents=True, mode=0o700)
        aborted_ids.add(aborted["operation_id"])
        records.append((aborted, float(len(chain) + offset)))

    plan = plan_retention(
        records=records,
        checkpoints_root=checkpoints,
        releases_root=releases,
        current_release_directory=str(releases / "104"),
        selected=_release(releases, 104),
        protected_operation_ids=frozenset(),
    )

    # A committed update never restores old data, so no finished checkpoint
    # serves recovery: every one, and every failed attempt's quarantine, goes.
    assert RETAINED_CHECKPOINTS == 0 and RETAINED_RELEASES == 2
    assert plan.kept_checkpoints == ()
    assert {path.name for path in plan.remove_checkpoints} == {
        record["operation_id"] for record in chain
    } | aborted_ids
    assert plan.kept_releases == ("104", "103")
    assert {path.name for path in plan.remove_releases} == {"100", "101", "102"}
    assert plan.left_alone == ()


@pytest.mark.parametrize("defect", ["pointer", "unfinished"])
def test_plan_refuses_as_a_whole_when_the_machine_state_disagrees(tmp_path, defect):
    checkpoints, releases = _roots(tmp_path, range(100, 103))
    record = _record(
        checkpoints,
        _release(releases, 100),
        _release(releases, 101),
        phase="committed" if defect == "pointer" else "pointer_switched",
    )
    current = "102" if defect == "pointer" else "101"
    with pytest.raises(SupervisorError, match="nothing is pruned"):
        plan_retention(
            records=[(record, 1.0)],
            checkpoints_root=checkpoints,
            releases_root=releases,
            current_release_directory=str(releases / current),
            selected=_release(releases, 101),
            protected_operation_ids=frozenset(),
        )


def test_plan_reclaims_failed_preparations_and_leaves_unrecognized_entries_alone(tmp_path):
    checkpoints, releases = _roots(tmp_path, range(100, 102))
    committed = _record(checkpoints, _release(releases, 100), _release(releases, 101))
    _workspace(checkpoints, committed["operation_id"])
    old_orphan = _workspace(checkpoints, str(uuid.uuid4()), age=60)
    young_orphan = _workspace(checkpoints, str(uuid.uuid4()), age=60)
    adoption = _workspace(checkpoints, str(uuid.uuid4()), age=600)
    (checkpoints / "scratch").mkdir(mode=0o700)
    (checkpoints / "notes.txt").write_text("operator notes")
    (releases / "install.log").write_text("log")
    (releases / "99").symlink_to(releases / "100")
    # Preparation is serialized, so an unsealed build is a failed attempt.
    (releases / "42").mkdir(mode=0o700)

    plan = plan_retention(
        records=[(committed, 1.0)],
        checkpoints_root=checkpoints,
        releases_root=releases,
        current_release_directory=str(releases / "101"),
        selected=_release(releases, 101),
        protected_operation_ids=frozenset({adoption.name}),
    )

    assert set(plan.remove_checkpoints) == {
        old_orphan,
        young_orphan,
        checkpoints / committed["operation_id"],
    }
    assert plan.kept_checkpoints == (adoption.name,)
    assert plan.remove_releases == (releases / "42",)
    reasons = "\n".join(plan.left_alone)
    assert str(checkpoints / "scratch") in reasons and str(checkpoints / "notes.txt") in reasons
    assert str(releases / "install.log") in reasons and str(releases / "99") in reasons


def test_remove_retained_tree_unlocks_read_only_trees_and_never_follows_links(tmp_path):
    root = tmp_path / "releases"
    root.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o700)
    (victim / "keep").write_text("must survive")
    target = root / "101"
    target.mkdir(mode=0o700)
    protected = target / "inputs"
    protected.mkdir(mode=0o700)
    (protected / "data").write_text("reusable input")
    (protected / "data").chmod(0o400)
    protected.chmod(0o500)
    (target / "elsewhere").symlink_to(victim, target_is_directory=True)

    remove_retained_tree(target, root)

    assert not target.exists()
    assert (victim / "keep").read_text() == "must survive"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


@pytest.mark.parametrize("shape", ["link", "nested", "outside"])
def test_remove_retained_tree_refuses_anything_but_an_owned_direct_child(tmp_path, shape):
    root = tmp_path / "releases"
    root.mkdir(mode=0o700)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    (other / "keep").write_text("untouched")
    if shape == "link":
        directory = root / "101"
        directory.symlink_to(other, target_is_directory=True)
    elif shape == "nested":
        (root / "101").mkdir(mode=0o700)
        directory = root / "101" / "assets"
        directory.mkdir(mode=0o700)
    else:
        directory = other

    with pytest.raises(SupervisorError):
        remove_retained_tree(directory, root)
    assert (other / "keep").read_text() == "untouched"
    if shape == "nested":
        assert directory.exists()


def test_filesystem_worker_removes_only_a_well_formed_request(tmp_path, monkeypatch, capsys):
    import io

    from rcp_supervisor import fs_worker

    root = tmp_path / "update-checkpoints"
    root.mkdir(mode=0o700)
    workspace = root / str(uuid.uuid4())
    workspace.mkdir(mode=0o700)
    (workspace / "payload").write_text("x")

    def worker(request: dict) -> tuple[int, str, str]:
        payload = json.dumps(request).encode()
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
        code = fs_worker.main(["remove"])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    code, _out, err = worker({"directory": str(workspace)})
    assert code == 1 and "unsupported" in err
    assert workspace.exists()

    code, out, err = worker({"directory": str(workspace), "root": str(root)})
    assert code == 0, err
    assert json.loads(out) == {"version": 1, "status": "removed"}
    assert not workspace.exists()


def _fake_runtime(tmp_path: Path, releases: Path, checkpoints: Path, *, live: int, current: int):
    supervisor = tmp_path / "supervisor"
    supervisor.mkdir(mode=0o700)
    return SimpleNamespace(
        paths=SimpleNamespace(
            checkpoints_root=checkpoints,
            releases_root=releases,
            supervisor=supervisor,
            current=tmp_path / "current",
        ),
        deployment_lock=nullcontext,
        selected_release=lambda: _release(releases, live),
        current_release_directory=lambda: str(releases / str(current)),
        remove_retained=remove_retained_tree,
        prune_backup_captures=lambda: None,
    )


def test_prune_retained_removes_through_the_runtime_from_the_journal(tmp_path):
    checkpoints, releases = _roots(tmp_path, range(100, 104))
    operations = tmp_path / "operations"
    operations.mkdir(mode=0o700)
    store = OperationStore(operations, releases)
    chain = [
        _record(checkpoints, _release(releases, build), _release(releases, build + 1))
        for build in range(100, 103)
    ]
    for index, record in enumerate(chain):
        _workspace(checkpoints, record["operation_id"])
        store.write(record)
        journal = operations / f"{record['operation_id']}.json"
        os.utime(journal, (NOW + index, NOW + index))
    runtime = _fake_runtime(tmp_path, releases, checkpoints, live=103, current=103)

    plan = prune_retained(runtime, store)

    assert not any((checkpoints / record["operation_id"]).exists() for record in chain)
    assert not (releases / "100").exists() and not (releases / "101").exists()
    assert all((releases / str(build)).is_dir() for build in (102, 103))
    names = {field["name"]: field["value"] for field in plan.fields()}
    assert names["removed_checkpoints"] == 3 and names["removed_releases"] == 2
    assert names["kept_releases"] == "103, 102"
    assert store.active() is None and len(store.records()) == 3


def test_a_refused_prune_is_reported_and_does_not_fail_the_committed_update(tmp_path):
    checkpoints, releases = _roots(tmp_path, range(100, 103))
    operations = tmp_path / "operations"
    operations.mkdir(mode=0o700)
    store = OperationStore(operations, releases)
    store.write(_record(checkpoints, _release(releases, 100), _release(releases, 101)))
    runtime = _fake_runtime(tmp_path, releases, checkpoints, live=101, current=102)

    fields = driver._retention_after_commit(runtime, store)

    assert [field["name"] for field in fields] == ["retention"]
    assert fields[0]["value"].startswith("not pruned: The installed release pointer")
    assert (releases / "100").is_dir()


def test_consumed_snapshot_releases_slot_and_pruning_follows_filesystem_catalog(tmp_path):
    from rcp_supervisor.checkpoint import create_stopped_snapshot, restore_checkpoint
    from rcp_supervisor.retention import _owns_checkpoint

    root = tmp_path / "checkpoints"
    root.mkdir()
    identity = str(uuid.uuid4())
    operation = root / identity
    operation.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    (live / "retained").mkdir(mode=0o700)
    saved = create_stopped_snapshot(operation / "checkpoint", (live,), boundary_sha256="b" * 64)
    catalog = json.loads((saved.directory / "checkpoint.json").read_text())
    record = {"operation_id": identity, "checkpoint": {"directory": str(saved.directory)}}
    assert _owns_checkpoint(record, root)
    restore_checkpoint(saved)
    assert not _owns_checkpoint(record, root)
    for workspace in catalog["workspaces"]:
        quarantine = next(Path(workspace).glob("quarantine-*"))
        (quarantine / "retained").chmod(0)
    remove_retained_tree(operation, root)
    assert not operation.exists()
    assert all(not Path(workspace).exists() for workspace in catalog["workspaces"])
    assert (live / "retained").is_dir()


def _metadata_fixture(tmp_path):
    checkpoints, releases = _roots(tmp_path, range(100, 103))
    operations = tmp_path / "operations"
    operations.mkdir(mode=0o700)
    store = OperationStore(operations, releases)
    runtime = _fake_runtime(tmp_path, releases, checkpoints, live=102, current=102)
    return runtime.paths, store


def test_success_bounds_journals_and_removes_interrupted_writes(tmp_path):
    from rcp_supervisor.limits import RETAINED_OPERATION_JOURNALS
    from rcp_supervisor.retention import prune_supervisor_metadata

    paths, store = _metadata_fixture(tmp_path)
    records = []
    for index in range(RETAINED_OPERATION_JOURNALS + 3):
        record = _record(
            paths.checkpoints_root,
            _release(paths.releases_root, 101),
            _release(paths.releases_root, 102),
        )
        store.write(record)
        os.utime(store.directory / f"{record['operation_id']}.json", (NOW + index, NOW + index))
        records.append(record)
    temporary = store.directory / f"{uuid.uuid4()}.tmp"
    temporary.write_text("interrupted")
    temporary.chmod(0o600)
    prune_supervisor_metadata(paths, store)
    assert {record["operation_id"] for record, _ in store.records()} == {
        record["operation_id"] for record in records[-RETAINED_OPERATION_JOURNALS:]
    }
    assert not temporary.exists()


def test_diagnostics_have_a_count_bound_and_are_cleared_after_success(tmp_path):
    from rcp_supervisor.limits import RETAINED_SUPERVISOR_LOGS
    from rcp_supervisor.retention import prune_logs, prune_supervisor_metadata

    paths, store = _metadata_fixture(tmp_path)
    logs = paths.supervisor / "logs"
    logs.mkdir(mode=0o700)
    for index in range(RETAINED_SUPERVISOR_LOGS + 3):
        path = logs / f"error-{index}.log"
        path.write_text("failure")
        os.utime(path, (NOW + index, NOW + index))
    untouched = logs / "operator-notes"
    untouched.write_text("keep")
    prune_logs(logs)
    assert len(list(logs.glob("*.log"))) == RETAINED_SUPERVISOR_LOGS
    assert not (logs / "error-0.log").exists()
    prune_supervisor_metadata(paths, store)
    assert list(logs.iterdir()) == [untouched]


def test_receipts_are_bounded_by_retained_release_trees(tmp_path):
    from rcp_supervisor.retention import prune_supervisor_metadata

    paths, store = _metadata_fixture(tmp_path)
    receipts = paths.supervisor / "release-receipts"
    receipts.mkdir()
    for build in range(95, 104):
        (receipts / f"{build}.json").write_text("{}")
    prune_supervisor_metadata(paths, store)
    assert {path.stem for path in receipts.iterdir()} == {"100", "101", "102"}


def test_success_clears_download_bundles_locks_and_interrupted_staging(tmp_path):
    from rcp_supervisor.retention import prune_supervisor_metadata

    paths, store = _metadata_fixture(tmp_path)
    bundles = paths.supervisor / "bundles"
    bundles.mkdir()
    for _ in range(3):
        identity = str(uuid.uuid4())
        (bundles / identity).mkdir()
        (bundles / f".{identity}.fetch-interrupted").mkdir()
        (bundles / f".{identity}.fetch.lock").touch()
    untouched = bundles / "operator-files"
    untouched.mkdir()
    prune_supervisor_metadata(paths, store)
    assert list(bundles.iterdir()) == [untouched]


@pytest.mark.parametrize("storage", ["operator", "versions"])
def test_root_environments_keep_newest_and_executing_runtime(tmp_path, monkeypatch, storage):
    from rcp_supervisor.limits import RETAINED_ROOT_ENVIRONMENTS
    from rcp_supervisor.retention import prune_supervisor_metadata

    paths, store = _metadata_fixture(tmp_path)
    root = paths.supervisor / storage
    root.mkdir()
    children = []
    for index in range(RETAINED_ROOT_ENVIRONMENTS + 3):
        child = root / (str(index) if storage == "operator" else f"0.1.{index}")
        (child / ".venv/bin").mkdir(parents=True)
        (child / ".venv/bin/python").touch()
        (child / "installed.json").write_text("{}")
        os.utime(child, (NOW + index, NOW + index))
        children.append(child)
    monkeypatch.setattr(sys, "executable", str(children[0] / ".venv/bin/python"))
    failed = root / ("99" if storage == "operator" else "0.2.0")
    failed.mkdir()
    prune_supervisor_metadata(paths, store)
    assert set(root.iterdir()) == {children[0], *children[-RETAINED_ROOT_ENVIRONMENTS:]}
    assert not failed.exists()


def test_root_cache_and_unreferenced_python_are_reclaimed(tmp_path):
    from rcp_supervisor.retention import prune_supervisor_metadata

    paths, store = _metadata_fixture(tmp_path)
    python = paths.supervisor / "python"
    for name in ("cpython-live", "cpython-old"):
        (python / name / "bin").mkdir(parents=True)
        (python / name / "bin/python").touch()
    (python / ".temp/interrupted-download").mkdir(parents=True)
    environment = paths.supervisor / "operator/102"
    (environment / ".venv/bin").mkdir(parents=True)
    (environment / "installed.json").write_text("{}")
    (environment / ".venv/bin/python").symlink_to(python / "cpython-live/bin/python")
    cache = paths.supervisor / "cache"
    (cache / "download").mkdir(parents=True)
    (cache / "download/wheel").write_text("cached")
    prune_supervisor_metadata(paths, store)
    assert {path.name for path in python.iterdir()} == {"cpython-live"}
    assert list(cache.iterdir()) == []


def test_preparation_lock_excludes_pruning_but_allows_startup_operation_lock(tmp_path):
    from rcp_supervisor.operations import OperationBusy

    paths, store = _metadata_fixture(tmp_path)
    other = OperationStore(store.directory, paths.releases_root)
    with store.preparing():
        with other.locked():
            assert other.active() is None
        with pytest.raises(OperationBusy), other.preparing():
            pytest.fail("simultaneous preparations must refuse")


def test_success_clears_backup_capture_stages_without_touching_agent_scratch(tmp_path):
    from rcp_supervisor.retention import prune_backup_captures

    stages = tmp_path / "run-stage"
    stages.mkdir()
    task = stages / str(uuid.uuid4())
    task.mkdir()
    (task / "work").write_text("agent scratch")
    for _ in range(3):
        backup = stages / f"backup-{uuid.uuid4()}"
        backup.mkdir(mode=0o700)
        (backup / "receipt.json").write_text("retained failure")
    prune_backup_captures(tmp_path)
    assert list(stages.iterdir()) == [task]
    assert (task / "work").read_text() == "agent scratch"


def test_backup_capture_cleanup_refuses_links(tmp_path):
    from rcp_supervisor.retention import prune_backup_captures

    stages = tmp_path / "run-stage"
    stages.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (stages / f"backup-{uuid.uuid4()}").symlink_to(victim)
    with pytest.raises(SupervisorError, match="unsafe"):
        prune_backup_captures(tmp_path)
    assert victim.is_dir()


def test_success_removes_only_recognized_atomic_metadata_staging(tmp_path):
    from rcp_supervisor.retention import prune_supervisor_metadata

    paths, store = _metadata_fixture(tmp_path)
    temporary = paths.supervisor / ".selected.json.abc123xy"
    temporary.write_text("interrupted")
    temporary.chmod(0o600)
    pointer = paths.current.parent / f".current-{uuid.uuid4().hex}"
    pointer.symlink_to(paths.releases_root / "101")
    untouched = paths.supervisor / ".selected.json.operator-notes"
    untouched.write_text("retain")
    prune_supervisor_metadata(paths, store)
    assert not temporary.exists() and not pointer.is_symlink()
    assert untouched.read_text() == "retain"


def test_checkpoint_records_external_workspace_before_creating_it(tmp_path, monkeypatch):
    from rcp_supervisor import checkpoint

    live = tmp_path / "live"
    live.mkdir()
    (live / "data").write_text("preserve")
    destination = tmp_path / "checkpoint"

    def interrupt(path, document):
        assert document["workspaces"]
        assert not any(Path(name).exists() for name in document["workspaces"])
        raise OSError("interrupted publication")

    monkeypatch.setattr(checkpoint, "_write_json", interrupt)
    with pytest.raises(OSError, match="interrupted publication"):
        checkpoint.create_stopped_snapshot(destination, (live,), boundary_sha256="b" * 64)
    assert not list(tmp_path.rglob(".rcp-checkpoint-*"))


def test_runtime_logs_bound_failure_bytes_and_count(tmp_path, monkeypatch):
    from rcp_supervisor import runtime as runtime_module
    from rcp_supervisor.limits import RETAINED_SUPERVISOR_LOGS

    runtime = runtime_module.SystemRuntime.__new__(runtime_module.SystemRuntime)
    runtime.paths = SimpleNamespace(supervisor=tmp_path)
    directory = tmp_path / "logs"
    directory.mkdir(mode=0o700)
    original_lstat = Path.lstat

    def root_log_directory(path):
        info = original_lstat(path)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=0) if path == directory else info

    monkeypatch.setattr(Path, "lstat", root_log_directory)
    monkeypatch.setattr(runtime_module, "MAX_APP_OUTPUT_BYTES", 1024)
    for index in range(RETAINED_SUPERVISOR_LOGS):
        path = directory / f"error-old-{index}.log"
        path.write_text("old")
        os.utime(path, (0, 0))
    with pytest.raises(RuntimeError, match="failed"), runtime._log("error") as (output, name):
        output.write(b"x" * 4096)
        raise RuntimeError("failed")
    assert Path(name).stat().st_size == 1024
    assert len(list(directory.iterdir())) == RETAINED_SUPERVISOR_LOGS


def test_self_update_keeps_runtime_resolved_before_current_pointer_changes(tmp_path, monkeypatch):
    from rcp_supervisor import retention

    paths, store = _metadata_fixture(tmp_path)
    versions = paths.supervisor / "versions"
    children = []
    for index in range(4):
        child = versions / f"0.1.{index}"
        child.mkdir(parents=True)
        (child / "installed.json").write_text("{}")
        os.utime(child, (NOW + index, NOW + index))
        children.append(child)
    current = paths.supervisor / "current"
    current.symlink_to(children[0])
    # Import took place via the old pointer; sys.executable may resolve directly
    # to a shared managed Python and cannot identify this package environment.
    monkeypatch.setattr(
        retention, "_RUNNING_MODULE", (current / "rcp_supervisor/retention.py").resolve()
    )
    current.unlink()
    current.symlink_to(children[-1])
    retention.prune_supervisor_metadata(paths, store)
    assert set(versions.iterdir()) == {children[0], children[-2], children[-1]}
