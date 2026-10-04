from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from rcp.agents import AgentEvent
from rcp.agents.browser_grant import BrowserGrant, BrowserOwnerKey, BrowserTurnStatus
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
    original = browser_runtime_seam.acquire_browser_grant

    def acquire(owner, *, execution, workspace_dir):
        calls.append(owner)
        return original(owner, execution=execution, workspace_dir=workspace_dir)

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
        assert grant.reason_code == "runtime_not_wired"
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

    def acquire(owner, *, execution, workspace_dir):
        return BrowserGrant(
            requested=True,
            status="granted",
            owner=owner,
            session_name=owner.token(),
            invocation_dir=workspace_dir,
            path_prefix="/tools/bin",
            env={"PLAYWRIGHT_CLI_SESSION": owner.token()},
        )

    def finish(grant):
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
    assert closed == [{"execution": None, "delete_profile": True}]
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
    assert closed == [{"execution": None, "delete_profile": False}]
    assert len(store.browser_owners(task.project_id)) == 1

    def unavailable(*args, **kwargs):
        raise OSError("host unreachable")

    monkeypatch.setattr(browser_runtime_seam, "close_browser_owner", unavailable)
    close_chat_browser_owners(store, task.project_id, delete_profile=True)
    retry_browser_cleanup(store, finished_operation_id=execution.operation_id)
    assert store.browser_owners(task.project_id)[0]["close_requested"] == 1


def test_ssh_owner_identity_resolves_host_and_account(monkeypatch):
    monkeypatch.setattr(
        "rcp.runs.browser_lifecycle.subprocess.run",
        lambda *a, **kw: SimpleNamespace(stdout="hostname host.example\nuser worker\nport 2222\n"),
    )
    assert json.loads(browser_host_key("alias")) == ["host.example", "worker", "2222"]
