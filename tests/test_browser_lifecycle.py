from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from rcp.agents import AgentEvent
from rcp.providers.browser_grant import BrowserGrant, BrowserOwnerKey, BrowserTurnStatus
from rcp.runs import browser_runtime_seam
from rcp.runs.browser_lifecycle import (
    acquire_turn_browser,
    browser_host_key,
    browser_turn,
    close_chat_browser_owners,
    retry_browser_cleanup,
)
from rcp.runs.shared import _ProviderOutcome, _stream_agent_events
from rcp.storage import AppStore

from .test_browser_host import host as host
from .test_discuss_recorded_finalization import _discuss_app


def test_owner_is_stable_namespaced_and_frozen():
    owner = BrowserOwnerKey(space_id="s", project_id="p", stage_name="a/b", host_key="h")
    assert owner.token() == BrowserOwnerKey.model_validate_json(owner.model_dump_json()).token()
    assert owner.token().replace("browser-", "").isalnum()
    for field in type(owner).model_fields:
        assert owner.model_copy(update={field: "other"}).token() != owner.token()
    with pytest.raises(ValidationError):
        owner.host_key = "other"


@pytest.mark.parametrize(
    ("capability", "requested", "status"),
    [
        ("work_auto", True, "unavailable"),
        ("orchestrate", True, "unavailable"),
        ("discuss", True, "unavailable"),
        ("paper_readonly", True, "unavailable"),
        ("scratch_patch", True, "unavailable"),
        ("discuss", False, "not_requested"),
    ],
)
def test_browser_admission_matrix(tmp_path, monkeypatch, capability, requested, status):
    store = AppStore(tmp_path / "app.sqlite3")
    calls = []

    def acquire(owner, *, execution, workspace_dir, data_dir, retained_lease_ids):
        calls.append(owner)
        return BrowserGrant(
            requested=True, status="unavailable", reason_code="runtime_checked", owner=owner
        )

    monkeypatch.setattr(browser_runtime_seam, "acquire_browser_grant", acquire)
    grant = acquire_turn_browser(
        requested=requested,
        capability=capability,
        store=store,
        project_id="project",
        stage_root="/stage/chat",
        execution_host="",
        execution=None,
        workspace_dir="/stage/chat/workspace",
        chat_id="chat",
    )
    assert grant.status == status
    assert len(calls) == int(requested and capability in {"work_auto", "orchestrate", "discuss"})
    if calls:
        assert grant.reason_code == "runtime_checked"
        assert calls[0].stage_name == "chat"


@pytest.mark.asyncio
async def test_stream_finalizes_loss_after_provider_exception_and_closes_after_toggle(
    tmp_path, monkeypatch
):
    _service, request, execution = _discuss_app(tmp_path)
    store = execution.store
    task = store.agent_task(execution.operation_id)
    request.browser_requested = True
    request.provider = "claude"
    store.set_chat_browser_requested(task.project_id, request.chat_id, browser_requested=True)
    workspace = tmp_path / "stage" / "workspace"
    workspace.mkdir(parents=True)
    execution.checkpoint_stage("", str(workspace.parent))
    calls = []

    def acquire(owner, *, execution, workspace_dir, data_dir, retained_lease_ids):
        return BrowserGrant(
            requested=True,
            status="granted",
            owner=owner,
            lease_id="lease",
            session_name=owner.token(),
            invocation_dir=workspace_dir,
            path_prefix="/tools/bin",
            env={"PLAYWRIGHT_CLI_SESSION": owner.token()},
        )

    def finish(grant, **kwargs):
        calls.append(grant)
        return BrowserTurnStatus(status="lost", reason_code="session_lost")

    class Launcher:
        async def stream(self, provider, prompt, *, browser_grant, **kwargs):
            assert browser_grant.status == "granted"
            store.set_chat_browser_requested(
                task.project_id, request.chat_id, browser_requested=False
            )
            yield AgentEvent(event="session", session_id="session")
            raise OSError("provider stopped")

    closed = []
    monkeypatch.setattr(browser_runtime_seam, "acquire_browser_grant", acquire)
    monkeypatch.setattr(browser_runtime_seam, "finish_browser_grant", finish)
    monkeypatch.setattr(
        browser_runtime_seam, "close_browser_owner", lambda owner, **kw: closed.append(kw)
    )
    with pytest.raises(OSError):
        async with browser_turn(
            request,
            workspace=workspace,
            execution_host="",
            execution=execution,
            remote_stage=None,
            capability="discuss",
        ) as grant:
            async for _ in _stream_agent_events(
                Launcher(),
                request,
                "turn",
                workspace=workspace,
                session_id=None,
                read_dirs=[],
                write_dirs=[],
                write_scope=None,
                execution_host="",
                execution=execution,
                remote_stage=None,
                capability="discuss",
                outcome=_ProviderOutcome(),
                binary=None,
                browser_grant=grant,
            ):
                pass
    assert len(calls) == 1
    assert store.browser_turn_status(execution.operation_id).status == "lost"
    assert closed == [
        {
            "execution": None,
            "delete_profile": True,
            "data_dir": store.path.parent,
            "retained_lease_ids": [],
        }
    ]
    assert store.browser_owners(task.project_id) == []


