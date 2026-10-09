from __future__ import annotations

import html
import re
import threading
from http.client import HTTPConnection

from rcp.artifact_preview import preview_server
from rcp.artifacts import html_preview_document


def test_preview_matches_viewer_and_refuses_escaping_paths(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    page = b'<!DOCTYPE html><script src="https://example.com/c.js" onerror="f()"></script><a href="https://example.com">x</a>'
    (root / "index.html").write_bytes(page)
    (root / "data.bin").write_bytes(b"\x00\xff")
    (tmp_path / "secret").write_text("private")
    (root / "link").symlink_to(tmp_path / "secret")
    (root / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    viewer, _ = html_preview_document(page)
    frame = html.unescape(re.search(r'srcdoc="([^"]*)"', viewer).group(1))
    csp = html.unescape(
        re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', frame).group(1)
    )
    with preview_server(root) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            for path, status in [
                ("/", 200),
                ("/data.bin", 200),
                ("/../secret", 403),
                ("/%2e%2e/secret", 403),
                ("/link", 403),
                ("/linked-dir/secret", 403),
            ]:
                connection.request("GET", path)
                response = connection.getresponse()
                content = response.read()
                assert response.status == status
                assert (
                    response.getheader("Content-Security-Policy") == f"sandbox allow-scripts; {csp}"
                )
                if path == "/":
                    # The viewer's own markup for the page, after its policy and bootstrap.
                    assert frame.endswith(content.decode().removeprefix("<!DOCTYPE html>"))
                    assert frame.startswith("<!DOCTYPE html>")
                elif path == "/data.bin":
                    assert content == b"\x00\xff"
        finally:
            connection.close()
            server.shutdown()
            thread.join()
