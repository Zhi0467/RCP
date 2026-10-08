"""An artifact a reply embeds renders through the one viewer, in its inline presentation."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

from rcp.agents.artifact_contract import artifact_contract
from rcp.artifact_theme import ARTIFACT_THEME_TOKENS, artifact_theme_css, theme_tokens
from rcp.artifact_views import (
    InlineAppearance,
    artifact_content,
    artifact_viewer_document,
)
from rcp.artifacts import (
    AgentArtifactDescriptor,
    classify_artifact_bytes,
    descriptor_for,
    html_preview_document,
)
from rcp.limits import ARTIFACT_INLINE_INITIAL_HEIGHT_PX, ARTIFACT_INLINE_MAX_HEIGHT_PX
from rcp.storage import Artifact

from .helpers import assert_frozen_backend_ships, create_named_app, signed_in_client

PAGE = b"<!DOCTYPE html><html><head><title>Game</title></head><body><canvas></canvas></body></html>"


def _srcdoc(document: str) -> str:
    match = re.search(r'srcdoc="([^"]*)"', document)
    assert match, document[:400]
    return html.unescape(match.group(1))


def _descriptor(name: str, media_type: str) -> AgentArtifactDescriptor:
    return AgentArtifactDescriptor(
        artifact_id="0123456789abcdef01234567",
        name=name,
        media_type=media_type,  # pyright: ignore[reportArgumentType]
        size_bytes=64,
    )


def test_a_declared_doctype_leads_the_page_so_it_renders_in_standards_mode() -> None:
    document, _ = html_preview_document(PAGE)
    page = _srcdoc(document)
    assert page.startswith("<!DOCTYPE html><meta http-equiv=")
    assert page.count("<!DOCTYPE html>") == 1

    spaced, _ = html_preview_document(b"\n  <!doctype html><p>x</p>")
    assert _srcdoc(spaced).startswith("<!doctype html>")

    # A page without a doctype keeps the mode its author's browser gives it.
    bare, _ = html_preview_document(b"<p>x</p><!doctype html>")
    assert _srcdoc(bare).startswith("<meta http-equiv=")
    assert _srcdoc(bare).count("<!doctype html>") == 1


@pytest.mark.parametrize(("theme", "mode"), [("aqua", "dark"), ("classic", "light")])
def test_inline_html_paints_with_the_reply_theme_and_reports_its_height(theme, mode) -> None:
    appearance = InlineAppearance(theme, mode)
    document, media_type, csp = artifact_content("game.html", "text/html", PAGE, inline=appearance)
    assert media_type == "text/html"
    page = _srcdoc(str(document))
    # Every frame in the chain names the same scheme, or the browser paints an
    # opaque backdrop behind the nested page.
    assert f":root{{color-scheme:{mode}}}" in str(document).split("<iframe")[0]
    assert artifact_theme_css(theme, mode) in page
    for name, value in theme_tokens(theme, mode).items():
        assert f"--rcp-{name}:{value};" in page
    assert "height:auto!important" in page
    assert "send({kind:'rcp-artifact-size',height})" in page
    assert "type:'rcp-artifact-size'" in str(document)
    # The theme precedes the page's own markup, so the page's rules still win.
    assert page.index(artifact_theme_css(theme, mode)) < page.index("<html>")
    # The opaque sandbox and the page's own policy are unchanged.
    assert 'sandbox="allow-scripts"' in str(document)
    assert "connect-src 'none'" in html.unescape(page)
    assert "allow-same-origin" not in str(document)
    assert "frame-ancestors" not in csp


def test_panel_content_is_unchanged_by_the_inline_presentation() -> None:
    document, _, _ = artifact_content("game.html", "text/html", PAGE)
    page = _srcdoc(str(document))
    assert "--rcp-ink" not in page
    assert "rcp-artifact-size" not in str(document)
    assert "color-scheme" not in str(document)
    markdown, _, csp = artifact_content("notes.md", "text/markdown", b"# Notes")
    assert "<script" not in str(markdown)
    assert csp.startswith("sandbox;")


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("notes.md", b"# Notes\n\n<script>alert(1)</script>\n\n| a | b |\n|---|---|\n| 1 | 2 |"),
        ("log.txt", b"line one\n<script>alert(1)</script>\n"),
    ],
)
def test_inline_text_runs_only_rcp_s_nonce_bound_height_report(name, data) -> None:
    document, media_type, csp = artifact_content(
        name, classify_artifact_bytes(name, data), data, inline=InlineAppearance("aqua", "dark")
    )
    page = str(document)
    assert media_type == "text/html"
    nonce = re.search(r"script-src 'nonce-([^']+)'", csp)
    assert nonce
    scripts = re.findall(r"<script([^>]*)>", page)
    assert scripts == [f' nonce="{nonce.group(1)}"']
    assert "&lt;script&gt;" in page
    assert csp.startswith("sandbox allow-scripts;")
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'self'" in csp
    assert "--rcp-ink:#e5ebf2;" in page and "color-scheme:dark" in page
    # Two renders never share a nonce.
    _, _, again = artifact_content(
        name, classify_artifact_bytes(name, data), data, inline=InlineAppearance("aqua", "dark")
    )
    assert again != csp


def test_the_inline_shell_has_no_chrome_and_bounds_its_content() -> None:
    assert_frozen_backend_ships("artifact_inline.js", "artifact_selection.js")
    descriptor = _descriptor("game.html", "text/html")
    document, csp = artifact_viewer_document(
        descriptor,
        content_url="/content?version_id=v1",
        state="temporary",
        keep_url="/keep",
        live_url="/live",
        presentation="inline",
        selectable=True,
    )
    assert "<header" not in document and 'id="keep"' not in document
    assert 'id="composer"' not in document
    assert f'"maxHeight": {ARTIFACT_INLINE_MAX_HEIGHT_PX}' in document
    assert f'"initialHeight": {ARTIFACT_INLINE_INITIAL_HEIGHT_PX}' in document
    assert '"selectable": true' in document
    assert '<iframe id="preview" sandbox="allow-scripts"' in document
    assert "liveUrl=" in document
    assert "frame-ancestors 'self'" in csp
    assert "connect-src 'self'" in csp

    image, image_csp = artifact_viewer_document(
        _descriptor("plot.svg", "image/svg+xml"),
        content_url="/content",
        state="temporary",
        presentation="inline",
    )
    assert '<img id="previewImage" src="/content"' in image
    assert "liveUrl=" not in image and "connect-src" not in image_csp

    panel, _ = artifact_viewer_document(descriptor, content_url="/content", state="temporary")
    assert "inlineConfig" not in panel


def test_the_agent_is_told_to_embed_with_the_tokens_and_bound_rcp_applies() -> None:
    contract = artifact_contract("/stage/turns/op/artifacts")
    assert "![" in contract and "(/stage/turns/op/artifacts/" in contract
    for name in ARTIFACT_THEME_TOKENS:
        assert f"--rcp-{name}" in contract
    assert f"{ARTIFACT_INLINE_MAX_HEIGHT_PX}px" in contract
    # Every advertised token is one the frame actually receives.
    assert set(theme_tokens("aqua", "light")) == set(ARTIFACT_THEME_TOKENS)


def _stored(app, name: str, data: bytes) -> AgentArtifactDescriptor:
    store = app.state.background_tasks.store
    descriptor = descriptor_for(
        "inline-turn", name, media_type=classify_artifact_bytes(name, data), size_bytes=len(data)
    )
    store.create_artifact(
        Artifact(
            artifact_id=descriptor.artifact_id,
            project_id=app.state.default_project_id,
            supplier="turn",
            supplier_id="inline-turn",
            source_name=name,
            media_type=descriptor.media_type,
            created_at=store.now(),
            origin_operation_id="inline-turn",
        ),
        data=data,
    )
    return descriptor


def test_stored_routes_serve_the_inline_presentation(manifest, tmp_path: Path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    game = _stored(app, "game.html", PAGE)
    notes = _stored(app, "notes.md", b"# Notes")
    base = f"/api/projects/{project_id}/artifacts"
    with signed_in_client(app) as client:
        shell = client.get(f"{base}/{game.artifact_id}/viewer", params={"presentation": "inline"})
        assert shell.status_code == 200, shell.text
        assert "inlineConfig" in shell.text and "<header" not in shell.text
        assert '"selectable": true' in shell.text
        panel = client.get(f"{base}/{game.artifact_id}/viewer")
        assert "inlineConfig" not in panel.text

        content = client.get(
            f"{base}/{game.artifact_id}/content",
            params={"presentation": "inline", "theme": "classic", "color_mode": "dark"},
        )
        assert content.status_code == 200
        assert artifact_theme_css("classic", "dark") in _srcdoc(content.text)

        # An unknown appearance falls back to the app's default instead of failing.
        fallback = client.get(
            f"{base}/{game.artifact_id}/content",
            params={"presentation": "inline", "theme": "neon", "color_mode": "dim"},
        )
        assert artifact_theme_css("aqua", "light") in _srcdoc(fallback.text)

        text = client.get(f"{base}/{notes.artifact_id}/viewer", params={"presentation": "inline"})
        assert '"selectable": false' in text.text
        assert (
            client.get(
                f"{base}/{notes.artifact_id}/content", params={"presentation": "sideways"}
            ).status_code
            == 422
        )
