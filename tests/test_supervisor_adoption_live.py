from __future__ import annotations

import json
import tarfile

import pytest

from tests import supervisor_adoption_live as live
from tests import supervisor_reboot_live as reboot


def test_adoption_controller_refuses_host_before_downloading_or_creating_guest(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("RCP_REBOOT_DISPOSABLE", raising=False)
    monkeypatch.setattr(live, "download_image", lambda *_: pytest.fail("download before guard"))
    monkeypatch.setattr(live, "Guest", lambda *_args, **_kw: pytest.fail("guest before guard"))
    with pytest.raises(live.QualificationUnavailable, match="confirmation"):
        live.drive("24.04", tmp_path, tmp_path, tmp_path)


def test_adoption_payload_adds_only_declared_files_to_the_shared_guest_payload(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    tests = workspace / "tests"
    tests.mkdir(parents=True)
    names = (
        "__init__.py",
        "supervisor_reboot_guest.py",
        "supervisor_reboot_data.py",
        "supervisor_adoption_guest.py",
    )
    for name in names:
        (tests / name).write_text("fixture\n")
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    for name in ("base", "target"):
        (bundles / name).mkdir()
        (bundles / name / "release.json").write_text("{}\n")
    (bundles / "build-receipt.json").write_text("{}\n")
    adoption = tmp_path / "adoption"
    adoption.mkdir()
    for name in ("historical-source.bundle", "node-runtime.tar.gz", "package-receipt.json"):
        (adoption / name).write_text("fixture\n")
    (adoption / "unrelated-private-file").write_text("must not be uploaded\n")
    uv = tmp_path / "uv"
    uv.write_text("fixture executable\n")
    monkeypatch.setattr(reboot.shutil, "which", lambda _: str(uv))
    destination = tmp_path / "payload.tar.gz"
    reboot.payload(workspace, bundles, destination, adoption=adoption)
    with tarfile.open(destination) as archive:
        assert {name for name in archive.getnames() if name.startswith("adoption/")} == {
            "adoption/historical-source.bundle",
            "adoption/node-runtime.tar.gz",
            "adoption/package-receipt.json",
        }
        assert "tests/supervisor_adoption_guest.py" in archive.getnames()
        assert "uv" in archive.getnames()


def test_adoption_failure_preserves_completed_evidence_and_stops_only_owned_guest(
    tmp_path, monkeypatch
):
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    (bundles / "build-receipt.json").write_text('{"build": 1}\n')
    adoption = tmp_path / "adoption"
    adoption.mkdir()
    (adoption / "package-receipt.json").write_text('{"source_commit": "historical"}\n')
    stopped = []

    class Guest:
        process = None

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return "5d974c98-d926-48f5-872c-4bdc3d9203b6"

        def install_payload(self, source):
            pass

        def ssh(self, argv, **kwargs):
            raise RuntimeError("adoption refused an incomplete protected backup")

        def power_off(self):
            stopped.append(True)

    monkeypatch.setattr(live, "Guest", Guest)
    monkeypatch.setattr(live, "preflight", lambda *_: {"accelerator": "kvm"})
    monkeypatch.setattr(live, "download_image", lambda *_: (tmp_path / "image", "digest"))
    monkeypatch.setattr(live, "payload", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="incomplete protected backup"):
        live.drive("24.04", bundles, adoption, tmp_path)
    receipt = json.loads((tmp_path / "qualification.json").read_text())
    assert receipt["status"] == "failed" and receipt["actual_reboot_proven"] is False
    assert receipt["build_receipt"] == {"build": 1}
    assert receipt["source_payload"] == {"source_commit": "historical"}
    assert receipt["boot_ids"] == ["5d974c98-d926-48f5-872c-4bdc3d9203b6"]
    assert stopped == [True]
