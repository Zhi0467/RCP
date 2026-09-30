from __future__ import annotations

import errno
import hashlib
import io
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import date
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

import pytest
from PIL import Image

import rcp.artifact_replace as artifact_replace_module
from rcp.agents import AgentProcessControl
from rcp.agents.prompts import PromptFactory
from rcp.artifact_comments import comment_panel
from rcp.artifact_replace import ArtifactReplacementConflict
from rcp.artifact_views import artifact_viewer_document
from rcp.artifacts import (
    AgentArtifactDescriptor,
    classify_artifact_bytes,
    descriptor_for,
    read_local_regular_file,
    replace_local_regular_file,
)
from rcp.background import AgentTaskExecution
from rcp.runs.chat import (
    _discover_chat_artifacts,
    finalize_artifact_edit,
    stage_artifact_context,
)
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord, Artifact
from rcp.transport import LocalStateWorkspace, RemoteRunStage
from rcp.transport.remote_lock_holder import replace_staged_artifact

from .helpers import assert_frozen_backend_ships, create_named_app


def _workspace(tmp_path: Path) -> LocalStateWorkspace:
    research = tmp_path / "repository" / ".research"
    research.mkdir(parents=True)
    return LocalStateWorkspace(research, str(research))


def _artifact_staged_marker_name(name: str, expected: bytes, candidate: bytes) -> str:
    name_hash = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    return (
        f".rcp-artifact-{name_hash}-{hashlib.sha256(expected).hexdigest()}-"
        f"{hashlib.sha256(candidate).hexdigest()}-{'a' * 16}"
    )


def _simulate_remounted_device(monkeypatch) -> None:
    real_fstat = os.fstat
    real_stat = os.stat

    class RemountedStat:
        def __init__(self, original) -> None:
            self._original = original
            self.st_dev = original.st_dev + 1

        def __getattr__(self, name: str):
            return getattr(self._original, name)

    def remounted_fstat(descriptor: int):
        return RemountedStat(real_fstat(descriptor))

    def remounted_stat(*args, **kwargs):
        return RemountedStat(real_stat(*args, **kwargs))

    monkeypatch.setattr(os, "fstat", remounted_fstat)
    monkeypatch.setattr(os, "stat", remounted_stat)


def _stored_artifact(app, scope_id, name, data, *, kept=False):
    store = app.state.background_tasks.store
    descriptor = descriptor_for(
        scope_id, name, media_type=classify_artifact_bytes(name, data), size_bytes=len(data)
    )
    store.create_artifact(
        Artifact(
            artifact_id=descriptor.artifact_id,
            project_id=app.state.default_project_id,
            supplier="turn",
            supplier_id=scope_id,
            source_name=name,
            media_type=descriptor.media_type,
            created_at=store.now(),
            kept_at=store.now() if kept else None,
            origin_operation_id=scope_id,
        ),
        data=data,
    )
    return descriptor.model_copy(update={"kept_at": store.now() if kept else None})


def _publish_artifact(store, artifact_id, data, operation_id="other-edit"):
    return store.publish_artifact_version(
        artifact_id,
        base_version=store.artifact(artifact_id).current_version,
        operation_id=operation_id,
        data=data,
    )


def test_remote_revision_stages_the_stored_copy_without_reading_source_stage(
    manifest,
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    chat_id = "84909c31-6f1c-4ea4-96dd-af901977770e"
    origin_id = "75aa25d5-20ee-48e0-9fe6-baf370e3db27"
    revision_id = "267e12d4-b0cb-4c47-91f0-d04360d5e7b6"
    source_bytes = b"<!doctype html><p>remote source</p>"
    source = descriptor_for(
        origin_id, "remote.html", media_type="text/html", size_bytes=len(source_bytes)
    )
    _stored_artifact(app, origin_id, source.name, source_bytes)
    base_request = RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        chat_scope="project",
        chat_id=chat_id,
        message="Revise it.",
        mode="discuss",
        run_truth_scope=["repo-a"],
    )
    revision_request = RunRequest.model_validate(
        {
            **base_request.model_dump(mode="python"),
            "mode": "work",
            "session_id": "remote-session",
            "artifact_context": {
                "source": "task",
                "operation_id": origin_id,
                "artifact_id": source.artifact_id,
                "selections": [
                    {
                        "kind": "text",
                        "text": "remote source",
                        "comment": "Revise this.",
                    }
                ],
            },
        }
    )
    now = store.now()
    for operation_id, request, stage_root, result in (
        (
            origin_id,
            base_request,
            "/remote/source-stage",
            {"messages": ["Created."], "artifacts": [source.model_dump(mode="json")]},
        ),
        (revision_id, revision_request, "/remote/revision-stage", None),
    ):
        store.create_agent_task(
            AgentTaskRecord(
                operation_id=operation_id,
                project_id=project_id,
                kind="project_chat",
                status="running" if operation_id == revision_id else "succeeded",
                request=request.model_dump(mode="json"),
                result=result,
                created_at=now,
                updated_at=now,
                status_message="Running.",
                native_session_id="remote-session",
                stage_host="research-gpu",
                stage_root=stage_root,
                dispatch_authority=resolve_dispatch_authority("project_chat", request),
            )
        )
        store.record_agent_task_receipt(
            operation_id,
            "operation_created",
            {
                "kind": "project_chat",
                "attempt": 1,
                "has_parent": False,
                "resumed": False,
            },
        )

    class FakeRemoteRunStage:
        def __init__(self, host: str) -> None:
            assert host == "research-gpu"
            self.root = "/remote/source-stage"

        def attach_artifact_source(self, root: str):
            self.root = root
            return self

        def read_artifact_bytes(self, scope_id: str, name: str, *, max_bytes: int) -> bytes:
            assert (self.root, scope_id, name) == (
                "/remote/source-stage",
                origin_id,
                source.name,
            )
            assert len(source_bytes) <= max_bytes
            return source_bytes

        def put_directory(self, _source: Path, label: str, *, reuse: bool) -> str:
            assert reuse is True
            return f"/remote/revision-stage/inputs/{label}"

    monkeypatch.setattr("rcp.runs.chat.RemoteRunStage", FakeRemoteRunStage)
    execution = AgentTaskExecution(
        operation_id=revision_id,
        store=store,
        control=AgentProcessControl(),
    )
    staged = stage_artifact_context(
        app.state.service,
        revision_request,
        execution,
        local_stage=None,
        remote_stage=FakeRemoteRunStage("research-gpu"),
        artifact_path=f"/remote/revision-stage/workspace/turns/{revision_id}/artifacts",
    )

    assert staged is not None
    assert staged.protected_write_paths == ()


