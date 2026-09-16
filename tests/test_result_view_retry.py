from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.background import BackgroundAgentTasks
from rcp.core.models import AuthorizedHuman
from rcp.service import RunRequest
from rcp.storage import AgentTaskRecord, AppStore


async def _unused_stream(*_args, **_kwargs):
    if False:
        yield ""


def test_initial_result_view_revision_start_persists_its_exact_saved_stage(
    tmp_path, monkeypatch
) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    store = AppStore(tmp_path / "rcp.sqlite3")
    owner = store.local_owner
    assert owner is not None
    request = RunRequest(
        provider="codex",
        model="",
        reasoning="high",
        run_on="local",
        chat_scope="node",
        node_id="experiment/pilot",
        message="Use a log scale.",
        chat_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        mode="work",
        result_view={"action": "revise", "view_id": "a" * 24},
    )
    tasks = BackgroundAgentTasks(store, _unused_stream)
    monkeypatch.setattr(tasks, "_spawn_record", lambda record, *_args, **_kwargs: record)

    started = tasks.start(
        "project-one",
        "node_chat",
        request,
        authorized_by=AuthorizedHuman(
            space_id=store.space_id,
            user_id=owner.user_id,
            display_name="Result view owner",
        ),
        stage_root=str(stage),
    )

    assert started.native_session_id == request.session_id
    assert started.stage_host is None
    assert started.stage_root == str(stage)

    with pytest.raises(ValueError, match="saved native session and exact stage"):
        tasks.start(
            "project-one",
            "node_chat",
            request,
            authorized_by=started.authorized_by,
        )


def _failed_revision_task(store: AppStore, stage: Path) -> AgentTaskRecord:
    session_id = str(uuid.uuid4())
    request = RunRequest(
        provider="codex",
        model="",
        reasoning="high",
        run_on="local",
        chat_scope="node",
        node_id="experiment/pilot",
        message="Use a log scale.",
        chat_id=str(uuid.uuid4()),
        session_id=session_id,
        mode="work",
        result_view={"action": "revise", "view_id": "a" * 24},
    )
    now = store.now()
    record = store.create_agent_task(
        AgentTaskRecord(
            operation_id=str(uuid.uuid4()),
            project_id="project-one",
            kind="node_chat",
            status="failed",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            finished_at=now,
            status_message="The result-view revision failed.",
            error="provider stream disconnected",
            native_session_id=session_id,
            stage_root=str(stage),
            phase="failed",
            last_activity_at=now,
        )
    )
    assert record.can_retry
    return record


def test_result_view_revision_retry_reuses_exact_session_stage_and_profile(
    tmp_path, monkeypatch
) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    store = AppStore(tmp_path / "rcp.sqlite3")
    previous = _failed_revision_task(store, stage)
    tasks = BackgroundAgentTasks(store, _unused_stream)
    captured: dict[str, object] = {}

    def capture(project_id, kind, request, **kwargs):
        captured.update(project_id=project_id, kind=kind, request=request, **kwargs)
        return previous

    monkeypatch.setattr(tasks, "_create_and_spawn", capture)

    tasks.retry(previous.operation_id)

    request = captured["request"]
    assert isinstance(request, RunRequest)
    assert request.session_id == previous.native_session_id
    assert (request.provider, request.model, request.reasoning, request.run_on) == (
        "codex",
        "",
        "high",
        "local",
    )
    assert captured["continuation"] == "retry"
    assert captured["stage_root"] == str(stage)
    assert captured["stage_host"] is None


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("provider", "claude"),
        ("model", "different-model"),
        ("reasoning", "low"),
        ("run_on", "remote"),
    ],
)
def test_result_view_revision_retry_rejects_profile_handoff(
    tmp_path, monkeypatch, override: str, value: str
) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    store = AppStore(tmp_path / "rcp.sqlite3")
    previous = _failed_revision_task(store, stage)
    tasks = BackgroundAgentTasks(store, _unused_stream)
    monkeypatch.setattr(
        tasks,
        "_create_and_spawn",
        lambda *_args, **_kwargs: pytest.fail("revision retry must not start a handoff"),
    )

    with pytest.raises(ValueError, match="cannot start a fresh provider session"):
        tasks.retry(previous.operation_id, **{override: value})


def test_result_view_revision_retry_reports_lost_stage_without_redrawing(
    tmp_path, monkeypatch
) -> None:
    stage = tmp_path / "rcp-run.chat"
    stage.mkdir()
    store = AppStore(tmp_path / "rcp.sqlite3")
    previous = _failed_revision_task(store, stage)
    stage.rmdir()
    tasks = BackgroundAgentTasks(store, _unused_stream)
    monkeypatch.setattr(
        tasks,
        "_create_and_spawn",
        lambda *_args, **_kwargs: pytest.fail("revision retry must not start a handoff"),
    )

    with pytest.raises(ValueError, match="workspace is unavailable") as failure:
        tasks.retry(previous.operation_id)

    assert "existing view was not redrawn" in str(failure.value)


@pytest.mark.parametrize("continuation", ["resume", "collect"])
def test_recovery_of_a_result_view_turn_admits_collection(tmp_path, continuation) -> None:
    """Collection recovers a result-view turn instead of refusing it as a new one.

    A transport-lost Work turn that was creating or revising a result view is
    collectible like any other, so the recovery it reaches must be the same
    logical turn. Treating it as a fresh dispatch rejected the child outright
    and stranded the finished journal it was admitted to replay.
    """

    from rcp.runs.tasks.result_views import _prepare_result_view_turn

    store = AppStore(tmp_path / "rcp.sqlite3")
    request = RunRequest(
        provider="codex",
        run_on="local",
        chat_scope="node",
        node_id="experiment/pilot",
        chat_id=str(uuid.uuid4()),
        message="Plot it.",
        mode="work",
        result_view={"action": "revise", "view_id": "b" * 24},
    )
    execution = SimpleNamespace(continuation=continuation, store=store, operation_id="turn")

    with pytest.raises(ValueError, match="scoped to an existing Experiment"):
        _prepare_result_view_turn(
            request,
            execution,
            tmp_path,
            None,
            focused_node=None,
            logical_operation_id="turn",
            revision_preflight=None,
        )
