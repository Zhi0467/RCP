from __future__ import annotations

import errno
import hashlib
import io
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient
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
from rcp.limits import CHAT_ARTIFACT_MAX_COUNT
from rcp.runs.chat import (
    _discover_chat_artifacts,
    _local_chat_artifact_directory,
    _project_write_scope,
    finalize_artifact_revision,
    stage_artifact_context,
)
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord, Artifact, ArtifactRevisionCandidateRecord
from rcp.transport import LocalStateWorkspace, RemoteRunStage, StateUnavailable
from rcp.transport.remote_lock_holder import replace_staged_artifact

from .helpers import assert_frozen_backend_ships, authorized_human, create_named_app


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


def _seed_pending_local_candidate(
    app,
    tmp_path: Path,
    *,
    kept: bool,
    revision_status: str = "succeeded",
) -> tuple[AgentArtifactDescriptor, ArtifactRevisionCandidateRecord, str | None, bytes, bytes]:
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    origin_id = "aa97f8cc-031a-4ddd-8834-04832012a0d1"
    revision_id = "0e251f79-c866-41f7-814e-94a4ab1673dc"
    name = "comparison.html"
    first = b"<!doctype html><p>base</p>"
    second = b"<!doctype html><p>candidate</p>"
    kept_filename = name if kept else None
    source = _stored_artifact(app, origin_id, name, first, kept=kept)
    request = RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        chat_scope="project",
        chat_id="586a3844-d144-4bd0-8012-d681a9563aaf",
        message="Create a comparison.",
        mode="discuss",
        run_truth_scope=["repo-a"],
    )
    now = store.now()
    origin = store.create_agent_task(
        AgentTaskRecord(
            operation_id=origin_id,
            project_id=project_id,
            kind="project_chat",
            status="succeeded",
            request=request.model_dump(mode="json"),
            result={"messages": ["Created."], "artifacts": [source.model_dump(mode="json")]},
            created_at=now,
            updated_at=now,
            status_message="Completed.",
            native_session_id="candidate-session",
            stage_root=str(tmp_path / "candidate-source-stage"),
        )
    )
    store.record_agent_task_receipt(
        origin_id,
        "operation_created",
        {"kind": "project_chat", "attempt": 1, "has_parent": False, "resumed": False},
    )
    if not kept:
        source_directory = _local_chat_artifact_directory(store, origin, origin_id)
        source_directory.mkdir(parents=True)
        (source_directory / name).write_bytes(first)
    revision_request = RunRequest.model_validate(
        {
            **request.model_dump(mode="python"),
            "mode": "work",
            "session_id": "candidate-session",
            "artifact_context": {
                "source": "task",
                "operation_id": origin_id,
                "artifact_id": source.artifact_id,
                "selections": [{"kind": "text", "text": "base", "comment": "Revise this."}],
            },
        }
    )
    revision = store.create_agent_task(
        AgentTaskRecord(
            operation_id=revision_id,
            project_id=project_id,
            kind="project_chat",
            status=revision_status,
            request=revision_request.model_dump(mode="json"),
            result={"messages": ["Changed."]},
            created_at=now,
            updated_at=now,
            status_message="Completed.",
            native_session_id="candidate-session",
            stage_root=str(tmp_path / "candidate-revision-stage"),
            dispatch_authority=resolve_dispatch_authority("project_chat", revision_request),
        )
    )
    store.record_agent_task_receipt(
        revision_id,
        "operation_created",
        {"kind": "project_chat", "attempt": 1, "has_parent": False, "resumed": False},
    )
    candidate_directory = _local_chat_artifact_directory(store, revision, revision_id)
    candidate_directory.mkdir(parents=True)
    (candidate_directory / name).write_bytes(second)
    _stored_artifact(app, revision_id, name, second)
    candidate = store.create_artifact_revision_candidate(
        ArtifactRevisionCandidateRecord(
            candidate_id="c" * 24,
            project_id=project_id,
            source_operation_id=origin_id,
            source_artifact_id=source.artifact_id,
            revision_operation_id=revision_id,
            stage_host="",
            stage_root=revision.stage_root or "",
            artifact_scope_id=revision_id,
            source_name=name,
            media_type="text/html",
            base_sha256=hashlib.sha256(first).hexdigest(),
            candidate_sha256=hashlib.sha256(second).hexdigest(),
            candidate_size_bytes=len(second),
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )
    return source, candidate, kept_filename, first, second


@pytest.mark.parametrize("kept", [False, True])
def test_artifact_revision_source_is_in_the_provider_enforced_deny_scope(
    manifest,
    tmp_path: Path,
    kept: bool,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    source, candidate, kept_filename, _, _ = _seed_pending_local_candidate(
        app,
        tmp_path,
        kept=kept,
    )
    store = app.state.background_tasks.store
    service = app.state.service
    revision = store.agent_task(candidate.revision_operation_id)
    assert revision is not None and revision.stage_root
    request = RunRequest.model_validate(revision.request)
    execution = AgentTaskExecution(
        operation_id=revision.operation_id,
        store=store,
        control=AgentProcessControl(),
    )
    local_stage = Path(revision.stage_root)
    staged = stage_artifact_context(
        service,
        request,
        execution,
        local_stage=local_stage,
        remote_stage=None,
        artifact_path=str(_local_chat_artifact_directory(store, revision, revision.operation_id)),
    )
    assert staged is not None
    context = service.assemble_chat(request)
    scope = _project_write_scope(
        context,
        service,
        "laptop",
        local_stage=local_stage,
        workspace=local_stage,
        remote_stage=None,
        data_dir=tmp_path / "data",
        execution=execution,
        capability="work_auto",
        additional_protected_write_paths=list(staged.protected_write_paths),
    )
    source_task = store.agent_task(candidate.source_operation_id)
    assert source_task is not None
    assert source.artifact_id == candidate.source_artifact_id
    assert str((tmp_path / "data").resolve()) in scope.protected_write_paths
    assert str((tmp_path / "data" / "artifacts").resolve()) in scope.protected_write_paths


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


def test_viewer_assembles_context_without_dispatch_or_mode_change() -> None:
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

    assert "rcp-artifact-context" in document
    assert 'BroadcastChannel("rcp-artifact-context")' in document
    assert "/#/projects/project?view=chats&amp;chat=chat&amp;branch_id=branch%2Fid" in document
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


def test_episode_report_without_originating_chat_is_readonly_but_saveable() -> None:
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
        save_url="/save",
    )

    assert 'id="pending"' not in document
    assert "rcp-artifact-context" not in document
    assert "rcp-artifact-selection-enable" not in document
    assert ">Comment</button>" not in document
    assert "fetch(config.saveUrl" in document
    assert 'id="keep"' not in document
    assert ">report</span>" in document
    assert "connect-src 'self'" in csp

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
        save_url="/save",
    )
    assert 'id="pending"' in with_chat
    assert '"chatAvailable": true' in with_chat
    assert "fetch(config.saveUrl" in with_chat


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