def test_svg_is_an_ordinary_bounded_artifact() -> None:
    data = b'<svg xmlns="http://www.w3.org/2000/svg"><text>result</text></svg>'

    assert classify_artifact_bytes("result.svg", data) == "image/svg+xml"
    descriptor = descriptor_for(
        "01234567-89ab-cdef-0123-456789abcdef",
        "result.svg",
        media_type="image/svg+xml",
        size_bytes=len(data),
    )

    assert descriptor.media_type == "image/svg+xml"
    assert descriptor.size_bytes == len(data)


def test_keep_reuses_human_artifacts_directory_and_reads_external_edits(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    artifacts = workspace.root.parent / "artifacts"
    artifacts.mkdir()
    human_file = artifacts / "notes.html"
    human_file.write_text("human", encoding="utf-8")

    kept = workspace.keep_artifact(
        source_name="curves.html",
        project_name="Pilot",
        data=b"<p>first</p>",
        today=date(2026, 8, 27),
    )

    assert human_file.read_text(encoding="utf-8") == "human"
    assert kept == "curves-pilot-26-08-27.html"
    (artifacts / kept).write_bytes(b"<p>external</p>")
    assert workspace.read_kept_artifact(kept) == b"<p>external</p>"

    workspace.replace_kept_artifact(kept, b"<p>agent revision</p>")
    assert workspace.read_kept_artifact(kept) == b"<p>agent revision</p>"


def test_local_artifact_read_preserves_transient_operational_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    target = artifacts / "result.html"
    target.write_bytes(b"<p>result</p>")
    real_open = os.open

    def fail_target_open(path, flags, *args, **kwargs):
        if path == target.name and kwargs.get("dir_fd") is not None:
            raise OSError(errno.EIO, "simulated read failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_target_open)
    with pytest.raises(OSError, match="simulated read failure"):
        read_local_regular_file(artifacts, target.name, max_bytes=1024)


def test_local_artifact_read_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    fifo = artifacts / "result.csv"
    os.mkfifo(fifo)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(read_local_regular_file, artifacts, fifo.name, max_bytes=1024)
        try:
            with pytest.raises(ValueError):
                pending.result(timeout=10)
        finally:
            # Release a reader stuck in open() so a failure cannot hang the suite.
            with suppress(OSError):
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))


