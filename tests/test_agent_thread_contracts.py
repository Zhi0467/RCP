from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from rcp.agents.command_protocol import validate_command_request
from rcp.service import (
    ChatMessage,
    _canonical_chat_path,
    _StoredChatRecord,
    _validate_stored_chat_record,
)


def _record(**changes):
    return {
        "sessionId": "11111111-1111-4111-8111-111111111111",
        "uuid": str(uuid.uuid4()),
        "chatScope": "project",
        "timestamp": "2026-11-01T00:00:00+00:00",
        "type": "user",
        "role": "user",
        "text": "hello",
        **changes,
    }


def test_old_writer_record_stays_public_and_source_absent(tmp_path):
    raw = _record()
    before = json.dumps(raw).encode()
    path = _canonical_chat_path(tmp_path, raw["sessionId"], chat_scope="project", node_id=None)
    path.parent.mkdir()
    path.write_bytes(before)
    record = _StoredChatRecord.model_validate_json(path.read_bytes())
    _validate_stored_chat_record(record, first=None, root=tmp_path, path=path)
    assert record.private_owner is None
    assert "source" not in record.model_dump(by_alias=True)
    assert path.read_bytes() == before
    message = ChatMessage(
        message_id=raw["uuid"], role="user", text="hello", timestamp=raw["timestamp"]
    )
    assert "source" not in message.model_dump()


def test_private_record_is_bound_to_owner_and_filename(tmp_path):
    record = _StoredChatRecord.model_validate(_record(privateOwner="user_test", source="handoff"))
    path = _canonical_chat_path(
        tmp_path, record.session_id, chat_scope="project", node_id=None, private_owner="user_test"
    )
    assert path.name == f"private-user_test-{record.session_id}.jsonl"
    _validate_stored_chat_record(record, first=None, root=tmp_path, path=path)
    with pytest.raises(ValueError):
        _validate_stored_chat_record(
            record,
            first=None,
            root=tmp_path,
            path=path.with_name(f"project-{record.session_id}.jsonl"),
        )
    with pytest.raises(ValueError):
        _validate_stored_chat_record(
            record.model_copy(update={"private_owner": "user_other"}),
            first=record,
            root=tmp_path,
            path=path,
        )


def test_projects_and_handoff_envelopes():
    envelope = dict(mailbox_id="a" * 32, request_id="b" * 32, credential="c" * 64)

    def parse(verb, arguments, **extra):
        return validate_command_request(
            json.dumps({**envelope, "verb": verb, "arguments": arguments, **extra})
        )

    assert parse("projects", {"action": "list"}).verb == "projects"
    assert parse("projects", {"action": "show", "project_id": "p"}).verb == "projects"
    for args in ({"action": "show"}, {"action": "list", "project_id": "p"}):
        with pytest.raises(ValidationError):
            parse("projects", args)
    assert (
        parse(
            "handoff", {"project_id": "p", "message_file": "request.txt"}, idempotency_key="once"
        ).verb
        == "handoff"
    )
    with pytest.raises(ValidationError):
        parse("handoff", {"project_id": "p", "message_file": "request.txt"})
    for filename in ("../request.txt", ".", "..", "/request.txt"):
        with pytest.raises(ValidationError):
            parse("handoff", {"project_id": "p", "message_file": filename}, idempotency_key="once")


def test_space_record_reader_refuses_moved_or_project_records(tmp_path):
    from rcp.service import canonical_space_chat_path, read_space_chat_transcript

    record = _record(chatScope="space", privateOwner="user_test", source="voice")
    path = canonical_space_chat_path(tmp_path, "user_test", record["sessionId"])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record) + "\n")
    transcript = read_space_chat_transcript(tmp_path, "user_test", record["sessionId"], title="Pip")
    assert transcript is not None
    assert transcript.kind == "space_chat"
    assert transcript.messages[0].source == "voice"
    assert read_space_chat_transcript(tmp_path, "user_other", record["sessionId"]) is None
    with pytest.raises(ValueError):
        _validate_stored_chat_record(
            _StoredChatRecord.model_validate(record), first=None, root=tmp_path, path=path
        )
    record["chatScope"] = "project"
    path.write_text(json.dumps(record) + "\n")
    assert read_space_chat_transcript(tmp_path, "user_test", record["sessionId"]) is None


def test_private_project_reader_roundtrip_and_pre_pagination_filter(manifest, tmp_path):
    from rcp.runs.chat import _append_chat_exchange
    from rcp.service import RunRequest
    from tests.helpers import create_named_app

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    public_id, private_id = str(uuid.uuid4()), str(uuid.uuid4())
    for chat_id in (public_id, private_id):
        _append_chat_exchange(
            service,
            RunRequest(chat_id=chat_id, chat_scope="project", message="hello"),
            "reply",
            None,
            None,
        )
    public_path = service.chat_path(private_id, chat_scope="project", node_id=None)
    private_path = service.chat_path(
        private_id, chat_scope="project", node_id=None, private_owner="user_test"
    )
    records = [json.loads(line) for line in public_path.read_text().splitlines()]
    private_path.write_text(
        "\n".join(
            json.dumps({**record, "privateOwner": "user_test", "source": "handoff"})
            for record in records
        )
        + "\n"
    )
    public_path.unlink()
    transcript = service.chat_transcript(private_id)
    assert transcript is not None
    assert [message.source for message in transcript.messages] == ["handoff", "handoff"]
    page = service.chat_summaries(limit=1, excluded_chat_ids={private_id})
    assert page.total == 1
    assert [summary.chat_id for summary in page.items] == [public_id]
    private_path.rename(public_path)
    assert service.chat_transcript(private_id) is None
