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


def downloadable_report(document: str) -> str:
    """The stored report with the reader layout inlined, so a saved copy reads the same."""

    style, script = _reader_files()
    reader = f"<style>{style}</style><script>{script}</script>"
    head = _HEAD.search(document)
    prologue = _PROLOGUE.match(document)
    at = head.end() if head else prologue.end() if prologue else 0
    return document[:at] + reader + document[at:]
