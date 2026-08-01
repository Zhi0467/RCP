from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import pytest

from rcp.config import MachineConfig, RepositoryConfig, load_manifest
from rcp.core.models import Patch
from rcp.history import HistoryManager, PatchRejected
from rcp.paper import PaperService
from rcp.storage import AppStore
from rcp.transport import (
    BatchPublishFailed,
    LocalStateWorkspace,
    RemoteRunStage,
    SSHStateWorkspace,
    StateUnavailable,
    StateWorkspace,
    repository_access,
)

from .helpers import seed_patch


class RecordingWorkspace(StateWorkspace):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "test-host:/canonical/.research")
        self.remote = True
        self.transactions = 0
        self.refreshes = 0
        self.published: list[list[str]] = []
        self.committed_batches: list[str] = []
        self.committed_patches: list[str] = []

    @contextmanager
    def transaction(self):
        self.transactions += 1
        with super().transaction():
            yield

    def refresh(self):
        self.refreshes += 1
        return super().refresh()

    def publish(self, relative_paths):
        self.published.append([str(Path(item)) for item in relative_paths])

    def publish_committed_batch(self, relative_paths, batch_directory):
        self.committed_batches.append(str(Path(batch_directory)))
        self.publish(relative_paths)

    def publish_committed_patch(self, relative_paths, patch_path):
        self.committed_patches.append(str(Path(patch_path)))
        self.publish(relative_paths)


def _accept_question_patch() -> Patch:
    return Patch(
        kind="approval",
        author="human",
        summary="Accept the research question.",
        ops=[
            {
                "op": "set_standing",
                "node_id": "rq/learning-after-shift",
                "standing": "accepted",
            }
        ],
    )


def test_history_and_paper_publish_only_explicit_canonical_files(manifest, tmp_path) -> None:
    workspace = RecordingWorkspace(manifest.research_dir)
    history = HistoryManager(manifest, workspace)
    history.initialize()
    history.append(seed_patch())
    paper = PaperService(manifest, AppStore(tmp_path / "app.sqlite3"), workspace)
    created = paper.create()

    published = {path for batch in workspace.published for path in batch}
    assert "patches/000001.json" in published
    assert workspace.committed_patches == ["patches/000001.json"]
    assert "graph.json" in published
    assert "research.md" in published
    assert "paper/introduction.md" not in published
    assert created.sync_state == "unsynced"
    assert workspace.transactions == 2

    saved = paper.save(created.content, created.base_hash)
    published = {path for batch in workspace.published for path in batch}
    assert "paper/introduction.md" in published
    assert saved.sync_state == "synced"
    assert workspace.transactions == 3


def test_coherent_remote_initialization_reuses_refreshed_snapshot_without_publish(
    manifest,
) -> None:
    HistoryManager(manifest).initialize()
    workspace = RecordingWorkspace(manifest.research_dir)
    workspace.refresh()

    result = HistoryManager(load_manifest(manifest.path), workspace).initialize()

    assert result.state.revision == 0
    assert workspace.refreshes == 1
    assert workspace.transactions == 0
    assert workspace.published == []


def test_remote_initialization_repairs_and_publishes_mismatched_outputs(manifest) -> None:
    HistoryManager(manifest).initialize()
    (manifest.research_dir / "graph.json").write_text("{}\n", encoding="utf-8")
    workspace = RecordingWorkspace(manifest.research_dir)
    workspace.refresh()

    result = HistoryManager(load_manifest(manifest.path), workspace).initialize()

    assert result.state.revision == 0
    assert workspace.refreshes == 1
    assert workspace.transactions == 1
    assert workspace.published == [
        [
            "graph.json",
            "glossary.json",
            "proposals.json",
            "coverage.json",
            "cursors.json",
            "scope-base.json",
            "research.md",
            "manifest.toml",
        ]
    ]
    graph = json.loads((manifest.research_dir / "graph.json").read_text(encoding="utf-8"))
    assert graph["project_truth_scope"] == ["repo-a", "repo-b"]


