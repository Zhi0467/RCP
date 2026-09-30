from __future__ import annotations

import errno
import hashlib
import html
import os
import re
import stat
import xml.etree.ElementTree as ET
from contextlib import suppress
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from rcp.artifact_replace import (
    recover_regular_file_replacement_in_open_directory,
    replace_regular_file_in_open_directory,
)
from rcp.limits import ARTIFACT_DISPLAY_TITLE_MAX_CHARS

ArtifactMediaType = Literal[
    "text/html",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "text/markdown",
    "text/plain",
    "text/csv",
    "application/json",
    "application/pdf",
    "application/octet-stream",
]

ArtifactView = Literal["html", "image", "markdown", "text", "pdf", "file"]

# Supported file types are an artifact contract, not an operational tuning knob.
ARTIFACT_MEDIA_TYPES: dict[str, ArtifactMediaType] = {
    ".html": "text/html",
    ".htm": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

ARTIFACT_MEDIA_TYPES.update(
    {
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".csv": "text/csv",
        ".json": "application/json",
        ".pdf": "application/pdf",
        **dict.fromkeys(
            (
                ".txt",
                ".log",
                ".tsv",
                ".jsonl",
                ".yaml",
                ".yml",
                ".toml",
                ".ipynb",
                ".py",
                ".js",
                ".ts",
                ".jsx",
                ".tsx",
                ".mjs",
                ".cjs",
                ".c",
                ".h",
                ".cpp",
                ".hpp",
                ".cc",
                ".rs",
                ".go",
                ".java",
                ".sh",
                ".bash",
                ".zsh",
                ".r",
                ".jl",
                ".rb",
                ".sql",
                ".css",
                ".scss",
                ".xml",
                ".tex",
                ".lua",
                ".swift",
            ),
            "text/plain",
        ),
    }
)


def artifact_view(media_type: ArtifactMediaType) -> ArtifactView:
    if media_type.startswith("image/"):
        return "image"
    return {
        "text/html": "html",
        "text/markdown": "markdown",
        "text/plain": "text",
        "text/csv": "text",
        "application/json": "text",
        "application/pdf": "pdf",
    }.get(media_type, "file")


class AgentArtifactDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    name: str = Field(min_length=1, max_length=255)
    media_type: ArtifactMediaType
    size_bytes: int | None = Field(default=None, ge=1)
    kept_filename: str | None = Field(default=None, min_length=1, max_length=255)
    kept_at: str | None = None

    def is_kept(self) -> bool:
        return self.kept_at is not None or self.kept_filename is not None


class _HTMLDocumentTitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.excluded_depth = 0
        self.in_title = False
        self.found_title = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"svg", "math", "template"}:
            self.excluded_depth += 1
        elif tag == "title" and not self.excluded_depth and not self.found_title:
            self.in_title = True
            self.found_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"svg", "math", "template"}:
            self.excluded_depth = max(0, self.excluded_depth - 1)
        elif tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.parts.append(data)


def html_document_title(document: str) -> str | None:
    """Read the first HTML title, excluding foreign and inert title elements."""
    parser = _HTMLDocumentTitleParser()
    parser.feed(document)
    parser.close()
    title = " ".join("".join(parser.parts).split())
    if len(title) > ARTIFACT_DISPLAY_TITLE_MAX_CHARS:
        title = title[: ARTIFACT_DISPLAY_TITLE_MAX_CHARS - 1].rstrip() + "…"
    return title or None


def artifact_id(scope_id: str, name: str) -> str:
    """Return an opaque, task-scope-bound identifier without exposing a path."""
    return hashlib.sha256(f"{scope_id}\0{name}".encode()).hexdigest()[:24]