def test_temporary_artifact_revision_checks_digest_after_staging(
    tmp_path: Path,
    monkeypatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    target = artifacts / "result.html"
    original = b"<p>original</p>"
    external = b"<p>external edit while staging</p>"
    recovery = tmp_path / "recovery" / "turn-1"
    target.write_bytes(original)
    real_fsync = os.fsync
    staged = False

    def mutate_after_staging(descriptor: int) -> None:
        nonlocal staged
        real_fsync(descriptor)
        if not staged:
            staged = True
            target.write_bytes(external)

    monkeypatch.setattr(os, "fsync", mutate_after_staging)

    replaced = replace_local_regular_file(
        artifacts,
        target.name,
        b"<p>candidate</p>",
        expected_sha256=hashlib.sha256(original).hexdigest(),
        recovery_directory=recovery,
    )

    assert replaced is False
    assert target.read_bytes() == external
    assert sorted(path.name for path in artifacts.iterdir()) == [target.name]


def test_temporary_artifact_revision_preserves_an_edit_at_atomic_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    target = artifacts / "result.html"
    original = b"<p>original</p>"
    external = b"<p>external edit in the former check-replace gap</p>"
    recovery = tmp_path / "recovery" / "turn-1"
    target.write_bytes(original)
    exchange = artifact_replace_module.exchange_regular_files
    injected = False

    def save_then_exchange(
        first_directory_fd: int,
        first: str,
        second_directory_fd: int,
        second: str,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            target.write_bytes(external)
        exchange(first_directory_fd, first, second_directory_fd, second)

    monkeypatch.setattr(artifact_replace_module, "exchange_regular_files", save_then_exchange)

    replaced = replace_local_regular_file(
        artifacts,
        target.name,
        b"<p>candidate</p>",
        expected_sha256=hashlib.sha256(original).hexdigest(),
        recovery_directory=recovery,
    )

    assert replaced is False
    assert target.read_bytes() == external
    assert sorted(path.name for path in artifacts.iterdir()) == [target.name]


@pytest.mark.parametrize(
    "newest",
    [b"<p>newest external edit racing rollback</p>", b"<p>candidate</p>"],
    ids=("different-bytes", "candidate-bytes-new-inode"),
)
def test_temporary_artifact_revision_preserves_a_save_that_races_rollback(
    tmp_path: Path,
    monkeypatch,
    newest: bytes,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    target = artifacts / "result.html"
    original = b"<p>original</p>"
    external = b"<p>external edit before publication</p>"
    recovery = tmp_path / "recovery" / "turn-1"
    target.write_bytes(original)
    exchange = artifact_replace_module.exchange_regular_files
    exchanges = 0

    def save(data: bytes) -> None:
        staged = target.with_name(".external-save")
        staged.write_bytes(data)
        os.replace(staged, target)

    def save_then_exchange(
        first_directory_fd: int,
        first: str,
        second_directory_fd: int,
        second: str,
    ) -> None:
        nonlocal exchanges
        exchanges += 1
        if exchanges == 1:
            save(external)
        elif exchanges == 2:
            save(newest)
        exchange(first_directory_fd, first, second_directory_fd, second)

    monkeypatch.setattr(artifact_replace_module, "exchange_regular_files", save_then_exchange)

    replaced = replace_local_regular_file(
        artifacts,
        target.name,
        b"<p>candidate</p>",
        expected_sha256=hashlib.sha256(original).hexdigest(),
        recovery_directory=recovery,
    )

    assert replaced is False
    assert exchanges == 3
    assert target.read_bytes() == newest
    assert sorted(path.name for path in artifacts.iterdir()) == [target.name]


@pytest.mark.parametrize(
    "newest",
    [b"<p>newest external edit racing rollback</p>", b"<p>candidate</p>"],
    ids=("different-bytes", "candidate-bytes-new-inode"),
)
def test_temporary_artifact_revision_recovers_rollback_after_remount(
    tmp_path: Path,
    monkeypatch,
    newest: bytes,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    target = artifacts / "result.html"
    original = b"<p>original</p>"
    external = b"<p>external edit before publication</p>"
    candidate = b"<p>candidate</p>"
    recovery = tmp_path / "recovery" / "turn-1"
    target.write_bytes(original)
    exchange = artifact_replace_module.exchange_regular_files
    exchanges = 0

    def save(data: bytes) -> None:
        staged = target.with_name(".external-save")
        staged.write_bytes(data)
        os.replace(staged, target)

    def save_exchange_then_crash(
        first_directory_fd: int,
        first: str,
        second_directory_fd: int,
        second: str,
    ) -> None:
        nonlocal exchanges
        exchanges += 1
        if exchanges == 1:
            save(external)
        elif exchanges == 2:
            save(newest)
            exchange(first_directory_fd, first, second_directory_fd, second)
            raise OSError("simulated interruption after racing rollback")
        exchange(first_directory_fd, first, second_directory_fd, second)

    monkeypatch.setattr(
        artifact_replace_module,
        "exchange_regular_files",
        save_exchange_then_crash,
    )

    with pytest.raises(OSError, match="simulated interruption after racing rollback"):
        replace_local_regular_file(
            artifacts,
            target.name,
            candidate,
            expected_sha256=hashlib.sha256(original).hexdigest(),
            recovery_directory=recovery,
        )

    monkeypatch.setattr(artifact_replace_module, "exchange_regular_files", exchange)
    _simulate_remounted_device(monkeypatch)
    replaced = replace_local_regular_file(
        artifacts,
        target.name,
        candidate,
        expected_sha256=hashlib.sha256(external).hexdigest(),
        recovery_directory=recovery,
    )

    assert replaced is False
    assert target.read_bytes() == newest
    assert sorted(path.name for path in artifacts.iterdir()) == [target.name]


def test_temporary_artifact_revision_ignores_an_agent_planted_recovery_marker(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    target = artifacts / "result.html"
    original = b"<p>original</p>"
    candidate = b"<p>accepted candidate</p>"
    hostile = b"<p>unaccepted planted payload</p>"
    target.write_bytes(original)
    name_hash = hashlib.sha256(target.name.encode("utf-8")).hexdigest()[:24]
    hostile_name = (
        f".rcp-artifact-{name_hash}-{'a' * 64}-{hashlib.sha256(original).hexdigest()}-{'b' * 16}"
    )
    (artifacts / hostile_name).write_bytes(hostile)

    replaced = replace_local_regular_file(
        artifacts,
        target.name,
        candidate,
        expected_sha256=hashlib.sha256(original).hexdigest(),
        recovery_directory=tmp_path / "recovery" / "turn-1",
    )

    assert replaced is True
    assert target.read_bytes() == candidate
    assert (artifacts / hostile_name).read_bytes() == hostile


def test_conditional_artifact_revision_refuses_agent_writable_recovery_state(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    target = artifacts / "result.html"
    original = b"<p>original</p>"
    target.write_bytes(original)

    with pytest.raises(ValueError, match="outside agent-writable output"):
        replace_local_regular_file(
            artifacts,
            target.name,
            b"<p>candidate</p>",
            expected_sha256=hashlib.sha256(original).hexdigest(),
            recovery_directory=artifacts,
        )

    assert target.read_bytes() == original


def test_conditional_artifact_revision_discards_a_partial_prepublication_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    target = artifacts / "result.html"
    recovery = tmp_path / "recovery" / "turn-1"
    original = b"<p>original</p>"
    candidate = b"<p>candidate</p>"
    target.write_bytes(original)
    real_write = os.write
    real_unlink = os.unlink
    interrupted = False

    def interrupt_candidate_write(descriptor: int, data) -> int:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            real_write(descriptor, data[:4])
            raise OSError("simulated process death during candidate write")
        return real_write(descriptor, data)

    monkeypatch.setattr(os, "write", interrupt_candidate_write)
    monkeypatch.setattr(os, "unlink", lambda *_args, **_kwargs: None)

    with pytest.raises(OSError, match="process death during candidate write"):
        replace_local_regular_file(
            artifacts,
            target.name,
            candidate,
            expected_sha256=hashlib.sha256(original).hexdigest(),
            recovery_directory=recovery,
        )

    marker = next(recovery.iterdir())
    assert marker.read_bytes() == candidate[:4]
    assert target.read_bytes() == original

    monkeypatch.setattr(os, "write", real_write)
    monkeypatch.setattr(os, "unlink", real_unlink)
    replaced = replace_local_regular_file(
        artifacts,
        target.name,
        candidate,
        expected_sha256=hashlib.sha256(original).hexdigest(),
        recovery_directory=recovery,
    )

    assert replaced is True
    assert target.read_bytes() == candidate
    assert not recovery.exists()


def test_conditional_artifact_revision_refuses_a_nonregular_staged_marker(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    target = artifacts / "result.html"
    recovery = tmp_path / "recovery" / "turn-1"
    original = b"<p>original</p>"
    candidate = b"<p>candidate</p>"
    target.write_bytes(original)
    recovery.mkdir(parents=True, mode=0o700)
    marker = recovery / _artifact_staged_marker_name(target.name, original, candidate)
    marker.mkdir()

    with pytest.raises(ValueError, match="staging marker is not a regular file"):
        replace_local_regular_file(
            artifacts,
            target.name,
            candidate,
            expected_sha256=hashlib.sha256(original).hexdigest(),
            recovery_directory=recovery,
        )

    assert target.read_bytes() == original
    assert marker.is_dir()


def test_temporary_artifact_revision_recovers_a_pending_exchange_after_remount(
    tmp_path: Path,
    monkeypatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    target = artifacts / "result.html"
    original = b"<p>original</p>"
    candidate = b"<p>candidate</p>"
    external = b"<p>external edit preserved across retry</p>"
    recovery = tmp_path / "recovery" / "turn-1"
    target.write_bytes(original)
    exchange = artifact_replace_module.exchange_regular_files
    injected = False

    def save_exchange_then_crash(
        first_directory_fd: int,
        first: str,
        second_directory_fd: int,
        second: str,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            target.write_bytes(external)
            exchange(first_directory_fd, first, second_directory_fd, second)
            raise OSError("simulated interruption after publication")
        exchange(first_directory_fd, first, second_directory_fd, second)

    monkeypatch.setattr(
        artifact_replace_module,
        "exchange_regular_files",
        save_exchange_then_crash,
    )

    with pytest.raises(OSError, match="simulated interruption"):
        replace_local_regular_file(
            artifacts,
            target.name,
            candidate,
            expected_sha256=hashlib.sha256(original).hexdigest(),
            recovery_directory=recovery,
        )

    monkeypatch.setattr(artifact_replace_module, "exchange_regular_files", exchange)
    _simulate_remounted_device(monkeypatch)
    replaced = replace_local_regular_file(
        artifacts,
        target.name,
        candidate,
        expected_sha256=hashlib.sha256(candidate).hexdigest(),
        recovery_directory=recovery,
    )

    assert replaced is False
    assert target.read_bytes() == external
    assert sorted(path.name for path in artifacts.iterdir()) == [target.name]


def test_temporary_artifact_recovery_does_not_resurrect_a_deleted_live_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    target = artifacts / "result.html"
    original = b"<p>original</p>"
    candidate = b"<p>candidate</p>"
    external = b"<p>displaced external edit</p>"
    recovery = tmp_path / "recovery" / "turn-1"
    target.write_bytes(original)
    exchange = artifact_replace_module.exchange_regular_files

    def exchange_then_crash(*args) -> None:
        target.write_bytes(external)
        exchange(*args)
        raise OSError("simulated interruption after publication")

    monkeypatch.setattr(artifact_replace_module, "exchange_regular_files", exchange_then_crash)
    with pytest.raises(OSError, match="simulated interruption"):
        replace_local_regular_file(
            artifacts,
            target.name,
            candidate,
            expected_sha256=hashlib.sha256(original).hexdigest(),
            recovery_directory=recovery,
        )
    target.unlink()
    monkeypatch.setattr(artifact_replace_module, "exchange_regular_files", exchange)

    with pytest.raises(ArtifactReplacementConflict):
        replace_local_regular_file(
            artifacts,
            target.name,
            candidate,
            expected_sha256=hashlib.sha256(original).hexdigest(),
            recovery_directory=recovery,
        )

    assert not target.exists()
    quarantined = list(recovery.glob(".rcp-artifact-quarantine-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == external


def test_kept_artifact_revision_checks_digest_after_staging(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = _workspace(tmp_path)
    artifacts = workspace.root.parent / "artifacts"
    artifacts.mkdir()
    target = artifacts / "result.html"
    original = b"<p>original</p>"
    external = b"<p>external edit while staging</p>"
    target.write_bytes(original)
    real_fsync = os.fsync
    staged = False

    def mutate_after_staging(descriptor: int) -> None:
        nonlocal staged
        real_fsync(descriptor)
        if not staged:
            staged = True
            target.write_bytes(external)

    monkeypatch.setattr(os, "fsync", mutate_after_staging)

    replaced = workspace.replace_kept_artifact(
        target.name,
        b"<p>candidate</p>",
        expected_sha256=hashlib.sha256(original).hexdigest(),
    )

    assert replaced is False
    assert target.read_bytes() == external
    assert sorted(path.name for path in artifacts.iterdir()) == [target.name]


def test_kept_artifact_revision_discards_a_partial_prepublication_write(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    artifacts = workspace.root.parent / "artifacts"
    artifacts.mkdir()
    target = artifacts / "result.html"
    original = b"<p>original</p>"
    candidate = b"<p>candidate</p>"
    target.write_bytes(original)
    recovery = workspace.root / ".publish" / "artifact-replacements"
    recovery.mkdir(parents=True, mode=0o700)
    marker = recovery / _artifact_staged_marker_name(target.name, original, candidate)
    marker.write_bytes(candidate[:4])

    replaced = workspace.replace_kept_artifact(
        target.name,
        candidate,
        expected_sha256=hashlib.sha256(original).hexdigest(),
    )

    assert replaced is True
    assert target.read_bytes() == candidate
    assert not marker.exists()


def test_remote_temporary_revision_checks_digest_after_stream_staging(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "rcp-run-stage"
    scope_id = "turn-1"
    artifacts = root / "workspace" / "turns" / scope_id / "artifacts"
    artifacts.mkdir(parents=True)
    (root / "inputs").mkdir()
    target = artifacts / "result.html"
    original = b"<p>original</p>"
    external = b"<p>external edit while staging</p>"
    candidate = b"x" * (2 * 1024 * 1024)
    target.write_bytes(original)
    stage = RemoteRunStage("example.test")
    stage.root = PurePosixPath(root)

    def run_locally(
        arguments: list[str],
        *,
        input_data: bytes | None = None,
        timeout_seconds: float = 60,
    ) -> subprocess.CompletedProcess[bytes]:
        assert input_data is not None
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        process.stdin.write(input_data[: 1024 * 1024])
        process.stdin.flush()
        target.write_bytes(external)
        process.stdin.write(input_data[1024 * 1024 :])
        process.stdin.close()
        assert process.stdout is not None and process.stderr is not None
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        returncode = process.wait(timeout=timeout_seconds)
        return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)

    monkeypatch.setattr(stage, "_ssh_bytes", run_locally)

    replaced = stage.replace_artifact_bytes(
        scope_id,
        target.name,
        candidate,
        expected_sha256=hashlib.sha256(original).hexdigest(),
    )

    assert replaced is False
    assert target.read_bytes() == external
    assert sorted(path.name for path in artifacts.iterdir()) == [target.name]


def test_remote_kept_revision_checks_digest_after_copy_staging(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repository" / ".research"
    stage = root / ".publish" / "artifact-1-1"
    artifacts = root.parent / "artifacts"
    stage.mkdir(parents=True)
    artifacts.mkdir()
    (stage / "content.bin").write_bytes(b"<p>candidate</p>")
    target = artifacts / "result.html"
    original = b"<p>original</p>"
    external = b"<p>external edit while staging</p>"
    target.write_bytes(original)
    real_fsync = os.fsync
    staged = False

    def mutate_after_staging(descriptor: int) -> None:
        nonlocal staged
        real_fsync(descriptor)
        if not staged:
            staged = True
            target.write_bytes(external)

    monkeypatch.setattr(os, "fsync", mutate_after_staging)

    result = replace_staged_artifact(
        {
            "root": str(root),
            "stage": str(stage),
            "name": target.name,
            "expected_sha256": hashlib.sha256(original).hexdigest(),
        },
        str(root / ".refresh.lock"),
    )

    assert result["ok"] is False
    assert result["conflict"] is True
    assert target.read_bytes() == external
    assert sorted(path.name for path in artifacts.iterdir()) == [target.name]


def test_remote_kept_revision_reports_missing_source_as_conflict(tmp_path: Path) -> None:
    root = tmp_path / "repository" / ".research"
    stage = root / ".publish" / "artifact-1-1"
    artifacts = root.parent / "artifacts"
    stage.mkdir(parents=True)
    artifacts.mkdir()
    (stage / "content.bin").write_bytes(b"<p>candidate</p>")

    result = replace_staged_artifact(
        {
            "root": str(root),
            "stage": str(stage),
            "name": "result.html",
            "expected_sha256": hashlib.sha256(b"<p>original</p>").hexdigest(),
        },
        str(root / ".refresh.lock"),
    )

    assert result["ok"] is False
    assert result["conflict"] is True
    assert not (artifacts / "result.html").exists()


def test_remote_kept_revision_does_not_turn_operational_error_into_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repository" / ".research"
    stage = root / ".publish" / "artifact-1-1"
    artifacts = root.parent / "artifacts"
    stage.mkdir(parents=True)
    artifacts.mkdir()
    candidate = b"<p>candidate</p>"
    original = b"<p>original</p>"
    (stage / "content.bin").write_bytes(candidate)
    (artifacts / "result.html").write_bytes(original)

    def unavailable_exchange(*_args) -> None:
        raise OSError(errno.EIO, "simulated storage failure")

    monkeypatch.setattr(artifact_replace_module, "exchange_regular_files", unavailable_exchange)
    with pytest.raises(OSError, match="simulated storage failure"):
        replace_staged_artifact(
            {
                "root": str(root),
                "stage": str(stage),
                "name": "result.html",
                "expected_sha256": hashlib.sha256(original).hexdigest(),
            },
            str(root / ".refresh.lock"),
        )

    assert (artifacts / "result.html").read_bytes() == original


def test_remote_kept_revision_preserves_an_edit_at_atomic_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repository" / ".research"
    stage = root / ".publish" / "artifact-1-1"
    artifacts = root.parent / "artifacts"
    stage.mkdir(parents=True)
    artifacts.mkdir()
    (stage / "content.bin").write_bytes(b"<p>candidate</p>")
    target = artifacts / "result.html"
    original = b"<p>original</p>"
    external = b"<p>remote external edit in the former gap</p>"
    target.write_bytes(original)
    exchange = artifact_replace_module.exchange_regular_files
    injected = False

    def save_then_exchange(
        first_directory_fd: int,
        first: str,
        second_directory_fd: int,
        second: str,
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            target.write_bytes(external)
        exchange(first_directory_fd, first, second_directory_fd, second)

    monkeypatch.setattr(
        artifact_replace_module,
        "exchange_regular_files",
        save_then_exchange,
    )

    result = replace_staged_artifact(
        {
            "root": str(root),
            "stage": str(stage),
            "name": target.name,
            "expected_sha256": hashlib.sha256(original).hexdigest(),
        },
        str(root / ".refresh.lock"),
    )

    assert result["ok"] is False
    assert result["conflict"] is True
    assert target.read_bytes() == external
    assert sorted(path.name for path in artifacts.iterdir()) == [target.name]


def test_keep_refuses_unsafe_artifacts_entry(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    target = workspace.root.parent / "elsewhere"
    target.mkdir()
    (workspace.root.parent / "artifacts").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="artifacts path is not a regular directory"):
        workspace.keep_artifact(
            source_name="curves.html",
            project_name="Pilot",
            data=b"<p>first</p>",
        )


def test_viewer_sends_comments_through_stored_artifact_route() -> None:
    descriptor = AgentArtifactDescriptor(
        artifact_id="0123456789abcdef01234567",
        name="curves.html",
        media_type="text/html",
        size_bytes=128,
    )

    document, csp = artifact_viewer_document(
        content_url="/preview",
        keep_url="/keep",
        state="temporary",
        panel=comment_panel(
            {
                "projectId": "project",
                "chatId": "chat",
                "operationId": "operation",
                "source": "task",
                "artifactId": "0123456789abcdef01234567",
                "branchId": "branch/id",
            }
        ),
        descriptor=descriptor,
    )

    assert "rcp-artifact-context" not in document
    assert "BroadcastChannel" not in document
    assert "rcp-artifact-edit-started" in document
    assert "/api/projects/project/artifacts/0123456789abcdef01234567/comments" in document
    assert "frame-ancestors 'self'" in csp
    assert 'id="state"' not in document
    assert "mode" not in document
    assert "fetch(config.keepUrl" in document
    assert "right - left < 4" in document
    assert 'id="pending"' in document
    assert "installSelectionConfirmation" in document
    assert 'frame.addEventListener("load", enableSelection)' in document
    assert 'type: "rcp-artifact-selection-enable"' in document
    assert "installArtifactSelection(boxLayer, " in document
    assert 'id="box"' not in document
    assert ">Comment</button>" in document
    assert "if(raw.kind==='text'&&typeof raw.text==='string') appendSelection" not in document
    assert "connect-src 'self'" in csp
    assert "img-src 'self' data: blob:" in csp


def test_readonly_artifact_viewer_does_not_enable_selection() -> None:
    document, _csp = artifact_viewer_document(
        content_url="/preview",
        keep_url=None,
        state="temporary",
        descriptor=AgentArtifactDescriptor(
            artifact_id="0123456789abcdef01234567",
            name="curves.html",
            media_type="text/html",
            size_bytes=128,
        ),
    )

    assert 'id="preview"' in document
    assert 'id="pending"' not in document
    assert "rcp-artifact-selection-enable" not in document


@pytest.mark.parametrize("chat_id", ["chat", None])
@pytest.mark.parametrize("suffix", [".html", ".png"])
def test_viewer_filename_cannot_add_preview_attributes(chat_id: str | None, suffix: str) -> None:
    name = f'x" onload="alert(1)" onerror="alert(1){suffix}'
    document, _csp = artifact_viewer_document(
        content_url="/preview",
        keep_url="/keep",
        state="temporary",
        panel=comment_panel(
            {
                "projectId": "project",
                "chatId": chat_id,
                "operationId": "operation",
                "source": "task",
                "artifactId": "0123456789abcdef01234567",
            }
        )
        if chat_id
        else None,
        descriptor=descriptor_for(
            "scope", name, media_type="text/html" if suffix == ".html" else "image/png"
        ),
    )

    class Preview(HTMLParser):
        attrs: dict[str, str | None] = {}

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag in {"iframe", "img"}:
                self.attrs = dict(attrs)

    parser = Preview()
    parser.feed(document)
    label = "title" if suffix == ".html" else "alt"
    assert set(parser.attrs) == {"id", "src", label} | ({"sandbox"} if suffix == ".html" else set())
    assert parser.attrs[label] == name


def test_episode_report_shell_has_no_repository_save_action() -> None:
    descriptor = AgentArtifactDescriptor(
        artifact_id="0123456789abcdef01234567",
        name="episode-report.html",
        media_type="text/html",
        size_bytes=128,
    )

    document, csp = artifact_viewer_document(
        content_url="/preview",
        keep_url=None,
        state="report",
        descriptor=descriptor,
    )

    assert 'id="pending"' not in document
    assert "rcp-artifact-context" not in document
    assert "rcp-artifact-selection-enable" not in document
    assert ">Comment</button>" not in document
    assert 'id="save"' not in document
    assert 'id="keep"' not in document
    assert 'id="state"' not in document
    assert "connect-src 'self'" not in csp

    with_chat, _csp = artifact_viewer_document(
        content_url="/preview",
        keep_url=None,
        state="report",
        panel=comment_panel(
            {
                "projectId": "project",
                "chatId": "chat",
                "operationId": "operation",
                "source": "task",
                "artifactId": "0123456789abcdef01234567",
                "branchId": "branch/id",
            }
        ),
        descriptor=descriptor,
    )
    assert 'id="pending"' in with_chat
    assert 'id="message"' in with_chat
    assert 'id="save"' not in with_chat


def test_box_selection_must_stay_inside_its_normalized_viewport() -> None:
    with pytest.raises(ValueError, match="must stay inside its viewport"):
        RunRequest.model_validate(
            {
                "artifact_context": {
                    "operation_id": "origin",
                    "artifact_id": "0123456789abcdef01234567",
                    "selections": [
                        {
                            "kind": "box",
                            "rect": {"x": 0.75, "y": 0, "width": 0.5, "height": 0.5},
                            "viewport": {"width": 800, "height": 600},
                        }
                    ],
                }
            }
        )


# A box from the viewer before elements were named measured the viewer area, not the
# image, so it is described but never cropped.
@pytest.mark.parametrize("current_viewer", [True, False])
def test_a_box_on_an_image_reaches_the_agent_as_a_crop_of_that_region(
    manifest,
    tmp_path: Path,
    current_viewer: bool,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    store = app.state.background_tasks.store
    image = Image.new("RGB", (100, 50), "red")
    image.paste("blue", (50, 0, 100, 50))
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    data = encoded.getvalue()
    origin_id = "5b6f0c8e-3c1f-4d1a-9a67-7a3f2d0c9e11"
    source = _stored_artifact(app, origin_id, "plot.png", data, kept=True)
    request = RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        chat_scope="project",
        chat_id="0b1f6f9e-8d59-4c3b-9d0e-3f5b8a2c7d41",
        message="What is in the right half?",
        mode="discuss",
    )
    now = store.now()
    for operation_id, result in (
        (origin_id, {"artifacts": [source.model_dump(mode="json")]}),
        (
            "9e3c1a7b-2f4d-4b8e-8c6a-1d2e3f4a5b6c",
            None,
        ),
    ):
        store.create_agent_task(
            AgentTaskRecord(
                operation_id=operation_id,
                project_id=app.state.default_project_id,
                kind="project_chat",
                status="succeeded" if result else "running",
                request=request.model_dump(mode="json"),
                result=result,
                created_at=now,
                updated_at=now,
                status_message="Running.",
                native_session_id="image-session",
                stage_root=str(tmp_path / f"stage-{operation_id}"),
            )
        )
    store.record_agent_task_receipt(
        origin_id,
        "operation_created",
        {"kind": "project_chat", "attempt": 1, "has_parent": False, "resumed": False},
    )
    turn = RunRequest.model_validate(
        {
            **request.model_dump(mode="python"),
            "session_id": "image-session",
            "artifact_context": {
                "operation_id": origin_id,
                "artifact_id": source.artifact_id,
                "selections": [
                    {
                        "kind": "box",
                        "rect": {"x": 0.5, "y": 0, "width": 0.5, "height": 1},
                        "viewport": {"width": 100, "height": 50},
                        **({"elements": []} if current_viewer else {"labels": "right"}),
                        "comment": "What is this?",
                    }
                ],
            },
        }
    )
    execution = AgentTaskExecution(
        operation_id="9e3c1a7b-2f4d-4b8e-8c6a-1d2e3f4a5b6c",
        store=store,
        control=AgentProcessControl(),
    )

    staged = stage_artifact_context(
        service,
        turn,
        execution,
        local_stage=tmp_path / "stage-9e3c1a7b-2f4d-4b8e-8c6a-1d2e3f4a5b6c",
        remote_stage=None,
        artifact_path=str(tmp_path / "artifacts"),
    )

    assert staged is not None
    if not current_viewer:
        assert "crop_path" not in staged.pointer["selections"][0]
        return
    crop_path = Path(staged.pointer["selections"][0]["crop_path"])
    with Image.open(crop_path) as crop:
        assert crop.size == (50, 50)
        assert [color for _, color in crop.convert("RGB").getcolors()] == [(0, 0, 255)]
    prompt = PromptFactory.discuss_turn_prompt(
        artifact_path=str(tmp_path / "artifacts"),
        human_message=turn.message or "",
        attachments=[staged.pointer],
    )
    assert str(crop_path) in prompt


def test_packaged_backend_ships_the_artifact_replacement_source() -> None:
    # The lock holder is composed from this file's source text, so the frozen
    # backend must carry it as data and check it at startup.
    assert_frozen_backend_ships("artifact_replace.py")


def test_report_context_stages_its_current_version(manifest, tmp_path):
    from .test_saved_artifacts_api import _create_chat_report

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    task, report = _create_chat_report(app, tmp_path)
    store = app.state.background_tasks.store
    _publish_artifact(store, report.artifact_id, b"<h1>Later version</h1>")
    request = RunRequest(
        chat_scope="node",
        node_id=task.request["node_id"],
        chat_id=task.request["chat_id"],
        artifact_context={
            "source": "episode_report",
            "operation_id": task.operation_id,
            "episode_id": report.episode_id,
            "artifact_id": report.artifact_id,
            "selections": [{"kind": "text", "text": "comparison", "comment": "Explain"}],
        },
    )
    execution = AgentTaskExecution(
        operation_id=task.operation_id, store=store, control=AgentProcessControl()
    )
    staged = stage_artifact_context(
        app.state.service,
        request,
        execution,
        local_stage=tmp_path / "comment-stage",
        remote_stage=None,
        artifact_path="unused",
    )
    assert staged is not None
    assert Path(staged.pointer["path"]).read_bytes() == b"<h1>Later version</h1>"
    assert staged.pointer["source_artifact_id"] == report.artifact_id


@pytest.mark.parametrize("workspace_layout", ["root", "split"])
@pytest.mark.parametrize("undo_during_edit", [False, True])
def test_edit_reuses_staged_file_and_publishes_or_preserves_conflict(
    manifest, tmp_path, undo_during_edit, workspace_layout
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    origin_id, operation_id = "artifact-origin", "artifact-edit"
    source = _stored_artifact(app, origin_id, "page.html", b"<p>original</p>")
    base = _publish_artifact(store, source.artifact_id, b"<p>base</p>")
    stage = tmp_path / "edit-stage"
    workspace = stage / "workspace" if workspace_layout == "split" else stage
    directory = workspace / "turns" / operation_id / "artifacts"
    workspace.mkdir(parents=True)
    request = RunRequest(
        mode="discuss",
        artifact_context={
            "operation_id": origin_id,
            "artifact_id": source.artifact_id,
            "selections": [],
        },
        artifact_edit={
            "artifact_id": source.artifact_id,
            "base_version": base.version_id,
            "source_name": source.name,
            "media_type": source.media_type,
            "operation_id": operation_id,
            "staged_scope_id": operation_id,
            "origin_operation_id": origin_id,
            "launch_kind": "discuss",
        },
    )
    for task_id in (origin_id, operation_id):
        store.create_agent_task(
            AgentTaskRecord(
                operation_id=task_id,
                project_id=app.state.default_project_id,
                kind="project_chat",
                status="running" if task_id == operation_id else "succeeded",
                request=request.model_dump(mode="json"),
                created_at=store.now(),
                updated_at=store.now(),
                status_message="Editing",
                stage_root=str(stage),
            )
        )
    execution = AgentTaskExecution(
        operation_id=operation_id, store=store, control=AgentProcessControl()
    )
    from rcp.runs.chat import _chat_stage_name, prepare_artifact_edit_directory

    assert prepare_artifact_edit_directory(request, execution, workspace, None) == directory

    fresh_request = request.model_copy(
        update={
            "chat_id": "existing-chat",
            "artifact_edit": request.artifact_edit.model_copy(update={"fresh_session": True}),
        }
    )
    assert (
        _chat_stage_name(app.state.service, fresh_request, execution)
        == "artifact-edit-artifact-edit"
    )
    staged = stage_artifact_context(
        app.state.service,
        request,
        execution,
        local_stage=stage,
        remote_stage=None,
        artifact_path=str(directory),
    )
    target = Path(staged.pointer["path"])
    assert target == directory / source.name
    assert target.read_bytes() == b"<p>base</p>"
    assert target.stat().st_mode & 0o200
    target.write_bytes(b"<p>edited</p>")
    assert prepare_artifact_edit_directory(request, execution, workspace, None) == directory
    assert target.read_bytes() == b"<p>edited</p>"
    if undo_during_edit:
        store.undo_artifact(source.artifact_id)
    resumed = stage_artifact_context(
        app.state.service,
        request,
        execution,
        local_stage=stage,
        remote_stage=None,
        artifact_path=str(directory),
    )
    assert resumed.pointer == staged.pointer
    assert target.read_bytes() == b"<p>edited</p>"
    target.unlink()
    with pytest.raises(FileNotFoundError):
        stage_artifact_context(
            app.state.service,
            request,
            execution,
            local_stage=stage,
            remote_stage=None,
            artifact_path=str(directory),
        )
    assert not target.exists()
    outside = tmp_path / "protected.html"
    outside.write_bytes(b"<p>protected</p>")
    target.symlink_to(outside)
    with pytest.raises(ValueError):
        stage_artifact_context(
            app.state.service,
            request,
            execution,
            local_stage=stage,
            remote_stage=None,
            artifact_path=str(directory),
        )
    assert outside.read_bytes() == b"<p>protected</p>"
    target.unlink()
    edited = (
        b'<script type="application/json" id="rcp-live">'
        b'{"version":1,"needs":[{"kind":"job","key":"missing"}]}</script>'
    )
    target.write_bytes(edited)
    (directory / "extra.txt").write_text("other output")
    discovered = _discover_chat_artifacts(execution, operation_id, directory, None)
    assert [a.name for a in discovered] == ["extra.txt"]
    results = finalize_artifact_edit(
        request,
        execution,
        artifact_scope_id=operation_id,
        artifact_directory=directory,
        remote_stage=None,
        artifacts=discovered,
        service=app.state.service,
    )
    again = finalize_artifact_edit(
        request,
        execution,
        artifact_scope_id=operation_id,
        artifact_directory=directory,
        remote_stage=None,
        artifacts=discovered,
        service=app.state.service,
    )
    assert results == again
    if undo_during_edit:
        assert store.read_artifact_bytes(source.artifact_id) == b"<p>original</p>"
        assert len(results) == 2
        published = store.artifact(results[-1].artifact_id)
    else:
        assert len(results) == 1
        published = store.artifact(source.artifact_id)
    assert store.read_artifact_bytes(published.artifact_id) == edited
    live = {v.version_id: v.live for v in store.artifact_versions(published.artifact_id)}
    assert "missing" in live[published.current_version].invalid_reason
    if not undo_during_edit:
        assert len(store.artifact_versions(source.artifact_id)) == 3
        target.write_bytes(b"<p>different retry bytes</p>")
        assert (
            finalize_artifact_edit(
                request,
                execution,
                artifact_scope_id=operation_id,
                artifact_directory=directory,
                remote_stage=None,
                artifacts=discovered,
                service=app.state.service,
            )
            == discovered
        )
        assert store.read_artifact_bytes(source.artifact_id) == edited


def test_comment_eligibility_matches_viewer_types():
    from rcp.artifact_comments import supports_comments
    from rcp.artifacts import ARTIFACT_MEDIA_TYPES, artifact_view

    for media_type in set(ARTIFACT_MEDIA_TYPES.values()) | {"application/octet-stream"}:
        assert supports_comments(media_type) == (artifact_view(media_type) not in {"pdf", "file"})


def test_remote_edit_staging_reuses_existing_file_and_refuses_symlinks(tmp_path):
    from rcp.transport.remote_stage_root import stage_artifact

    directory = tmp_path / "turns" / "edit" / "artifacts"
    directory.mkdir(parents=True)
    target = directory / "page.html"
    stage_artifact(str(tmp_path), "edit", target.name, b"base")
    assert target.read_bytes() == b"base"
    target.write_bytes(b"agent edit")
    stage_artifact(str(tmp_path), "edit", target.name, b"base")
    assert target.read_bytes() == b"agent edit"
    target.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"protected")
    target.symlink_to(outside)
    with pytest.raises(ValueError):
        stage_artifact(str(tmp_path), "edit", target.name, b"base")
    assert outside.read_bytes() == b"protected"


def test_remote_edit_directory_can_retry_before_staging_but_never_reset_after(tmp_path):
    from rcp.transport.remote_stage_root import prepare_edit_artifacts

    prepare_edit_artifacts(str(tmp_path), "edit", False)
    directory = tmp_path / "turns" / "edit" / "artifacts"
    (directory / "retained.txt").write_text("partial edit")
    prepare_edit_artifacts(str(tmp_path), "edit", False)
    assert (directory / "retained.txt").read_text() == "partial edit"
    (directory / "retained.txt").unlink()
    directory.rmdir()
    with pytest.raises(SystemExit):
        prepare_edit_artifacts(str(tmp_path), "edit", True)
    assert not directory.exists()
