from __future__ import annotations

import shutil
import subprocess
from contextlib import nullcontext
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rcp.agents import AgentEvent, AgentLauncher
from rcp.api import provider_login
from rcp.api.dependencies import get_launcher, get_store
from rcp.background import BackgroundAgentTasks
from rcp.core.transition_models import GraphHeadRef
from rcp.runs.auto_research import AutoResearchStartRequest
from rcp.runs.auto_research_admission import start_auto_research
from rcp.runs.auto_research_recovery import reconcile_due_auto_research_recoveries

from .helpers import fabricated_authorizer, wait_for_task
from .test_auto_research_recovery import (
    _install_recovery_callback,
    _sse,
    _store,
    _wait_for_recovery,
)


def test_verify_releases_and_claims_only_this_account_once(manifest, tmp_path, monkeypatch):
    store = _store(tmp_path)
    project = store.project("project")
    store.upsert_project(project.model_copy(update={"locator": str(manifest.path)}))
    other_manifest = tmp_path / "other-manifest.toml"
    shutil.copy2(manifest.path, other_manifest)
    store.upsert_project(
        project.model_copy(update={"project_id": "other", "locator": str(other_manifest)})
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    calls = []

    async def stream(project_id, _kind, request, execution):
        calls.append((project_id, execution.continuation))
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id=f"{project_id}-session"))
        if execution.continuation == "fresh":
            yield _sse(AgentEvent(event="error", text="refresh_token_reused"))
        else:
            yield _sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(background)
    roots = []
    for project_id, provider in [("project", "codex"), ("other", "claude")]:
        _, root = start_auto_research(
            background,
            project_id,
            AutoResearchStartRequest(
                invocation_ceiling=4, run_truth_scope=["repo"], provider=provider
            ),
            authorized_by=fabricated_authorizer(),
            graph_base_head=GraphHeadRef(revision=0),
            ensure_graph_target=lambda _episode: None,
            episode_id=f"episode-{project_id}",
            operation_id=f"root-{project_id}",
        )
        roots.append(wait_for_task(store, root.operation_id, expect="failed"))
        _wait_for_recovery(store, f"task:root-{project_id}")
    # Claude has no observed signatures. Construct the already-classified recovery
    # at its storage boundary so the route's account isolation is tested separately.
    store.schedule_auto_research_task_recovery(
        roots[1].operation_id,
        failure_kind="provider_auth",
        retry_mode="blocked",
        diagnostic="signed out",
    )
    store.mark_provider_login_failed("claude", "", generation=0, detail="signed out", source="turn")
    blocked_other = store.auto_research_recovery("task:root-other")
    assert blocked_other.status == "blocked"
    assert store.auto_research_recovery("task:root-project").status == "blocked"

    launcher = AgentLauncher(login_state=store.provider_login_state)
    monkeypatch.setattr(launcher.credential_gate, "hold_blocking", lambda *_: nullcontext())
    monkeypatch.setattr(
        launcher,
        "_probe",
        lambda _host, command, **_: subprocess.CompletedProcess(command, 0, "OK", ""),
    )
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: SimpleNamespace(user_id="member")),
    )
    monkeypatch.setattr(provider_login, "get_background_tasks", lambda _: background)
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_launcher] = lambda: launcher
    client = TestClient(app)

    response = client.post("/api/providers/codex/logins/verify", json={"host": ""})
    assert response.status_code == 200, response.text
    assert response.json()["resumed"]["recoveries"] == 1
    assert response.json()["state"]["generation"] == 1
    assert response.json()["state"]["changed_by"] == "member"
    recovery = store.auto_research_recovery("task:root-project")
    child = wait_for_task(store, recovery.admitted_operation_id, expect="succeeded")
    assert child.parent_operation_id == roots[0].operation_id
    assert store.episode(child.episode_id).invocations_used == 1
    assert store.auto_research_recovery("task:root-other") == blocked_other
    assert store.provider_login_state("claude", "").state == "signed_out"
    assert reconcile_due_auto_research_recoveries(background) == 0
    again = client.post("/api/providers/codex/logins/verify", json={"host": ""})
    assert again.status_code == 200, again.text
    assert again.json()["resumed"]["recoveries"] == 0
    assert calls.count(("project", "retry")) == 1
    assert calls.count(("other", "retry")) == 0


