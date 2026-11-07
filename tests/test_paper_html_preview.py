from __future__ import annotations

import base64
from contextlib import contextmanager

import pytest

from rcp.paper import PaperLive, PaperOp
from rcp.paper.html_preview import inline_html_images, read_html_preview
from rcp.paper.persistence import PaperSnapshotReader
from rcp.storage import AppStore
from rcp.transport import LocalStateWorkspace

from .paper_helpers import apply, human_edit, start_paper

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect id="mark" width="7" height="9"/></svg>'


@pytest.fixture
def paper(tmp_path):
    store = AppStore(tmp_path / "state.sqlite")
    root = tmp_path / "state"
    root.mkdir()
    live = start_paper(PaperLive(store, lambda _: LocalStateWorkspace(root, str(root))))
    apply(
        live, [PaperOp(kind="create", file_id="page", path="blog/main.html", text="<p>Draft</p>")]
    )
    return live


@contextmanager
def preview(live, revision=None):
    with live.store.connection() as connection:
        connection.execute("BEGIN")
        reader = PaperSnapshotReader(connection)
        prepared = read_html_preview(reader, "p", "page", revision)
        yield prepared, reader


def image(live, path="figures/plot.svg", file_id="image", data=SVG):
    apply(live, [PaperOp(kind="create", file_id=file_id, path=path, text=data.decode())])


def test_relative_images_use_only_the_captured_saved_tree_and_preserve_svg_fragments(paper):
    image(paper)
    human_edit(paper, "page", '<img alt="plot" src="../figures/plot.svg?download=1#mark">')
    with preview(paper) as (prepared, reader):
        revision = prepared.revision
        assert prepared.warnings == []
        rendered = inline_html_images(prepared, reader, "p").decode()
        assert "data:image/svg+xml;base64," + base64.b64encode(SVG).decode() + "#mark" in rendered
    human_edit(paper, "image", SVG.decode().replace('width="7"', 'width="17"'))
    with preview(paper, revision) as (old, reader):
        assert inline_html_images(old, reader, "p").decode() == rendered
    with preview(paper) as (current, reader):
        assert current.key != prepared.key
        assert inline_html_images(current, reader, "p").decode() != rendered


def test_state_tracks_only_referenced_asset_identity_path_and_bytes(paper):
    human_edit(paper, "page", '<img src="../figures/plot.svg">')
    with preview(paper) as (missing, _):
        assert missing.warnings
    image(paper)
    with preview(paper) as (ready, _):
        assert ready.key != missing.key
        assert not ready.warnings
    apply(paper, [PaperOp(kind="create", path="unrelated.md", text="Unrelated")])
    with preview(paper) as (unchanged, _):
        assert unchanged.key == ready.key and unchanged.revision != ready.revision
    apply(paper, [PaperOp(kind="rename", file_id="image", path="figures/renamed.svg")])
    with preview(paper) as (renamed, _):
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
    apply(paper, [PaperOp(kind="create", path="notes.txt", text="not an image")])
    human_edit(paper, "page", f'<img alt="plot" src="{source}">')
    with preview(paper) as (prepared, reader):
        assert prepared.warnings
        rendered = inline_html_images(prepared, reader, "p").decode()
        assert 'alt="plot"' in rendered
        assert "src=" not in rendered


def test_inline_images_keep_existing_sources_without_reading_other_tree_bodies(paper, monkeypatch):
    human_edit(paper, "page", '<img src="data:image/png;base64,aGVsbG8="><img src="blob:local">')
    original = PaperSnapshotReader.raw_file
    reads = []

    def raw(reader, project_id, file_id, revision=None):
        reads.append(file_id)
        return original(reader, project_id, file_id, revision)

    monkeypatch.setattr(PaperSnapshotReader, "raw_file", raw)
    with preview(paper) as (prepared, reader):
        rendered = inline_html_images(prepared, reader, "p").decode()
        assert "data:image/png;base64,aGVsbG8=" in rendered and "blob:local" in rendered
        assert not prepared.warnings
    assert reads == ["page"]


def test_asset_budgets_are_checked_before_loading_and_repeated_reads_are_deduplicated(
    paper, monkeypatch
):
    from rcp import limits

    image(paper)
    human_edit(paper, "page", '<img src="../figures/plot.svg"><img src="../figures/plot.svg">')
    original = PaperSnapshotReader.raw_file
    reads = []

    def raw(reader, project_id, file_id, revision=None):
        reads.append(file_id)
        return original(reader, project_id, file_id, revision)

    monkeypatch.setattr(PaperSnapshotReader, "raw_file", raw)
    with preview(paper) as (prepared, reader):
        assert not prepared.warnings
        assert inline_html_images(prepared, reader, "p").count(b"data:image/") == 2
    assert reads == ["page", "image"]
    reads.clear()
    monkeypatch.setattr(limits, "PAPER_HTML_PREVIEW_MAX_ASSET_BYTES", len(SVG) - 1)
    with preview(paper) as (prepared, reader):
        assert prepared.warnings
        assert b"data:image/" not in inline_html_images(prepared, reader, "p")
    assert reads == ["page"]


