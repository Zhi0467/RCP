from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-macos.sh"
TAG = "v9.9.9"
ZIP = f"RCP-{TAG}-macos-arm64.zip"

# macOS-only tools the script calls; the rest (curl, shasum, mv) run for real.
SHIMS = {
    "uname": "echo Darwin",
    "sysctl": 'echo "$FAKE_ARM64"',
    "sw_vers": "echo 14.5",
    "pgrep": 'exit "$FAKE_PGREP"',
    "ditto": 'exec python3 -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$3" "$4"',
    "codesign": "exit 0",
    # Fails only the rename that puts the new app in place, when asked to.
    "mv": 'case "$FAKE_FAIL_SWAP:$1" in 1:*/.rcp-install.*/RCP.app) exit 1 ;; esac; exec /bin/mv "$@"',
}


class _Releases(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/latest":
            self.send_response(302)
            self.send_header("Location", f"http://{self.headers['Host']}/tag/{TAG}")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def install(tmp_path):
    release = tmp_path / "releases" / "download" / f"desktop-{TAG}"
    release.mkdir(parents=True)
    with zipfile.ZipFile(release / ZIP, "w") as archive:
        archive.writestr("RCP.app/Contents/MacOS/rcp-desktop", "new")
    digest = hashlib.sha256((release / ZIP).read_bytes()).hexdigest()
    (release / f"{ZIP}.sha256").write_text(f"{digest}  {ZIP}\n")

    shims = tmp_path / "shims"
    shims.mkdir()
    for name, body in SHIMS.items():
        (shims / name).write_text(f"#!/bin/sh\n{body}\n")
        (shims / name).chmod(0o755)

    applications = tmp_path / "Applications"
    (applications / "RCP.app" / "Contents" / "MacOS").mkdir(parents=True)
    (applications / "RCP.app" / "Contents" / "MacOS" / "rcp-desktop").write_text("old")

    handler = partial(_Releases, directory=str(tmp_path / "releases"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def run(**fake: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(tmp_path),
            "RCP_RELEASES_URL": f"http://127.0.0.1:{server.server_port}",
            "RCP_APPLICATIONS_DIR": str(applications),
            "FAKE_ARM64": "1",
            "FAKE_PGREP": "1",
            "FAKE_FAIL_SWAP": "0",
            **fake,
        }
        return subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)

    yield run, applications, release
    server.shutdown()
    server.server_close()
    applications.chmod(0o755)


def _installed(applications: Path) -> str:
    return (applications / "RCP.app" / "Contents" / "MacOS" / "rcp-desktop").read_text()


def test_replaces_the_installed_app_and_leaves_no_staging(install):
    run, applications, _release = install
    result = run()
    assert result.returncode == 0, result.stderr
    assert _installed(applications) == "new"
    assert sorted(path.name for path in applications.iterdir()) == ["RCP.app"]


@pytest.mark.parametrize(
    "reason", ["intel", "running", "unwritable", "checksum", "unpublished", "swap"]
)
def test_refusal_leaves_the_existing_app_untouched(install, reason):
    run, applications, release = install
    fake = {}
    if reason == "intel":
        fake["FAKE_ARM64"] = ""
    elif reason == "running":
        fake["FAKE_PGREP"] = "0"
    elif reason == "unwritable":
        applications.chmod(0o555)
    elif reason == "swap":
        fake["FAKE_FAIL_SWAP"] = "1"
    elif reason == "checksum":
        (release / f"{ZIP}.sha256").write_text(f"{'0' * 64}  {ZIP}\n")
    else:
        (release / ZIP).unlink()
    result = run(**fake)
    assert result.returncode != 0
    applications.chmod(0o755)
    assert _installed(applications) == "old"
    assert sorted(path.name for path in applications.iterdir()) == ["RCP.app"]
