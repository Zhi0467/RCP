from __future__ import annotations

import stat
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from rcp.agents import AgentProcessControl
from rcp.background import AgentTaskExecution
from rcp.runs.bulletin import handle_bulletin
from rcp.runs.questions import work_command_handler
from rcp.storage import Artifact
from rcp.transport.workspace_mailbox import RunStageMailbox

from .test_bulletin_commands import request
from .test_lessons_store import store as store
from .test_work_questions import identity


def artifact(app_store, **changes):
    return app_store.create_artifact(
        Artifact(
            **{
                "artifact_id": "a" * 24,
                "project_id": "project",
                "supplier": "turn",
                "supplier_id": "other-agent-turn",
                "chat_id": "other-chat",
                "source_name": "plot.png",
                "media_type": "image/png",
                "created_at": app_store.now(),
                **changes,
            }
        ),
        data=b"\x89PNG\r\n\x00\xff",
    )


@pytest.mark.parametrize("artifact_id", ["a" * 24, "legacy-artifact_id"])
def test_agent_reads_other_chat_artifact_as_immutable_current_bytes(store, tmp_path, artifact_id):
    source = artifact(store, artifact_id=artifact_id)
    store.mutate_bulletin_post(
        "project",
        "main",
        text=f"See <artifact:{source.artifact_id}>",
        key="link",
        user_id=store.local_owner.user_id,
    )
    execution = AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl())
    mailbox = RunStageMailbox.for_stage(local_stage=tmp_path, remote_stage=None)
    handler = work_command_handler(execution, None, mailbox=mailbox)
    read = handler(request("read"), identity())
    assert read.result["posts"][0]["artifacts"][0]["artifact_id"] == source.artifact_id
    command = request("artifact", artifact_id=source.artifact_id)
    first = handler(command, identity())
    assert first.status == "ok"
    path = Path(first.result["path"])
    assert path.read_bytes() == b"\x89PNG\r\n\x00\xff"
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert path.parent == tmp_path / "inputs"
    assert first.result["version"] == source.current_version
    assert first.result["media_type"] == "image/png"
    store.publish_artifact_version(
        source.artifact_id,
        base_version=source.current_version,
        operation_id="other-agent-edit",
        data=b"new bytes",
    )
    second = handle_bulletin(
        store, "turn", command, post_allowed=False, turn_artifacts=False, mailbox=mailbox
    )
    assert second.status == "ok"
    assert Path(second.result["path"]).read_bytes() == b"new bytes"
    assert path.read_bytes() == b"\x89PNG\r\n\x00\xff"
    assert store.list_bulletin("project", "main")["total"] == 1


@pytest.mark.parametrize(
    "unavailable", ["missing", "other_project", "expired", "nonmember", "profile", "disabled"]
)
def test_artifact_read_refuses_unavailable_source_or_authority(
    store, tmp_path, monkeypatch, unavailable
):
    changes = {}
    if unavailable == "other_project":
        changes["project_id"] = "other-project"
    if unavailable == "expired":
        changes["expires_at"] = (
            datetime.fromisoformat(store.now()) - timedelta(seconds=1)
        ).isoformat()
    if unavailable != "missing":
        artifact(store, **changes)
    if unavailable == "nonmember":
        monkeypatch.setattr("rcp.storage.bulletin.live_project_member", lambda *_: False)
    elif unavailable == "profile":
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET dispatch_authority_json=NULL WHERE operation_id='turn'"
            )
    elif unavailable == "disabled":
        monkeypatch.setattr(store, "bulletin_enabled_for_task", lambda _: False)
    mailbox = RunStageMailbox.for_stage(local_stage=tmp_path, remote_stage=None)
    response = handle_bulletin(
        store,
        "turn",
        request("artifact", artifact_id="a" * 24),
        post_allowed=False,
        turn_artifacts=False,
        mailbox=mailbox,
    )
    assert response.status == "invalid"
    assert not (tmp_path / "inputs").exists()


def test_a_linked_turn_file_is_saved_when_the_turn_publishes_it(store, tmp_path):
    from rcp.runs.chat import _discover_chat_artifacts

    task = store.agent_task("turn")
    store.record_agent_task_receipt(
        "turn",
        "operation_created",
        {"kind": task.kind, "attempt": task.attempt, "has_parent": False, "resumed": False},
    )
    execution = AgentTaskExecution(operation_id="turn", store=store, control=AgentProcessControl())
    handler = work_command_handler(execution, None)
    link = handler(request("link", file_name="plot.png"), identity())
    assert link.status == "ok"
    store.execute_bulletin_command(
        "project", "turn", "post", key="link", text=f"Plot: {link.result['token']}"
    )
    directory = tmp_path / "artifacts"
    directory.mkdir()
    (directory / "plot.png").write_bytes(b"\x89PNG\r\n\x00\xff")
    [descriptor] = _discover_chat_artifacts(execution, "turn", directory, None)
    assert descriptor.artifact_id == link.result["artifact_id"]
    assert descriptor.kept_at is not None
    assert store.artifact(descriptor.artifact_id).expires_at is None
    posts = store.list_bulletin("project", "main")["posts"]
    assert posts[0]["artifacts"][0]["artifact_id"] == descriptor.artifact_id