@pytest.mark.parametrize("legacy_workspace", [False, True])
def test_archive_cleanup_is_deferred_and_unreachable_cleanup_is_retained(
    tmp_path, monkeypatch, legacy_workspace
):
    _service, request, execution = _discuss_app(tmp_path)
    store = execution.store
    task = store.agent_task(execution.operation_id)
    stage_root = tmp_path / "stage"
    workspace = stage_root if legacy_workspace else stage_root / "workspace"
    execution.checkpoint_stage("", str(stage_root))
    owner = BrowserOwnerKey(
        space_id=store.space_id,
        project_id=task.project_id,
        stage_name="stage",
        host_key=browser_host_key(""),
    )
    store.record_browser_owner(
        owner,
        execution_host="",
        workspace_dir=str(workspace),
        stage_root=str(stage_root),
        chat_id=request.chat_id,
    )
    closed = []
    monkeypatch.setattr(
        browser_runtime_seam, "close_browser_owner", lambda owner, **kw: closed.append(kw)
    )
    close_chat_browser_owners(store, task.project_id, request.chat_id, delete_profile=False)
    assert closed == []
    retry_browser_cleanup(store, finished_operation_id=execution.operation_id)
    assert closed == [
        {
            "execution": None,
            "delete_profile": False,
            "data_dir": store.path.parent,
            "retained_lease_ids": [],
        }
    ]
    assert len(store.browser_owners(task.project_id)) == 1

    def unavailable(*args, **kwargs):
        raise OSError("host unreachable")

    monkeypatch.setattr(browser_runtime_seam, "close_browser_owner", unavailable)
    close_chat_browser_owners(store, task.project_id, delete_profile=True)
    retry_browser_cleanup(store, finished_operation_id=execution.operation_id)
    assert store.browser_owners(task.project_id)[0]["close_requested"] == 1


def test_off_cleanup_spares_a_browser_turned_back_on(tmp_path, monkeypatch):
    from rcp.api.chats import _delete_browser_if_still_off

    store = AppStore(tmp_path / "app.sqlite3")
    owner = BrowserOwnerKey(
        space_id=store.space_id, project_id="project", stage_name="chat", host_key="local"
    )
    store.record_browser_owner(
        owner,
        execution_host="",
        workspace_dir=str(tmp_path / "workspace"),
        stage_root=str(tmp_path),
        chat_id="chat",
    )
    store.set_chat_browser_requested("project", "chat", browser_requested=True)
    # The off request read its preference before the re-enable committed.
    monkeypatch.setattr(store, "chat_browser_requested", lambda *_args: False)
    monkeypatch.setattr(browser_runtime_seam, "close_browser_owner", lambda *a, **kw: None)
    _delete_browser_if_still_off(store, "project", "chat")
    assert store.browser_owners("project")[0]["close_requested"] == 0


