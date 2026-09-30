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

    def create(needs, *, artifact_id="page", supplier="turn", live_data_allowed=True):
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
                episode_id=store.agent_task(task.operation_id).episode_id,
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
    reconcile_artifact_live_snapshots(live.store, live.catalog, "project")
    assert (
        live.store.read_artifact_live_snapshot(artifact.artifact_id, artifact.current_version)
        is None
    )
    live.service.manifest.repositories = []
    with pytest.raises(ValueError, match="readable roots"):
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
    reconcile_artifact_live_snapshots(live.store, live.catalog, "project")
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


def test_incomplete_server_capture_has_durable_backoff_then_retries(live, monkeypatch):
    job(live)
    metrics = live.root / "missing.jsonl"
    artifact = live.create([{"kind": "job", "key": "train"}, file_need(metrics)])
    reconcile_artifact_live_snapshots(live.store, live.catalog, "project")
    version = live.store.artifact_versions(artifact.artifact_id)[0]
    assert version.live_snapshot is None
    assert version.live.capture_attempts == 1 and version.live.capture_error
    assert not read(live, artifact).complete
    metrics.write_text('{"loss":0.1}\n')
    reconcile_artifact_live_snapshots(live.store, live.catalog, "project")
    assert live.store.artifact_versions(artifact.artifact_id)[0].live.capture_attempts == 1
    later = (
        datetime.fromisoformat(version.live.next_capture_at) + timedelta(seconds=1)
    ).isoformat()
    monkeypatch.setattr(live.store, "now", lambda: later)
    reconcile_artifact_live_snapshots(live.store, live.catalog, "project")
    assert read(live, artifact).final


def test_historical_artifact_cannot_read_live_sources(live):
    metrics = live.root / "metrics.jsonl"
    metrics.write_text("{}\n")
    artifact = live.create([file_need(metrics)])
    with live.store.connection() as connection:
        connection.execute("UPDATE graph_runs SET history_only=1 WHERE operation_id='turn'")
    with pytest.raises(ValueError, match="Historical"):
        read(live, artifact)


def test_tail_caps_and_symlink_refusal(live, monkeypatch):
    monkeypatch.setattr("rcp.live_artifact_runtime.LIVE_ARTIFACT_MAX_BYTES", 12)
    monkeypatch.setattr("rcp.live_artifact_runtime.LIVE_ARTIFACT_MAX_ROWS", 2)
    metrics = live.root / "metrics.txt"
    metrics.write_text("first\nsecond\nthird\nfourth\n")
    artifact = live.create([file_need(metrics, format="text")])
    snapshot = read(live, artifact).snapshots[0]
    assert snapshot.rows == ["fourth"] and snapshot.truncated
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
    from rcp.storage import EpisodeRecord
    from tests.helpers import authorized_human

    now = live.store.now()
    episode = live.store.create_episode(
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
    with live.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET episode_id=? WHERE operation_id='turn'", (episode.episode_id,)
        )
    live.store.end_episode_without_report(episode.episode_id, ending="completed")
    artifact = live.create([{"kind": "episode"}])
    reconcile_artifact_live_snapshots(live.store, live.catalog, "project")
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
    with pytest.raises(ValueError, match="readable roots"):
        read(live, artifact)


def test_graph_target_revalidated_before_saved_final(live):
    job(live)
    artifact = live.create([{"kind": "job", "key": "train"}])
    reconcile_artifact_live_snapshots(live.store, live.catalog, "project")

    def refuse(*args, **kwargs):
        raise ValueError("graph target unavailable")

    live.service.for_graph_target = refuse
    with pytest.raises(ValueError, match="graph target"):
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
    assert snapshot.id == "claim" and snapshot.type == "hypothesis" and snapshot.title == "Claim"
    assert [item.model_dump() for item in snapshot.evidence] == [
        {"id": "result", "title": "Measurement", "stance": "supports"}
    ]
    del graph.nodes["claim"]
    assert not read(live, artifact).complete


def test_changed_execution_host_refuses_unsaved_live_source(live):
    metrics = live.root / "metrics.jsonl"
    metrics.write_text("{}\n")
    artifact = live.create([file_need(metrics)])
    with live.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET stage_host='changed-host' WHERE operation_id='turn'"
        )
    with pytest.raises(ValueError, match="execution host"):
        read(live, artifact)


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


def test_artifact_episode_cannot_substitute_another_task_episode(live):
    artifact = live.create([{"kind": "node", "id": "absent"}])
    # A forged association must fail before opening either episode's sources.
    from rcp.live_artifact_runtime import _owner

    substituted = artifact.model_copy(update={"episode_id": "other-episode"})
    with pytest.raises(ValueError, match="original task episode"):
        _owner(live.store, substituted)


@pytest.mark.parametrize("supplier", ["turn", "episode_ending"])
def test_artifact_rule_disables_live_data_for_every_supplier(live, supplier):
    metrics = live.root / "metrics.jsonl"
    metrics.write_text("{}\n")
    artifact = live.create([file_need(metrics)], supplier=supplier, live_data_allowed=False)
    snapshot = read(live, artifact)
    assert snapshot.static and snapshot.reason
    assert not snapshot.snapshots
    assert live.store.artifact_versions(artifact.artifact_id)[0].live.invalid_reason


@pytest.mark.parametrize("saved", [False, True])
def test_disabled_rule_revokes_existing_live_bindings_and_saved_final(live, saved):
    job(live)
    artifact = live.create([{"kind": "job", "key": "train"}])
    if saved:
        reconcile_artifact_live_snapshots(live.store, live.catalog, "project")
        assert read(live, artifact).final
    disabled = artifact.model_copy(update={"live_data_allowed": False})
    with live.store.connection() as connection:
        connection.execute(
            "UPDATE artifacts SET metadata=? WHERE artifact_id=?",
            (disabled.model_dump_json(), artifact.artifact_id),
        )
    reconcile_artifact_live_snapshots(live.store, live.catalog, "project")
    assert bool(live.store.artifact_versions(artifact.artifact_id)[0].live_snapshot) == saved
    assert read(live, artifact).static
    assert not read(live, artifact).snapshots
    resolve_artifact_live_version(
        live.store, live.service, artifact.artifact_id, artifact.current_version
    )
    assert live.store.artifact_versions(artifact.artifact_id)[0].live.invalid_reason