def test_work_revision_waits_for_human_accept_without_a_second_card(
    manifest,
    tmp_path: Path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    chat_id = "3a979535-17c3-4fd2-85fc-219de0ee7a75"
    origin_id = "241df76b-d927-496d-a9a1-02ba7537f9ec"
    revision_id = "d70b7937-ed31-44b7-9823-c2af557d3161"
    name = "curves.html"
    first = b"<!doctype html><p>first</p>"
    second = b"<!doctype html><p>second</p>"
    source = _stored_artifact(app, origin_id, name, first, kept=True)
    origin_request = RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        chat_scope="project",
        chat_id=chat_id,
        message="Create the curves.",
        mode="discuss",
    )
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=origin_id,
            project_id=project_id,
            kind="project_chat",
            status="succeeded",
            request=origin_request.model_dump(mode="json"),
            result={"messages": ["Created."], "artifacts": [source.model_dump(mode="json")]},
            created_at=now,
            updated_at=now,
            status_message="Completed.",
            native_session_id="artifact-session",
            stage_root=str(tmp_path / "origin-stage"),
        )
    )
    store.record_agent_task_receipt(
        origin_id,
        "operation_created",
        {"kind": "project_chat", "attempt": 1, "has_parent": False, "resumed": False},
    )
    revision_request = RunRequest.model_validate(
        {
            **origin_request.model_dump(mode="python"),
            "message": "Make the requested change.",
            "mode": "work",
            "session_id": "artifact-session",
            "artifact_context": {
                "source": "task",
                "operation_id": origin_id,
                "artifact_id": source.artifact_id,
                "selections": [
                    {"kind": "text", "text": "first", "comment": "Change this to second."}
                ],
            },
        }
    )
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=revision_id,
            project_id=project_id,
            kind="project_chat",
            status="running",
            request=revision_request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="Running.",
            native_session_id="artifact-session",
            stage_root=str(tmp_path / "revision-stage"),
        )
    )
    execution = AgentTaskExecution(
        operation_id=revision_id,
        store=store,
        control=AgentProcessControl(),
    )
    revision = store.agent_task(revision_id)
    assert revision is not None
    artifact_directory = _local_chat_artifact_directory(store, revision, revision_id)
    artifact_directory.mkdir(parents=True)
    (artifact_directory / name).write_bytes(second)
    extras = []
    for index in range(9):
        extra_name = f"a-extra-{index}.html"
        extra_data = f"<!doctype html><p>extra {index}</p>".encode()
        (artifact_directory / extra_name).write_bytes(extra_data)
        extras.append(
            descriptor_for(
                revision_id, extra_name, media_type="text/html", size_bytes=len(extra_data)
            )
        )
    store.record_agent_task_receipt(
        revision_id,
        "artifact_revision_base",
        {
            "source_operation_id": origin_id,
            "source_artifact_id": source.artifact_id,
            "sha256": hashlib.sha256(first).hexdigest(),
        },
    )

    remaining = finalize_artifact_revision(
        revision_request,
        execution,
        artifact_scope_id=revision_id,
        artifact_directory=artifact_directory,
        remote_stage=None,
        artifacts=_discover_chat_artifacts(execution, revision_id, artifact_directory, None),
    )

    assert remaining == extras[: CHAT_ARTIFACT_MAX_COUNT - 1]
    assert store.read_artifact_bytes(source.artifact_id) == first
    pending = store.unresolved_artifact_revision_candidate(origin_id, source.artifact_id)
    assert pending is not None and pending.status == "pending"
    updated = store.agent_task(origin_id)
    assert updated is not None
    assert updated.result["artifacts"] == [source.model_dump(mode="json")]
    assert any(
        receipt.category == "artifact_revision_staged"
        for receipt in store.agent_task_receipts(revision_id)
    )
    store.complete_agent_task(revision_id, applied_revision=None, result={})
    lifecycle = next(
        item for item in store.run_stage_lifecycles() if item.stage_root == revision.stage_root
    )
    assert lifecycle.must_exist is True
    assert lifecycle.protect_from_cleanup is True
    with (
        store.connection() as connection,
        pytest.raises(
            ValueError,
            match="every artifact revision candidate to be settled",
        ),
    ):
        store._require_finished_transfer_state(connection, project_id)

    client = TestClient(app)
    detail = client.get(f"/api/projects/{project_id}/tasks/{origin_id}")
    projected_artifact = detail.json()["result"]["artifacts"][0]
    candidate = projected_artifact["revision_candidate"]
    assert candidate["candidate_id"] == pending.candidate_id
    assert candidate["can_accept"] is True
    assert projected_artifact["can_discuss"] is True
    assert projected_artifact["can_revise"] is False
    viewer = client.get(
        f"/api/projects/{project_id}/tasks/{origin_id}/artifacts/{source.artifact_id}/viewer"
    )
    assert viewer.status_code == 200
    assert '"chatAvailable": true' in viewer.text
    preview = client.get(
        f"/api/projects/{project_id}/artifact-revisions/{pending.candidate_id}/content"
    )
    assert preview.status_code == 200
    assert "second" in preview.text

    accepted = client.post(
        f"/api/projects/{project_id}/artifact-revisions/{pending.candidate_id}/accept"
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert store.read_artifact_bytes(source.artifact_id) == second
    updated = store.agent_task(origin_id)
    assert updated is not None
    assert updated.result["artifacts"] == [
        source.model_copy(update={"size_bytes": len(second)}).model_dump(mode="json")
    ]
    assert (
        client.post(
            f"/api/projects/{project_id}/artifact-revisions/{pending.candidate_id}/accept"
        ).json()["status"]
        == "accepted"
    )
    lifecycle = next(
        item for item in store.run_stage_lifecycles() if item.stage_root == revision.stage_root
    )
    assert lifecycle.must_exist is False
    assert lifecycle.protect_from_cleanup is False


def test_revision_conflict_preserves_external_edit_until_human_rejects(
    manifest,
    tmp_path: Path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    source, candidate, kept_filename, _, _ = _seed_pending_local_candidate(
        app,
        tmp_path,
        kept=True,
    )
    assert kept_filename is not None
    workspace = app.state.background_tasks.store
    source_task_before = app.state.background_tasks.store.agent_task(candidate.source_operation_id)
    assert source_task_before is not None
    external = b"<!doctype html><p>external edit</p>"
    _publish_artifact(workspace, candidate.source_artifact_id, external)
    client = TestClient(app)
    base = (
        f"/api/projects/{app.state.default_project_id}/artifact-revisions/{candidate.candidate_id}"
    )

    response = client.post(f"{base}/accept")

    assert response.status_code == 409, response.text
    assert "changed after this candidate" in response.json()["detail"]
    assert workspace.read_artifact_bytes(candidate.source_artifact_id) == external
    conflicted = app.state.background_tasks.store.artifact_revision_candidate(
        candidate.candidate_id
    )
    assert conflicted is not None and conflicted.status == "conflicted"
    source_task_after = app.state.background_tasks.store.agent_task(candidate.source_operation_id)
    assert source_task_after is not None
    assert source_task_after.updated_at > source_task_before.updated_at
    projected = client.get(
        f"/api/projects/{app.state.default_project_id}/tasks/{candidate.source_operation_id}"
    ).json()["result"]["artifacts"][0]
    assert projected["artifact_id"] == source.artifact_id
    assert projected["revision_candidate"]["can_accept"] is False
    assert projected["revision_candidate"]["can_reject"] is True

    rejected = client.post(f"{base}/reject")

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert workspace.read_artifact_bytes(candidate.source_artifact_id) == external
    assert (
        "revision_candidate"
        not in client.get(
            f"/api/projects/{app.state.default_project_id}/tasks/{candidate.source_operation_id}"
        ).json()["result"]["artifacts"][0]
    )


def test_revision_accept_conflicts_when_external_edit_has_invalid_media_bytes(
    manifest,
    tmp_path: Path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, candidate, kept_filename, _, _ = _seed_pending_local_candidate(
        app,
        tmp_path,
        kept=True,
    )
    assert kept_filename is not None
    workspace = app.state.background_tasks.store
    invalid_html = b"\x00not HTML"
    _publish_artifact(workspace, candidate.source_artifact_id, invalid_html)

    response = TestClient(app).post(
        f"/api/projects/{app.state.default_project_id}"
        f"/artifact-revisions/{candidate.candidate_id}/accept"
    )

    assert response.status_code == 409, response.text
    assert "changed after this candidate" in response.json()["detail"]
    assert workspace.read_artifact_bytes(candidate.source_artifact_id) == invalid_html
    conflicted = app.state.background_tasks.store.artifact_revision_candidate(
        candidate.candidate_id
    )
    assert conflicted is not None and conflicted.status == "conflicted"
    assert conflicted.diagnostic == response.json()["detail"]


def test_revision_accept_conflicts_when_current_artifact_was_deleted(
    manifest,
    tmp_path: Path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, candidate, kept_filename, _, _ = _seed_pending_local_candidate(
        app,
        tmp_path,
        kept=True,
    )
    assert kept_filename is not None
    store = app.state.background_tasks.store
    store.artifact_file_path(store.artifact_versions(candidate.source_artifact_id)[0]).unlink()
    client = TestClient(app)
    base = (
        f"/api/projects/{app.state.default_project_id}/artifact-revisions/{candidate.candidate_id}"
    )

    response = client.post(f"{base}/accept")

    assert response.status_code == 409, response.text
    assert "no longer available" in response.json()["detail"]
    conflicted = app.state.background_tasks.store.artifact_revision_candidate(
        candidate.candidate_id
    )
    assert conflicted is not None and conflicted.status == "conflicted"
    assert conflicted.diagnostic == response.json()["detail"]
    retry = client.post(f"{base}/accept")
    assert retry.status_code == 409


def test_revision_accept_keeps_transiently_unavailable_source_pending(
    manifest,
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, candidate, kept_filename, first, _ = _seed_pending_local_candidate(
        app,
        tmp_path,
        kept=True,
    )
    assert kept_filename is not None
    workspace = app.state.background_tasks.store
    original_read = workspace.read_artifact_bytes

    def unavailable(*_args, **_kwargs) -> bytes:
        raise StateUnavailable("temporary read failure")

    monkeypatch.setattr(workspace, "read_artifact_bytes", unavailable)

    response = TestClient(app).post(
        f"/api/projects/{app.state.default_project_id}"
        f"/artifact-revisions/{candidate.candidate_id}/accept"
    )

    assert response.status_code == 503, response.text
    pending = app.state.background_tasks.store.artifact_revision_candidate(candidate.candidate_id)
    assert pending is not None and pending.status == "pending"
    assert pending.diagnostic is None
    assert original_read(candidate.source_artifact_id) == first


@pytest.mark.parametrize(
    "failure",
    [OSError("post-exchange fsync failed"), StateUnavailable("remote reply disconnected")],
    ids=("local-post-exchange", "remote-commit-then-disconnect"),
)
def test_revision_accept_retries_ambiguous_publication_without_allowing_reject(
    manifest,
    tmp_path: Path,
    monkeypatch,
    failure: Exception,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, candidate, kept_filename, _, second = _seed_pending_local_candidate(
        app,
        tmp_path,
        kept=True,
    )
    assert kept_filename is not None
    workspace = app.state.background_tasks.store
    original_replace = workspace.publish_artifact_version

    def publish_then_fail(*args, **kwargs) -> bool:
        original_replace(*args, **kwargs)
        raise failure

    monkeypatch.setattr(workspace, "publish_artifact_version", publish_then_fail)
    client = TestClient(app)
    base = (
        f"/api/projects/{app.state.default_project_id}/artifact-revisions/{candidate.candidate_id}"
    )

    response = client.post(f"{base}/accept")

    assert response.status_code == 503, response.text
    accepting = app.state.background_tasks.store.artifact_revision_candidate(candidate.candidate_id)
    assert accepting is not None and accepting.status == "accepting"
    assert workspace.read_artifact_bytes(candidate.source_artifact_id) == second
    assert client.post(f"{base}/reject").status_code == 409

    monkeypatch.setattr(workspace, "publish_artifact_version", original_replace)
    retry = client.post(f"{base}/accept")
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "accepted"


def test_revision_accept_detects_an_edit_during_publication(
    manifest,
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, candidate, kept_filename, _, _ = _seed_pending_local_candidate(
        app,
        tmp_path,
        kept=True,
    )
    assert kept_filename is not None
    workspace = app.state.background_tasks.store
    original_replace = workspace.publish_artifact_version
    external = b"<!doctype html><p>racing external edit</p>"

    def race_then_replace(artifact_id, *, data, base_version, operation_id):
        original_replace(
            artifact_id, data=external, base_version=base_version, operation_id="racing-edit"
        )
        return original_replace(
            artifact_id, data=data, base_version=base_version, operation_id=operation_id
        )

    monkeypatch.setattr(workspace, "publish_artifact_version", race_then_replace)

    response = TestClient(app).post(
        f"/api/projects/{app.state.default_project_id}"
        f"/artifact-revisions/{candidate.candidate_id}/accept"
    )

    assert response.status_code == 409, response.text
    assert "changed while this candidate was being accepted" in response.json()["detail"]
    assert workspace.read_artifact_bytes(candidate.source_artifact_id) == external


def test_retry_rechecks_unresolved_artifact_revision_admission(
    manifest,
    tmp_path: Path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, candidate, _, _, _ = _seed_pending_local_candidate(
        app,
        tmp_path,
        kept=True,
        revision_status="failed",
    )

    response = TestClient(app).post(
        f"/api/projects/{app.state.default_project_id}/tasks/"
        f"{candidate.revision_operation_id}/retry"
    )

    assert response.status_code == 409, response.text
    assert "pending artifact revision" in response.json()["detail"]


def test_keep_during_pending_revision_moves_accept_to_the_kept_artifact(
    manifest,
    tmp_path: Path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    source, candidate, _, first, second = _seed_pending_local_candidate(
        app,
        tmp_path,
        kept=False,
    )
    project_id = app.state.default_project_id
    client = TestClient(app)

    kept = client.post(
        f"/api/projects/{project_id}/tasks/{candidate.source_operation_id}"
        f"/artifacts/{source.artifact_id}/keep"
    )

    source_task = app.state.background_tasks.store.agent_task(candidate.source_operation_id)
    assert source_task is not None
    source_lifecycle = next(
        item
        for item in app.state.background_tasks.store.run_stage_lifecycles()
        if item.stage_root == source_task.stage_root
    )
    assert source_lifecycle.must_exist is False
    assert source_lifecycle.protect_from_cleanup is False
    assert kept.status_code == 200, kept.text
    assert kept.json()["kept_filename"] is None
    assert (
        app.state.background_tasks.store.read_artifact_bytes(candidate.source_artifact_id) == first
    )
    accepted = client.post(
        f"/api/projects/{project_id}/artifact-revisions/{candidate.candidate_id}/accept"
    )
    assert accepted.status_code == 200
    assert (
        app.state.background_tasks.store.read_artifact_bytes(candidate.source_artifact_id) == second
    )


def test_pending_revision_protects_temporary_source_and_blocks_history_detachment(
    manifest,
    tmp_path: Path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    source, candidate, _, _, _ = _seed_pending_local_candidate(app, tmp_path, kept=False)
    store = app.state.background_tasks.store
    source_task = store.agent_task(candidate.source_operation_id)
    assert source_task is not None

    source_lifecycle = next(
        item for item in store.run_stage_lifecycles() if item.stage_root == source_task.stage_root
    )

    assert source_lifecycle.must_exist is True
    assert source_lifecycle.protect_from_cleanup is True
    assert f"artifact_revision_sources:{candidate.candidate_id}" in source_lifecycle.owner_refs
    with pytest.raises(ValueError, match="unresolved artifact revision"):
        store.mark_agent_tasks_history_only(
            [candidate.source_operation_id, candidate.revision_operation_id]
        )

    rejected = TestClient(app).post(
        f"/api/projects/{app.state.default_project_id}"
        f"/artifact-revisions/{candidate.candidate_id}/reject"
    )
    assert rejected.status_code == 200
    projected = next(
        item for item in store.run_stage_lifecycles() if item.stage_root == source_task.stage_root
    )
    assert projected.must_exist is False
    assert projected.protect_from_cleanup is False


def test_interrupted_accept_recovers_from_the_already_published_digest(
    manifest,
    tmp_path: Path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, candidate, kept_filename, _, second = _seed_pending_local_candidate(
        app,
        tmp_path,
        kept=True,
    )
    assert kept_filename is not None
    store = app.state.background_tasks.store
    workspace = app.state.background_tasks.store
    store.begin_artifact_revision_acceptance(
        candidate.candidate_id,
        decided_by=authorized_human(app),
    )
    _publish_artifact(
        workspace, candidate.source_artifact_id, second, candidate.revision_operation_id
    )

    client = TestClient(app)
    recovered = client.post(
        f"/api/projects/{app.state.default_project_id}"
        f"/artifact-revisions/{candidate.candidate_id}/accept"
    )

    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "accepted"


def test_offline_restore_abandons_pending_candidate_and_preserves_source(
    manifest,
    tmp_path: Path,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    _, unrestored, kept_filename, first, _ = _seed_pending_local_candidate(
        app,
        tmp_path,
        kept=True,
    )
    assert kept_filename is not None
    store = app.state.background_tasks.store
    store.detach_restored_lifecycle(
        diagnostic="Offline restore detached provider state.",
        confirmed_by="operator",
    )
    abandoned = store.artifact_revision_candidate(unrestored.candidate_id)
    assert abandoned is not None and abandoned.status == "abandoned"
    assert store.read_artifact_bytes(unrestored.source_artifact_id) == first
    lifecycle = next(
        item for item in store.run_stage_lifecycles() if item.stage_root == unrestored.stage_root
    )
    assert lifecycle.protect_from_cleanup is False


def test_remote_candidate_accept_uses_storage_without_contacting_stages(
    manifest,
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    project_id = app.state.default_project_id
    origin_id = "461998aa-3ae0-4c6e-bee4-f26760279c06"
    revision_id = "ee9bcf12-ea01-4e11-bf20-9429e65c0ed0"
    name = "remote.html"
    first = b"<!doctype html><p>remote base</p>"
    second = b"<!doctype html><p>remote candidate</p>"
    source = descriptor_for(origin_id, name, media_type="text/html", size_bytes=len(first))
    request = RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="remote",
        chat_scope="project",
        chat_id="f916649f-3abe-461d-b567-21b78a3befcf",
        message="Create remote output.",
        mode="discuss",
    )
    now = store.now()
    for operation_id, mode, stage_root, result in (
        (
            origin_id,
            "discuss",
            "/remote/source-stage",
            {"messages": ["Created."], "artifacts": [source.model_dump(mode="json")]},
        ),
        (revision_id, "work", "/remote/candidate-stage", {"messages": ["Changed."]}),
    ):
        store.create_agent_task(
            AgentTaskRecord(
                operation_id=operation_id,
                project_id=project_id,
                kind="project_chat",
                status="succeeded",
                request=request.model_copy(update={"mode": mode}).model_dump(mode="json"),
                result=result,
                created_at=now,
                updated_at=now,
                status_message="Completed.",
                native_session_id="remote-candidate-session",
                stage_host="research-gpu",
                stage_root=stage_root,
            )
        )
        store.record_agent_task_receipt(
            operation_id,
            "operation_created",
            {"kind": "project_chat", "attempt": 1, "has_parent": False, "resumed": False},
        )
    candidate = store.create_artifact_revision_candidate(
        ArtifactRevisionCandidateRecord(
            candidate_id="d" * 24,
            project_id=project_id,
            source_operation_id=origin_id,
            source_artifact_id=source.artifact_id,
            revision_operation_id=revision_id,
            stage_host="research-gpu",
            stage_root="/remote/candidate-stage",
            artifact_scope_id=revision_id,
            source_name=name,
            media_type="text/html",
            base_sha256=hashlib.sha256(first).hexdigest(),
            candidate_sha256=hashlib.sha256(second).hexdigest(),
            candidate_size_bytes=len(second),
            status="pending",
            created_at=now,
            updated_at=now,
        )
    )
    _stored_artifact(app, origin_id, name, first)
    _stored_artifact(app, revision_id, name, second)
    response = TestClient(app).post(
        f"/api/projects/{project_id}/artifact-revisions/{candidate.candidate_id}/accept"
    )

    assert response.status_code == 200, response.text
    assert store.read_artifact_bytes(source.artifact_id) == second


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


def test_expired_pending_revision_survives_pruning_and_can_be_accepted(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    source, candidate, _, first, second = _seed_pending_local_candidate(app, tmp_path, kept=False)
    store = app.state.background_tasks.store
    replacement_id = descriptor_for(
        candidate.artifact_scope_id,
        source.name,
        media_type=source.media_type,
        size_bytes=len(second),
    ).artifact_id
    expired = (datetime.fromisoformat(store.now()) - timedelta(days=1)).isoformat()
    with store.connection() as connection:
        connection.execute(
            "UPDATE artifacts SET metadata = json_set(metadata, '$.expires_at', ?)", (expired,)
        )
    assert store.prune_operational_storage()["artifacts"] == 0
    assert store.read_artifact_bytes(source.artifact_id) == first
    assert store.read_artifact_bytes(replacement_id) == second
    response = TestClient(app).post(
        f"/api/projects/{app.state.default_project_id}/artifact-revisions/{candidate.candidate_id}/accept"
    )
    assert response.status_code == 200, response.text
    assert store.read_artifact_bytes(source.artifact_id) == second


def test_report_context_stages_its_immutable_first_version(manifest, tmp_path):
    from .test_saved_artifacts_api import _create_chat_report

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    task, report = _create_chat_report(app, tmp_path)
    store = app.state.background_tasks.store
    first = store.read_artifact_bytes(report.artifact_id, report.artifact_version_id)
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
    assert Path(staged.pointer["path"]).read_bytes() == first
    assert staged.pointer["source_artifact_id"] == report.artifact_id


def test_active_revision_protects_source_before_candidate_capture(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    source, candidate, _, first, _ = _seed_pending_local_candidate(
        app, tmp_path, kept=False, revision_status="running"
    )
    store = app.state.background_tasks.store
    expired = (datetime.fromisoformat(store.now()) - timedelta(days=1)).isoformat()
    with store.connection() as connection:
        connection.execute("DELETE FROM artifact_revision_candidates")
        connection.execute(
            "UPDATE artifacts SET metadata = json_set(metadata, '$.expires_at', ?) "
            "WHERE artifact_id = ?",
            (expired, source.artifact_id),
        )
    assert store.prune_operational_storage()["artifacts"] == 0
    assert store.read_artifact_bytes(source.artifact_id) == first
    response = TestClient(app).get(
        f"/api/projects/{candidate.project_id}/artifacts/{source.artifact_id}/download"
    )
    assert response.status_code == 200
    assert response.content == first