def test_shared_snapshot_lock_keeps_refresh_behind_single_patch_publication(
    manifest, monkeypatch
) -> None:
    HistoryManager(manifest).append(seed_patch())
    root = manifest.research_dir
    writer_workspace = StateWorkspace(root, "test-host:/canonical/.research")
    reader_workspace = StateWorkspace(root, "test-host:/canonical/.research")
    writer_workspace.remote = True
    reader_workspace.remote = True
    assert writer_workspace.snapshot_lock is reader_workspace.snapshot_lock

    remote_patches = {"patches/000001.json"}
    publish_entered = threading.Barrier(2)
    publish_release = threading.Barrier(2)
    refresh_probed = threading.Barrier(2)
    refresh_continue = threading.Barrier(2)
    refresh_invoked = threading.Barrier(2)
    reader_lock_probe: list[bool] = []

    def publish_patch(_relative_paths, patch_path) -> None:
        publish_entered.wait(timeout=5)
        publish_release.wait(timeout=5)
        remote_patches.add(Path(patch_path).as_posix())

    def mirror_remote_patches() -> bool:
        for patch_path in (root / "patches").glob("[0-9][0-9][0-9][0-9][0-9][0-9].json"):
            if patch_path.relative_to(root).as_posix() not in remote_patches:
                patch_path.unlink()
        return True

    original_refresh = reader_workspace.refresh

    def refresh_like_chat_read() -> bool:
        acquired = reader_workspace.snapshot_lock.acquire(blocking=False)
        reader_lock_probe.append(acquired)
        if acquired:
            reader_workspace.snapshot_lock.release()
        refresh_probed.wait(timeout=5)
        refresh_continue.wait(timeout=5)
        refresh_invoked.wait(timeout=5)
        return original_refresh()

    monkeypatch.setattr(writer_workspace, "publish_committed_patch", publish_patch)
    monkeypatch.setattr(reader_workspace, "_refresh_snapshot", mirror_remote_patches)
    monkeypatch.setattr(reader_workspace, "refresh", refresh_like_chat_read)
    writer = HistoryManager(load_manifest(manifest.path), writer_workspace)
    reader = HistoryManager(load_manifest(manifest.path), reader_workspace)

    with ThreadPoolExecutor(max_workers=2) as pool:
        append_future = pool.submit(
            writer.append,
            _accept_question_patch(),
            expected_revision=1,
        )
        publish_entered.wait(timeout=5)
        refresh_future = pool.submit(reader_workspace.refresh)
        refresh_probed.wait(timeout=5)
        refresh_continue.wait(timeout=5)
        refresh_invoked.wait(timeout=5)
        publish_release.wait(timeout=5)

        appended, _ = append_future.result(timeout=5)
        assert refresh_future.result(timeout=5) is True

    assert reader_lock_probe == [False]
    assert appended.revision == 2
    assert (root / "patches" / "000002.json").is_file()
    assert reader.materialize(write_outputs=False).state.revision == 2


def test_shared_snapshot_lock_keeps_batch_append_out_of_refresh_and_replay(
    manifest, monkeypatch
) -> None:
    HistoryManager(manifest).append(seed_patch())
    root = manifest.research_dir
    reader_workspace = StateWorkspace(root, "test-host:/canonical/.research")
    writer_workspace = StateWorkspace(root, "test-host:/canonical/.research")
    reader_workspace.remote = True
    writer_workspace.remote = True
    reader = HistoryManager(load_manifest(manifest.path), reader_workspace)
    writer = HistoryManager(load_manifest(manifest.path), writer_workspace)

    refresh_entered = threading.Barrier(2)
    refresh_release = threading.Barrier(2)
    writer_probed = threading.Barrier(2)
    writer_continue = threading.Barrier(2)
    replay_entered = threading.Barrier(2)
    replay_release = threading.Barrier(2)
    writer_lock_probe: list[bool] = []
    published_batches: list[str] = []

    def paused_refresh() -> bool:
        refresh_entered.wait(timeout=5)
        refresh_release.wait(timeout=5)
        return True

    real_replay = reader._replay

    def paused_replay(pending_patch_paths=None):
        replay_entered.wait(timeout=5)
        replay_release.wait(timeout=5)
        return real_replay(pending_patch_paths)

    def append_after_probe():
        acquired = writer_workspace.snapshot_lock.acquire(blocking=False)
        writer_lock_probe.append(acquired)
        if acquired:
            writer_workspace.snapshot_lock.release()
        writer_probed.wait(timeout=5)
        writer_continue.wait(timeout=5)
        return writer.append_batch([_accept_question_patch()], expected_revision=1)

    def record_batch_publish(_relative_paths, batch_directory) -> None:
        published_batches.append(Path(batch_directory).as_posix())

    monkeypatch.setattr(reader_workspace, "_refresh_snapshot", paused_refresh)
    monkeypatch.setattr(reader, "_replay", paused_replay)
    monkeypatch.setattr(writer_workspace, "publish_committed_batch", record_batch_publish)

    with ThreadPoolExecutor(max_workers=2) as pool:
        read_future = pool.submit(reader.current_materialization)
        refresh_entered.wait(timeout=5)
        append_future = pool.submit(append_after_probe)
        writer_probed.wait(timeout=5)
        writer_continue.wait(timeout=5)
        refresh_release.wait(timeout=5)
        replay_entered.wait(timeout=5)
        batch_existed_during_replay = any((root / "patches").glob("batch-*"))
        replay_release.wait(timeout=5)

        read_result = read_future.result(timeout=5)
        prepared, appended_result = append_future.result(timeout=5)

    assert writer_lock_probe == [False]
    assert batch_existed_during_replay is False
    assert read_result.state.revision == 1
    assert [patch.revision for patch in prepared] == [2]
    assert appended_result.state.revision == 2
    assert len(published_batches) == 1
    assert published_batches[0].startswith("patches/batch-000002-000002-")


