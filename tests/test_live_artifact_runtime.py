from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.compute_jobs.models import ComputeJobRecord
from rcp.core.models import GraphState
from rcp.live_artifact_runtime import (
    artifact_live_snapshot,
    reconcile_artifact_live_snapshots,
    resolve_artifact_live_version,
)
from rcp.regular_file_reader import read_local_regular_file
from rcp.storage import AgentTaskRecord, AppStore, Artifact, ProjectRecord


@pytest.fixture
def live(tmp_path, manifest):
    # The real store and file readers run end to end; only graph opening is a
    # fixed owner, so these tests do not start providers or an HTTP server.
    store = AppStore(tmp_path / "data" / "rcp.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator=str(manifest.path),
            name="project",
            state_location=str(tmp_path),
            state_remote=False,
            added_at=store.now(),
        )
    )
    task = store.create_agent_task(
        AgentTaskRecord(
            operation_id="turn",
            project_id="project",
            kind="project_chat",
            status="succeeded",
            request={},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="",
        )
    )
    root = Path(manifest.repositories[0].path)
    root.mkdir(parents=True, exist_ok=True)
    graph = GraphState()
    service = SimpleNamespace(manifest=manifest, history=SimpleNamespace(state=lambda: graph))
    service.for_graph_target = lambda target, **kwargs: service
    catalog = SimpleNamespace(open=lambda project_id: service)

    def create(
        needs, *, artifact_id="page", supplier="turn", live_data_allowed=True, episode_id=None
    ):
        data = (
            '<html><script type="application/json" id="rcp-live">'
            + json.dumps({"version": 1, "needs": needs})
            + "</script></html>"
        ).encode()
        artifact = store.create_artifact(
            Artifact(
                artifact_id=artifact_id,
                project_id="project",
                supplier=supplier,
                supplier_id="turn",
                live_data_allowed=live_data_allowed,
                origin_operation_id=task.operation_id,
                episode_id=episode_id or store.agent_task(task.operation_id).episode_id,
                source_name="page.html",
                media_type="text/html",
                created_at=store.now(),
            ),
            data=data,
        )
        resolve_artifact_live_version(
            store, service, artifact.artifact_id, artifact.current_version
        )
        return artifact

    return SimpleNamespace(
        store=store, service=service, catalog=catalog, root=root, create=create, task=task
    )


def file_need(path, *, read="tail", format="jsonl"):
    return {"kind": "file", "path": str(path), "read": read, "format": format}


def read(live, artifact):
    return artifact_live_snapshot(
        live.store, live.catalog, artifact.artifact_id, artifact.current_version
    )


def capture(live):
    reconcile_artifact_live_snapshots(live.store, live.catalog, "project")


def version(live, artifact):
    return live.store.artifact_versions(artifact.artifact_id)[0]


def episode(live):
    from rcp.storage import EpisodeRecord
    from tests.helpers import authorized_human

    now = live.store.now()
    return live.store.create_episode(
        EpisodeRecord(
            episode_id="episode",
            project_id="project",
            mode="experiment_loop",
            control_node_id="experiment",
            authorized_by=authorized_human(live.store),
            status="queued",
            invocation_ceiling=4,
            invocations_used=0,
            created_at=now,
            updated_at=now,
        )
    )


def job(live):
    log = live.root / "job.log"
    log.write_text("started\nfinished\n")
    record = live.store.create_compute_job(
        ComputeJobRecord(
            job_id="job",
            project_id="project",
            origin_operation_id="turn",
            execution_machine="laptop",
            backend_id="systemd_user",
            backend_handle="handle",
            job_root=str(live.root),
            cwd=str(live.root),
            argv=["train"],
            log_path=str(log),
            exit_path=str(live.root / "exit"),
            created_at=live.store.now(),
            status="exited",
            exit_status=0,
            ended_at=live.store.now(),
        )
    )
    live.store.record_agent_task_receipt(
        "turn",
        "compute_command_started",
        {"verb": "launch", "key": "train", "arguments_sha256": "a"},
        tier="diagnostic",
    )
    live.store.record_agent_task_receipt(
        "turn",
        "compute_command_result",
        {
            "verb": "launch",
            "key": "train",
            "arguments_sha256": "a",
            "response": {"status": "ok", "result": {"job_id": record.job_id}},
        },
        tier="diagnostic",
    )
    return record


