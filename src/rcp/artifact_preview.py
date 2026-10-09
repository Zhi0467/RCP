"""The artifact sandbox policy (CSP and sanitizer) and its preview server.

Stdlib only: the browser runtime ships this file to the execution host.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import stat
from contextlib import suppress
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

PREVIEW_COMMAND = "rcp-artifact-preview"

# Chromium ignores navigate-to; the opaque sandbox prevents access to the parent,
# but scripts can still navigate their own frame. This is not zero network access.
ARTIFACT_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; object-src 'none'; "
    "frame-src 'none'; child-src 'none'; media-src 'none'; worker-src 'none'; "
    "form-action 'none'; base-uri 'none'; navigate-to 'none'"
)


def _is_inline_image(value: str | None) -> bool:
    """Match image sources the artifact CSP already allows (``img-src data: blob:``)."""
    lowered = (value or "").strip().casefold()
    return lowered.startswith(("data:image/", "blob:"))


class ArtifactHTMLSanitizer(HTMLParser):
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
        # RCP's own policy and bootstrap precede the page, so a doctype left in
        # place would follow content and drop the page into quirks mode.
        self.doctype: str | None = None
        # Each loading attribute removed, as ``<tag attr>``, for the preview to report.
        self.removed: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta" and any(
            name.casefold() == "http-equiv" and (value or "").casefold() == "refresh"
            for name, value in attrs
        ):
            return
        rendered: list[tuple[str, str | None]] = []
        for name, value in attrs:
            lowered = name.casefold()
            if tag == "img" and lowered == "src" and _is_inline_image(value):
                rendered.append((name, value))
                continue
            if lowered in {"download", "target"}:
                continue
            if (
                lowered in self._request_attributes
                or lowered.endswith(":href")
                or lowered.endswith(":src")
            ):
                self.removed.append(f"<{tag} {name}>")
                continue
            if lowered == "href":
                if tag == "a" and value and _is_http_url(value):
                    rendered.append(("data-rcp-href", value))
                elif tag != "a":
                    self.removed.append(f"<{tag} {name}>")
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
        # Whitespace and comments may legally precede a doctype.
        leading = all(not part.strip() or part.startswith("<!--") for part in self.parts)
        if self.doctype is None and leading and decl.casefold().startswith("doctype"):
            self.doctype = f"<!{decl}>"
            return
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.parts.append(f"<?{data}>")


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


class PreviewHandler(SimpleHTTPRequestHandler):
    """Serve regular files through no-follow directory descriptors, never listings."""

    def end_headers(self):
        self.send_header("Content-Security-Policy", f"sandbox allow-scripts; {ARTIFACT_CSP}")
        super().end_headers()

    def send_head(self):
        parts = unquote(urlsplit(self.path).path).split("/")
        if any(part in {"..", "."} or "\x00" in part for part in parts):
            self.send_error(403)
            return None
        parts = [part for part in parts if part]
        if not parts or self.path.endswith("/"):
            parts.append("index.html")
        descriptor = None
        try:
            descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(parts) - 1:
                    flags |= os.O_DIRECTORY
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("Not a regular file")
            stream = os.fdopen(descriptor, "rb")
            descriptor = None
        except (OSError, ValueError):
            self.send_error(403)
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        content_type = self.guess_type(parts[-1])
        if content_type == "text/html":
            content_type = "text/html; charset=utf-8"
            with stream:
                content = stream.read()
            try:
                source = content.decode("utf-8")
            except UnicodeDecodeError:
                self.send_error(415, "HTML artifacts must be UTF-8")
                return None
            # The viewer's sanitizer, so the page loses what RCP strips. No palette:
            # this checks the fallbacks Expand and a download rely on.
            sanitizer = ArtifactHTMLSanitizer()
            sanitizer.feed(source)
            sanitizer.close()
            # RCP removes these silently, so the browser reports no blocked load.
            report = (
                "<script>console.error("
                + json.dumps("RCP removed these loads: " + ", ".join(sanitizer.removed)).replace(
                    "<", "\\u003c"
                )
                + ")</script>"
                if sanitizer.removed
                else ""
            )
            page = (sanitizer.doctype or "") + report + "".join(sanitizer.parts)
            stream = io.BytesIO(page.encode())
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
        self.send_response(200)
        self.send_header("Content-type", content_type)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        return stream


def preview_server(directory: Path, port: int = 0) -> ThreadingHTTPServer:
    # Resolve platform aliases in parent paths, but refuse a symlink as the root.
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Preview directory must be a real directory")
    handler = partial(PreviewHandler, directory=str(directory.resolve()))
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    with preview_server(args.directory, args.port) as server:
        print(f"http://127.0.0.1:{server.server_port}/", flush=True)
        with suppress(KeyboardInterrupt):
            server.serve_forever()


if __name__ == "__main__":
    main()