def test_second_graph_run_is_refused(tmp_path) -> None:
    workspace = LocalStateWorkspace(tmp_path / ".research", str(tmp_path))

    with (
        workspace.run_lock(),
        pytest.raises(StateUnavailable, match="already in progress"),
        workspace.run_lock(),
    ):
        pass


def test_remote_refresh_and_transaction_use_one_canonical_lock_and_sync(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / ".research"
    root.mkdir()
    (root / "graph.json").write_text("{}\n", encoding="utf-8")
    workspace = SSHStateWorkspace(root, "research.example", "/srv/project")
    ssh_calls: list[list[str]] = []
    rsync_calls: list[list[str]] = []

    def fake_ssh(arguments):
        ssh_calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def fake_run(arguments, **_kwargs):
        rsync_calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(workspace, "_ssh", fake_ssh)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert workspace.refresh() is True
    assert ssh_calls == [
        ["test", "-f", "/srv/project/.research/manifest.toml"],
        ["mkdir", "/srv/project/.research/.refresh.lock"],
        ["rmdir", "/srv/project/.research/.refresh.lock"],
    ]
    assert len([call for call in rsync_calls if "--delete" in call]) == 1

    ssh_calls.clear()
    rsync_calls.clear()
    with workspace.transaction():
        workspace.publish(["graph.json"])

    assert ssh_calls.count(["mkdir", "/srv/project/.research/.refresh.lock"]) == 1
    assert ssh_calls.count(["test", "-f", "/srv/project/.research/manifest.toml"]) == 1
    assert ssh_calls[-1] == ["rmdir", "/srv/project/.research/.refresh.lock"]
    assert len([call for call in rsync_calls if "--delete" in call]) == 1
    assert len([call for call in rsync_calls if "-aR" in call]) == 1


def test_remote_batch_publication_stages_then_commits_directory_last(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / ".research"
    batch = Path("patches/batch-000002-000003-test")
    for relative, content in (
        (batch / "000002.json", "{}"),
        (batch / "000003.json", "{}"),
        (Path("graph.json"), "{}"),
        (Path("research.md"), "accepted"),
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    workspace = SSHStateWorkspace(root, "research.example", "/srv/project")
    ssh_calls = []
    rsync_calls = []

    def fake_ssh(arguments):
        ssh_calls.append(arguments)
        return subprocess.CompletedProcess([], 0, "", "")

    def fake_run(arguments, **kwargs):
        rsync_calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(workspace, "_ssh", fake_ssh)
    monkeypatch.setattr(subprocess, "run", fake_run)

    workspace.publish_committed_batch(
        [batch / "000002.json", batch / "000003.json", "graph.json", "research.md"],
        batch,
    )

    assert len(rsync_calls) == 1
    assert ".publish/batch-000002-000003-test" in rsync_calls[0][0][-1]
    assert ssh_calls[-1][0:2] == ["python3", "-c"]
    assert ssh_calls[-1][5] == batch.as_posix()
    apply_script = ssh_calls[-1][2]
    assert apply_script.index("os.replace(commit_source, commit_target)") < apply_script.index(
        "os.replace(source, target)"
    )
    assert "if source.is_file()" in apply_script
    assert workspace.reachable is True


def test_remote_patch_publication_commits_file_before_derived_outputs(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / ".research"
    patch = Path("patches/000001.json")
    for relative in (patch, Path("graph.json")):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
    workspace = SSHStateWorkspace(root, "research.example", "/srv/project")
    ssh_calls: list[list[str]] = []
    rsync_calls: list[tuple[list[str], dict]] = []

    def fake_ssh(arguments):
        ssh_calls.append(arguments)
        return subprocess.CompletedProcess([], 0, "", "")

    def fake_run(arguments, **kwargs):
        rsync_calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(workspace, "_ssh", fake_ssh)
    monkeypatch.setattr(subprocess, "run", fake_run)

    workspace.publish_committed_patch([patch, "graph.json"], patch)

    assert len(rsync_calls) == 1
    assert ".publish/patch-000001.json" in rsync_calls[0][0][-1]
    apply = ssh_calls[-1]
    assert apply[0:2] == ["python3", "-c"]
    assert apply[5] == patch.as_posix()
    assert apply[7] == "file"
    assert apply[2].index("os.replace(commit_source, commit_target)") < apply[2].index(
        "os.replace(source, target)"
    )


def test_remote_patch_publish_probes_commit_and_repairs_idempotently(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / ".research"
    patch = Path("patches/000001.json")
    for relative in (patch, Path("graph.json")):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
    workspace = SSHStateWorkspace(root, "research.example", "/srv/project")
    apply_attempts = 0
    calls: list[list[str]] = []

    def fake_ssh(arguments):
        nonlocal apply_attempts
        calls.append(arguments)
        if arguments[0:2] == ["python3", "-c"]:
            apply_attempts += 1
            return subprocess.CompletedProcess(
                [],
                1 if apply_attempts == 1 else 0,
                "",
                "derived output failed",
            )
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(workspace, "_ssh", fake_ssh)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(arguments, 0, "", ""),
    )

    workspace.publish_committed_patch([patch, "graph.json"], patch)

    assert apply_attempts == 2
    assert ["test", "-f", "/srv/project/.research/patches/000001.json"] in calls
    assert workspace.reachable is True


def test_remote_patch_publish_reports_unknown_when_commit_probe_fails(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / ".research"
    patch = Path("patches/000001.json")
    for relative in (patch, Path("graph.json")):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
    workspace = SSHStateWorkspace(root, "research.example", "/srv/project")

    def fake_ssh(arguments):
        if arguments[0:2] == ["python3", "-c"]:
            return subprocess.CompletedProcess([], 1, "", "apply disconnected")
        if arguments[0:2] == ["test", "-f"]:
            return subprocess.CompletedProcess([], 255, "", "probe disconnected")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(workspace, "_ssh", fake_ssh)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(arguments, 0, "", ""),
    )

    with pytest.raises(BatchPublishFailed) as caught:
        workspace.publish_committed_patch([patch, "graph.json"], patch)

    assert caught.value.commit_status == "unknown"
    assert "probe disconnected" in str(caught.value)


def test_failed_remote_single_patch_before_commit_rolls_back_local_mirror(manifest) -> None:
    workspace = RecordingWorkspace(manifest.research_dir)
    history = HistoryManager(manifest, workspace)
    history.append(seed_patch())
    graph_before = (manifest.research_dir / "graph.json").read_bytes()

    def fail_before_commit(_relative_paths, _patch_path):
        raise BatchPublishFailed("remote staging failed", commit_status="absent")

    workspace.publish_committed_patch = fail_before_commit

    with pytest.raises(BatchPublishFailed, match="remote staging failed"):
        history.append(_accept_question_patch(), expected_revision=1)

    assert [patch.revision for patch in history.load_patches()] == [1]
    assert (manifest.research_dir / "graph.json").read_bytes() == graph_before
    assert not (manifest.research_dir / "patches" / "000002.json").exists()
    assert not list((manifest.research_dir / "patches").glob(".unconfirmed-000002.json-*"))
    assert workspace.materialization_repair_required is False


def test_confirmed_remote_single_patch_is_success_and_schedules_output_repair(manifest) -> None:
    workspace = RecordingWorkspace(manifest.research_dir)
    history = HistoryManager(manifest, workspace)
    history.append(seed_patch())

    def fail_after_commit(_relative_paths, _patch_path):
        raise BatchPublishFailed("derived output repair failed", commit_status="present")

    workspace.publish_committed_patch = fail_after_commit

    appended, result = history.append(_accept_question_patch(), expected_revision=1)

    assert appended.revision == 2
    assert result.state.nodes["rq/learning-after-shift"].standing == "accepted"
    assert [patch.revision for patch in history.load_patches()] == [1, 2]
    assert workspace.materialization_repair_required is True


def test_unknown_remote_single_patch_is_quarantined_from_local_replay(manifest) -> None:
    workspace = RecordingWorkspace(manifest.research_dir)
    history = HistoryManager(manifest, workspace)
    history.append(seed_patch())

    def lose_commit_probe(_relative_paths, _patch_path):
        raise BatchPublishFailed("commit probe failed", commit_status="unknown")

    workspace.publish_committed_patch = lose_commit_probe

    with pytest.raises(BatchPublishFailed, match="commit probe failed"):
        history.append(_accept_question_patch(), expected_revision=1)

    assert [patch.revision for patch in history.load_patches()] == [1]
    assert not (manifest.research_dir / "patches" / "000002.json").exists()
    assert list((manifest.research_dir / "patches").glob(".unconfirmed-000002.json-*"))
    assert history.materialize(write_outputs=False).state.revision == 1
    assert workspace.materialization_repair_required is True


def test_rejected_remote_single_patch_remains_in_committed_history(manifest) -> None:
    workspace = RecordingWorkspace(manifest.research_dir)
    history = HistoryManager(manifest, workspace)
    history.append(seed_patch())
    rejected = Patch(
        kind="refresh",
        author="agent",
        summary="Invalid gated transition.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "hyp/replanning-restores-plasticity",
                        "changes": {"status": "supported"},
                    }
                ],
            }
        ],
    )

    with pytest.raises(PatchRejected):
        history.append(rejected)

    assert [patch.revision for patch in history.load_patches()] == [1, 2]
    assert workspace.committed_patches[-1] == "patches/000002.json"


