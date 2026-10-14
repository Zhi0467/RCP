from __future__ import annotations

import json
import uuid

import pytest

from rcp.limits import TURN_OUTCOME_REASON_MAX_CHARS
from rcp.runs.tasks.work import stream_work_run

from .helpers import signed_in_client
from .test_api import ScriptedLauncher, _seeded_project, _wait_for_run


@pytest.mark.parametrize(
    "turns,expected,calls",
    [
        ([{"status": "done"}], {"status": "done", "reason": None}, 1),
        (
            [{"status": "partial", "reason": "Logs unavailable"}],
            {"status": "partial", "reason": "Logs unavailable"},
            1,
        ),
        (
            [{"status": "could_not", "reason": "Host unreachable"}],
            {"status": "could_not", "reason": "Host unreachable"},
            1,
        ),
        ([None], {"status": "not_stated", "reason": None}, 2),
        ([{"status": "unknown"}], {"status": "not_stated", "reason": None}, 2),
        ([{"status": "partial"}], {"status": "not_stated", "reason": None}, 2),
        ([{"status": "partial", "reason": " "}], {"status": "not_stated", "reason": None}, 2),
        (
            [{"status": "partial", "reason": "one\ntwo"}],
            {"status": "not_stated", "reason": None},
            2,
        ),
        (
            [{"status": "partial", "reason": "x" * (TURN_OUTCOME_REASON_MAX_CHARS + 1)}],
            {"status": "not_stated", "reason": None},
            2,
        ),
        ([None, {"status": "done"}], {"status": "done", "reason": None}, 2),
    ],
    ids=[
        "done",
        "partial",
        "could-not",
        "missing",
        "bad-status",
        "missing-reason",
        "blank",
        "multiline",
        "too-long",
        "corrected",
    ],
)
def test_work_outcome_settles_and_projects(manifest, tmp_path, turns, expected, calls):
    app, service = _seeded_project(manifest, tmp_path)
    launcher = ScriptedLauncher(
        [{} if value is None else {"outcome.json": json.dumps(value)} for value in turns],
        outcome=None,
        message="Done. This answer is not the outcome source.",
    )

    async def stream(_project_id, _kind, request, execution):
        async for frame in stream_work_run(
            service, launcher, request, tmp_path / "data", execution=execution
        ):
            yield frame

    app.state.background_tasks.stream = stream
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    chat_id = str(uuid.uuid4())
    response = client.post(
        f"/api/projects/{project_id}/tasks/project_chat",
        json={
            "chat_id": chat_id,
            "message": "Check the logs.",
            "run_truth_scope": ["repo-a"],
            "mode": "work",
        },
    )
    assert response.status_code == 202, response.json()
    record = _wait_for_run(client, project_id, response.json()["operation_id"])
    assert record["status"] == "succeeded"
    assert record["result"]["turn_outcome"] == expected
    assert launcher.calls == calls
    assert launcher.resumed_sessions == [None] + [launcher.native_session_id] * (calls - 1)
    assert len(set(launcher.workspaces)) == 1
    transcript = service.chat_transcript(chat_id)
    assert transcript is not None
    assert transcript.messages[0].turn_outcome is None
    outcome = transcript.messages[-1].turn_outcome
    assert outcome is not None
    assert outcome.model_dump() == expected