def descriptor_for(
    scope_id: str,
    name: str,
    *,
    media_type: ArtifactMediaType,
    size_bytes: int | None = None,
    kept_filename: str | None = None,
    kept_at: str | None = None,
) -> AgentArtifactDescriptor:
    return AgentArtifactDescriptor(
        artifact_id=artifact_id(scope_id, name),
        name=name,
        media_type=media_type,
        size_bytes=size_bytes,
        kept_filename=kept_filename,
        kept_at=kept_at,
    )


def classify_artifact_bytes(name: str, data: bytes) -> ArtifactMediaType:
    """Recognize bounded bytes; unrecognized files remain downloadable."""
    media_type = ARTIFACT_MEDIA_TYPES.get(Path(name).suffix.casefold(), "application/octet-stream")
    if media_type in {"text/html", "text/markdown", "text/plain", "text/csv", "application/json"}:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return "application/octet-stream"
        return media_type if "\x00" not in text else "application/octet-stream"
    if media_type == "image/svg+xml":
        try:
            root = ET.fromstring(data.decode("utf-8"))
        except (UnicodeDecodeError, ET.ParseError):
            return "application/octet-stream"
        return (
            media_type
            if root.tag.rsplit("}", 1)[-1].casefold() == "svg"
            else "application/octet-stream"
        )
    valid = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
        "application/pdf": data.startswith(b"%PDF-"),
    }
    return media_type if valid.get(media_type, False) else "application/octet-stream"


