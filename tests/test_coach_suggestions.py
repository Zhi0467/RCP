from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from rcp.agents import AgentEvent
from rcp.agents.launcher import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.paper.coach_seam import SavedPaperFile
from rcp.runs.tasks.coach import stream_coach
from rcp.service import CoachRequest
from rcp.storage.models import AgentTaskRecord

from .helpers import create_named_app
from .paper_fakes import wire_saved_paper


@pytest.fixture
def coach(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    live = wire_saved_paper(service)
    return service, live, tmp_path / "data"


async def run_coach(service, launcher, data, *, mode="review", execution=None):
    return [
        AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        async for frame in stream_coach(
            service,
            launcher,
            service.paper,
            CoachRequest(message="Edit the paper", mode=mode),
            data,
            execution,
        )
    ]


def execution_for(service):
    store = service.paper.store
    task_id = str(uuid4())
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=task_id,
            project_id=service.paper.project_id,
            kind="paper_coach",
            status="running",
            request={"message": "Edit the paper", "provider": "codex"},
            created_at=now,
            updated_at=now,
            status_message="running",
        )
    )
    return AgentTaskExecution(task_id, store, AgentProcessControl())


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_editing, expected_turns", [(False, 2), (True, 3)])
async def test_saved_snapshot_refreshes_same_session_only_affected_files(
    coach, keep_editing, expected_turns
):
    service, live, data = coach
    live.current.files.append(
        SavedPaperFile(file_id="other", path="other.md", kind="text", text="Other\n")
    )
    live.history[1] = live.current.model_copy(deep=True)
    paths = []
    sessions = []

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            sessions.append(kwargs["session_id"])
            base = [path for path in kwargs["read_dirs"] if path.name.startswith("paper-base-")][-1]
            paths.append(base)
            assert base.stat().st_mode & 0o222 == 0
            assert (base / "paper.md").stat().st_mode & 0o222 == 0
            assert (base / "other.md").exists() == (len(paths) == 1)
            assert kwargs["capability"] == "paper_suggest"
            assert len(kwargs["write_dirs"]) == 1
            output = kwargs["write_dirs"][0] / "files" / "paper.md"
            output.parent.mkdir()
            output.write_text("Proposed replacement.\n")
            if keep_editing or len(paths) == 1:
                live.human_edit("paper-file", f"Human version {len(paths)}.\n")
            yield AgentEvent(event="session", session_id="same-native-session")
            yield AgentEvent(event="done")

    events = await run_coach(service, Launcher(), data)
    assert not [event for event in events if event.event == "error"]
    assert sessions == [None] + ["same-native-session"] * (expected_turns - 1)
    assert all(not path.exists() for path in paths)
    groups = service.paper_changes.groups(service.paper.project_id)
    assert len(groups) == 1
    assert groups[0].state == ("outdated" if keep_editing else "pending")
    assert live.applied == []


@pytest.mark.asyncio
async def test_direct_apply_uses_saved_mode_and_admission(coach):
    service, live, data = coach
    execution = execution_for(service)

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            path = kwargs["write_dirs"][0] / "files" / "paper.md"
            path.parent.mkdir()
            path.write_text("Applied text\n")
            yield AgentEvent(event="session", session_id="direct-native")
            yield AgentEvent(event="done")

    events = await run_coach(service, Launcher(), data, mode="apply_directly", execution=execution)
    assert not [event for event in events if event.event == "error"]
    assert live.current.files[0].text == "Applied text\n"
    assert live.applied[0]["actor"] == "Coach, applied directly"
    receipts = execution.store.agent_task_receipts(execution.operation_id)
    base = next(r.payload for r in receipts if r.category == "paper_turn_base")
    assert base["revision"] == 1
    assert base["file_ids"] == ["paper-file"]
    assert base["mode"] == "apply_directly"
    assert not Path(base["snapshot_path"]).exists()


@pytest.mark.asyncio
async def test_invalid_output_records_exact_diagnostics_and_cleans_snapshot(coach):
    service, _live, data = coach
    execution = execution_for(service)

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            (kwargs["write_dirs"][0] / "moves.json").write_text(
                json.dumps({"delete": ["../outside"]})
            )
            yield AgentEvent(event="session", session_id="invalid-native")
            yield AgentEvent(event="done")

    events = await run_coach(service, Launcher(), data, execution=execution)
    errors = [event.text for event in events if event.event == "error"]
    assert errors
    receipts = execution.store.agent_task_receipts(execution.operation_id)
    diagnostic = next(
        r.payload["diagnostics"] for r in receipts if r.category == "paper_changes_invalid"
    )
    assert errors == ["; ".join(diagnostic)]
    assert service.paper_changes.groups(service.paper.project_id) == []
    assert service.paper.sessions()[0].native_session_id == "invalid-native"
    assert not list((data / "run-stage").glob("*/paper-base-*"))


@pytest.mark.asyncio
async def test_stop_prevents_refresh_and_direct_application(coach):
    service, live, data = coach
    execution = execution_for(service)
    calls = []

    class Launcher:
        async def stream(self, _provider, _prompt, **kwargs):
            calls.append(kwargs)
            path = kwargs["write_dirs"][0] / "files" / "paper.md"
            path.parent.mkdir()
            path.write_text("Proposed\n")
            execution.control.pause_requested.set()
            yield AgentEvent(event="session", session_id="stopped-native")
            yield AgentEvent(event="done")

    await run_coach(service, Launcher(), data, mode="apply_directly", execution=execution)
    assert len(calls) == 1
    assert live.applied == []
    assert not list((data / "run-stage").glob("*/paper-base-*"))