def test_failed_remote_batch_publish_rolls_the_local_mirror_back(manifest) -> None:
    workspace = RecordingWorkspace(manifest.research_dir)
    history = HistoryManager(manifest, workspace)
    history.append(seed_patch())
    graph_before = (manifest.research_dir / "graph.json").read_bytes()

    def fail_publish(_relative_paths, _batch_directory):
        raise StateUnavailable("remote commit failed")

    workspace.publish_committed_batch = fail_publish

    with pytest.raises(StateUnavailable, match="remote commit failed"):
        history.append_batch(
            [
                Patch(
                    kind="approval",
                    author="human",
                    summary="Accept the research question.",
                    ops=[
                        {
                            "op": "set_standing",
                            "node_id": "rq/learning-after-shift",
                            "standing": "accepted",
                        }
                    ],
                )
            ],
            expected_revision=1,
        )

    assert [patch.revision for patch in history.load_patches()] == [1]
    assert history.state().revision == 1
    assert (manifest.research_dir / "graph.json").read_bytes() == graph_before
    assert not list((manifest.research_dir / "patches").glob("batch-*"))
    assert list((manifest.research_dir / "patches").glob(".unconfirmed-batch-*"))


def test_confirmed_remote_batch_commit_is_not_rolled_back(manifest) -> None:
    workspace = RecordingWorkspace(manifest.research_dir)
    history = HistoryManager(manifest, workspace)
    history.append(seed_patch())

    def fail_after_commit(_relative_paths, _batch_directory):
        raise BatchPublishFailed("derived output repair failed", commit_status="present")

    workspace.publish_committed_batch = fail_after_commit

    history.append_batch(
        [
            Patch(
                kind="approval",
                author="human",
                summary="Accept the research question.",
                ops=[
                    {
                        "op": "set_standing",
                        "node_id": "rq/learning-after-shift",
                        "standing": "accepted",
                    }
                ],
            )
        ],
        expected_revision=1,
    )

    assert [patch.revision for patch in history.load_patches()] == [1, 2]
    assert workspace.materialization_repair_required is True

    publish = workspace.publish

    def fail_repair(_paths):
        raise StateUnavailable("repair is still blocked")

    workspace.publish = fail_repair
    with pytest.raises(StateUnavailable, match="repair is still blocked"):
        history.state()
    assert workspace.materialization_repair_required is True

    workspace.publish = publish
    assert history.state().nodes["rq/learning-after-shift"].standing == "accepted"
    assert workspace.materialization_repair_required is False


