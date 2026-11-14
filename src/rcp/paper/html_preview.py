"""Resolve HTML images from one Paper state without loading unrelated bodies."""

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
from rcp.artifacts import ARTIFACT_MEDIA_TYPES, FrameAddon
from rcp.paper.models import PaperEntry, PaperLimit, PaperSource, file_format, paper_path


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
    file: PaperEntry
    revision: int
    key: str
    source: str
    tags: list[_ImageTag]
    images: list[_Asset | str | None]
    warnings: list[str]


@dataclass(frozen=True)
class PaperHTMLContent:
    file: PaperEntry
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


def _resolve_image(source: str | None, page: str, entries: dict[str, PaperEntry]) -> _Asset:
    path, fragment = _relative_path(source, page)
    entry = entries.get(path)
    if entry is None:
        raise ValueError("Saved image is missing")
    extension = posixpath.splitext(path)[1].lower()
    media_type = ARTIFACT_MEDIA_TYPES.get(extension, "application/octet-stream")
    if not media_type.startswith("image/"):
        raise ValueError("Unsupported image type")
    return _Asset(
        path,
        entry.file_id,
        entry.sha256,
        media_type,
        entry.size,
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
    page: str,
    manifest: dict[str, PaperEntry],
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
                resolved[value] = _resolve_image(value, page, manifest)
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


def read_html_preview(source: PaperSource, file_id: str) -> PaperHTMLPreview:
    entry = next((entry for entry in source.entries if entry.file_id == file_id), None)
    if entry is None or file_format(entry.path, entry.kind) != "html":
        raise KeyError("Paper file has no HTML preview")
    body = source.read(entry)
    text = body.decode("utf-8")
    parser = _Images(text)
    parser.feed(text)
    parser.close()
    sources = [value for tag in parser.tags for name, value in tag.attrs if name == "src"]
    inline_sources = [value if _is_inline_image(value) else None for value in sources]
    images: list[_Asset | str | None] = list(inline_sources)
    expanded = len(_render(text, parser.tags, inline_sources).encode())
    if max(expanded, len(body)) > limits.PAPER_HTML_PREVIEW_MAX_OUTPUT_BYTES:
        raise PaperLimit("Paper HTML exceeds its preview byte limit")
    dependencies, warnings = _resolve_sources(
        entry.path, {item.path: item for item in source.entries}, sources, images, expanded
    )
    identity = [entry.file_id, entry.path, hashlib.sha256(body).hexdigest(), dependencies]
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return PaperHTMLPreview(entry, source.revision, key, text, parser.tags, images, warnings)


def inline_html_images(preview: PaperHTMLPreview, source: PaperSource) -> bytes:
    entries = {entry.file_id: entry for entry in source.entries}
    cached: dict[str, bytes] = {}
    replacements: list[str | None] = []
    for image in preview.images:
        if not isinstance(image, _Asset):
            replacements.append(image)
            continue
        if image.sha256 not in cached:
            cached[image.sha256] = source.read(entries[image.file_id])
        data = cached[image.sha256]
        replacements.append(
            f"data:{image.media_type};base64," + base64.b64encode(data).decode() + image.fragment
        )
    return _render(preview.source, preview.tags, replacements).encode()


def read_html_content(source: PaperSource, file_id: str) -> PaperHTMLContent:
    preview = read_html_preview(source, file_id)
    return PaperHTMLContent(
        preview.file, preview.revision, inline_html_images(preview, source), preview.warnings
    )


def source_frame_addon() -> FrameAddon:
    """Reports a word the member double-clicked, with how often the page's text showed it
    before, and each text selection with where it sits, for the Comment offer."""
    return FrameAddon(
        frame_script="""
listen(document,'dblclick',(event)=>{
  if(!event.isTrusted) return;
  const selection=document.getSelection();
  const word=String(selection||'').trim();
  if(!word||!selection.rangeCount||!document.body) return;
  const start=selection.getRangeAt(0);
  const hidden='script,style,template,noscript';
  const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,{acceptNode:(node)=>
    node.parentElement?.closest(hidden)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
  let before='';
  for(let node=walker.nextNode();node;node=walker.nextNode()){
    if(node===start.startContainer){before+=node.data.slice(0,start.startOffset);break;}
    if(start.comparePoint(node,0)>0) break;
    before+=node.data;
  }
  send({kind:'rcp-paper-source',word,occurrence:before.split(word).length-1});
});
const unitOf=(value)=>Math.min(1,Math.max(0,value));
const offerSelection=()=>{
  const selection=document.getSelection();
  const text=String(selection||'').trim();
  if(!text||!selection.rangeCount){send({kind:'rcp-paper-selection',text:''});return;}
  const box=selection.getRangeAt(0).getBoundingClientRect();
  const width=innerWidth||1,height=innerHeight||1;
  send({kind:'rcp-paper-selection',text:text.slice(0,4096),rect:{
    x:unitOf(box.left/width),y:unitOf(box.top/height),
    width:unitOf(box.width/width),height:unitOf(box.height/height)}});
};
listen(document,'pointerup',(event)=>{if(event.isTrusted) setTimeout(offerSelection,0);});
listen(document,'keyup',(event)=>{if(event.isTrusted && event.shiftKey) offerSelection();});
""",
        wrapper_script="""channelListeners.push((value)=>{
  if(value.kind==='rcp-paper-source' && window.parent!==window)
    parentPost({type:'rcp-paper-source',version:1,word:value.word,occurrence:value.occurrence},'*');
  if(value.kind==='rcp-paper-selection' && window.parent!==window)
    parentPost({type:'rcp-paper-selection',version:1,text:value.text,rect:value.rect},'*');
});
""",
    )