def test_resolution_and_current_file_snapshot_are_version_bound(live):
    metrics = live.root / "metrics.jsonl"
    metrics.write_text('{"loss":2}\n')
    artifact = live.create([file_need(metrics)])
    assert read(live, artifact).snapshots[0].rows == [{"loss": 2}]
    metrics.write_text('{"loss":1}\n')
    assert read(live, artifact).snapshots[0].rows == [{"loss": 1}]
    capture(live)
    assert (
        live.store.read_artifact_live_snapshot(artifact.artifact_id, artifact.current_version)
        is None
    )
    live.service.manifest.repositories = []
    with pytest.raises(ValueError):
        read(live, artifact)


def test_invalid_needs_remain_static_with_reason(live, tmp_path):
    for index, needs in enumerate(
        [
            [file_need(tmp_path / "outside")],
            [{"kind": "job", "key": "absent"}],
            [{"kind": "episode"}],
            [{"kind": "node", "id": "missing"}],
        ]
    ):
        artifact = live.create(needs, artifact_id=f"page{index}")
        snapshot = read(live, artifact)
        assert snapshot.static and snapshot.reason
        assert not snapshot.final


def test_server_final_capture_survives_deleted_sources_and_history_transfer(live):
    record = job(live)
    metrics = live.root / "metrics.jsonl"
    metrics.write_text('{"loss":0.5}\n')
    artifact = live.create([{"kind": "job", "key": "train"}, file_need(metrics)])
    capture(live)
    metrics.unlink()
    Path(record.log_path).unlink()
    with live.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET history_only=1, stage_host=NULL, stage_root=NULL WHERE operation_id='turn'"
        )
        connection.execute("DELETE FROM compute_jobs")
    live.service.manifest.repositories = []
    snapshot = read(live, artifact)
    assert snapshot.final and snapshot.complete
    assert snapshot.snapshots[1].rows == [{"loss": 0.5}]

    def refuse(*args, **kwargs):
        raise ValueError("graph target unavailable")

    live.service.for_graph_target = refuse
    with pytest.raises(ValueError):
        read(live, artifact)


def test_incomplete_server_capture_has_durable_backoff_then_retries(live, monkeypatch):
    job(live)
    metrics = live.root / "missing.jsonl"
    artifact = live.create([{"kind": "job", "key": "train"}, file_need(metrics)])
    capture(live)
    saved = version(live, artifact)
    assert saved.live_snapshot is None
    assert saved.live.capture_attempts == 1 and saved.live.capture_error
    assert not read(live, artifact).complete
    metrics.write_text('{"loss":0.1}\n')
    capture(live)
    assert version(live, artifact).live.capture_attempts == 1
    later = (datetime.fromisoformat(saved.live.next_capture_at) + timedelta(seconds=1)).isoformat()
    monkeypatch.setattr(live.store, "now", lambda: later)
    capture(live)
    assert read(live, artifact).final


@pytest.mark.parametrize(
    "contents, rows",
    [
        ("first\nsecond\nthird\nfourth\n", ["fourth"]),
        ("discard\nfirst\nfinal\n", ["first", "final"]),
    ],
)
def test_tail_caps_at_complete_lines(live, monkeypatch, contents, rows):
    monkeypatch.setattr("rcp.live_artifact_runtime.LIVE_ARTIFACT_MAX_BYTES", 12)
    monkeypatch.setattr("rcp.live_artifact_runtime.LIVE_ARTIFACT_MAX_ROWS", 2)
    metrics = live.root / "metrics.txt"
    metrics.write_text(contents)
    artifact = live.create([file_need(metrics, format="text")])
    snapshot = read(live, artifact).snapshots[0]
    assert snapshot.rows == rows and snapshot.truncated
    metrics.unlink()
    metrics.symlink_to(live.root / "target")
    (live.root / "target").write_text("private")
    assert read(live, artifact).snapshots[0].error


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "fifo"])
def test_local_and_shipped_reader_refuse_unsafe_files(tmp_path, kind):
    root = tmp_path.resolve()
    (root / "real").mkdir()
    (root / "real" / "data").write_text("data")
    path = root / "bad"
    if kind == "symlink":
        path.symlink_to(root / "real" / "data")
    elif kind == "directory_symlink":
        path.symlink_to(root / "real", target_is_directory=True)
        path /= "data"
    else:
        os.mkfifo(path)
    with pytest.raises((ValueError, OSError)):
        read_local_regular_file(path.parent, path.name, max_bytes=100, tail=True)
    import rcp.regular_file_reader as module

    result = subprocess.run(
        [sys.executable, "-c", Path(module.__file__).read_text(), str(path), "100", "tail"],
        capture_output=True,
        timeout=5,
    )
    assert result.returncode != 0 and not result.stdout