def test_remote_batch_retries_remaining_outputs_after_commit_point(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / ".research"
    batch = Path("patches/batch-000001-000001-test")
    for relative in (batch / "000001.json", Path("graph.json")):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
    workspace = SSHStateWorkspace(root, "research.example", "/srv/project")
    apply_attempts = 0

    def fake_ssh(arguments):
        nonlocal apply_attempts
        if arguments[0:2] == ["python3", "-c"]:
            apply_attempts += 1
            return subprocess.CompletedProcess(
                [],
                1 if apply_attempts == 1 else 0,
                "",
                "partial apply",
            )
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(workspace, "_ssh", fake_ssh)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(arguments, 0, "", ""),
    )

    workspace.publish_committed_batch([batch / "000001.json", "graph.json"], batch)

    assert apply_attempts == 2
    assert workspace.reachable is True


def test_local_repository_pointer_has_no_host() -> None:
    access = repository_access(
        RepositoryConfig(alias="rcp", machine="laptop", path="/Users/me/research/RCP"),
        MachineConfig(alias="laptop"),
    )

    assert access.model_dump() == {
        "alias": "rcp",
        "machine": "laptop",
        "host": "",
        "path": "/Users/me/research/RCP",
    }


def test_remote_repository_pointer_keeps_host_and_untouched_path() -> None:
    access = repository_access(
        RepositoryConfig(alias="cot-steering", machine="remote-1", path="/home/research/cot-loop"),
        MachineConfig(alias="remote-1", host="research.example"),
    )

    assert access.host == "research.example"
    assert access.path == "/home/research/cot-loop"


