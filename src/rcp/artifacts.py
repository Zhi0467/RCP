from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import stat
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

ArtifactMediaType = Literal[
    "text/html",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
]

# Supported file types are an artifact contract, not an operational tuning knob.
ARTIFACT_MEDIA_TYPES: dict[str, ArtifactMediaType] = {
    ".html": "text/html",
    ".htm": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class AgentArtifactDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    name: str = Field(min_length=1, max_length=255)
    media_type: ArtifactMediaType


def artifact_id(scope_id: str, name: str) -> str:
    """Return an opaque, task-scope-bound identifier without exposing a path."""
    return hashlib.sha256(f"{scope_id}\0{name}".encode()).hexdigest()[:24]


def descriptor_for(scope_id: str, name: str) -> AgentArtifactDescriptor:
    media_type = ARTIFACT_MEDIA_TYPES[Path(name).suffix.casefold()]
    return AgentArtifactDescriptor(
        artifact_id=artifact_id(scope_id, name),
        name=name,
        media_type=media_type,
    )


def validate_artifact_bytes(name: str, data: bytes) -> ArtifactMediaType:
    """Validate extension, bounded caller-provided bytes, and the format signature."""
    try:
        media_type = ARTIFACT_MEDIA_TYPES[Path(name).suffix.casefold()]
    except KeyError as exc:
        raise ValueError("unsupported artifact type") from exc
    valid = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    }
    if media_type == "text/html":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("HTML artifact is not UTF-8") from exc
        if "\x00" in text:
            raise ValueError("HTML artifact contains NUL bytes")
    elif not valid[media_type]:
        raise ValueError(f"artifact bytes do not match {media_type}")
    return media_type


def read_local_regular_file(directory: Path, name: str, *, max_bytes: int) -> bytes:
    """Read one direct regular child without following a symlink."""
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("artifact name must be a plain base name")
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_fd = _open_local_directory(directory)
    try:
        try:
            file_fd = os.open(name, os.O_RDONLY | no_follow, dir_fd=directory_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError("artifact is not a readable regular file") from exc
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
        raise ValueError("artifact directory is not a regular directory") from exc


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


def html_preview_document(data: bytes) -> tuple[str, str]:
    """Build an RCP-owned wrapper and its CSP for an opaque sandboxed document."""
    source = data.decode("utf-8")
    sanitizer = _ArtifactHTMLSanitizer()
    sanitizer.feed(source)
    sanitizer.close()
    secret = secrets.token_urlsafe(24)
    secret_json = json.dumps(secret)
    bootstrap = f"""<script>(()=>{{
const secret={secret_json};
const send=window.parent.postMessage.bind(window.parent);
const closest=Element.prototype.closest;
window.addEventListener('click',(event)=>{{
  if(!event.isTrusted || !(event.target instanceof Element)) return;
  const anchor=closest.call(event.target,'a[data-rcp-href]');
  if(!anchor) return;
  event.preventDefault(); event.stopImmediatePropagation();
  send({{kind:'rcp-reference',secret,url:anchor.getAttribute('data-rcp-href')}},'*');
}},true);
document.currentScript?.remove();
}})();</script>"""
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
    wrapper_script = f"""<script>
window.addEventListener('message',(event)=>{{
  const value=event.data;
  if(!value || value.kind!=='rcp-reference' || value.secret!=={secret_json} ||
     typeof value.url!=='string') return;
  try {{
    const target=new URL(value.url);
    if(target.protocol==='http:' || target.protocol==='https:')
      window.open(target.href,'_blank','noopener,noreferrer');
  }} catch {{}}
}});
</script>"""
    document = (
        "<!doctype html><meta charset=\"utf-8\">"
        "<title>Artifact preview</title>"
        "<style>html,body,iframe{border:0;margin:0;width:100%;height:100%;display:block}</style>"
        f'<iframe id="artifact" sandbox="allow-scripts" srcdoc="{html.escape(artifact, quote=True)}">'
        "</iframe>"
        + wrapper_script
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
