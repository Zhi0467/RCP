from __future__ import annotations

from rcp.report_reader import downloadable_report


def test_a_downloaded_report_carries_the_reader_inside_its_head() -> None:
    saved = downloadable_report("<html><HEAD lang='en'><title>T</title></HEAD><body></body></html>")
    head, _, rest = saved.partition("<title>")
    assert head.startswith("<html><HEAD lang='en'><style>")
    assert "installReportReader" in head
    assert rest == "T</title></HEAD><body></body></html>"


def test_a_report_without_a_head_keeps_its_doctype_first_when_downloaded() -> None:
    saved = downloadable_report("<!doctype html><html lang='en'><meta charset='utf-8'><p>x</p>")
    assert saved.startswith("<!doctype html><html lang='en'><style>")
    assert saved.endswith("</script><meta charset='utf-8'><p>x</p>")
