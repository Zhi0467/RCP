"""Old-writer session fixtures and a provider-process boundary for switch tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

from rcp.agents import AgentEvent, AgentLauncher
from rcp.background import BackgroundAgentTasks
from rcp.runs.shared import _ProviderOutcome, _sse, _stream_agent_events
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import ProjectRecord
from rcp.storage.models import AuthorizedHuman

from .test_provider_login_resolver import _login, _task

PROJECT = "00000000-0000-4000-8000-000000000003"


def switched_session(accounts, stage, provider, direction, *, status="failed", repair=False):
    """Persist the login and runtime receipt older writers gave an existing session."""
    store = accounts.store
    owner = store.local_owner
    assert owner is not None
    owner = store.rename_space_user(owner.user_id, "Member")
    member = AuthorizedHuman(
        space_id=store.space_id, user_id=owner.user_id, display_name=owner.display_name
    )
    own = _login(accounts, provider, own=True, member=member.user_id)
    shared = _login(accounts, provider, machine=True)
    old, new = (own, shared) if direction == "personal_to_team" else (shared, own)
    store.set_provider_login_setting(member.user_id, provider, "", "own_then_shared")
    store.upsert_project(
        ProjectRecord(
            project_id=PROJECT,
            locator="unused",
            name="Project",
            state_location="unused",
            state_remote=False,
            added_at=store.now(),
        )
    )
    store.seat_project_member(PROJECT, member.user_id, seated_by=member.user_id)
    stage.mkdir()
    request = RunRequest(
        provider=provider,
        mode="work" if repair else "discuss",
        run_on="local",
        chat_scope="project",
        chat_id="00000000-0000-4000-8000-000000000004",
        reasoning="medium",
        model="",
        message="Continue the task.",
    )
    _task(store, "previous", old, started=store.now(), member=member.user_id)
    result = {"graph_update": {"status": "rejected", "repairable": True}} if repair else None
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET project_id='00000000-0000-4000-8000-000000000003', kind='project_chat', status=?, request_json=?, stage_root=?, "
            "authorized_space_id=?, authorized_display_name=?, result_json=?, dispatch_authority_json=? WHERE operation_id='previous'",
            (
                status,
                request.model_dump_json(),
                str(stage),
                member.space_id,
                member.display_name,
                json.dumps(result) if result else None,
                resolve_dispatch_authority("project_chat", request).model_dump_json(),
            ),
        )
    store.mark_provider_login_signed_out(
        old, member_id=member.user_id, source="restore", detail="expired"
    )
    return old, new, member, request


def switch_background(store, stage, selected):
    """Run real admission, workers and shared login selection with a synthetic turn body."""

    async def provider_stream(_provider, _prompt, **kwargs):
        selected.append((kwargs["login_key"], kwargs["session_id"], kwargs.get("fork_session_id")))
        await kwargs["before_start"]()
        yield AgentEvent(event="session", session_id=kwargs["session_id"] or "new-session")
        yield AgentEvent(event="done")

    async def stream(_project_id, _kind, request, execution):
        outcome = _ProviderOutcome()
        async for frame in _stream_agent_events(
            cast(AgentLauncher, SimpleNamespace(stream=provider_stream)),
            request,
            "Continue",
            workspace=stage,
            session_id=request.session_id,
            read_dirs=[],
            write_dirs=[],
            write_scope=None,
            execution_host="",
            execution=execution,
            remote_stage=None,
            capability="discuss",
            outcome=outcome,
            binary=None,
        ):
            yield frame
        if not outcome.failed:
            yield _sse(AgentEvent(event="done"))

    return BackgroundAgentTasks(store, stream)
