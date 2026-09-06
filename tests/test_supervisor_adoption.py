from __future__ import annotations

import io
import json
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from tests import supervisor_adoption_build as build
from tests import supervisor_adoption_guest as adoption


def test_source_bundle_preserves_requested_history_without_changing_the_checkout(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*arguments):
        return subprocess.run(
            ["git", "-C", str(source), *arguments],
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Adoption fixture")
    git("config", "user.email", "adoption@example.invalid")
    (source / "version").write_text("historical\n")
    git("add", "version")
    git("commit", "-qm", "Historical merged source")
    previous = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    (source / "version").write_text("new current main\n")
    git("commit", "-qam", "Later main")
    current = git("rev-parse", "HEAD")
    bundle = tmp_path / "historical.bundle"
    assert build.source_bundle(str(source), previous, bundle) == tree
    target = tmp_path / "guest-checkout"
    subprocess.run(
        ["git", "clone", str(bundle), str(target)], check=True, capture_output=True, timeout=30
    )
    assert (target / "version").read_text() == "historical\n"
    assert git("rev-parse", "HEAD") == current
    assert not git("status", "--porcelain")
    assert (
        subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        == previous
    )


def test_node_package_contains_only_required_runtime_and_npm(tmp_path, monkeypatch):
    runtime = tmp_path / "node-runtime"
    (runtime / "bin").mkdir(parents=True)
    node = runtime / "bin/node"
    node.write_bytes(b"\x7fELF" + b"\0" * 14 + b"\x3e\x00")
    npm = runtime / "lib/node_modules/npm"
    (npm / "bin").mkdir(parents=True)
    (npm / "bin/npm-cli.js").write_text("// npm fixture\n")
    (runtime / "unrelated-runner-cache").write_text("must never be copied")
    monkeypatch.setattr(
        build,
        "run",
        lambda argv: SimpleNamespace(stdout="v24.1.0\n" if len(argv) == 2 else "11.0.0\n"),
    )
    output = tmp_path / "runtime.tar.gz"
    receipt = build.node_runtime(node, output)
    assert receipt["node_version"] == "v24.1.0" and receipt["npm_version"] == "11.0.0"
    with tarfile.open(output) as archive:
        names = archive.getnames()
        assert "bin/node" in names and "lib/node_modules/npm/bin/npm-cli.js" in names
        assert not any("cache" in name for name in names)
        assert archive.getmember("bin/npm").linkname == "../lib/node_modules/npm/bin/npm-cli.js"
    outside = tmp_path / "outside"
    outside.write_text("private")
    (npm / "unsafe").symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        build.node_runtime(node, tmp_path / "unsafe.tar.gz")


def payload(tmp_path, monkeypatch):
    monkeypatch.setattr(adoption, "PAYLOAD", tmp_path)
    files = {}
    for name in ("historical-source.bundle", "node-runtime.tar.gz"):
        (tmp_path / name).write_bytes(b"fixture")
        files[name] = adoption.sha256(tmp_path / name)
    receipt = {
        "source_commit": adoption.SOURCE_COMMIT,
        "source_origin": adoption.SOURCE_ORIGIN,
        "source_tree": "a" * 40,
        "node_version": "v24.1.0",
        "npm_version": "11.0.0",
        "files": files,
    }
    (tmp_path / "package-receipt.json").write_text(json.dumps(receipt))
    return receipt


def test_payload_requires_exact_historical_identity_and_bytes(tmp_path, monkeypatch):
    receipt = payload(tmp_path, monkeypatch)
    assert adoption.verify_payload() == receipt
    (tmp_path / "historical-source.bundle").write_text("changed")
    with pytest.raises(ValueError, match="SHA-256"):
        adoption.verify_payload()
    receipt = payload(tmp_path, monkeypatch)
    receipt["source_commit"] = "b" * 40
    (tmp_path / "package-receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="historical baseline"):
        adoption.verify_payload()


@pytest.mark.parametrize(
    "bad_name,link", [("../../etc/rcp", None), ("bin/unrelated", None), ("bin/npm", "/etc/passwd")]
)
def test_runtime_archive_refuses_escape_or_unrelated_files_before_extraction(
    tmp_path, monkeypatch, bad_name, link
):
    monkeypatch.setattr(adoption, "PAYLOAD", tmp_path)
    with tarfile.open(tmp_path / "node-runtime.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo(bad_name)
        if link is not None:
            member.type = tarfile.SYMTYPE
            member.linkname = link
            archive.addfile(member)
        else:
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
    monkeypatch.setattr(
        adoption.guest, "run", lambda *_args, **_kwargs: pytest.fail("unsafe extraction")
    )
    with pytest.raises(ValueError, match="archive member|escapes"):
        adoption.install_node()


def test_bootstrap_checks_disposable_marker_before_any_host_mutation(monkeypatch):
    def refuse():
        raise RuntimeError("not a disposable guest")

    monkeypatch.setattr(adoption.guest, "require_guest", refuse)
    monkeypatch.setattr(
        adoption.guest, "run", lambda *_args, **_kwargs: pytest.fail("host mutation")
    )
    with pytest.raises(RuntimeError, match="disposable"):
        adoption.bootstrap()
