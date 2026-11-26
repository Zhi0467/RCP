"""The reader layout RCP gives an episode report written as ``article.report``.

The report stays the agent's stored HTML; the layout is added when it is served,
so every report shows the current layout and one without the skeleton is unchanged.
"""

from __future__ import annotations

import importlib.resources
import re
from html.parser import HTMLParser

from rcp.artifact_comments import selection_frame_addon
from rcp.artifact_views import combined_addon
from rcp.artifacts import FrameAddon

_VOID = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    ]
)
_PAGE = frozenset({"html", "body"})


class _ReportArticleFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.open: list[str] = []
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "article" and set(self.open) <= _PAGE:
            classes = (dict(attrs).get("class") or "").split()
            self.found = self.found or "report" in classes
        if tag not in _VOID:
            self.open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.open:
            del self.open[len(self.open) - 1 - self.open[::-1].index(tag) :]


def is_reader_report(document: str) -> bool:
    """Whether the page body holds the ``<article class="report">`` the layout reads."""

    finder = _ReportArticleFinder()
    finder.feed(document)
    finder.close()
    return finder.found


_HEAD = re.compile(r"<head\b[^>]*>", re.IGNORECASE)


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
    if head is None:
        return reader + document
    return document[: head.end()] + reader + document[head.end() :]
