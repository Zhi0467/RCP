from __future__ import annotations

from html.parser import HTMLParser

import pytest

from rcp import artifact_views
from rcp.artifact_comments import selection_frame_addon
from rcp.artifacts import html_preview_document


class Elements(HTMLParser):
    def __init__(self, document: str):
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text = ""
        self.feed(document)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text += data


@pytest.mark.parametrize("kind", ["markdown", "text"])
def test_text_previews_are_script_free_and_escape_source(kind: str) -> None:
    source = b"<script>alert(1)</script>\n[link](https://example.test)\n![alt](https://example.test/image)\n$x$"
    document, csp = (
        artifact_views.markdown_document(source)
        if kind == "markdown"
        else artifact_views.text_document("source.txt", source)
    )
    parsed = Elements(document)
    assert not {"script", "img", "a"} & {tag for tag, _ in parsed.tags}
    assert all("href" not in attrs for _, attrs in parsed.tags)
    assert "<script>alert(1)</script>" in parsed.text
    assert "$x$" in parsed.text
    assert "sandbox" in csp and "allow-scripts" not in csp


def test_markdown_renders_gfm_tables() -> None:
    document, _ = artifact_views.markdown_document(b"| a | b |\n|---|---|\n| 1 | 2 |\n")
    tags = [tag for tag, _ in Elements(document).tags]
    assert tags.count("th") == 2 and tags.count("td") == 2


@pytest.mark.parametrize("kind", ["markdown", "text"])
@pytest.mark.parametrize("budget", ["bytes", "lines"])
def test_render_budgets_bound_source_before_parsing(monkeypatch, kind: str, budget: str) -> None:
    monkeypatch.setattr(
        artifact_views, "ARTIFACT_PREVIEW_MAX_BYTES", 8 if budget == "bytes" else 100
    )
    monkeypatch.setattr(
        artifact_views, "ARTIFACT_PREVIEW_MAX_LINES", 2 if budget == "lines" else 100
    )
    source = b"one\ntwo\nexcluded"
    document, _ = (
        artifact_views.markdown_document(source)
        if kind == "markdown"
        else artifact_views.text_document("source.txt", source)
    )
    parsed = Elements(document)
    assert any(attrs.get("id") == "truncated" for _, attrs in parsed.tags)
    assert "excluded" not in parsed.text


@pytest.mark.parametrize("name", ["data.json", "notebook.ipynb"])
def test_json_pretty_printing_preserves_order_and_respects_budget(monkeypatch, name: str) -> None:
    source = b'{"z":1,"a":2}'
    pretty, _ = artifact_views.text_document(name, source)
    assert '"z": 1' in Elements(pretty).text
    assert pretty.index("z") < pretty.index("&quot;a&quot;")
    monkeypatch.setattr(artifact_views, "ARTIFACT_PREVIEW_MAX_BYTES", len(source))
    raw, _ = artifact_views.text_document(name, source)
    assert source.decode() in Elements(raw).text
    assert not any(attrs.get("id") == "truncated" for _, attrs in Elements(raw).tags)


def test_html_selection_addon_is_explicit() -> None:
    readonly, _ = html_preview_document(b"<p>source</p>")
    editable, _ = html_preview_document(b"<p>source</p>", frame_addon=selection_frame_addon())
    assert "installArtifactSelection" not in readonly
    assert "rcp-artifact-selection" not in readonly
    assert "rcp-reference" in readonly
    assert "installArtifactSelection" in editable
    assert "allow-same-origin" not in editable


def test_html_preview_keeps_only_inline_image_sources() -> None:
    inline = "data:image/png;base64,iVBORw0KGgo="
    document, wrapper_csp = html_preview_document(
        f"<img src=' {inline}'><img src='http://example.test/a.png'>"
        "<script src='data:text/javascript,alert(1)'></script>".encode()
    )
    srcdoc = next(attrs["srcdoc"] for tag, attrs in Elements(document).tags if tag == "iframe")
    artifact = Elements(srcdoc or "").tags
    assert [attrs.get("src") for tag, attrs in artifact if tag == "img"] == [f" {inline}", None]
    assert all("src" not in attrs for tag, attrs in artifact if tag == "script")
    # The srcdoc frame inherits the wrapper policy, so it must admit the same sources.
    directives = dict(part.strip().split(" ", 1) for part in wrapper_csp.split(";"))
    assert set(directives["img-src"].split()) == {"data:", "blob:"}