def test_ssh_owner_identity_resolves_host_and_account(monkeypatch):
    monkeypatch.setattr(
        "rcp.runs.browser_lifecycle.subprocess.run",
        lambda *a, **kw: SimpleNamespace(stdout="hostname host.example\nuser worker\nport 2222\n"),
    )
    assert json.loads(browser_host_key("alias")) == ["host.example", "worker", "2222"]


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("settlement", ["fail", "finalize", "pause"])
async def test_detached_remote_browser_lease_finishes_only_on_settlement(
    tmp_path, monkeypatch, restart, settlement
):
    import asyncio

    from rcp.background import BackgroundAgentTasks
    from rcp.browser.models import SessionCheck, SessionLease
    from rcp.runs.remote_finalization import WaitingTask
    from rcp.runs.remote_reconciliation import Reconciliation
    from rcp.runs.shared import _sse

    from .test_work_agent_io import _recorded_pass

    _, request, execution = _discuss_app(tmp_path)
    store = execution.store
    task = store.agent_task(execution.operation_id)
    root = tmp_path / "stage"
    execution.checkpoint_stage("remote", str(root))
    request.browser_requested = True
    store.set_chat_browser_requested(task.project_id, request.chat_id, browser_requested=True)
    monkeypatch.setattr("rcp.runs.browser_lifecycle.browser_host_key", lambda _: "host")
    lease = SessionLease(
        session_name="live",
        invocation_dir=str(root / "workspace"),
        path_prefix="/tools",
        env={},
        owner_token="owner",
        lease_id="original-lease",
        host="remote",
        data_dir=str(store.path.parent),
    )
    acquired, released, closed = [], [], []

    def ensure(*args, **kwargs):
        acquired.append(lease)
        return lease

    def release(owner_token, *, lease_id, **kwargs):
        released.append(lease_id)
        return SessionCheck(alive=True)

    monkeypatch.setattr(browser_runtime_seam, "ensure_session", ensure)
    monkeypatch.setattr(browser_runtime_seam, "release_session", release)
    monkeypatch.setattr(
        browser_runtime_seam, "close_browser_owner", lambda *a, **kw: closed.append(kw)
    )
    pid_file = str(root / "provider.pid")
    async with browser_turn(
        request,
        workspace=root / "workspace",
        execution_host="remote",
        execution=execution,
        remote_stage=None,
        capability="discuss",
    ):
        store.begin_remote_provider_pass(
            execution.operation_id, "remote", str(root), pid_file, supervised=True
        )
        store.set_chat_browser_requested(task.project_id, request.chat_id, browser_requested=False)
        close_chat_browser_owners(store, task.project_id, request.chat_id, delete_profile=True)
    assert released == []
    assert closed == []
    assert store.browser_turn_status(execution.operation_id).lease_id == lease.lease_id
    store.update_agent_task_message(
        execution.operation_id, "Detached", phase="awaiting_remote_result"
    )
    retry_browser_cleanup(store)
    assert closed == []
    if restart:
        store = AppStore(store.path)

    async def recorded_stream(*args):
        yield _sse(AgentEvent(event="answer", text="Recovered"))
        yield _sse(AgentEvent(event="done"))

    manager = BackgroundAgentTasks(store, None, recorded_stream=recorded_stream)
    monkeypatch.setattr(
        "rcp.runs.remote_finalization.plan_remote_reconciliation",
        lambda _: [
            WaitingTask(
                store.agent_task(execution.operation_id),
                Reconciliation(settlement, "Stopped", pid_file, _recorded_pass("", "Recovered")),
                recorded_stream,
            )
        ],
    )
    if settlement == "pause":
        monkeypatch.setattr(
            "rcp.background.AgentProcessControl.remote_process_state", lambda *_: (False, {})
        )
        monkeypatch.setattr(
            "rcp.background.AgentProcessControl.stop_remote_process", lambda *a, **kw: True
        )
        monkeypatch.setattr(
            "rcp.runs.remote_reconciliation.reconcile_remote_pass",
            lambda *a, **kw: Reconciliation("fail", "Stopped", pid_file),
        )
        result = await asyncio.to_thread(manager.pause, execution.operation_id)
        assert result.status == "paused"
    else:
        assert not await asyncio.to_thread(manager._reconcile_remote_results)
        assert store.agent_task(execution.operation_id).status == (
            "failed" if settlement == "fail" else "succeeded"
        )
    assert acquired == [lease]
    assert released == [lease.lease_id]
    status = store.browser_turn_status(execution.operation_id)
    assert status.status == "granted"
    assert status.reason_code is None
    assert status.lease_id is None
    assert len(closed) == 1
    assert closed[0]["delete_profile"] is True
    assert store.browser_owners(task.project_id) == []