def test_remote_run_inputs_are_published_as_one_bundle(tmp_path, monkeypatch) -> None:
    source_file = tmp_path / "schema.json"
    source_file.write_text('{"type":"object"}\n', encoding="utf-8")
    source_directory = tmp_path / "conversations"
    source_directory.mkdir()
    (source_directory / "session.jsonl").write_text("{}\n", encoding="utf-8")
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath("/tmp/rcp-run.test")
    ssh_calls: list[list[str]] = []
    rsync_calls: list[list[str]] = []

    def fake_ssh(arguments):
        ssh_calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def fake_run(arguments, **_kwargs):
        rsync_calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(stage, "_ssh", fake_ssh)
    monkeypatch.setattr(subprocess, "run", fake_run)

    schema_path = stage.put_file(source_file, "schema.json")
    conversations_path = stage.put_directory(source_directory, "conversations")
    pending = stage._pending_inputs

    assert schema_path == "/tmp/rcp-run.test/inputs/schema.json"
    assert conversations_path == "/tmp/rcp-run.test/inputs/conversations"
    assert rsync_calls == []
    assert ssh_calls == []

    stage.finalize_inputs()

    assert len(rsync_calls) == 1
    assert rsync_calls[0][0:2] == ["rsync", "-a"]
    assert len(ssh_calls) == 1
    assert ssh_calls[0][0:2] == ["python3", "-c"]
    assert json.loads(ssh_calls[0][5]) == ["conversations", "schema.json"]
    assert ssh_calls[0][6] == "1"
    assert stage._pending_inputs is None
    assert pending is not None
    assert not pending.exists()