def _verify_client(store, background, monkeypatch):
    launcher = AgentLauncher(login_state=store.provider_login_state)
    monkeypatch.setattr(launcher.credential_gate, "hold_blocking", lambda *_: nullcontext())
    monkeypatch.setattr(
        launcher,
        "_probe",
        lambda _host, command, **_: subprocess.CompletedProcess(command, 0, "OK", ""),
    )
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: SimpleNamespace(user_id="member")),
    )
    monkeypatch.setattr(provider_login, "get_background_tasks", lambda _: background)
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_launcher] = lambda: launcher
    return TestClient(app)


def test_verify_retries_failed_experiment_once(manifest, tmp_path, monkeypatch):
    import hashlib

    from .helpers import wait_until
    from .test_background import _experiment_request

    store = _store(tmp_path)
    store.upsert_project(
        store.project("project").model_copy(update={"locator": str(manifest.path)})
    )
    stage = tmp_path / "experiment-stage"
    stage.mkdir()
    calls = []

    async def stream(_project, _kind, _request, execution):
        calls.append(execution.continuation)
        execution.checkpoint_stage("", str(stage))
        if execution.continuation == "fresh":
            store.record_agent_task_contract(
                execution.operation_id,
                "experiment_episode_context_candidate",
                "{}",
                hashlib.sha256(b"{}").hexdigest(),
            )
        yield _sse(AgentEvent(event="session", session_id="experiment-session"))
        if execution.continuation == "fresh":
            yield _sse(AgentEvent(event="error", text="refresh_token_reused"))
        else:
            yield _sse(AgentEvent(event="done"))

    background = BackgroundAgentTasks(store, stream)
    root = background.start(
        "project",
        "node_chat",
        _experiment_request(),
        authorized_by=fabricated_authorizer(),
    )
    root = wait_for_task(store, root.operation_id, expect="failed")
    wait_until(lambda: root.operation_id not in background._workers)
    remote = store.mark_provider_login_failed(
        "codex", "other-machine", generation=0, detail="signed out", source="turn"
    )
    client = _verify_client(store, background, monkeypatch)
    response = client.post("/api/providers/codex/logins/verify", json={"host": ""})
    assert response.status_code == 200, response.text
    assert response.json()["resumed"]["experiments"] == 1
    children = [
        task
        for task in store.episode_tasks(root.episode_id)
        if task.parent_operation_id == root.operation_id
    ]
    assert len(children) == 1
    wait_for_task(store, children[0].operation_id, expect="succeeded")
    wait_until(lambda: children[0].operation_id not in background._workers)
    assert store.episode(root.episode_id).invocations_used == 1
    again = client.post("/api/providers/codex/logins/verify", json={"host": ""})
    assert again.status_code == 200, again.text
    assert again.json()["resumed"]["experiments"] == 0
    assert calls == ["fresh", "retry"]
    assert store.provider_login_state("codex", "other-machine") == remote


def test_verify_resumes_parked_report_allocation_once(manifest, tmp_path, monkeypatch):
    from rcp.runs.episodes.report import start_episode_report
    from rcp.runs.tasks.episode_report import stream_episode_report_run

    from .helpers import wait_until
    from .test_episode_report import _ReportLauncher, _setup_report

    service, store, request, _execution, _stage = _setup_report(manifest, tmp_path)
    report_launcher = _ReportLauncher(["auth", "valid"])

    async def stream(_project, _kind, report_request, execution):
        async for event in stream_episode_report_run(
            service, report_launcher, report_request, execution
        ):
            yield event

    background = BackgroundAgentTasks(store, stream)
    report = start_episode_report(background, request.episode_id)
    wait_for_task(store, report.operation_id, expect="failed")
    wait_until(lambda: report.operation_id not in background._workers)
    assert store.episode(request.episode_id).wrapup_state == "pending"
    with store.connection() as connection:
        task_count = connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0]
    client = _verify_client(store, background, monkeypatch)
    response = client.post("/api/providers/codex/logins/verify", json={"host": ""})
    assert response.status_code == 200, response.text
    assert response.json()["resumed"]["reports"] == 1
    wait_for_task(store, report.operation_id, expect="succeeded")
    wait_until(lambda: report.operation_id not in background._workers)
    assert store.episode(request.episode_id).wrapup_state == "ready"
    assert store.episode(request.episode_id).report_attempts_used == 2
    assert report_launcher.calls == 2
    again = client.post("/api/providers/codex/logins/verify", json={"host": ""})
    assert again.status_code == 200, again.text
    assert again.json()["resumed"]["reports"] == 0
    assert report_launcher.calls == 2
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == task_count
