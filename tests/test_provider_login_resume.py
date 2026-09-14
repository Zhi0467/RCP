from __future__ import annotations

import shutil
import subprocess
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rcp.agents import AgentEvent, AgentLauncher
from rcp.agents import launcher as launcher_module
from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.api import provider_login
from rcp.api.dependencies import get_launcher, get_provider_sign_ins, get_store
from rcp.background import BackgroundAgentTasks
from rcp.core.transition_models import GraphHeadRef
from rcp.runs.auto_research import AutoResearchStartRequest
from rcp.runs.auto_research_admission import start_auto_research
from rcp.runs.auto_research_recovery import reconcile_due_auto_research_recoveries
from rcp.runs.provider_sign_in import ProviderSignInRunner

from .helpers import fabricated_authorizer, wait_for_task
from .test_auto_research_recovery import (
    _install_recovery_callback,
    _sse,
    _store,
    _wait_for_recovery,
)


def _resume_app(store, background, monkeypatch):
    # Exercise the same composed episode pass the timer and sign-in share.
    from rcp.api import app as app_module

    callback = background.on_task_settled
    with monkeypatch.context() as patch:
        patch.setattr(app_module, "AppStore", lambda _: store)
        patch.setattr(app_module, "BackgroundAgentTasks", lambda *_, **__: background)
        composed = app_module.create_app(data_dir=store.path.parent / "resume-app")
    background.on_task_settled = callback
    app = FastAPI()
    app.include_router(provider_login.router)
    app.state.services = composed.state.services
    return app


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

    ProviderCredentialStore.for_data_dir(store.path.parent).store_token(
        "claude", "", "test-token", member_id="member", now=store.now()
    )
    background = BackgroundAgentTasks(store, stream)
    _install_recovery_callback(background)
    roots = []
    for project_id, provider in [("project", "codex"), ("other", "claude")]:
        _, root = start_auto_research(
            background,
            project_id,
            AutoResearchStartRequest(
                invocation_ceiling=4,
                run_truth_scope=["repo"],
                provider=provider,
                run_on="laptop",
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

    monkeypatch.setattr(
        launcher_module, "_discover_local_provider", lambda _provider: "/test/codex"
    )
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
    app = _resume_app(store, background, monkeypatch)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_launcher] = lambda: launcher
    runner = ProviderSignInRunner(
        store, launcher, ProviderCredentialStore.for_data_dir(store.path.parent)
    )
    runner.resume_account = app.state.services.provider_sign_ins.resume_account
    app.dependency_overrides[get_provider_sign_ins] = lambda: runner
    client = TestClient(app)

    response = client.post("/api/providers/codex/logins/verify", json={"host": ""})
    assert response.status_code == 200, response.text
    assert response.json()["resumed"]["checked"] >= 1
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
    assert again.json()["resumed"]["checked"] >= 1
    assert calls.count(("project", "retry")) == 1
    assert calls.count(("other", "retry")) == 0


def _verify_client(store, background, monkeypatch):
    monkeypatch.setattr(
        launcher_module, "_discover_local_provider", lambda _provider: "/test/codex"
    )
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
    app = _resume_app(store, background, monkeypatch)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_launcher] = lambda: launcher
    runner = ProviderSignInRunner(
        store, launcher, ProviderCredentialStore.for_data_dir(store.path.parent)
    )
    runner.resume_account = app.state.services.provider_sign_ins.resume_account
    app.dependency_overrides[get_provider_sign_ins] = lambda: runner
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
    assert response.json()["resumed"]["checked"] >= 1
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
    assert again.json()["resumed"]["checked"] >= 1
    assert calls == ["fresh", "retry"]
    assert store.provider_login_state("codex", "other-machine") == remote


def test_verify_resumes_parked_report_allocation_once(manifest, tmp_path, monkeypatch):
    from rcp.runs.episodes.report import start_episode_report
    from rcp.runs.tasks.episode_report import stream_episode_report_run

    from .helpers import wait_until
    from .test_episode_report import _ReportLauncher, _setup_report

    service, store, request, _execution, _stage = _setup_report(manifest, tmp_path)
    # The report fixture omits the mode adapter; its admitted visible turn is finished.
    store.complete_agent_task("operation", applied_revision=None, result={})
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
    assert response.json()["resumed"]["checked"] >= 1
    wait_for_task(store, report.operation_id, expect="succeeded")
    wait_until(lambda: report.operation_id not in background._workers)
    assert store.episode(request.episode_id).wrapup_state == "ready"
    assert store.episode(request.episode_id).report_attempts_used == 2
    assert report_launcher.calls == 2
    again = client.post("/api/providers/codex/logins/verify", json={"host": ""})
    assert again.status_code == 200, again.text
    assert again.json()["resumed"]["checked"] >= 1
    assert report_launcher.calls == 2
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == task_count


