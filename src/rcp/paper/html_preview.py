"""Resolve HTML images from one saved Paper manifest without loading unrelated bodies."""

from __future__ import annotations

import base64
import hashlib
import json
import posixpath
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit

from rcp import limits
from rcp.artifact_preview import _html_attributes, _is_inline_image
from rcp.artifacts import ARTIFACT_MEDIA_TYPES
from rcp.paper.models import PaperFile, PaperLimit, file_format, paper_path
from rcp.paper.persistence import PaperSnapshotReader


@dataclass(frozen=True)
class _ImageTag:
    start: int
    end: int
    attrs: list[tuple[str, str | None]]
    closed: bool


class _Images(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.offsets = [0, *(index + 1 for index, char in enumerate(source) if char == "\n")]
        self.tags: list[_ImageTag] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            line, column = self.getpos()
            start = self.offsets[line - 1] + column
            raw = self.get_starttag_text()
            assert raw is not None
            self.tags.append(_ImageTag(start, start + len(raw), attrs, raw.endswith("/>")))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


@dataclass(frozen=True)
class _Asset:
    path: str
    file_id: str
    sha256: str
    media_type: str
    size: int
    fragment: str

    @property
    def uri_size(self) -> int:
        return (
            len(f"data:{self.media_type};base64,") + 4 * ((self.size + 2) // 3) + len(self.fragment)
        )


@dataclass(frozen=True)
class PaperHTMLPreview:
    file: PaperFile
    revision: int
    key: str
    source: str
    tags: list[_ImageTag]
    images: list[_Asset | str | None]
    warnings: list[str]


@dataclass(frozen=True)
class PaperHTMLContent:
    file: PaperFile
    revision: int
    data: bytes
    warnings: list[str]


def _render(source: str, tags: list[_ImageTag], replacements: list[str | None]) -> str:
    values = iter(replacements)
    parts = []
    through = 0
    for tag in tags:
        parts.append(source[through : tag.start])
        attrs = []
        for name, value in tag.attrs:
            if name == "src":
                value = next(values)
                if value is None:
                    continue
            attrs.append((name, value))
        parts.append("<img" + _html_attributes(attrs) + ("/>" if tag.closed else ">"))
        through = tag.end
    return "".join([*parts, source[through:]])


def _relative_path(source: str | None, page: str) -> tuple[str, str]:
    value = (source or "").strip()
    if any(ord(char) < 32 or char == "\\" for char in value):
        raise ValueError("Invalid image path")
    url = urlsplit(value)
    decoded = unquote(url.path, errors="strict")
    if url.scheme or url.netloc or decoded.startswith("/") or not decoded:
        raise ValueError("Image must name a relative Paper file")
    path = posixpath.normpath(posixpath.join(posixpath.dirname(page), decoded))
    return paper_path(path), quote(unquote(url.fragment, errors="strict"), safe="")


def _resolve_image(
    reader: PaperSnapshotReader, source: str | None, page: str, entries: dict[str, dict]
) -> _Asset:
    path, fragment = _relative_path(source, page)
    entry = entries.get(path)
    if entry is None:
        raise ValueError("Saved image is missing")
    extension = posixpath.splitext(path)[1].lower()
    media_type = ARTIFACT_MEDIA_TYPES.get(extension, "application/octet-stream")
    if not media_type.startswith("image/"):
        raise ValueError("Unsupported image type")
    sha = entry.get("text_sha256") or entry["blob_sha256"]
    if entry["kind"] == "binary":
        row = reader.connection.execute(
            "SELECT size FROM paper_blobs WHERE sha256=?", (sha,)
        ).fetchone()
        if row is None:
            raise ValueError("Saved image bytes are missing")
        size = row[0]
    else:
        size = entry["size"]
    return _Asset(
        path,
        entry["file_id"],
        sha,
        media_type,
        size,
        "#" + fragment if fragment and media_type == "image/svg+xml" else "",
    )


def _admit_image(asset: _Asset, used: set[str], total: int, expanded: int) -> tuple[int, int]:
    new_bytes = 0 if asset.sha256 in used else asset.size
    if asset.size > limits.PAPER_HTML_PREVIEW_MAX_ASSET_BYTES:
        raise ValueError("Image exceeds its preview byte limit")
    if total + new_bytes > limits.PAPER_HTML_PREVIEW_MAX_ASSET_TOTAL_BYTES:
        raise ValueError("Images exceed the preview byte limit")
    next_expanded = expanded + len(_html_attributes([("src", "")])) + asset.uri_size
    if next_expanded > limits.PAPER_HTML_PREVIEW_MAX_OUTPUT_BYTES:
        raise ValueError("Image exceeds the expanded preview byte limit")
    return total + new_bytes, next_expanded


def _resolve_sources(
    reader: PaperSnapshotReader,
    page: str,
    manifest: dict[str, dict],
    sources: list[str | None],
    images: list[_Asset | str | None],
    expanded: int,
) -> tuple[list[object], list[str]]:
    dependencies: list[object] = []
    used: set[str] = set()
    total = 0
    warning_count = 0
    first_warning = ""
    resolved: dict[str | None, _Asset] = {}
    for index, value in enumerate(sources):
        if _is_inline_image(value):
            continue
        try:
            if value not in resolved:
                resolved[value] = _resolve_image(reader, value, page, manifest)
            asset = resolved[value]
            dependencies.append((asset.path, asset.file_id, asset.sha256))
            total, expanded = _admit_image(asset, used, total, expanded)
        except ValueError as exc:
            images[index] = None
            dependencies.append((value, str(exc)))
            warning_count += 1
            if not first_warning:
                first_warning = (
                    f"{str(exc)}: {(value or '')[: limits.ARTIFACT_ERROR_MAX_CHARS // 2]}"
                )
        else:
            images[index] = asset
            used.add(asset.sha256)
    warnings = (
        [
            f"Paper preview: {first_warning} ({warning_count} image references unavailable)"[
                : limits.ARTIFACT_ERROR_MAX_CHARS
            ]
        ]
        if warning_count
        else []
    )
    return dependencies, warnings


def read_html_preview(
    reader: PaperSnapshotReader, project_id: str, file_id: str, revision: int | None = None
) -> PaperHTMLPreview:
    revision, entries = reader.manifest(project_id, revision)
    entry = next((entry for entry in entries if entry["file_id"] == file_id), None)
    if entry is None or file_format(entry["path"], entry["kind"]) != "html":
        raise KeyError("Paper file has no HTML preview")
    file, body = reader.raw_file(project_id, file_id, revision)
    source = body.decode("utf-8")
    parser = _Images(source)
    parser.feed(source)
    parser.close()
    sources = [value for tag in parser.tags for name, value in tag.attrs if name == "src"]
    inline_sources = [value if _is_inline_image(value) else None for value in sources]
    images: list[_Asset | str | None] = list(inline_sources)
    expanded = len(_render(source, parser.tags, inline_sources).encode())
    if max(expanded, len(body)) > limits.PAPER_HTML_PREVIEW_MAX_OUTPUT_BYTES:
        raise PaperLimit("Paper HTML exceeds its preview byte limit")
    dependencies, warnings = _resolve_sources(
        reader, file.path, {entry["path"]: entry for entry in entries}, sources, images, expanded
    )
    identity = [file.file_id, file.path, hashlib.sha256(body).hexdigest(), dependencies]
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return PaperHTMLPreview(file, revision, key, source, parser.tags, images, warnings)


def inline_html_images(
    preview: PaperHTMLPreview, reader: PaperSnapshotReader, project_id: str
) -> bytes:
    cached: dict[str, bytes] = {}
    replacements: list[str | None] = []
    for image in preview.images:
        if not isinstance(image, _Asset):
            replacements.append(image)
            continue
        if image.sha256 not in cached:
            _, cached[image.sha256] = reader.raw_file(project_id, image.file_id, preview.revision)
        data = cached[image.sha256]
        replacements.append(
            f"data:{image.media_type};base64," + base64.b64encode(data).decode() + image.fragment
        )
    return _render(preview.source, preview.tags, replacements).encode()


def read_html_content(
    reader: PaperSnapshotReader, project_id: str, file_id: str, revision: int | None = None
) -> PaperHTMLContent:
    preview = read_html_preview(reader, project_id, file_id, revision)
    return PaperHTMLContent(
        preview.file,
        preview.revision,
        inline_html_images(preview, reader, project_id),
        preview.warnings,
    )
