from __future__ import annotations

import json
import re

import pytest

from rcp.limits import (
    LIVE_ARTIFACT_MAX_NEEDS,
    LIVE_ARTIFACT_MAX_PATTERN_CHARS,
    LIVE_ARTIFACT_MAX_PATTERN_SEGMENTS,
)
from rcp.live_artifacts import (
    LiveTag,
    parse_live_tag,
)
from rcp.skill_registry import official_registry

from .helpers import assert_frozen_backend_ships


def _html(needs: list[dict], **extra: object) -> str:
    return (
        '<script type="application/json" id="rcp-live">'
        + json.dumps({"version": 1, "needs": needs, **extra})
        + "</script>"
    )


def test_skill_examples_validate_against_live_schema() -> None:
    assert_frozen_backend_ships("skills/live-pages/SKILL.md")
    body = official_registry().package_body("skill", "live-pages")
    examples = re.findall(r"```html\n(.*?)\n```", body, re.DOTALL)
    assert len(examples) == 3
    parsed = [parse_live_tag(example, allow_episode=True) for example in examples]
    assert all(isinstance(tag, LiveTag) for tag in parsed)
    assert {need.kind for tag in parsed for need in tag.needs} == {
        "job",
        "file",
        "files",
        "episode",
    }
    with pytest.raises(ValueError):
        parse_live_tag(examples[-1])


def test_static_page_has_no_live_declaration() -> None:
    assert parse_live_tag("<html><p>Finished</p></html>") is None


@pytest.mark.parametrize(
    "needs",
    [
        [{"kind": "job", "key": ""}],
        [{"kind": "node", "id": ""}],
        [{"kind": "file", "path": "relative", "read": "tail", "format": "text"}],
        [{"kind": "file", "path": "/project/../private", "read": "tail", "format": "text"}],
        [{"kind": "file", "path": "/project/a", "read": "glob", "format": "text"}],
        [{"kind": "file", "path": "/project/a", "read": "tail", "format": "binary"}],
        [{"kind": "episode", "id": "another-episode"}],
        [{"kind": "unknown"}],
        [],
        [{"kind": "job", "key": "one"}] * (LIVE_ARTIFACT_MAX_NEEDS + 1),
    ],
)
def test_invalid_declarations_fail_closed(needs: list[dict]) -> None:
    with pytest.raises(ValueError):
        parse_live_tag(_html(needs), allow_episode=True)


@pytest.mark.parametrize(
    "html",
    [
        '<script id="rcp-live">{}</script>',
        '<div type="application/json" id="rcp-live">{}</div>',
        '<script type="application/json" id="rcp-live">{',
        _html([{"kind": "job", "key": "a"}]) * 2,
        _html([{"kind": "job", "key": "a"}], version=2),
        _html([{"kind": "job", "key": "a"}], paths=["/secret"]),
    ],
)
def test_invalid_tags_fail_closed(html: str) -> None:
    with pytest.raises(ValueError):
        parse_live_tag(html)


@pytest.mark.parametrize(
    "pattern",
    [
        "../x",
        "/x",
        "**/x",
        "a/**b",
        "a/../x",
        "a//x",
        "./x",
        "a/",
        "",
        "a/" * LIVE_ARTIFACT_MAX_PATTERN_SEGMENTS + "x",
        "x" * (LIVE_ARTIFACT_MAX_PATTERN_CHARS + 1),
    ],
)
def test_folder_patterns_refuse_unbounded_or_unsafe_paths(pattern):
    with pytest.raises(ValueError):
        parse_live_tag(
            _html(
                [
                    dict(
                        kind="files",
                        dir="/project/run",
                        pattern=pattern,
                        read="whole",
                        format="text",
                    )
                ]
            )
        )
