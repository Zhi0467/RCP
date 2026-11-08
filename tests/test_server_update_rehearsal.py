from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests import server_update_rehearsal as rehearsal
from tests.supervisor_helpers import make_bundle


def private_input(tmp_path):
    data = tmp_path / "snapshot-data"
    local = tmp_path / "snapshot-project"
    data.mkdir()
    local.mkdir()
    (data / "rcp.sqlite3").write_bytes(b"private database")
    (local / "patches.jsonl").write_text("private canonical history\n")
    manifest = {
        "version": 1,
        "source_manifest_sha256": "a" * 64,
        "data": {
            "snapshot": str(data),
            "destination": "/home/rcp/rcp-server/data",
            "tree_sha256": rehearsal.tree_digest(data),
        },
        "local_roots": [
            {
                "snapshot": str(local),
                "destination": "/srv/private-project",
                "tree_sha256": rehearsal.tree_digest(local),
            }
        ],
        "expected": {
            "space_id": "private-space",
            "project_ids": ["private-project"],
            "canonical_files": {
                "/srv/private-project/patches.jsonl": hashlib.sha256(
                    (local / "patches.jsonl").read_bytes()
                ).hexdigest()
            },
        },
    }
    path = tmp_path / "private.json"
    path.write_text(json.dumps(manifest))
    return path, manifest


def test_private_input_verifies_all_snapshot_bytes_and_identity_mapping(tmp_path):
    path, manifest = private_input(tmp_path)
    assert rehearsal.validate_private_state(path, "a" * 64) == {
        **manifest,
        "input_sha256": rehearsal.file_digest(path),
    }
    (Path(manifest["local_roots"][0]["snapshot"]) / "patches.jsonl").write_text("changed\n")
    with pytest.raises(ValueError, match="tree"):
        rehearsal.validate_private_state(path, "a" * 64)


@pytest.mark.parametrize("change", ["missing", "source", "destination", "canonical", "overlap"])
def test_private_input_refuses_incomplete_or_inconsistent_snapshots(tmp_path, change):
    path, manifest = private_input(tmp_path)
    if change == "missing":
        shutil.rmtree(Path(manifest["data"]["snapshot"]))
    elif change == "source":
        manifest["source_manifest_sha256"] = "b" * 64
    elif change == "destination":
        manifest["local_roots"][0]["destination"] = "/srv/../etc/rcp"
    elif change == "canonical":
        manifest["expected"]["canonical_files"] = {"/outside/file": "a" * 64}
    else:
        manifest["local_roots"][0]["destination"] = manifest["data"]["destination"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        rehearsal.validate_private_state(path, "a" * 64)


@pytest.mark.skipif(sys.platform != "linux", reason="GNU metadata and special-node preservation")
def test_tree_digest_retains_relative_paths_metadata_links_and_special_nodes(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "bytes").write_bytes(b"private bytes")
    os.link(root / "bytes", root / "hardlink")
    (root / "symlink").symlink_to("bytes")
    os.mkfifo(root / "fifo")
    with monkeypatch.context() as context:
        context.chdir(root)
        with socket.socket(socket.AF_UNIX) as stream:
            stream.bind("socket")
    copied = tmp_path / "copy"
    subprocess.run(["cp", "-a", str(root), str(copied)], check=True)
    assert rehearsal.tree_digest(root) == rehearsal.tree_digest(copied)
    (copied / "bytes").chmod(0o600)
    assert rehearsal.tree_digest(root) != rehearsal.tree_digest(copied)


def passed_report(source, candidate, digest):
    return {
        "version": 1,
        "journey": "installed-update",
        "status": "passed",
        "source": {key: source[key] for key in ("manifest_sha256", "commit", "version")},
        "candidate": candidate,
        "input_kind": "captured-private-state",
        "input_sha256": digest,
        "procedure": "stopped-source",
        "supervisor_version": candidate["supervisor_version"],
        "journal": {
            "phase": "committed",
            "previous_manifest_sha256": source["manifest_sha256"],
            "target_manifest_sha256": candidate["manifest_sha256"],
        },
        "serving": {"status": "ok", "commit": candidate["commit"], "version": candidate["version"]},
        "rollback_exact": True,
        "retry_committed": True,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "failed"),
        ("status", "skipped"),
        ("journey", "preparation-only"),
        ("rollback_exact", {}),
        ("retry_committed", False),
        ("procedure", "automatic-bypass"),
        ("procedure", "graceful"),
        ("input_kind", "synthetic-fixture"),
        ("supervisor_version", "0.0.0"),
        ("input_sha256", "c" * 64),
        ("journal", {"phase": "aborted"}),
        ("serving", {"status": "ok", "commit": "short"}),
    ],
)
def test_full_report_requires_every_installed_update_phase(field, value):
    source = {
        "manifest_sha256": "a" * 64,
        "commit": "a" * 40,
        "version": "0.4.40+build.3094.gaaaaaaa",
    }
    candidate = {
        "manifest_sha256": "b" * 64,
        "commit": "b" * 40,
        "version": "0.4.42+build.9999.gbbbbbbb",
        "supervisor_version": "1.0.0",
    }
    report = passed_report(source, candidate, "d" * 64)
    rehearsal.validate_report(report, source, candidate, "d" * 64, "stopped-source")
    report[field] = value
    with pytest.raises(ValueError, match="report"):
        rehearsal.validate_report(report, source, candidate, "d" * 64, "stopped-source")


