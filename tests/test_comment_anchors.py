from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from rcp.artifact_comments import supports_comments
from rcp.comments.models import COMMENT_MEDIA_TYPES, CommentAnchor
from rcp.comments.render import render_anchor, render_thread
from rcp.service import ArtifactContextRequest, ArtifactSelection, RunRequest
from rcp.storage import AgentTaskRecord, AppStore


@pytest.mark.parametrize("kind", ["whole", "text", "box", "element", "pdf"])
def test_anchor_kinds_and_bounds(kind):
    fields = {
        "whole": {},
        "text": {"text": "quote"},
        "box": {
            "rect": {"x": 0, "y": 0, "width": 0.5, "height": 0.5},
            "viewport": {"width": 800, "height": 600},
        },
        "element": {"path": "#title", "label": "Title", "text": "quote"},
        "pdf": {"page": 2, "rect": {"x": 0, "y": 0, "width": 0.5, "height": 0.5}, "text": "quote"},
    }[kind]
    body = {"kind": kind, **fields, "where": "page:2", "drawn_version": "v1", "comment": "x" * 6000}
    anchor = TypeAdapter(CommentAnchor).validate_python(body)
    assert TypeAdapter(ArtifactSelection).validate_python(body) == anchor
    assert "page:2" in render_anchor(anchor)
    with pytest.raises(ValidationError):
        TypeAdapter(CommentAnchor).validate_python({**body, "where": "x" * 257})
    with pytest.raises(ValidationError):
        TypeAdapter(CommentAnchor).validate_python({**body, "comment": "x" * 6001})


def test_old_writer_context_row_replay(tmp_path):
    old_request = '{"source":"task","operation_id":"old-op","artifact_id":"aaaaaaaaaaaaaaaaaaaaaaaa","edit_now":true,"base_version":"v1","selections":[{"kind":"text","text":"old quote","surrounding_text":"the old quote","comment":"fix"},{"kind":"box","rect":{"x":0.1,"y":0.2,"width":0.3,"height":0.4},"viewport":{"width":800,"height":600},"labels":"legacy label","comment":"move"}]}'
    store = AppStore(tmp_path / "old-writer.sqlite")
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="replay",
            project_id="project",
            kind="project_chat",
            status="queued",
            request={},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="",
        )
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=? WHERE operation_id=?",
            ('{"message":"Old comment","artifact_context":' + old_request + "}", "replay"),
        )
    restored = AppStore(tmp_path / "old-writer.sqlite").agent_task("replay")
    assert restored is not None
    parsed = RunRequest.model_validate(restored.request).artifact_context
    assert parsed is not None
    assert parsed.selections[0].kind == "text"
    assert parsed.selections[1].kind == "box"
    assert parsed.selections[0].text == "old quote"
    assert parsed.selections[1].labels == "legacy label"
    assert parsed.model_dump()["selections"][1]["elements"] is None
    assert ArtifactContextRequest.model_validate_json(parsed.model_dump_json()) == parsed
    assert json.loads(old_request)["edit_now"] == parsed.edit_now


def test_media_types_and_thread_rendering():
    assert supports_comments("application/pdf")
    assert not supports_comments("application/octet-stream")
    assert COMMENT_MEDIA_TYPES["pdf"] == {"application/pdf"}
    thread = {
        "anchor": {"kind": "text", "text": "quote", "where": "file:12"},
        "posts": [
            {
                "author": {"display_name": "Alice"},
                "text": "Ask <@member:b>",
                "mentions": [{"id": "b", "display_name": "Bob"}],
                "created_at": "2026-01-01",
            },
            {
                "author": {"display_name": "Bob"},
                "text": "Later",
                "mentions": [],
                "created_at": "2026-01-03",
            },
        ],
    }
    rendered = render_thread(thread, until="2026-01-02")
    assert "quote" in rendered and "file:12" in rendered
    assert "Alice: Ask @Bob" in rendered
    assert "Later" not in rendered
    assert "Bob: Later" in render_thread(thread)


def test_box_render_preserves_element_relative_region():
    anchor = {
        "kind": "box",
        "rect": {"x": 0, "y": 0, "width": 0.5, "height": 0.5},
        "viewport": {"width": 800, "height": 600},
        "elements": [
            {"path": "#figure", "region": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}}
        ],
    }
    rendered = render_anchor(anchor)
    assert "#figure" in rendered
    assert "x=0.1, y=0.2, width=0.3, height=0.4" in rendered