@pytest.mark.parametrize("run_on", ["removed-alias", "laptop"])
def test_verify_matches_failed_tasks_by_frozen_host_with_stale_alias(
    manifest, tmp_path, monkeypatch, run_on
):
    import hashlib

    from .helpers import wait_until
    from .test_background import _experiment_request

    store = _store(tmp_path)
    store.upsert_project(
        store.project("project").model_copy(update={"locator": str(manifest.path)})
    )

    async def stream(_project, _kind, _request, execution):
        if execution.continuation == "fresh":
            stage = tmp_path / execution.operation_id
            stage.mkdir()
            execution.checkpoint_stage("", str(stage))
            store.record_agent_task_contract(
                execution.operation_id,
                "experiment_episode_context_candidate",
                "{}",
                hashlib.sha256(b"{}").hexdigest(),
            )
        yield _sse(
            AgentEvent(event="session", session_id=_request.session_id or execution.operation_id)
        )
        if execution.continuation == "fresh":
            yield _sse(AgentEvent(event="error", text="refresh_token_reused"))
        else:
            yield _sse(AgentEvent(event="done"))

    original_manifest = manifest.path.read_text()
    if run_on == "removed-alias":
        manifest.path.write_text(original_manifest.replace("laptop", "removed-alias"))
    background = BackgroundAgentTasks(store, stream)
    roots = []
    for index in range(2):
        store.mark_provider_login_verified("codex", "", member_id="member", detail="Verified.")
        request = _experiment_request().model_copy(
            update={
                "run_on": run_on,
                "control_episode_id": f"00000000-0000-4000-8000-00000000050{index}",
                "control_node_id": f"exp/target-{index}",
                "node_id": f"exp/target-{index}",
                "chat_id": f"chat-{index}",
            }
        )
        task = background.start(
            "project", "node_chat", request, authorized_by=fabricated_authorizer()
        )
        roots.append(wait_for_task(store, task.operation_id, expect="failed"))
        wait_until(lambda operation_id=task.operation_id: operation_id not in background._workers)
    manifest.path.write_text(original_manifest)
    store.checkpoint_agent_task(
        roots[1].operation_id, stage_host="other-host", stage_root=str(tmp_path / "other-stage")
    )
    client = _verify_client(store, background, monkeypatch)
    response = client.post("/api/providers/codex/logins/verify", json={"host": ""})
    assert response.status_code == 200, response.text
    assert response.json()["resumed"]["checked"] >= 1
    children = [
        task
        for task in store.episode_tasks(roots[0].episode_id)
        if task.parent_operation_id == roots[0].operation_id
    ]
    assert len(children) == 1
    wait_for_task(store, children[0].operation_id, expect="succeeded")
    wait_until(lambda: children[0].operation_id not in background._workers)
    assert len(store.episode_tasks(roots[1].episode_id)) == 1


def test_verify_matches_watchers_by_frozen_host_not_alias(manifest, tmp_path, monkeypatch):
    from .test_auto_research_delivery import _arm_completed_graph_condition
    from .test_provider_login_admission import _idle_root

    background, episode, root = _idle_root(tmp_path)
    store = background.store
    store.upsert_project(
        store.project("project").model_copy(update={"locator": str(manifest.path)})
    )
    watcher = _arm_completed_graph_condition(store, episode, root)
    # The alias is gone from the manifest, but the watcher froze the local account.
    stale = watcher.model_copy(
        update={"continuation": watcher.continuation.model_copy(update={"run_on": "removed-alias"})}
    )
    # Frozen to another account: the verified local login must not touch it.
    elsewhere = watcher.model_copy(
        update={"watcher_id": "elsewhere", "execution_host": "other.example"}
    )
    monkeypatch.setattr(store, "completed_watcher_groups", lambda: [[stale], [elsewhere]])
    client = _verify_client(store, background, monkeypatch)
    delivered = []
    monkeypatch.setattr(
        client.app.state.services.watcher_delivery,
        "deliver_watcher_group",
        lambda group: delivered.append(group),
    )
    response = client.post("/api/providers/codex/logins/verify", json={"host": ""})
    assert response.status_code == 200, response.text
    assert [group[0].watcher_id for group in delivered] == [watcher.watcher_id]