def test_local_bundles_are_verified_before_their_receipts_are_trusted(tmp_path):
    bundle = make_bundle(tmp_path / "base")
    from rcp_supervisor.releases import verify_release

    release = verify_release(bundle)
    receipt = {
        "manifest_sha256": release.manifest_sha256,
        "full_commit": release.commit + "0" * 33,
        "tag": "v" + release.version.split("+", 1)[0],
    }
    bundle.with_name("base.receipt.json").write_text(json.dumps(receipt))
    assert rehearsal.bundle_identity(bundle)["commit"] == receipt["full_commit"]
    receipt["manifest_sha256"] = "a" * 64
    bundle.with_name("base.receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="receipt"):
        rehearsal.bundle_identity(bundle)


@pytest.mark.parametrize("procedure", ["graceful", "stopped-source"])
@pytest.mark.parametrize(
    "failure", [None, "prepare", "transfer", "driver", "missing", "skipped", "changed", "logs"]
)
def test_controller_transfers_private_bytes_only_offline_and_never_uploads(
    tmp_path, monkeypatch, failure, procedure
):
    path, manifest = private_input(tmp_path)
    source = {"manifest_sha256": "a" * 64, "commit": "a" * 40, "version": "0.4.40+build.1.gaaaaaaa"}
    candidate = {
        "manifest_sha256": "b" * 64,
        "commit": "b" * 40,
        "version": "0.4.42+build.2.gbbbbbbb",
        "supervisor_version": "0.0.15",
    }
    report = passed_report(source, candidate, rehearsal.file_digest(path))
    report["procedure"] = procedure
    if failure == "skipped":
        report["status"] = "skipped"
    events = []

    class FakeGuest:
        token = "owned-disposable-guest"
        offline = False

        def __init__(self, directory, **kwargs):
            assert kwargs["disk_gib"] == 80
            self.directory = directory
            directory.mkdir()

        def start(self):
            events.append("online boot")

        def install_payload(self, payload):
            events.append("code only")

        def ssh(self, argv, **kwargs):
            assert not any(
                "GITHUB_ACTIONS" in value or "upload-artifact" in value for value in argv
            )
            if "apt-get" in argv:
                assert not self.offline
            if "--prepare-guest" in argv:
                assert not self.offline
                assert "--service-uid" in argv and "--service-gid" in argv
                events.append("prepared")
                if failure == "prepare":
                    raise RuntimeError("prepare failed")
            if "--private-state" in argv:
                assert argv[argv.index("--procedure") + 1] == procedure
                assert self.offline
                assert "--prepare-guest" not in argv
                events.append("full driver")
                if failure == "driver":
                    raise RuntimeError("full driver failed")
                if failure == "changed":
                    path.write_text(path.read_text() + "\n")
            if argv[-1] == rehearsal.GUEST_OUTPUT + "/result.json":
                if failure == "missing":
                    raise RuntimeError("missing report")
                return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        def power_cycle(self, *, offline):
            assert offline
            self.offline = True
            events.append("offline boot")

        def copy(self, source, destination):
            assert self.offline
            events.append("private transfer")
            if failure == "transfer":
                raise RuntimeError("transfer failed")

        def collect(self, name, argv):
            assert "upload" not in name
            events.append("private logs")
            if failure == "logs":
                raise RuntimeError("logs failed")

        def power_off(self):
            events.append("stopped")

    def pack_private(state, output):
        assert events[-1] == "offline boot"
        events.append("private archive")
        for name in ("private.tar", "private-sockets.json", "private-state.json"):
            (output / name).write_text("private bytes")

    monkeypatch.setattr(rehearsal, "Guest", FakeGuest)
    monkeypatch.setattr(
        rehearsal, "bundle_identity", lambda p: source if p.name == "base" else candidate
    )
    monkeypatch.setattr(rehearsal, "preflight", lambda *args, **kwargs: {"supported": True})
    monkeypatch.setattr(rehearsal, "download_image", lambda *args: (tmp_path / "image", "a" * 64))
    monkeypatch.setattr(rehearsal, "code_payload", lambda *args: events.append("packed code"))
    monkeypatch.setattr(rehearsal, "private_payload", pack_private)
    monkeypatch.setattr(rehearsal, "guest_disk_gib", lambda state: 80)
    monkeypatch.setattr(
        rehearsal, "run", lambda *args, **kwargs: pytest.fail("No CI upload command may run")
    )
    output = tmp_path / "private-output"

    def drive():
        rehearsal.drive(
            tmp_path / "base",
            tmp_path / "candidate",
            path,
            output,
            "confirmed",
            procedure=procedure,
        )

    if failure:
        with pytest.raises((RuntimeError, ValueError)):
            drive()
        assert not (output / "result.json").exists()
    else:
        drive()
        assert json.loads((output / "result.json").read_text()) == report
        assert (
            events.index("prepared")
            < events.index("offline boot")
            < events.index("private transfer")
        )
        assert events.index("private transfer") < events.index("full driver")
    assert events[-1] == "stopped"
    assert stat_mode(output) == 0o700
    assert not any("upload" in event for event in events)


def stat_mode(path):
    return path.stat().st_mode & 0o777


def test_private_guest_capacity_follows_captured_storage(tmp_path, monkeypatch):
    _, state = private_input(tmp_path)
    allocated = 7 * 1024**3
    calls = []

    def measure(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"{allocated}\ttotal\n", "")

    monkeypatch.setattr(rehearsal, "run", measure)
    first = rehearsal.guest_disk_gib(state)
    assert first * 1024**3 > allocated * 2
    assert calls[0][-2:] == [state["data"]["snapshot"], state["local_roots"][0]["snapshot"]]
    allocated *= 2
    assert rehearsal.guest_disk_gib(state) > first


def test_controller_rejects_changed_input_before_creating_vm_or_report(tmp_path, monkeypatch):
    path, manifest = private_input(tmp_path)
    (Path(manifest["data"]["snapshot"]) / "rcp.sqlite3").write_text("changed")
    monkeypatch.setattr(rehearsal, "bundle_identity", lambda p: {"manifest_sha256": "a" * 64})
    monkeypatch.setattr(
        rehearsal, "Guest", lambda *args, **kwargs: pytest.fail("No VM may be created")
    )
    output = tmp_path / "private-output"
    with pytest.raises(ValueError, match="tree"):
        rehearsal.drive(tmp_path / "base", tmp_path / "candidate", path, output, "confirmed")
    assert not output.exists()


def test_online_payload_contains_only_code_and_verified_bundle_inputs(tmp_path, monkeypatch):
    import tarfile
    import uuid

    private = uuid.uuid4().hex.encode()
    (tmp_path / "private-snapshot").write_bytes(private)
    uv = tmp_path / "uv"
    uv.write_bytes(b"public uv executable")
    monkeypatch.setattr(rehearsal.shutil, "which", lambda name: str(uv))
    bundles = []
    for name in ("base", "candidate"):
        bundle = tmp_path / name
        bundle.mkdir()
        (bundle / "public-wheel.whl").write_bytes(b"public candidate bytes")
        bundle.with_name(name + ".receipt.json").write_text("{}")
        bundles.append(bundle)
    archive_path = tmp_path / "online-code.tar.gz"
    rehearsal.code_payload(*bundles, archive_path)
    with tarfile.open(archive_path) as archive:
        names = archive.getnames()
        assert "bundles/base/public-wheel.whl" in names
        assert "bundles/target/public-wheel.whl" in names
        assert "tests/server_installed_upgrade.py" in names
        assert "tests/server_update_rehearsal.py" in names
        assert "uv" in names
        assert all(name.startswith(("bundles/", "tests/")) or name == "uv" for name in names)
        for member in archive:
            if member.isfile():
                with archive.extractfile(member) as stream:
                    assert private not in stream.read()


@pytest.mark.skipif(sys.platform != "linux", reason="GNU tar and Linux metadata preservation")
def test_private_transport_restores_exact_trees_with_sockets_and_cross_root_links(
    tmp_path, monkeypatch
):
    from tests import server_installed_upgrade as installed

    path, state = private_input(tmp_path)
    data = Path(state["data"]["snapshot"])
    local = Path(state["local_roots"][0]["snapshot"])
    os.link(local / "patches.jsonl", data / "linked-canonical")
    (data / "dangling").symlink_to("missing")
    os.mkfifo(data / "fifo")
    os.setxattr(data / "rcp.sqlite3", "user.rehearsal", b"retained")
    with (data / "sparse").open("wb") as stream:
        stream.seek(1024 * 1024)
        stream.write(b"end")
    with monkeypatch.context() as context:
        context.chdir(data)
        with socket.socket(socket.AF_UNIX) as stream:
            stream.bind("socket")
    for root in (state["data"], *state["local_roots"]):
        root["tree_sha256"] = rehearsal.tree_digest(Path(root["snapshot"]))
    path.write_text(json.dumps(state))
    state = rehearsal.validate_private_state(path, "a" * 64)
    output = tmp_path / "transport"
    output.mkdir()
    unpacked = output / "snapshots"
    monkeypatch.setattr(rehearsal, "PRIVATE_ROOT", str(unpacked))
    monkeypatch.setattr(installed, "require_guest", lambda token: None, raising=False)
    monkeypatch.setattr(rehearsal.os, "geteuid", lambda: 0)
    transported = rehearsal.private_payload(state, output)
    rehearsal.unpack_private("owned-guest")
    for root in (transported["data"], *transported["local_roots"]):
        assert rehearsal.tree_digest(Path(root["snapshot"])) == root["tree_sha256"]
    restored_data = Path(transported["data"]["snapshot"])
    restored_local = Path(transported["local_roots"][0]["snapshot"])
    assert (restored_data / "linked-canonical").stat().st_ino == (
        restored_local / "patches.jsonl"
    ).stat().st_ino
    sparse = (restored_data / "sparse").stat()
    assert sparse.st_blocks * 512 < sparse.st_size