def test_episode_only_final_capture_without_viewer(live):
    owner = episode(live)
    with live.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET episode_id=? WHERE operation_id='turn'", (owner.episode_id,)
        )
    live.store.end_episode_without_report(owner.episode_id, ending="completed")
    artifact = live.create([{"kind": "episode"}])
    capture(live)
    snapshot = read(live, artifact)
    assert snapshot.final and snapshot.complete
    assert snapshot.snapshots[0].turn == 0
    assert snapshot.snapshots[0].budget_limit == 4


def test_bound_conversation_worktree_is_readable_and_removal_revokes(live, tmp_path):
    import uuid

    from rcp.core.models import ConversationWorktreeBinding

    chat_id = str(uuid.uuid4())
    root = tmp_path.resolve() / "bound-worktree"
    root.mkdir()
    metrics = root / "metrics.jsonl"
    metrics.write_text('{"loss":1}\n')
    repo = live.service.manifest.repositories[0]
    binding = ConversationWorktreeBinding(
        project_id="project",
        chat_id=chat_id,
        chat_scope="project",
        repository_alias=repo.alias,
        machine=repo.machine,
        execution_host="",
        shared_path=str(live.root),
        worktree_path=str(root),
        git_common_dir=str(live.root / ".git"),
        branch="work",
        starting_branch="main",
        starting_commit="a" * 40,
        status="creating",
    )
    live.store.create_conversation_worktree(binding)
    live.store.set_conversation_worktree_status("project", chat_id, "creating", "ready")
    with live.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=? WHERE operation_id='turn'",
            (json.dumps({"chat_id": chat_id}),),
        )
    artifact = live.create([file_need(metrics)])
    assert read(live, artifact).snapshots[0].rows == [{"loss": 1}]
    live.store.set_conversation_worktree_status("project", chat_id, "ready", "removing")
    with pytest.raises(ValueError):
        read(live, artifact)


def test_csv_byte_tail_does_not_invent_multiline_records(live, monkeypatch):
    monkeypatch.setattr("rcp.live_artifact_runtime.LIVE_ARTIFACT_MAX_BYTES", 16)
    metrics = live.root / "metrics.csv"
    metrics.write_text('name,result\n"long\nquoted\nname",12\n')
    artifact = live.create([file_need(metrics, format="csv")])
    snapshot = read(live, artifact)
    assert not snapshot.complete
    assert snapshot.snapshots[0].error
    assert not snapshot.snapshots[0].rows


def test_node_snapshot_uses_own_graph_and_evidence_stances(live):
    from rcp.core.models import Edge, Evidence, Hypothesis

    graph = live.service.history.state()
    graph.nodes["claim"] = Hypothesis(
        id="claim", title="Claim", type="hypothesis", statement="A measurable claim"
    )
    graph.nodes["result"] = Evidence(
        id="result", title="Measurement", type="evidence", observation="Observed result"
    )
    graph.edges["support"] = Edge(
        id="support", source="result", target="claim", relation="supports"
    )
    artifact = live.create([{"kind": "node", "id": "claim"}])
    snapshot = read(live, artifact).snapshots[0]
    assert snapshot.id == "claim" and snapshot.type == "hypothesis"
    assert [(item.id, item.stance) for item in snapshot.evidence] == [("result", "supports")]
    del graph.nodes["claim"]
    assert not read(live, artifact).complete


