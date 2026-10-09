"""Stdlib-only artifact preview, shipped to the browser's execution host."""

from __future__ import annotations

import argparse
import io
import os
import re
import stat
from contextlib import suppress
from functools import partial
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


class PreviewHandler(SimpleHTTPRequestHandler):
    """Serve regular files through no-follow directory descriptors, never listings."""

    def __init__(self, *args, directory: str, palette_css: str, **kwargs):
        self.palette_css = palette_css
        super().__init__(*args, directory=directory, **kwargs)

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
            # Keep standards mode, as the inline viewer does.
            doctype = re.match(rb"\s*<!doctype\s[^>]*>", content, re.IGNORECASE)
            offset = doctype.end() if doctype else 0
            style = f"<style>{self.palette_css}</style>".encode()
            stream = io.BytesIO(content[:offset] + style + content[offset:])
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
        self.send_response(200)
        self.send_header("Content-type", content_type)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        return stream


def preview_server(directory: Path, palette_css: str, port: int = 0) -> ThreadingHTTPServer:
    # Resolve platform aliases in parent paths, but refuse a symlink as the root.
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Preview directory must be a real directory")
    handler = partial(PreviewHandler, directory=str(directory.resolve()), palette_css=palette_css)
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--palette", type=Path, required=True)
    args = parser.parse_args(argv)
    with preview_server(args.directory, args.palette.read_text(), args.port) as server:
        print(f"http://127.0.0.1:{server.server_port}/", flush=True)
        with suppress(KeyboardInterrupt):
            server.serve_forever()


if __name__ == "__main__":
    main()
