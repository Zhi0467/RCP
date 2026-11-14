from __future__ import annotations

import base64

import pytest

from rcp.paper.html_preview import inline_html_images, read_html_preview
from rcp.paper.models import PaperEntry, PaperSource, digest, paper_file_kind

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect id="mark" width="7" height="9"/></svg>'


class Tree:
    """A Paper state in memory; each source captures it and counts body reads."""

    def __init__(self):
        self.files: dict[str, tuple[str, bytes]] = {"blog/main.html": ("page", b"<p>Draft</p>")}
        self.reads: list[str] = []

    def put(self, file_id: str, data: bytes | str) -> None:
        path = next((path for path, (known, _) in self.files.items() if known == file_id), None)
        self.files[path] = (file_id, data.encode() if isinstance(data, str) else data)

    def add(self, path: str, file_id: str, data: bytes | str) -> None:
        self.files[path] = (file_id, data.encode() if isinstance(data, str) else data)

    def move(self, file_id: str, path: str) -> None:
        old = next(name for name, (known, _) in self.files.items() if known == file_id)
        self.files[path] = self.files.pop(old)

    def source(self, revision: int = 1) -> PaperSource:
        captured = dict(self.files)
        entries = [
            PaperEntry(file_id, path, paper_file_kind(path, data), digest(data), len(data))
            for path, (file_id, data) in sorted(captured.items())
        ]

        def read(entry: PaperEntry) -> bytes:
            self.reads.append(entry.file_id)
            return captured[entry.path][1]

        return PaperSource(revision, entries, read)


@pytest.fixture
def paper():
    return Tree()


def preview(tree: Tree, source: PaperSource | None = None):
    source = source or tree.source()
    return read_html_preview(source, "page"), source


def image(tree, path="figures/plot.svg", file_id="image", data=SVG):
    tree.add(path, file_id, data)


def test_relative_images_use_only_the_captured_tree_and_preserve_svg_fragments(paper):
    image(paper)
    paper.put("page", '<img alt="plot" src="../figures/plot.svg?download=1#mark">')
    prepared, captured = preview(paper)
    assert prepared.warnings == []
    rendered = inline_html_images(prepared, captured).decode()
    assert "data:image/svg+xml;base64," + base64.b64encode(SVG).decode() + "#mark" in rendered
    paper.put("image", SVG.decode().replace('width="7"', 'width="17"'))
    assert inline_html_images(prepared, captured).decode() == rendered
    current, source = preview(paper)
    assert current.key != prepared.key
    assert inline_html_images(current, source).decode() != rendered


def test_state_tracks_only_referenced_asset_identity_path_and_bytes(paper):
    paper.put("page", '<img src="../figures/plot.svg">')
    missing, _ = preview(paper)
    assert missing.warnings
    image(paper)
    ready, _ = preview(paper)
    assert ready.key != missing.key
    assert not ready.warnings
    paper.add("unrelated.md", "unrelated", "Unrelated")
    unchanged, _ = preview(paper, paper.source(revision=2))
    assert unchanged.key == ready.key and unchanged.revision != ready.revision
    paper.move("image", "figures/renamed.svg")
    renamed, _ = preview(paper)
    assert renamed.key != ready.key and renamed.warnings


@pytest.mark.parametrize(
    "source",
    [
        "../../outside.svg",
        "%2e%2e/%2e%2e/outside.svg",
        "/figures/plot.svg",
        "//example.invalid/plot.svg",
        "https://example.invalid/plot.svg",
        "file:///figures/plot.svg",
        "..%5cfigures/plot.svg",
        "../figures/%00plot.svg",
        "../figures/missing.svg",
        "../notes.txt",
    ],
)
def test_unavailable_image_references_are_inert_and_visible(paper, source):
    image(paper)
    paper.add("notes.txt", "notes", "not an image")
    paper.put("page", f'<img alt="plot" src="{source}">')
    prepared, captured = preview(paper)
    assert prepared.warnings
    rendered = inline_html_images(prepared, captured).decode()
    assert 'alt="plot"' in rendered
    assert "src=" not in rendered


def test_inline_images_keep_existing_sources_without_reading_other_tree_bodies(paper):
    paper.put("page", '<img src="data:image/png;base64,aGVsbG8="><img src="blob:local">')
    prepared, captured = preview(paper)
    rendered = inline_html_images(prepared, captured).decode()
    assert "data:image/png;base64,aGVsbG8=" in rendered and "blob:local" in rendered
    assert not prepared.warnings
    assert paper.reads == ["page"]