def test_discovery_from_real_branch_service_resolves_own_node(manifest, tmp_path):
    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution
    from rcp.runs.chat import _discover_chat_artifacts
    from tests.test_branch_chats import _app_branch

    app, main, episode, task = _app_branch(manifest, tmp_path)
    store = app.state.catalog.store
    branch = main.for_graph_target(episode.graph_target)
    directory = tmp_path.resolve() / "artifacts"
    directory.mkdir()
    (directory / "live.html").write_text(
        '<html><script type="application/json" id="rcp-live">{"version":1,"needs":[{"kind":"node","id":"rq/learning-after-shift"}]}</script></html>'
    )
    execution = AgentTaskExecution(
        operation_id=task.operation_id, store=store, control=AgentProcessControl()
    )
    found = _discover_chat_artifacts(execution, task.operation_id, directory, None, service=branch)
    assert len(found) == 1
    artifact = store.artifact(found[0].artifact_id)
    binding = store.artifact_versions(artifact.artifact_id)[0].live
    assert binding.invalid_reason is None
    assert binding.graph_branch_id == episode.graph_target.branch_id
    snapshot = artifact_live_snapshot(
        store, app.state.catalog, artifact.artifact_id, artifact.current_version
    )
    assert snapshot.complete and snapshot.snapshots[0].id == "rq/learning-after-shift"


@pytest.mark.parametrize("supplier", ["turn", "episode_ending"])
@pytest.mark.parametrize("saved", [False, True])
def test_disabled_rule_revokes_existing_live_bindings_and_saved_final(live, saved, supplier):
    job(live)
    artifact = live.create([{"kind": "job", "key": "train"}], supplier=supplier)
    if saved:
        capture(live)
        assert read(live, artifact).final
    disabled = artifact.model_copy(update={"live_data_allowed": False})
    with live.store.connection() as connection:
        connection.execute(
            "UPDATE artifacts SET metadata=? WHERE artifact_id=?",
            (disabled.model_dump_json(), artifact.artifact_id),
        )
    capture(live)
    assert bool(version(live, artifact).live_snapshot) == saved
    assert read(live, artifact).static
    assert not read(live, artifact).snapshots
    resolve_artifact_live_version(
        live.store, live.service, artifact.artifact_id, artifact.current_version
    )
    assert version(live, artifact).live.invalid_reason


@pytest.mark.parametrize(
    ("format", "contents", "expected"),
    [
        ("jsonl", '{"loss": 1}\n{"loss":', [{"loss": 1}]),
        ("csv", 'step,loss\n1,"part', [["step", "loss"]]),
    ],
)
def test_structured_file_ignores_in_progress_last_line(live, format, contents, expected):
    metrics = live.root / f"metrics.{format}"
    metrics.write_text(contents)
    artifact = live.create([file_need(metrics, format=format)])
    snapshot = read(live, artifact).snapshots[0]
    assert snapshot.rows == expected
    assert snapshot.truncated and snapshot.error is None


def test_final_capture_stops_after_bounded_attempts(live, monkeypatch):
    from rcp.limits import LIVE_ARTIFACT_MAX_CAPTURE_ATTEMPTS

    job(live)
    artifact = live.create([{"kind": "job", "key": "train"}, file_need(live.root / "absent")])
    for _ in range(LIVE_ARTIFACT_MAX_CAPTURE_ATTEMPTS):
        capture(live)
        saved = version(live, artifact)
        if saved.live.next_capture_at:
            later = saved.live.next_capture_at
            monkeypatch.setattr(live.store, "now", lambda later=later: later)
    capture(live)
    final = version(live, artifact).live
    assert final.capture_attempts == LIVE_ARTIFACT_MAX_CAPTURE_ATTEMPTS
    assert final.capture_error and final.next_capture_at is None


@pytest.mark.parametrize(
    "revocation",
    [
        "UPDATE graph_runs SET history_only=1",
        "UPDATE graph_runs SET stage_host='changed-host'",
        "UPDATE graph_runs SET project_id='other'",
        "UPDATE graph_runs SET episode_id='other'",
        "UPDATE compute_jobs SET origin_operation_id='other'",
    ],
)
def test_revoked_sources_refuse_reads_and_final_capture(live, revocation):
    job(live)
    artifact = live.create([{"kind": "job", "key": "train"}])
    with live.store.connection() as connection:
        connection.execute(revocation)
    with pytest.raises(ValueError):
        read(live, artifact)
    capture(live)
    final = version(live, artifact).live
    assert final.invalid_reason and final.capture_error and final.next_capture_at is None
    capture(live)
    assert version(live, artifact).live.capture_attempts == 1


