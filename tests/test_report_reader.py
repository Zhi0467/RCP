from __future__ import annotations

import pytest

from rcp.report_reader import downloadable_report, is_reader_report


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (
            "<!doctype html><html><head><title>T</title></head>"
            "<body><article class='report'><p>x</p></article></body></html>",
            True,
        ),
        ("<article class='lead report'><h1>Implied body</h1></article>", True),
        ("<body><main><article class='report'></article></main></body>", False),
        ("<body><article><p>No class</p></article></body>", False),
        ("<body><p>Open<br><img src=x></p><article class='report'></article></body>", True),
    ],
)
def test_report_article_must_sit_directly_in_the_page_body(document: str, expected: bool) -> None:
    assert is_reader_report(document) is expected


def test_a_downloaded_report_carries_the_reader_inside_its_head() -> None:
    saved = downloadable_report("<html><HEAD lang='en'><title>T</title></HEAD><body></body></html>")
    head, _, rest = saved.partition("<title>")
    assert head.startswith("<html><HEAD lang='en'><style>")
    assert "installReportReader" in head
    assert rest == "T</title></HEAD><body></body></html>"