@pytest.mark.parametrize("pending_remote", [False, True])
def test_paused_task_does_not_block_browser_profile_cleanup(tmp_path, monkeypatch, pending_remote):
    _, request, execution = _discuss_app(tmp_path)
    store = execution.store
    root = tmp_path / "stage"
    host = "remote" if pending_remote else ""
    monkeypatch.setattr("rcp.runs.browser_lifecycle.browser_host_key", lambda _: "host")
    execution.checkpoint_stage(host, str(root))
    task = store.agent_task(execution.operation_id)
    owner = BrowserOwnerKey(
        space_id=store.space_id,
        project_id=task.project_id,
        stage_name=root.name,
        host_key="host",
    )
    store.record_browser_owner(
        owner,
        execution_host=host,
        workspace_dir=str(root / "workspace"),
        stage_root=str(root),
        chat_id=request.chat_id,
    )
    pid_file = str(root / "provider.pid")
    if pending_remote:
        store.begin_remote_provider_pass(
            execution.operation_id, host, str(root), pid_file, supervised=True
        )
    store.pause_agent_task(execution.operation_id)
    assert store.agent_task(execution.operation_id).status == "paused"
    closed = []
    monkeypatch.setattr(
        "rcp.runs.provider_process.AgentProcessControl.remote_stopped", lambda *a, **kw: False
    )
    monkeypatch.setattr(
        browser_runtime_seam, "close_browser_owner", lambda *a, **kw: closed.append(kw)
    )
    close_chat_browser_owners(store, task.project_id, request.chat_id, delete_profile=True)
    retry_browser_cleanup(store)
    if pending_remote:
        assert closed == []
        store.finish_remote_provider_pass(execution.operation_id, pid_file)
        retry_browser_cleanup(store)
    assert len(closed) == 1
    assert closed[0]["delete_profile"] is True
    assert closed[0]["data_dir"] == store.path.parent
    assert (closed[0]["execution"].host if pending_remote else closed[0]["execution"]) == (
        host if pending_remote else None
    )
    assert store.browser_owners(task.project_id) == []


@pytest.mark.asyncio
async def test_restart_preserves_pending_browser_and_releases_original_lease(
    host, tmp_path, monkeypatch
):
    from pathlib import PurePosixPath

    from rcp.browser import service
    from rcp.browser.host import UnavailableError
    from rcp.runs.browser_lifecycle import finish_recorded_browser
    from rcp.transport import RemoteRunStage

    epoch = "before"
    calls = []
    release_routes = []

    def invoke(payload, **kwargs):
        calls.append(payload.copy())
        if payload["action"] == "release":
            release_routes.append(kwargs)
        host.request.update(payload, controller_epoch=epoch)
        try:
            return getattr(host, payload["action"])()
        except UnavailableError as exc:
            return {"reason_code": exc.code, "detail": str(exc)}

    monkeypatch.setattr(service, "_invoke", invoke)
    monkeypatch.setattr("rcp.runs.browser_lifecycle.browser_host_key", lambda _: "host")
    _, request, execution = _discuss_app(tmp_path / "app")
    store = execution.store
    task = store.agent_task(execution.operation_id)
    request.browser_requested = True
    store.set_chat_browser_requested(task.project_id, request.chat_id, browser_requested=True)
    root = tmp_path / "stage"
    (root / "workspace").mkdir(parents=True)
    execution.checkpoint_stage("remote", str(root))
    remote = RemoteRunStage("remote")
    remote.root = PurePosixPath(root)
    pid_file = str(root / "provider.pid")
    async with browser_turn(
        request,
        workspace=root / "workspace",
        execution_host="remote",
        execution=execution,
        remote_stage=remote,
        capability="discuss",
    ) as grant:
        store.begin_remote_provider_pass(
            execution.operation_id, "remote", str(root), pid_file, supervised=True
        )
    lease_id = grant.lease_id
    owner = grant.owner.token()
    epoch = "after"
    importlib.reload(browser_runtime_seam)
    store.interrupt_active_agent_tasks()
    store = AppStore(store.path)
    other = acquire_turn_browser(
        requested=True,
        capability="discuss",
        store=store,
        project_id=task.project_id,
        stage_root=str(tmp_path / "other"),
        execution_host="remote",
        execution=remote,
        workspace_dir=str(tmp_path / "other" / "workspace"),
        chat_id=None,
    )
    assert other.reason_code == "capacity"
    assert host.closes == []
    record = json.loads(host.record_path(owner).read_text())
    assert record["leases"][lease_id]["controller_epoch"] == "after"
    store.finish_remote_provider_pass(execution.operation_id, pid_file)
    finish_recorded_browser(store, execution.operation_id)
    finish_recorded_browser(store, execution.operation_id)
    assert [call["lease_id"] for call in calls if call["action"] == "release"] == [lease_id]
    assert release_routes == [
        {
            "host": remote.host,
            "partition": remote.transport_partition,
            "data_dir": store.path.parent,
        }
    ]
    assert json.loads(host.record_path(owner).read_text())["leases"] == {}
    assert store.browser_turn_status(execution.operation_id).lease_id is None