def test_repeated_references_consume_expanded_output_budget(paper, monkeypatch):
    from rcp import limits

    image(paper)
    human_edit(paper, "page", '<img src="../figures/plot.svg"><img src="../figures/plot.svg">')
    encoded = "data:image/svg+xml;base64," + base64.b64encode(SVG).decode()
    monkeypatch.setattr(
        limits, "PAPER_HTML_PREVIEW_MAX_OUTPUT_BYTES", len(f'<img src="{encoded}"><img>')
    )
    with preview(paper) as (prepared, reader):
        assert prepared.warnings
        rendered = inline_html_images(prepared, reader, "p")
        assert rendered.count(b"data:image/") == 1
        assert len(rendered) <= limits.PAPER_HTML_PREVIEW_MAX_OUTPUT_BYTES


def test_unique_asset_bytes_consume_aggregate_budget_before_any_asset_body_read(paper, monkeypatch):
    from rcp import limits

    image(paper)
    other = SVG.replace(b'width="7"', b'width="8"')
    image(paper, "figures/other.svg", "other", other)
    human_edit(paper, "page", '<img src="../figures/plot.svg"><img src="../figures/other.svg">')
    monkeypatch.setattr(limits, "PAPER_HTML_PREVIEW_MAX_ASSET_TOTAL_BYTES", len(SVG))
    original = PaperSnapshotReader.raw_file
    reads = []

    def raw(reader, project_id, file_id, revision=None):
        reads.append(file_id)
        return original(reader, project_id, file_id, revision)

    monkeypatch.setattr(PaperSnapshotReader, "raw_file", raw)
    with preview(paper) as (prepared, reader):
        assert prepared.warnings
        assert reads == ["page"]
        assert inline_html_images(prepared, reader, "p").count(b"data:image/") == 1
    assert reads == ["page", "image"]


def test_binary_images_discard_fragments_and_use_only_the_captured_revision(paper):
    png = b"\x89PNG\r\n\x1a\nfixture"
    apply(
        paper,
        [
            PaperOp(
                kind="create", file_id="png", path="figures/plot.png", file_kind="binary", data=png
            )
        ],
    )
    human_edit(paper, "page", '<IMG\n alt="binary &amp; image" src="../figures/plot.png#drop"/>')
    with preview(paper) as (prepared, reader):
        revision = prepared.revision
        before = inline_html_images(prepared, reader, "p")
        assert b"#drop" not in before
        assert base64.b64encode(png) in before
        assert b"binary &amp; image" in before
    apply(paper, [PaperOp(kind="binary", file_id="png", data=png + b"next")])
    with preview(paper, revision) as (old, reader):
        assert inline_html_images(old, reader, "p") == before


def test_concurrent_saved_image_change_does_not_mix_revisions(paper):
    image(paper)
    human_edit(paper, "page", '<img src="../figures/plot.svg">')
    with preview(paper) as (prepared, reader):
        human_edit(paper, "image", SVG.decode().replace('width="7"', 'width="99"'))
        rendered = inline_html_images(prepared, reader, "p")
        assert base64.b64encode(SVG) in rendered


def test_image_replacement_preserves_authored_script_and_mixed_line_endings(paper):
    image(paper)
    prefix = '<!doctype html>\r<script>const example="<img src=missing.svg>";\n</script>\u2028<p>Draft</p>\n'
    human_edit(paper, "page", prefix + '<img src="../figures/plot.svg">')
    with preview(paper) as (prepared, reader):
        rendered = inline_html_images(prepared, reader, "p").decode()
        assert rendered.startswith(prefix)
        assert rendered.count("data:image/") == 1
        assert prepared.warnings == []


def test_exact_preview_budgets_are_inclusive(paper, monkeypatch):
    from rcp import limits

    image(paper, "figures/my plot.svg")
    human_edit(
        paper, "page", '<img src="../figures/my plot.svg"><img src="../figures/my%20plot.svg">'
    )
    with preview(paper) as (prepared, reader):
        rendered = inline_html_images(prepared, reader, "p")
        assert not prepared.warnings
    monkeypatch.setattr(limits, "PAPER_HTML_PREVIEW_MAX_ASSET_BYTES", len(SVG))
    monkeypatch.setattr(limits, "PAPER_HTML_PREVIEW_MAX_ASSET_TOTAL_BYTES", len(SVG))
    monkeypatch.setattr(limits, "PAPER_HTML_PREVIEW_MAX_OUTPUT_BYTES", len(rendered))
    with preview(paper) as (prepared, reader):
        assert not prepared.warnings
        assert inline_html_images(prepared, reader, "p") == rendered


def test_a_non_html_preview_refuses_before_reading_its_body(paper, monkeypatch):
    image(paper)

    def no_body(*_args, **_kwargs):
        raise AssertionError("A non-HTML body was loaded")

    monkeypatch.setattr(PaperSnapshotReader, "raw_file", no_body)
    with paper.store.connection() as connection:
        connection.execute("BEGIN")
        with pytest.raises(KeyError):
            read_html_preview(PaperSnapshotReader(connection), "p", "image")
