from __future__ import annotations

import pytest

from rcp.artifacts import html_document_title
from rcp.limits import ARTIFACT_DISPLAY_TITLE_MAX_CHARS


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (
            "<!doctype html><html><head><title>Reset &amp; stream\n ·  Partial report</title>"
            "</head><body><svg><title>Chart legend</title></svg></body></html>",
            "Reset & stream · Partial report",
        ),
        (
            "<TITLE>Compaction fidelity improves</TITLE><h1>Result</h1>",
            "Compaction fidelity improves",
        ),
        (
            "<svg><title>Chart title</title></svg><title>Research finding</title>",
            "Research finding",
        ),
        ("<template><title>Template title</title></template><title>Finding</title>", "Finding"),
        ("<title>First finding</title><title>Later title</title>", "First finding"),
        ("<svg><title>Diagram only</title></svg>", None),
        ("<h1>Report without a document title</h1>", None),
        ("<title> \n </title>", None),
    ],
)
def test_html_document_title_uses_document_metadata(document, expected):
    assert html_document_title(document) == expected


def test_oversized_document_title_keeps_a_bounded_readable_prefix():
    subject = "Adaptation preserves recall " * ARTIFACT_DISPLAY_TITLE_MAX_CHARS
    title = html_document_title(f"<title>{subject}</title>")
    assert title is not None
    assert len(title) <= ARTIFACT_DISPLAY_TITLE_MAX_CHARS
    assert title.endswith("…")
    assert subject.startswith(title.removesuffix("…"))