def test_expired_artifact_does_not_capture(live):
    job(live)
    artifact = live.create([{"kind": "job", "key": "train"}])
    expired = artifact.model_copy(
        update={
            "expires_at": (
                datetime.fromisoformat(live.store.now()) - timedelta(seconds=1)
            ).isoformat()
        }
    )
    with live.store.connection() as connection:
        connection.execute(
            "UPDATE artifacts SET metadata=? WHERE artifact_id=?",
            (expired.model_dump_json(), artifact.artifact_id),
        )
    capture(live)
    saved = version(live, artifact)
    assert saved.live.capture_attempts == 0 and saved.live_snapshot is None


def test_final_capture_reads_outside_lock_and_preserves_concurrent_snapshot(live, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    import rcp.live_artifact_runtime as runtime

    job(live)
    artifact = live.create([{"kind": "job", "key": "train"}])
    original = runtime._snapshot
    concurrent = read(live, artifact)
    concurrent.final = True
    concurrent.snapshots[0].log_tail = "concurrent capture"

    def capture_concurrently(*args, **kwargs):
        def save():
            with live.store.artifact_lock(artifact.artifact_id):
                live.store.save_artifact_live_snapshot(
                    artifact.artifact_id,
                    artifact.current_version,
                    data=concurrent.model_dump_json().encode(),
                )

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            pool.submit(save).result(timeout=5)
        finally:
            pool.shutdown(wait=False)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "_snapshot", capture_concurrently)
    capture(live)
    assert read(live, artifact).snapshots[0].log_tail == "concurrent capture"


def test_edit_produced_live_page_retains_episode_provenance(live):
    owner = episode(live)
    with live.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=? WHERE operation_id='turn'",
            (json.dumps({"artifact_edit": {"episode_id": owner.episode_id}}),),
        )
    metrics = live.root / "extra.jsonl"
    metrics.write_text('{"loss":1}\n')
    artifact = live.create([file_need(metrics), {"kind": "episode"}], episode_id=owner.episode_id)
    assert version(live, artifact).live.invalid_reason is None
    snapshot = read(live, artifact)
    assert snapshot.complete and snapshot.snapshots[0].rows == [{"loss": 1}]
    assert snapshot.snapshots[1].state == "queued"
    assert live.store.agent_task("turn").episode_id is None
    assert live.store.episode_tasks(owner.episode_id) == []


@pytest.mark.parametrize(
    ("format", "contents", "expected"),
    [("jsonl", '{"loss": 1}', [{"loss": 1}]), ("csv", "1,2", [["1", "2"]])],
)
def test_final_capture_preserves_complete_unterminated_record(live, format, contents, expected):
    job(live)
    metrics = live.root / f"metrics.{format}"
    metrics.write_text(contents)
    artifact = live.create([{"kind": "job", "key": "train"}, file_need(metrics, format=format)])
    if format == "jsonl":
        assert read(live, artifact).snapshots[1].rows == expected
    capture(live)
    snapshot = read(live, artifact)
    assert snapshot.final and snapshot.complete
    assert snapshot.snapshots[1].rows == expected
    assert not snapshot.snapshots[1].truncated


def test_running_live_version_never_opens_project_during_reconcile(live):
    job(live)
    artifact = live.create([{"kind": "job", "key": "train"}])
    with live.store.connection() as connection:
        connection.execute("UPDATE compute_jobs SET status='running', ended_at=NULL")

    def refuse_open(project_id):
        pytest.fail("Unended live version opened the project")

    live.catalog.open = refuse_open
    capture(live)
    saved = version(live, artifact)
    assert saved.live.capture_attempts == 0
    assert saved.live_snapshot is None


def files_need(directory, pattern="*/task-*/status.json", **options):
    return (
        dict(kind="files", dir=str(directory), pattern=pattern, read="whole", format="text")
        | options
    )


def test_folder_discovers_later_batches_without_rebinding(live):
    artifact = live.create([files_need(live.root)])
    initial = read(live, artifact)
    assert initial.complete and initial.snapshots[0].files == []
    for n in range(80):
        path = live.root / f"arm-{n // 10}" / f"task-{n % 10}" / "status.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"state":"done"}\n')
    snapshot = read(live, artifact).snapshots[0]
    assert len(snapshot.files) == 80
    assert [f.path for f in snapshot.files] == sorted(f.path for f in snapshot.files)
    assert all(f.rows == ['{"state":"done"}'] for f in snapshot.files)
    capture(live)
    assert version(live, artifact).live_snapshot is None
    live.service.manifest.repositories = []
    with pytest.raises(ValueError):
        read(live, artifact)


