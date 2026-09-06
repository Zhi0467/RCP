from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.supervisor_reboot_vm import (
    DISPOSABLE_CONFIRMATION,
    Guest,
    QualificationUnavailable,
    image_checksum,
    preflight,
    qemu_command,
    require_changed_boot_id,
)


def test_vm_preflight_refuses_an_unconfirmed_host(tmp_path: Path) -> None:
    with pytest.raises(QualificationUnavailable, match="confirmation"):
        preflight(tmp_path, "yes")


@pytest.mark.parametrize("environment", ["self-hosted", "", "production"])
def test_vm_preflight_never_uses_a_personal_or_production_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", environment)
    with pytest.raises(QualificationUnavailable, match="GitHub-hosted"):
        preflight(tmp_path, DISPOSABLE_CONFIRMATION)


@pytest.mark.parametrize("kvm_available", [True, False])
def test_preflight_requires_actual_qemu_kvm_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kvm_available: bool
) -> None:
    from tests import supervisor_reboot_vm as vm

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setattr(vm.platform, "system", lambda: "Linux")
    monkeypatch.setattr(vm.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(vm.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(vm.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(vm.shutil, "disk_usage", lambda _: SimpleNamespace(free=20 * 1024**3))
    original_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kwargs: (
            "MemAvailable: 8388608 kB\n"
            if str(path) == "/proc/meminfo"
            else original_read(path, *args, **kwargs)
        ),
    )
    # An advisory Python access check must neither reject a working QEMU nor
    # admit an unusable accelerator. The exact QEMU child proves KVM support.
    monkeypatch.setattr(vm.os, "access", lambda *args, **kwargs: not kvm_available)
    probes = []

    def run(argv, **kwargs):
        if "-machine" in argv:
            assert argv[argv.index("-machine") + 1] == "accel=kvm"
            assert kwargs["input"] == "quit\n"
            probes.append(argv)
            if not kvm_available:
                raise subprocess.CalledProcessError(
                    1, argv, stderr="Could not access KVM kernel module: Permission denied\n"
                )
        return subprocess.CompletedProcess(argv, 0, "QEMU qualification test\n", "")

    monkeypatch.setattr(vm, "run", run)
    if kvm_available:
        assert preflight(tmp_path, DISPOSABLE_CONFIRMATION)["accelerator"] == "kvm"
    else:
        with pytest.raises(QualificationUnavailable, match="Permission denied"):
            preflight(tmp_path, DISPOSABLE_CONFIRMATION)
    assert len(probes) == 1


def test_drive_preflight_failure_retains_initial_preflight_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests import supervisor_reboot_live as live

    initial = {"status": "supported", "accelerator": "kvm", "probe_exit": 0}
    live.write_receipt(tmp_path / "preflight.json", initial)
    monkeypatch.setattr(
        "sys.argv",
        [
            "qualification",
            "drive",
            "--ubuntu",
            "22.04",
            "--bundles",
            "unused",
            "--output",
            str(tmp_path),
        ],
    )

    def unavailable(*args):
        raise QualificationUnavailable("KVM device became unavailable")

    monkeypatch.setattr(live, "drive", unavailable)
    assert live.main() == 2
    assert json.loads((tmp_path / "preflight.json").read_text()) == initial
    failure = json.loads((tmp_path / "drive-preflight.json").read_text())
    assert failure["status"] == "qualification-unavailable"
    assert failure["actual_reboot_proven"] is False
    assert failure["reason"] == "KVM device became unavailable"


def test_guest_network_keeps_only_loopback_ssh_when_offline(tmp_path: Path) -> None:
    argv = qemu_command(tmp_path, 23456, offline=True)
    network = argv[argv.index("-netdev") + 1]
    assert "restrict=on" in network
    assert "hostfwd=tcp:127.0.0.1:23456-:22" in network
    assert "ipv6=off" in network
    assert "accel=kvm" in argv
    assert "cache=none" in argv[argv.index("-drive") + 1]
    assert "-snapshot" not in argv
    assert "restrict=off" in qemu_command(tmp_path, 23456, offline=False)[-3]


@pytest.mark.parametrize("port", [22, 0, -1, 65536])
def test_guest_rejects_privileged_or_invalid_ssh_ports(tmp_path: Path, port: int) -> None:
    with pytest.raises(ValueError, match="unprivileged"):
        qemu_command(tmp_path, port, offline=False)


def test_reboot_evidence_requires_changed_valid_boot_ids() -> None:
    before = "0d7ba3c5-d65f-4190-8274-1fe37d7b0a91"
    after = "5d974c98-d926-48f5-872c-4bdc3d9203b6"
    require_changed_boot_id(before, after)
    for pair in ((before, before), (before, ""), ("restarted", after), (before, after + "\n")):
        with pytest.raises(AssertionError, match="different Linux boot IDs"):
            require_changed_boot_id(*pair)


def test_ubuntu_image_checksum_is_unambiguous_and_exact() -> None:
    filename = "ubuntu-24.04-server-cloudimg-amd64.img"
    digest = "a" * 64
    checksum = f"{digest} *{filename}\n"
    assert image_checksum(checksum, filename) == digest
    assert image_checksum(checksum.replace(" *", "  "), filename) == digest
    for value in (
        "",
        checksum + checksum,
        checksum.replace(digest, "bad"),
        checksum.replace(filename, filename + ".other"),
    ):
        with pytest.raises(ValueError, match="exactly one"):
            image_checksum(value, filename)


def test_power_loss_only_kills_the_owned_child() -> None:
    class Process:
        killed = False
        waited = False

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            assert timeout > 0
            assert self.killed
            self.waited = True

    guest = object.__new__(Guest)
    process = Process()
    stream = io.StringIO()
    guest.process = process
    guest.log = stream
    guest.power_off()
    assert process.killed and process.waited
    assert guest.process is None and guest.log is None and stream.closed
    guest.power_off()


def test_guest_upload_cannot_address_another_machine(tmp_path: Path) -> None:
    guest = object.__new__(Guest)
    for destination in (
        "host:/tmp/file",
        "/etc/passwd",
        "/home/qualifier/file;reboot",
        "/home/qualifier/../etc",
    ):
        with pytest.raises(ValueError, match="plain path"):
            guest.copy(tmp_path / "unused", destination)
