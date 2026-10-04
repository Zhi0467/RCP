from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rcp.runs.chat import _append_chat_exchange
from rcp.service import RunRequest
from rcp.transport import StateUnavailable

from .helpers import create_named_app


def test_chat_append_and_projection_preserve_unicode_line_separators(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    text = "before\x85middle\u2028more\u2029after"
    request = RunRequest(chat_id=str(uuid.uuid4()), chat_scope="project", message=text)
    _append_chat_exchange(service, request, text, None, None)
    _append_chat_exchange(service, request, text, None, None)

    transcript = service.chat_transcript(request.chat_id)
    assert transcript is not None
    assert [(item.role, item.text) for item in transcript.messages] == [
        ("user", text),
        ("assistant", text),
        ("user", text),
        ("assistant", text),
    ]


def test_batch_chat_reader_preserves_single_chat_validation_and_ambiguity(
    manifest, tmp_path, monkeypatch
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    good, ambiguous, missing = (str(uuid.uuid4()) for _ in range(3))
    for chat_id, scope, node_id in [
        (good, "project", None),
        (ambiguous, "project", None),
        (ambiguous, "node", "rq/ambiguous"),
    ]:
        _append_chat_exchange(
            service,
            RunRequest(
                chat_id=chat_id, chat_scope=scope, node_id=node_id, message="Read this chat."
            ),
            "A retained answer.",
            None,
            None,
        )
    scans = []
    original = service._canonical_chat_files

    def counted():
        scans.append(True)
        return original()

    monkeypatch.setattr(service, "_canonical_chat_files", counted)
    transcripts = service.chat_transcripts([good, ambiguous, missing, good])
    assert list(transcripts) == [good]
    assert transcripts[good].messages[-1].text == "A retained answer."
    assert len(scans) == 1
    assert service.chat_transcript(ambiguous) is None
    assert service.chat_transcripts([]) == {}
    for invalid in ["not-a-uuid", good.upper()]:
        with pytest.raises(ValueError, match="chat_id must be"):
            service.chat_transcripts([good, invalid])


def test_chat_history_is_paginated_from_full_canonical_transcripts(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    project_id = app.state.default_project_id
    chat_dir = manifest.research_dir / "chat"
    chat_dir.mkdir(exist_ok=True)
    chat_ids: list[str] = []
    long_answer = "full-answer-" + ("x" * 20_000)

    for index in range(23):
        chat_id = str(uuid.uuid4())
        chat_ids.append(chat_id)
        timestamp = f"2026-07-31T{index:02d}:00:00+00:00"
        answer = long_answer if index == 0 else f"answer {index}"
        common = {
            "sessionId": chat_id,
            "nativeSessionId": str(uuid.uuid4()),
            "nodeId": None,
            "chatScope": "project",
            "provider": "codex",
            "model": "provider-default",
            "reasoning": "medium",
            "executionMachine": "laptop",
            "timestamp": timestamp,
        }
        records = [
            {
                **common,
                "uuid": str(uuid.uuid4()),
                "type": "user",
                "role": "user",
                "text": f"question {index}",
            },
            {
                **common,
                "uuid": str(uuid.uuid4()),
                "type": "assistant",
                "role": "assistant",
                "text": answer,
                "appliedRevision": None,
            },
        ]
        (chat_dir / f"project-{chat_id}.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    # Neither malformed files nor a valid transcript reached through a symlink
    # can enter the canonical list.
    (chat_dir / "malformed.jsonl").write_text("not json\n", encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        (chat_dir / f"project-{chat_ids[0]}.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (chat_dir / "outside-link.jsonl").symlink_to(outside)

    service = app.state.service
    original_read = service._read_chat_transcript
    parsed_paths: list[Path] = []

    def counted_read(path: Path):
        parsed_paths.append(path)
        return original_read(path)

    monkeypatch.setattr(service, "_read_chat_transcript", counted_read)

    first = client.get(f"/api/projects/{project_id}/chats?offset=0&limit=7")
    assert first.status_code == 200
    assert first.json()["total"] == 23
    assert first.json()["offset"] == 0
    assert first.json()["limit"] == 7
    assert len(first.json()["items"]) == 7
    assert first.json()["items"][0]["title"] == "question 22"
    parsed_after_first_page = len(parsed_paths)
    assert parsed_after_first_page > len(first.json()["items"])

    discovered: list[str] = []
    for offset in range(0, 28, 7):
        page = client.get(f"/api/projects/{project_id}/chats?offset={offset}&limit=7").json()
        discovered.extend(item["chat_id"] for item in page["items"])
    assert set(discovered) == set(chat_ids)
    assert len(discovered) == 23
    assert len(parsed_paths) == parsed_after_first_page

    parsed_paths.clear()
    transcript = client.get(f"/api/projects/{project_id}/chats/{chat_ids[0]}")
    assert transcript.status_code == 200
    assert [message["role"] for message in transcript.json()["messages"]] == [
        "user",
        "assistant",
    ]
    assert transcript.json()["messages"][-1]["text"] == long_answer
    assert len(transcript.json()["messages"][-1]["text"]) > 16_000
    assert parsed_paths == [chat_dir / f"project-{chat_ids[0]}.jsonl"]

    assert client.get(f"/api/projects/{project_id}/chats/not-a-uuid").status_code == 422
    assert client.get(f"/api/projects/{project_id}/chats/{uuid.uuid4()}").status_code == 404


def test_chat_history_reports_remote_refresh_failure_as_unavailable(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    project_id = app.state.default_project_id
    service.history.workspace.remote = True
    refresh_threads: list[int] = []

    def fail_refresh() -> bool:
        refresh_threads.append(threading.get_ident())
        raise StateUnavailable("remote transcript refresh failed")

    monkeypatch.setattr(service.history.workspace, "refresh", fail_refresh)
    request_thread = threading.get_ident()

    response = TestClient(app).get(f"/api/projects/{project_id}/chats")

    assert response.status_code == 503
    assert response.json()["detail"] == "remote transcript refresh failed"
    assert refresh_threads and refresh_threads[0] != request_thread


def test_non_main_project_route_does_not_build_project_snapshot(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    monkeypatch.setattr(
        app.state.service,
        "project_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("chat history must not serialize the whole project")
        ),
    )

    response = TestClient(app).get(f"/api/projects/{project_id}/chats")

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_chat_archive_and_title_are_project_display_choices(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    url = f"/api/projects/{app.state.default_project_id}"
    first, second = str(uuid.uuid4()), str(uuid.uuid4())

    def archive(chat_id, archived):
        return client.post(f"{url}/chats/{chat_id}/archive", json={"archived": archived})

    def rename(chat_id, title):
        return client.post(f"{url}/chats/{chat_id}/title", json={"title": title})

    def pin(chat_id, pinned):
        return client.post(f"{url}/chats/{chat_id}/pin", json={"pinned": pinned})

    assert archive(first, True).json() == {"archived": [first], "titles": {}, "pinned": []}
    assert archive(first, True).json()["archived"] == [first]
    assert archive(second, True).status_code == 200
    assert set(client.get(f"{url}/chat-display").json()["archived"]) == {first, second}
    assert archive(first, False).json()["archived"] == [second]
    assert archive("not-a-uuid", True).status_code == 422
    # A title survives an archive round trip and clears back to the derived name.
    assert rename(second, "  Loss   sweep ").json() == {
        "archived": [second],
        "titles": {second: "Loss sweep"},
        "pinned": [],
    }
    assert archive(second, False).json() == {
        "archived": [],
        "titles": {second: "Loss sweep"},
        "pinned": [],
    }
    assert rename(second, "x" * 121).status_code == 422
    assert rename(second, " ").json() == {"archived": [], "titles": {}, "pinned": []}
    # Pins list newest first, and an unpinned chat with no other choice leaves no row.
    assert pin(first, True).json()["pinned"] == [first]
    assert pin(second, True).json()["pinned"] == [second, first]
    assert pin(first, False).json()["pinned"] == [second]
    assert pin(second, False).json() == {"archived": [], "titles": {}, "pinned": []}
    assert client.get(f"{url}/chat-display").json() == {"archived": [], "titles": {}, "pinned": []}


def test_chat_read_marker_only_moves_forward(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    url = f"/api/projects/{app.state.default_project_id}"
    chat_id = str(uuid.uuid4())

    def mark(read_through):
        return client.post(f"{url}/chats/{chat_id}/read", json={"read_through": read_through})

    initial = client.get(f"{url}/chat-reads").json()
    assert initial["reads"] == {} and initial["latest_finished"] == {} and initial["baseline"]
    client.post(f"{url}/chats/{chat_id}/archive", json={"archived": True})
    assert client.get(f"{url}/chat-reads").json()["archived"] == [chat_id]
    later = "2026-09-27T12:00:00.000000+00:00"
    assert mark(later).json()["reads"] == {chat_id: later}
    # 09:00-02:00 is 11:00 UTC and 13:00+01:00 is 12:00 UTC, so neither moves it.
    assert mark("2026-09-27T09:00:00-02:00").json()["reads"] == {chat_id: later}
    assert mark("2026-09-27T13:00:00+01:00").json()["reads"] == {chat_id: later}
    assert mark("2026-09-27T12:30:00Z").json()["reads"] == {
        chat_id: "2026-09-27T12:30:00.000000+00:00"
    }
    assert mark("2026-09-27T12:30:00").status_code == 422
    assert (
        client.post(f"{url}/chats/not-a-uuid/read", json={"read_through": later}).status_code == 422
    )
    assert client.get(f"{url}/chat-reads").json()["baseline"] == initial["baseline"]


def test_browser_preference_is_local_and_snapshotted_at_admission(manifest, tmp_path):
    from rcp.runs.chat_admission import admit_fresh_chat_turn

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    store = app.state.catalog.store
    project_id = app.state.default_project_id
    chat_id = str(uuid.uuid4())
    url = f"/api/projects/{project_id}/chats/{chat_id}/browser"
    request = RunRequest(
        chat_id=chat_id, chat_scope="project", message="hello", browser_requested=True
    )
    _append_chat_exchange(app.state.service, request, "answer", None, None)
    before = app.state.service.chat_transcript(chat_id)
    assert client.get(url).json() == {"browser_requested": False}
    with admit_fresh_chat_turn(app.state.service, store, project_id, request) as admitted:
        assert not admitted.browser_requested
    assert client.put(url, json={"browser_requested": True}).status_code == 200
    with admit_fresh_chat_turn(app.state.service, store, project_id, request) as admitted:
        assert admitted.browser_requested
        assert client.put(url, json={"browser_requested": False}).status_code == 200
        assert admitted.browser_requested
    assert app.state.service.chat_transcript(chat_id) == before
    assert client.get(url).json() == {"browser_requested": False}
    assert client.put(url, json={"browser_requested": True, "env": {}}).status_code == 422