def test_folder_outside_roots_is_refused(live, tmp_path):
    artifact = live.create([files_need(tmp_path)])
    assert read(live, artifact).static
    assert version(live, artifact).live.invalid_reason


def test_folder_skips_symlinks_and_nonregular_entries(live):
    root = live.root / "results"
    root.mkdir()
    real = root / "real"
    real.mkdir()
    (real / "status.json").write_text("safe\n")
    (root / "linked-dir").symlink_to(real, target_is_directory=True)
    (real / "linked-file").symlink_to(real / "status.json")
    os.mkfifo(real / "fifo")
    artifact = live.create([files_need(root, "*/*")])
    snapshot = read(live, artifact)
    assert snapshot.complete
    assert [(f.path, f.rows) for f in snapshot.snapshots[0].files] == [
        ("real/status.json", ["safe"])
    ]


@pytest.mark.parametrize("cap", ["matches", "total_bytes", "file_bytes", "rows"])
def test_folder_caps_report_omitted_content(live, monkeypatch, cap):
    import rcp.live_artifact_runtime as runtime

    for name in ("a", "b", "c"):
        (live.root / name).write_text("one\ntwo\n")
    setting, value = {
        "matches": ("LIVE_ARTIFACT_MAX_FILES", 2),
        "total_bytes": ("LIVE_ARTIFACT_MAX_TOTAL_BYTES", 8),
        "file_bytes": ("LIVE_ARTIFACT_MAX_BYTES", 4),
        "rows": ("LIVE_ARTIFACT_MAX_ROWS", 1),
    }[cap]
    monkeypatch.setattr(runtime, setting, value)
    artifact = live.create([files_need(live.root, "*")])
    snapshot = read(live, artifact).snapshots[0]
    assert snapshot.truncated
    if cap == "matches":
        assert [f.path for f in snapshot.files] == ["a", "b"]
    if cap == "total_bytes":
        assert len(snapshot.files) == 1 and snapshot.files[0].rows == ["one", "two"]
    if cap in {"file_bytes", "rows"}:
        assert all(f.truncated and f.rows == ["one"] for f in snapshot.files)


@pytest.mark.parametrize("failure", ["missing_dir", "invalid_file"])
def test_folder_errors_prevent_final_capture(live, failure):
    job(live)
    root = live.root / "results"
    if failure == "invalid_file":
        root.mkdir()
        (root / "bad").write_bytes(b"\xff")
        (root / "good").write_text("available\n")
    artifact = live.create([{"kind": "job", "key": "train"}, files_need(root, "*")])
    snapshot = read(live, artifact)
    assert not snapshot.complete and snapshot.snapshots[1].error
    if failure == "invalid_file":
        bad, good = snapshot.snapshots[1].files
        assert bad.error and good.rows == ["available"] and not good.error
    capture(live)
    assert version(live, artifact).live_snapshot is None
    assert version(live, artifact).live.capture_error


def test_remote_folder_uses_one_shipped_reader_call_per_snapshot(live, monkeypatch):
    from rcp.transport import RemoteRunStage

    (live.root / "status.json").write_text("ready\n")
    host = "execution-host"
    repo = live.service.manifest.repositories[0]
    live.service.manifest.machine_map[repo.machine].host = host
    with live.store.connection() as connection:
        connection.execute("UPDATE graph_runs SET stage_host=? WHERE operation_id='turn'", (host,))
    calls = []

    def ssh(self, arguments, **kwargs):
        calls.append(arguments)
        return subprocess.run([sys.executable, *arguments[1:]], capture_output=True, timeout=5)

    monkeypatch.setattr(RemoteRunStage, "_ssh_bytes", ssh)
    artifact = live.create([files_need(live.root, "*.json")])
    assert not calls
    for count in (1, 2):
        snapshot = read(live, artifact)
        assert snapshot.complete and snapshot.snapshots[0].files[0].rows == ["ready"]
        assert len(calls) == count
