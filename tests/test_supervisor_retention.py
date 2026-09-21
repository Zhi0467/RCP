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
    RETENTION_ORPHAN_MIN_AGE_SECONDS,
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


def test_plan_keeps_the_newest_checkpoints_and_every_release_they_can_reach(tmp_path):
    checkpoints, releases = _roots(tmp_path, range(100, 105))
    chain = [
        _record(checkpoints, _release(releases, build), _release(releases, build + 1))
        for build in range(100, 104)
    ]
    for record in chain:
        _workspace(checkpoints, record["operation_id"])
    records = [(record, float(index)) for index, record in enumerate(chain)]
    # Two newer journals aborted after preparing a candidate but before any
    # checkpoint was recorded: their workspaces hold no rollback artifact and
    # must not take the retained slots; terminal and recorded, they are reclaimed.
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
        now=NOW,
    )

    assert RETAINED_CHECKPOINTS == 2 and RETAINED_RELEASES == 2
    newest = [record["operation_id"] for record in chain[-2:]]
    assert sorted(plan.kept_checkpoints) == sorted(newest)
    assert {path.name for path in plan.remove_checkpoints} == {
        record["operation_id"] for record in chain[:2]
    } | aborted_ids
    # 104 is live, 103 is its rollback target, 102 is where the older kept
    # checkpoint would roll back to. 100 and 101 are unreachable.
    assert plan.kept_releases == ("104", "103", "102")
    assert {path.name for path in plan.remove_releases} == {"100", "101"}
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
            now=NOW,
        )


def test_plan_reclaims_old_orphans_and_leaves_young_or_unknown_entries_alone(tmp_path):
    checkpoints, releases = _roots(tmp_path, range(100, 102))
    committed = _record(checkpoints, _release(releases, 100), _release(releases, 101))
    _workspace(checkpoints, committed["operation_id"])
    old_orphan = _workspace(
        checkpoints, str(uuid.uuid4()), age=RETENTION_ORPHAN_MIN_AGE_SECONDS + 60
    )
    young_orphan = _workspace(checkpoints, str(uuid.uuid4()), age=60)
    adoption = _workspace(checkpoints, str(uuid.uuid4()), age=10 * RETENTION_ORPHAN_MIN_AGE_SECONDS)
    (checkpoints / "scratch").mkdir(mode=0o700)
    (checkpoints / "notes.txt").write_text("operator notes")
    (releases / "install.log").write_text("log")
    (releases / "99").symlink_to(releases / "100")

    plan = plan_retention(
        records=[(committed, 1.0)],
        checkpoints_root=checkpoints,
        releases_root=releases,
        current_release_directory=str(releases / "101"),
        selected=_release(releases, 101),
        protected_operation_ids=frozenset({adoption.name}),
        now=NOW,
    )

    assert plan.remove_checkpoints == (old_orphan,)
    assert sorted(plan.kept_checkpoints) == sorted([committed["operation_id"], adoption.name])
    assert plan.remove_releases == ()
    reasons = "\n".join(plan.left_alone)
    assert str(young_orphan) in reasons and "age floor" in reasons
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

    assert not (checkpoints / chain[0]["operation_id"]).exists()
    assert all((checkpoints / record["operation_id"]).is_dir() for record in chain[1:])
    assert not (releases / "100").exists()
    assert all((releases / str(build)).is_dir() for build in (101, 102, 103))
    names = {field["name"]: field["value"] for field in plan.fields()}
    assert names["removed_checkpoints"] == 1 and names["removed_releases"] == 1
    assert names["kept_releases"] == "103, 102, 101"
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