def test_remote_stage_failed_finalize_cleans_local_pending_inputs(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "contract.md"
    source.write_text("Run the task.\n", encoding="utf-8")
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath("/tmp/rcp-run.test")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments, 23, "", "connection lost"
        ),
    )
    monkeypatch.setattr(
        stage,
        "_ssh",
        lambda arguments: subprocess.CompletedProcess(arguments, 44, "", "incomplete"),
    )
    stage.put_file(source, "contract.md")
    pending = stage._pending_inputs

    with pytest.raises(StateUnavailable, match="connection lost"):
        stage.finalize_inputs()

    assert stage._pending_inputs is None
    assert pending is not None
    assert not pending.exists()


def test_remote_stage_lists_workspace_files(monkeypatch) -> None:
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath("/tmp/rcp-run.test")
    workspace = str(stage.workspace)

    def fake_ssh(arguments):
        assert arguments[0] == "find"
        listing = f"{workspace}/patch.json\n{workspace}/notes.md\n{workspace}/nested/deep.json\n"
        return subprocess.CompletedProcess([], 0, listing, "")

    monkeypatch.setattr(stage, "_ssh", fake_ssh)

    assert stage.list_workspace_files() == ["notes.md", "patch.json"]


def test_remote_stage_workspace_operations_fail_closed(monkeypatch) -> None:
    """An unreachable workspace is not an empty one, and a failed delete is not a delete."""

    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath("/tmp/rcp-run.test")
    monkeypatch.setattr(
        stage,
        "_ssh",
        lambda _arguments: subprocess.CompletedProcess([], 255, "", "ssh: connect timed out"),
    )

    with pytest.raises(StateUnavailable):
        stage.list_workspace_files()
    with pytest.raises(StateUnavailable):
        stage.remove_workspace_file("patch.json")


def test_remote_stage_projects_conversations_by_copying_on_execution_host(monkeypatch) -> None:
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath("/tmp/rcp-run.test")
    calls: list[list[str]] = []

    def fake_ssh(arguments):
        calls.append(arguments)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(stage, "_ssh", fake_ssh)

    projected = stage.replace_conversation_inputs(
        [
            (
                "/home/research/provider-a/a.jsonl",
                "provider-a/repo-a/machine-a/session-a.jsonl",
            ),
            (
                "/home/research/provider-b/b.jsonl",
                "provider-b/repo-a/machine-a/session-b.jsonl",
            ),
        ]
    )

    assert len(calls) == 1
    assert calls[0][:2] == ["python3", "-c"]
    assert calls[0][3] == "/tmp/rcp-run.test/inputs/conversations"
    assert json.loads(calls[0][4]) == [
        [
            "/home/research/provider-a/a.jsonl",
            "provider-a/repo-a/machine-a/session-a.jsonl",
        ],
        [
            "/home/research/provider-b/b.jsonl",
            "provider-b/repo-a/machine-a/session-b.jsonl",
        ],
    ]
    assert projected == [
        "/tmp/rcp-run.test/inputs/conversations/provider-a/repo-a/machine-a/session-a.jsonl",
        "/tmp/rcp-run.test/inputs/conversations/provider-b/repo-a/machine-a/session-b.jsonl",
    ]


def test_remote_stage_conversation_projection_fails_closed(monkeypatch) -> None:
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath("/tmp/rcp-run.test")
    monkeypatch.setattr(
        stage,
        "_ssh",
        lambda _arguments: subprocess.CompletedProcess([], 1, "", "source disappeared"),
    )

    with pytest.raises(StateUnavailable, match="source disappeared"):
        stage.replace_conversation_inputs(
            [
                (
                    "/home/research/missing.jsonl",
                    "provider-a/repo-a/machine-a/missing.jsonl",
                )
            ]
        )
    with pytest.raises(StateUnavailable, match="saved conversation projection"):
        stage.require_conversation_inputs()


def test_remote_stage_removes_read_only_conversation_projection_only(monkeypatch) -> None:
    root = Path(tempfile.mkdtemp(prefix="rcp-run.", dir="/tmp"))
    workspace = root / "workspace"
    projection = root / "inputs" / "conversations"
    workspace.mkdir()
    projection.mkdir(parents=True)
    copied = projection / "conversation-0000.jsonl"
    copied.write_text("large transcript", encoding="utf-8")
    copied.chmod(0o400)
    projection.chmod(0o500)
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    monkeypatch.setattr(
        stage,
        "_ssh",
        lambda arguments: subprocess.run(
            arguments, capture_output=True, text=True, check=False
        ),
    )

    stage.remove_conversation_inputs()

    assert not projection.exists()
    assert workspace.is_dir()
    assert stage.root == PurePosixPath(str(root))
    assert stage.close() is True


