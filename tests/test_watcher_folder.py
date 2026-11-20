from __future__ import annotations

import json
from collections import Counter

import pytest

from rcp.storage import AppStore
from rcp.watcher_folder import reconcile_watcher_files, watcher_files
from rcp.watchers import WatcherBinding, WatcherCheckResult
from tests.test_watchers import _chat_turn


def _file(purpose, *commands):
    return json.dumps(
        {
            "purpose": purpose,
            "checks": [
                {"check_command": command, "log_path": "/tmp/job.log", "cwd": "/tmp"}
                for command in commands
            ],
        }
    )


def test_folder_changes_commit_together_and_keep_observation_history(tmp_path):
    store = AppStore(tmp_path / "state.db")
    _chat_turn(store, "turn")
    run = WatcherBinding(
        project_id="project", agent_id="chat", origin_turn_id="turn", machine_id="backend"
    )
    calls = []

    def observe(spec, host, timeout):
        calls.append(spec.check_command)
        return WatcherCheckResult(state="active", checked_at=store.now())

    before = watcher_files(store, run)
    assert before == {}
    initial = {
        "training.json": _file("First jobs", "job 1", "job 2"),
        "scratch.json": _file("Scratch job", "job 3"),
    }
    reconcile_watcher_files(store, run, before, initial, check_runner=observe)
    staged = watcher_files(store, run)
    assert set(staged) == set(initial)
    first = store.watcher("training")
    assert first is not None
    first_ids = [check.check_id for check in first.live_checks]
    _chat_turn(store, "second-turn")
    run = run.model_copy(update={"origin_turn_id": "second-turn"})
    edited = {"training.json": _file("Remaining jobs", "job 2", "job 4")}
    changed = reconcile_watcher_files(store, run, staged, edited, check_runner=observe)
    assert [(item.watcher_id, item.purpose) for item in changed] == [("training", "Remaining jobs")]
    assert reconcile_watcher_files(store, run, staged, edited, check_runner=observe) == changed
    record = store.watcher("training")
    assert record is not None and record.purpose == "Remaining jobs"
    assert record.origin_turn_id == "turn"
    checks = {check.check_command: check for check in record.checks}
    assert checks["job 1"].removed_at
    assert checks["job 2"].check_id in first_ids
    assert checks["job 4"].removed_at is None
    assert Counter(calls) == Counter(["job 1", "job 2", "job 3", "job 4"])
    cancelled = store.watcher("scratch")
    assert cancelled and cancelled.cancelled_by == "agent"


def test_stale_folder_edit_applies_no_other_change(tmp_path):
    store = AppStore(tmp_path / "state.db")
    _chat_turn(store, "turn")
    run = WatcherBinding(
        project_id="project", agent_id="chat", origin_turn_id="turn", machine_id="backend"
    )

    def observe(spec, host, timeout):
        return WatcherCheckResult(state="active", checked_at=store.now())

    initial = {"old.json": _file("One", "job 1")}
    reconcile_watcher_files(store, run, {}, initial, check_runner=observe)
    before = watcher_files(store, run)
    store.cancel_watchers(["old"], by="rcp", reason="Episode stopped.")
    with pytest.raises(ValueError, match="live"):
        reconcile_watcher_files(
            store,
            run.model_copy(update={"origin_turn_id": "turn2"}),
            before,
            {"new.json": _file("New", "job 3"), "old.json": _file("Retitled", "job 1")},
            check_runner=observe,
        )
    assert store.watcher("new") is None


@pytest.mark.parametrize("remote_host", [False, True], ids=["local", "shipped-python"])
def test_staging_and_readback_replace_stale_files_and_never_follow_links(
    tmp_path, remote_host, monkeypatch
):
    import subprocess
    from pathlib import PurePosixPath

    from rcp.transport.run_stage import RemoteRunStage
    from rcp.watcher_folder import read_watcher_folder, stage_watcher_folder

    store = AppStore(tmp_path / "state.db")
    _chat_turn(store, "turn")
    owner = WatcherBinding(
        project_id="project", agent_id="chat", origin_turn_id="turn", machine_id="backend"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    remote = None
    if remote_host:
        remote = RemoteRunStage("test")
        remote.root = PurePosixPath(tmp_path)
        monkeypatch.setattr(
            remote,
            "_ssh_bytes",
            lambda argv, *, input_data=None: subprocess.run(
                argv, input=input_data, capture_output=True, check=False
            ),
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    retained = outside / "protected.json"
    retained.write_text("outside")
    (workspace / "watchers").symlink_to(outside, target_is_directory=True)
    assert stage_watcher_folder(store, owner, workspace, remote) == {}
    assert retained.read_text() == "outside"
    folder = workspace / "watchers"
    assert not folder.is_symlink()
    (folder / "one.json").write_text("{}")
    assert read_watcher_folder(workspace, remote) == {"one.json": "{}"}
    (folder / "linked.json").symlink_to(retained)
    with pytest.raises(ValueError, match="nonregular|symlink"):
        read_watcher_folder(workspace, remote)
    stage_watcher_folder(store, owner, workspace, remote, attempt=1)
    assert read_watcher_folder(workspace, remote) == {}
    assert retained.read_text() == "outside"
