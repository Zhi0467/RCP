from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

from .server_upgrade_harness import REPOSITORY_ROOT, build_installed_candidate


def test_installed_candidate_builds_actual_checkout_without_mutating_it(tmp_path: Path) -> None:
    workspace = REPOSITORY_ROOT
    source = workspace / "src/rcp/__init__.py"
    original = source.read_bytes()
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
    output = tmp_path / "candidate"

    receipt = build_installed_candidate(output, run_number="900001", sha=sha)

    assert source.read_bytes() == original
    assert receipt["full_commit"] == sha
    assert receipt["synthetic_release_selection"] is True
    assert json.loads((tmp_path / "candidate.receipt.json").read_text()) == receipt
    (wheel,) = output.glob("rcp-*.whl")
    with zipfile.ZipFile(wheel) as bundle:
        assert f"+build.900001.g{sha[:7]}" in bundle.read("rcp/__init__.py").decode()
        assert (
            bundle.read("rcp/storage/base.py")
            == (workspace / "src/rcp/storage/base.py").read_bytes()
        )
        assert (
            bundle.read("rcp/web_dist/index.html")
            == (workspace / "web/dist/index.html").read_bytes()
        )
    (supervisor_wheel,) = output.glob("rcp_supervisor-*.whl")
    with zipfile.ZipFile(supervisor_wheel) as bundle:
        assert (
            bundle.read("rcp_supervisor/__init__.py")
            == (workspace / "supervisor/src/rcp_supervisor/__init__.py").read_bytes()
        )
    manifest = output / "manifest.sha256"
    assert receipt["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    names = set()
    for line in manifest.read_text().splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
        names.add(name)
    assert names == {
        wheel.name,
        supervisor_wheel.name,
        "requirements.lock.txt",
        "supervisor-requirements.lock.txt",
    }
    assert "--hash=sha256:" in (output / "requirements.lock.txt").read_text()

    rebuilt = tmp_path / "rebuilt"
    rebuilt_receipt = build_installed_candidate(rebuilt, run_number="900001", sha=sha)
    assert rebuilt_receipt["manifest_sha256"] == receipt["manifest_sha256"]
    assert (rebuilt / "supervisor-requirements.lock.txt").read_bytes() == (
        output / "supervisor-requirements.lock.txt"
    ).read_bytes()