@pytest.mark.asyncio
async def test_failed_remote_continuation_cleanup_settles_retained_lease(
    host, tmp_path, monkeypatch
):
    from pathlib import PurePosixPath

    from rcp.browser import service
    from rcp.transport import RemoteRunStage

    calls = []

    def invoke(payload, **kwargs):
        calls.append(payload["action"])
        host.request.update(payload)
        return getattr(host, payload["action"])()

    monkeypatch.setattr(service, "_invoke", invoke)
    monkeypatch.setattr("rcp.runs.browser_lifecycle.browser_host_key", lambda _: "host")
    _, request, execution = _discuss_app(tmp_path / "app")
    store = execution.store
    task = store.agent_task(execution.operation_id)
    request.browser_requested = True
    request.provider = "claude"
    store.set_chat_browser_requested(task.project_id, request.chat_id, browser_requested=True)
    root = tmp_path / "stage"
    (root / "workspace").mkdir(parents=True)
    execution.checkpoint_stage("remote", str(root))
    remote = RemoteRunStage("remote")
    remote.root = PurePosixPath(root)
    monkeypatch.setattr(remote, "finalize_inputs", lambda: None)
    monkeypatch.setattr("rcp.runs.shared._stage_or_reuse_task_input", lambda *a: "/supervisor.py")

    class Launcher:
        async def stream(self, *args, **kwargs):
            yield AgentEvent(event="remote_process_start", text=str(root / "provider.pid"))
            yield AgentEvent(event="session", session_id="wrong-session")

    outcome = _ProviderOutcome()
    async with browser_turn(
        request,
        workspace=root / "workspace",
        execution_host="remote",
        execution=execution,
        remote_stage=remote,
        capability="discuss",
    ) as grant:
        frames = [
            frame
            async for frame in _stream_agent_events(
                Launcher(),
                request,
                "resume",
                workspace=root / "workspace",
                session_id="saved-session",
                required_session_id="saved-session",
                read_dirs=[],
                write_dirs=[],
                write_scope=None,
                execution_host="remote",
                execution=execution,
                remote_stage=remote,
                capability="discuss",
                outcome=outcome,
                binary=None,
                supervise_remote=True,
                browser_grant=grant,
            )
        ]
    assert outcome.failed
    assert outcome.session_id is None
    assert frames
    store.fail_agent_task(execution.operation_id, "native session mismatch")
    stopped = False
    monkeypatch.setattr(
        "rcp.runs.provider_process.AgentProcessControl.remote_stopped", lambda *a, **kw: stopped
    )
    close_chat_browser_owners(store, task.project_id, request.chat_id, delete_profile=True)
    assert store.browser_owners(task.project_id)
    assert store.browser_turn_status(execution.operation_id).lease_id == grant.lease_id
    stopped = True
    retry_browser_cleanup(store)
    assert not store.unresolved_remote_provider_passes("remote", str(root))
    assert store.browser_turn_status(execution.operation_id).lease_id is None
    assert not host.record_path(grant.owner.token()).exists()
    assert store.browser_owners(task.project_id) == []
    assert calls == ["ensure", "release", "close"]