def test_asset_budgets_are_checked_before_loading_and_repeated_reads_are_deduplicated(
    paper, monkeypatch
):
    from rcp import limits

    image(paper)
    paper.put("page", '<img src="../figures/plot.svg"><img src="../figures/plot.svg">')
    prepared, captured = preview(paper)
    assert not prepared.warnings
    assert inline_html_images(prepared, captured).count(b"data:image/") == 2
    assert paper.reads == ["page", "image"]
    paper.reads.clear()
    monkeypatch.setattr(limits, "PAPER_HTML_PREVIEW_MAX_ASSET_BYTES", len(SVG) - 1)
    prepared, captured = preview(paper)
    assert prepared.warnings
    assert b"data:image/" not in inline_html_images(prepared, captured)
    assert paper.reads == ["page"]


def test_repeated_references_consume_expanded_output_budget(paper, monkeypatch):
    from rcp import limits

    image(paper)
    paper.put("page", '<img src="../figures/plot.svg"><img src="../figures/plot.svg">')
    encoded = "data:image/svg+xml;base64," + base64.b64encode(SVG).decode()
    monkeypatch.setattr(
        limits, "PAPER_HTML_PREVIEW_MAX_OUTPUT_BYTES", len(f'<img src="{encoded}"><img>')
    )
    prepared, captured = preview(paper)
    assert prepared.warnings
    rendered = inline_html_images(prepared, captured)
    assert rendered.count(b"data:image/") == 1
    assert len(rendered) <= limits.PAPER_HTML_PREVIEW_MAX_OUTPUT_BYTES


def test_unique_asset_bytes_consume_aggregate_budget_before_any_asset_body_read(paper, monkeypatch):
    from rcp import limits

    image(paper)
    image(paper, "figures/other.svg", "other", SVG.replace(b'width="7"', b'width="8"'))
    paper.put("page", '<img src="../figures/plot.svg"><img src="../figures/other.svg">')
    monkeypatch.setattr(limits, "PAPER_HTML_PREVIEW_MAX_ASSET_TOTAL_BYTES", len(SVG))
    prepared, captured = preview(paper)
    assert prepared.warnings
    assert paper.reads == ["page"]
    assert inline_html_images(prepared, captured).count(b"data:image/") == 1
    assert paper.reads == ["page", "image"]


def test_binary_images_discard_fragments_and_use_only_the_captured_tree(paper):
    png = b"\x89PNG\r\n\x1a\nfixture"
    paper.add("figures/plot.png", "png", png)
    paper.put("page", '<IMG\n alt="binary &amp; image" src="../figures/plot.png#drop"/>')
    prepared, captured = preview(paper)
    before = inline_html_images(prepared, captured)
    assert b"#drop" not in before
    assert base64.b64encode(png) in before
    assert b"binary &amp; image" in before
    paper.put("png", png + b"next")
    assert inline_html_images(prepared, captured) == before


def test_image_replacement_preserves_authored_script_and_mixed_line_endings(paper):
    image(paper)
    prefix = '<!doctype html>\r<script>const example="<img src=missing.svg>";\n</script>\u2028<p>Draft</p>\n'
    paper.put("page", prefix + '<img src="../figures/plot.svg">')
    prepared, captured = preview(paper)
    rendered = inline_html_images(prepared, captured).decode()
    assert rendered.startswith(prefix)
    assert rendered.count("data:image/") == 1
    assert prepared.warnings == []


def test_exact_preview_budgets_are_inclusive(paper, monkeypatch):
    from rcp import limits

    image(paper, "figures/my plot.svg")
    paper.put("page", '<img src="../figures/my plot.svg"><img src="../figures/my%20plot.svg">')
    prepared, captured = preview(paper)
    rendered = inline_html_images(prepared, captured)
    assert not prepared.warnings
    monkeypatch.setattr(limits, "PAPER_HTML_PREVIEW_MAX_ASSET_BYTES", len(SVG))
    monkeypatch.setattr(limits, "PAPER_HTML_PREVIEW_MAX_ASSET_TOTAL_BYTES", len(SVG))
    monkeypatch.setattr(limits, "PAPER_HTML_PREVIEW_MAX_OUTPUT_BYTES", len(rendered))
    prepared, captured = preview(paper)
    assert not prepared.warnings
    assert inline_html_images(prepared, captured) == rendered


def test_a_non_html_preview_refuses_before_reading_its_body(paper):
    image(paper)
    with pytest.raises(KeyError):
        read_html_preview(paper.source(), "image")
    assert paper.reads == []
