"""The reader layout RCP gives an episode report written as ``article.report``.

The report stays the agent's stored HTML; the layout is added when it is served,
so every report shows the current layout and one without the skeleton is unchanged.
"""

from __future__ import annotations

import importlib.resources
import re

from rcp.artifact_comments import selection_frame_addon
from rcp.artifact_views import combined_addon
from rcp.artifacts import FrameAddon

_HEAD = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_STYLE = re.compile(r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.IGNORECASE | re.DOTALL)
_PROLOGUE = re.compile(r"\s*(?:<!doctype[^>]*>\s*)?(?:<html\b[^>]*>)?", re.IGNORECASE)


def _reader_files() -> tuple[str, str]:
    resources = importlib.resources.files("rcp")
    return (
        resources.joinpath("report_reader.css").read_text("utf-8"),
        resources.joinpath("report_reader.js").read_text("utf-8"),
    )


def report_frame_addon() -> FrameAddon:
    """Comment selection plus the reader layout, for every route that shows a report."""

    style, script = _reader_files()
    reader = FrameAddon(frame_style=style, frame_script=script, wrapper_script="")
    return combined_addon(selection_frame_addon(), reader)


def layered_report(document: str) -> str:
    """Put the report's own style blocks in the ``report`` cascade layer.

    The reader stylesheet orders that layer below its own, so a reset, a page width
    or a locked viewport the agent wrote cannot undo the reading layout.
    """

    return _STYLE.sub(lambda match: f"{match[1]}@layer report{{{match[2]}}}{match[3]}", document)


def reader_report_bytes(data: bytes) -> bytes:
    return layered_report(data.decode("utf-8")).encode("utf-8")


def downloadable_report(document: str) -> str:
    """The stored report with the reader layout inlined, so a saved copy reads the same.

    The charset leads the inlined reader so a copy opened from disk still decodes as
    UTF-8; a declaration the report wrote later in its head would come too late.
    """

    style, script = _reader_files()
    reader = f'<meta charset="utf-8"><style>{style}</style><script>{script}</script>'
    document = layered_report(document)
    head = _HEAD.search(document)
    prologue = _PROLOGUE.match(document)
    at = head.end() if head else prologue.end() if prologue else 0
    return document[:at] + reader + document[at:]