def test_remote_stage_close_removes_read_only_trees_and_verifies_absence(monkeypatch) -> None:
    root = Path(tempfile.mkdtemp(prefix="rcp-run.", dir="/tmp"))
    projection = root / "inputs" / "conversations"
    projection.mkdir(parents=True)
    copied = projection / "conversation-0000.jsonl"
    copied.write_text("large transcript", encoding="utf-8")
    copied.chmod(0o400)
    projection.chmod(0o500)
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    monkeypatch.setattr(
        stage,
        "_ssh",
        lambda arguments: subprocess.run(
            arguments, capture_output=True, text=True, check=False
        ),
    )

    assert stage.close() is True
    assert not root.exists()
    assert stage.root is None


def test_remote_stage_close_keeps_root_when_deletion_failed(monkeypatch) -> None:
    root = Path(tempfile.mkdtemp(prefix="rcp-run.", dir="/tmp"))
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    monkeypatch.setattr(
        stage,
        "_ssh",
        lambda _arguments: subprocess.CompletedProcess([], 1, "", "still present"),
    )

    try:
        assert stage.close() is False
        assert root.exists()
        assert stage.root == PurePosixPath(str(root))
    finally:
        shutil.rmtree(root)


def test_remote_stage_sweeper_uses_read_only_tree_cleanup(monkeypatch) -> None:
    stage = RemoteRunStage("research.example")
    calls: list[list[str]] = []

    def fake_ssh(arguments):
        calls.append(arguments)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(stage, "_ssh", fake_ssh)

    stage.sweep(retain_days=7)

    assert calls[0][:2] == ["python3", "-c"]
    assert "make_writable" in calls[0][2]
    assert "remove_tree(target)" in calls[0][2]


def test_remote_stage_artifact_operations_are_exact_and_binary(monkeypatch) -> None:
    root = Path(tempfile.mkdtemp(prefix="rcp-run.", dir="/tmp"))
    (root / "workspace").mkdir()
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    monkeypatch.setattr(
        stage,
        "_ssh",
        lambda arguments: subprocess.run(
            arguments, capture_output=True, text=True, check=False
        ),
    )
    monkeypatch.setattr(
        stage,
        "_ssh_bytes",
        lambda arguments: subprocess.run(arguments, capture_output=True, check=False),
    )
    try:
        directory = Path(
            str(stage.prepare_artifact_directory("logical-turn", reuse=False))
        )
        payload = b"\x89PNG\r\n\x1a\n\x00\xffbinary"
        (directory / "plot.png").write_bytes(payload)
        (directory / "linked.png").symlink_to(directory / "plot.png")
        (directory / "nested").mkdir()
        (directory / "nested" / "hidden.png").write_bytes(payload)

        assert stage.list_artifact_files("logical-turn") == [("plot.png", len(payload))]
        assert (
            stage.read_artifact_bytes("logical-turn", "plot.png", max_bytes=1024)
            == payload
        )
        with pytest.raises(ValueError, match="plain base name"):
            stage.read_artifact_bytes("logical-turn", "../plot.png", max_bytes=1024)
    finally:
        shutil.rmtree(root)


def test_remote_stage_resume_rejects_symlinked_artifact_scope(monkeypatch) -> None:
    root = Path(tempfile.mkdtemp(prefix="rcp-run.", dir="/tmp"))
    workspace = root / "workspace"
    turns = workspace / "turns"
    outside = root / "outside"
    turns.mkdir(parents=True)
    (outside / "artifacts").mkdir(parents=True)
    (turns / "logical-turn").symlink_to(outside, target_is_directory=True)
    stage = RemoteRunStage("research.example")
    stage.root = PurePosixPath(str(root))
    monkeypatch.setattr(
        stage,
        "_ssh",
        lambda arguments: subprocess.run(
            arguments, capture_output=True, text=True, check=False
        ),
    )
    try:
        with pytest.raises(StateUnavailable, match="saved artifact directory"):
            stage.prepare_artifact_directory("logical-turn", reuse=True)
    finally:
        shutil.rmtree(root)