def read_local_regular_file(directory: Path, name: str, *, max_bytes: int) -> bytes:
    """Read one direct regular child without following a symlink."""
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("artifact name must be a plain base name")
    # O_NONBLOCK keeps a FIFO swapped in after listing from blocking the open;
    # the regular-file check below then refuses it, and regular reads ignore it.
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = _open_local_directory(directory)
    try:
        try:
            file_fd = os.open(name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise
            except OSError:
                raise exc from None
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("artifact is not a readable regular file") from exc
            raise
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("artifact is not a regular file")
            if metadata.st_size > max_bytes:
                raise ValueError("artifact exceeds the per-file limit")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(file_fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                raise ValueError("artifact exceeds the per-file limit")
            return data
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def list_local_regular_files(directory: Path) -> list[tuple[str, int]]:
    """List direct regular children without following any directory symlink."""
    directory_fd = _open_local_directory(directory)
    try:
        values: list[tuple[str, int]] = []
        for name in os.listdir(directory_fd):
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode):
                values.append((name, metadata.st_size))
        return sorted(values)
    finally:
        os.close(directory_fd)


def replace_local_regular_file(
    directory: Path,
    name: str,
    data: bytes,
    *,
    expected_sha256: str | None = None,
    recovery_directory: Path | None = None,
) -> bool:
    """Atomically replace one direct regular child if its digest is still expected."""

    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("artifact name must be a plain base name")
    if expected_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("expected artifact digest is invalid")
    if expected_sha256 is not None and recovery_directory is None:
        raise ValueError("conditional artifact replacement requires an RCP recovery directory")
    recovery_directory = recovery_directory or directory
    recovery_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_fd = _open_local_directory(directory)
    recovery_directory_fd = _open_local_directory(recovery_directory)
    try:
        return replace_regular_file_in_open_directory(
            directory_fd,
            recovery_directory_fd,
            name,
            data,
            expected_sha256=expected_sha256,
            mode=0o600,
        )
    finally:
        os.close(recovery_directory_fd)
        os.close(directory_fd)
        if recovery_directory != directory:
            with suppress(OSError):
                recovery_directory.rmdir()
            with suppress(OSError):
                recovery_directory.parent.rmdir()


def recover_local_regular_file_replacement(
    directory: Path,
    name: str,
    *,
    recovery_directory: Path,
) -> None:
    """Settle any RCP-owned conditional replacement journal for one local artifact."""

    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("artifact name must be a plain base name")
    if not recovery_directory.exists():
        return
    directory_fd = _open_local_directory(directory)
    recovery_directory_fd = _open_local_directory(recovery_directory)
    try:
        recover_regular_file_replacement_in_open_directory(
            directory_fd, recovery_directory_fd, name
        )
    finally:
        os.close(recovery_directory_fd)
        os.close(directory_fd)


def _open_local_directory(directory: Path) -> int:
    if not directory.is_absolute():
        raise ValueError("artifact directory must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current = os.open("/", flags)
    try:
        for part in directory.parts[1:]:
            following = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = following
        return current
    except FileNotFoundError:
        os.close(current)
        raise
    except OSError as exc:
        os.close(current)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("artifact directory is not a regular directory") from exc
        raise


class _ArtifactHTMLSanitizer(HTMLParser):
    """Neutralize browser capabilities while preserving inline presentation and scripts."""

    _request_attributes = {
        "src",
        "srcset",
        "poster",
        "action",
        "formaction",
        "ping",
        "data",
        "codebase",
        "background",
        "manifest",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta" and any(
            name.casefold() == "http-equiv" and (value or "").casefold() == "refresh"
            for name, value in attrs
        ):
            return
        rendered: list[tuple[str, str | None]] = []
        for name, value in attrs:
            lowered = name.casefold()
            if (
                lowered in self._request_attributes
                or lowered in {"download", "target"}
                or lowered.endswith(":href")
                or lowered.endswith(":src")
            ):
                continue
            if lowered == "href":
                if tag == "a" and value and _is_http_url(value):
                    rendered.append(("data-rcp-href", value))
                continue
            if lowered == "http-equiv" and tag == "meta":
                continue
            rendered.append((name, value))
        self.parts.append(f"<{tag}{_html_attributes(rendered)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        before = len(self.parts)
        self.handle_starttag(tag, attrs)
        if len(self.parts) > before:
            self.parts[-1] = self.parts[-1][:-1] + "/>"

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.parts.append(f"<?{data}>")


@dataclass(frozen=True)
class FrameAddon:
    frame_script: str
    wrapper_script: str


def html_preview_document(
    data: bytes, *, frame_addon: FrameAddon | None = None, result_view_gestures: bool = False
) -> tuple[str, str]:
    """Build an RCP-owned wrapper and its CSP for an opaque sandboxed document."""
    source = data.decode("utf-8")
    sanitizer = _ArtifactHTMLSanitizer()
    sanitizer.feed(source)
    sanitizer.close()
    bootstrap = (
        "<script>(()=>{\n"
        + """
const channel=new MessageChannel();
const privatePort=channel.port1;
const outwardPort=channel.port2;
const portPost=Function.prototype.call.bind(MessagePort.prototype.postMessage);
const portStart=Function.prototype.call.bind(MessagePort.prototype.start);
const listen=Function.prototype.call.bind(EventTarget.prototype.addEventListener);
const parentPost=window.parent.postMessage.bind(window.parent);
const closest=Function.prototype.call.bind(Element.prototype.closest);
const send=(value)=>portPost(privatePort,value);
listen(window,'click',(event)=>{
  if(!event.isTrusted || !(event.target instanceof Element)) return;
  const anchor=closest(event.target,'a[data-rcp-href]');
  if(!anchor) return;
  event.preventDefault(); event.stopImmediatePropagation();
  send({kind:'rcp-reference',url:anchor.getAttribute('data-rcp-href')});
},true);
"""
        + (frame_addon.frame_script if frame_addon else "")
        + """
portStart(privatePort);
parentPost({kind:'rcp-artifact-channel',version:1},'*',[outwardPort]);
document.currentScript?.remove();
})();</script>"""
    )
    # Chromium does not currently enforce ``navigate-to``. The opaque sandbox is
    # the boundary that prevents this document from navigating the RCP parent;
    # inline scripts may still navigate their own isolated child frame. Keep the
    # directive as defense in depth for engines that do implement it.
    artifact_csp = (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data: blob:; font-src data:; connect-src 'none'; object-src 'none'; "
        "frame-src 'none'; child-src 'none'; media-src 'none'; worker-src 'none'; "
        "form-action 'none'; base-uri 'none'; navigate-to 'none'"
    )
    artifact = (
        f'<meta http-equiv="Content-Security-Policy" content="{html.escape(artifact_csp)}">'
        + bootstrap
        + "".join(sanitizer.parts)
    )
    wrapper_script = (
        """<script>(()=>{
const artifact=()=>document.getElementById('artifact');
const listen=Function.prototype.call.bind(EventTarget.prototype.addEventListener);
const portPost=Function.prototype.call.bind(MessagePort.prototype.postMessage);
const portStart=Function.prototype.call.bind(MessagePort.prototype.start);
const parentPost=window.parent.postMessage.bind(window.parent);
const openWindow=window.open.bind(window);
const URLConstructor=URL;
let artifactPort=null;
const channelListeners=[];
const channelReady=[];
"""
        + (frame_addon.wrapper_script if frame_addon else "")
        + """
listen(window,'message',(event)=>{
  const value=event.data;
  const frame=artifact();
  if(artifactPort || !frame || event.source!==frame.contentWindow || !value ||
     value.kind!=='rcp-artifact-channel' || value.version!==1 ||
     Object.keys(value).length!==2 || event.ports.length!==1) return;
  artifactPort=event.ports[0];
  listen(artifactPort,'message',(portEvent)=>{
    const value=portEvent.data;
    if(!value || typeof value!=='object') return;
    for(const listener of channelListeners) listener(value);
    if(value.kind!=='rcp-reference' || typeof value.url!=='string') return;
    try {
      const target=new URLConstructor(value.url);
      if(target.protocol==='http:' || target.protocol==='https:')
        openWindow(target.href,'_blank','noopener,noreferrer');
    } catch {}
  });
  portStart(artifactPort);
  for(const ready of channelReady) ready();
},true);
})();</script>"""
    )
    result_view_script = ""
    if result_view_gestures:
        result_view_script = """<script>(()=>{
const legacyArtifact=document.getElementById('artifact');
const expectedKeys=['description','gesture','type','version'];
const utf8=new TextEncoder();
window.addEventListener('message',(event)=>{
  const value=event.data;
  if(event.source!==legacyArtifact.contentWindow || !value || typeof value!=='object') return;
  const keys=Object.keys(value).sort();
  if(keys.length!==expectedKeys.length ||
     keys.some((key,index)=>key!==expectedKeys[index])) return;
  if(value.type!=='rcp-result-view-gesture' || value.version!==1 ||
     (value.gesture!=='box' && value.gesture!=='underscore') ||
     typeof value.description!=='string' || !value.description.trim() ||
     utf8.encode(value.description).byteLength>2048) return;
  if(window.parent===window) return;
  window.parent.postMessage({
    type:'rcp-result-view-gesture',
    version:1,
    gesture:value.gesture,
    description:value.description
  },'*');
});
})();</script>"""
    document = (
        '<!doctype html><meta charset="utf-8">'
        "<title>Artifact preview</title>"
        "<style>html,body,iframe{border:0;margin:0;width:100%;height:100%;display:block}</style>"
        + wrapper_script
        + f'<iframe id="artifact" sandbox="allow-scripts" srcdoc="{html.escape(artifact, quote=True)}">'
        "</iframe>" + result_view_script
    )
    wrapper_csp = (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "frame-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'"
    )
    return document, wrapper_csp


def _html_attributes(attrs: list[tuple[str, str | None]]) -> str:
    return "".join(
        f" {html.escape(name, quote=True)}"
        if value is None
        else f' {html.escape(name, quote=True)}="{html.escape(value, quote=True)}"'
        for name, value in attrs
    )


def _is_http_url(value: str) -> bool:
    try:
        return urlsplit(value).scheme.casefold() in {"http", "https"}
    except ValueError:
        return False
