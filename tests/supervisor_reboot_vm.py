"""External controller for disposable, real Linux boots on hosted runners.

No command accepts a remote host. SSH always addresses a private QEMU guest
through a loopback-only forwarding port, and only the owned child is killed.
The ordinary test suite exercises helpers; qualification explicitly enables
this controller on a GitHub-hosted runner.

QEMU's ``restrict=on`` denies guest-initiated host/external traffic while keeping
the explicit SSH forwarding rule. See https://www.qemu.org/docs/master/system/invocation.html
and https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import uuid
from pathlib import Path
from typing import IO

DISPOSABLE_CONFIRMATION = "I_UNDERSTAND_THIS_CREATES_DISPOSABLE_VMS"
GUEST_ROOT = "/opt/rcp-supervisor-qualification"
GUEST_MARKER = "/etc/rcp-supervisor-qualification"
GUEST_MEMORY_MIB = 3072
GUEST_DISK_GIB = 12
BOOT_TIMEOUT = 240
COMMAND_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 600
MIN_FREE_BYTES = 12 * 1024**3
MIN_AVAILABLE_MEMORY_KIB = 4 * 1024**2
MAX_IMAGE_BYTES = 2 * 1024**3
MAX_CHECKSUM_BYTES = 1024**2
_BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_IMAGES = {
    "22.04": "ubuntu-22.04-server-cloudimg-amd64.img",
    "24.04": "ubuntu-24.04-server-cloudimg-amd64.img",
}


class QualificationUnavailable(RuntimeError):
    """The controller cannot prove a real guest reboot on this runner."""


def run(
    argv: list[str], *, timeout: int = COMMAND_TIMEOUT, **kwargs
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, check=True, capture_output=True, text=True, timeout=timeout, **kwargs
    )


def preflight(directory: Path, confirmation: str) -> dict:
    """Fail before downloads or VM creation, without modifying host settings."""
    if confirmation != DISPOSABLE_CONFIRMATION:
        raise QualificationUnavailable("The explicit disposable-VM confirmation is missing.")
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or platform.system() != "Linux"
        or platform.machine() != "x86_64"
    ):
        raise QualificationUnavailable(
            "Qualification is restricted to GitHub-hosted Linux x86_64 runners."
        )
    if Path("/etc/rcp").exists():
        raise QualificationUnavailable(
            "This host already has RCP configuration; refusing qualification."
        )
    required = (
        "qemu-system-x86_64",
        "qemu-img",
        "cloud-localds",
        "ssh",
        "scp",
        "ssh-keygen",
        "curl",
    )
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise QualificationUnavailable(f"Runner prerequisites are missing: {', '.join(missing)}.")
    memory = re.search(
        r"^MemAvailable:\s+(\d+) kB$", Path("/proc/meminfo").read_text(), re.MULTILINE
    )
    if memory is None or int(memory.group(1)) < MIN_AVAILABLE_MEMORY_KIB:
        raise QualificationUnavailable("The runner needs at least 4 GiB of available memory.")
    if (os.cpu_count() or 0) < 2 or shutil.disk_usage(directory).free < MIN_FREE_BYTES:
        raise QualificationUnavailable("The runner needs 2 CPUs and 12 GiB of free storage.")
    try:
        probe = run(
            [
                "qemu-system-x86_64",
                "-machine",
                "accel=kvm",
                "-m",
                "128",
                "-S",
                "-nodefaults",
                "-display",
                "none",
                "-monitor",
                "stdio",
            ],
            input="quit\n",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        diagnostic = getattr(exc, "stderr", None)
        detail = diagnostic.strip()[:2000] if isinstance(diagnostic, str) else str(exc)
        raise QualificationUnavailable(
            "QEMU could not initialize KVM; real reboot qualification is unavailable. " + detail
        ) from exc
    return {
        "accelerator": "kvm",
        "runner": os.environ.get("RUNNER_OS"),
        "qemu": run(["qemu-system-x86_64", "--version"]).stdout.splitlines()[0],
        "probe_exit": probe.returncode,
    }


def image_checksum(text: str, filename: str) -> str:
    matches = []
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if match and match.group(2) == filename:
            matches.append(match.group(1))
    if len(matches) != 1:
        raise ValueError("Ubuntu's checksum manifest must name exactly one selected image.")
    return matches[0]


def download_image(directory: Path, ubuntu: str) -> tuple[Path, str]:
    filename = _IMAGES[ubuntu]
    base = f"https://cloud-images.ubuntu.com/releases/{ubuntu}/release/"
    checksums = directory / "SHA256SUMS"
    image = directory / filename
    for name, destination, limit in (
        ("SHA256SUMS", checksums, MAX_CHECKSUM_BYTES),
        (filename, image, MAX_IMAGE_BYTES),
    ):
        run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--max-redirs",
                "3",
                "--connect-timeout",
                "15",
                "--max-time",
                str(DOWNLOAD_TIMEOUT),
                "--max-filesize",
                str(limit),
                "--output",
                str(destination),
                base + name,
            ],
            timeout=DOWNLOAD_TIMEOUT + 10,
        )
        if destination.stat().st_size > limit:
            raise ValueError("Ubuntu download exceeded its size limit.")
    expected = image_checksum(checksums.read_text(), filename)
    with image.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError("The downloaded Ubuntu cloud image failed its SHA-256 check.")
    return image, actual


def qemu_command(directory: Path, port: int, *, offline: bool) -> list[str]:
    if not 1024 <= port <= 65535:
        raise ValueError("The guest SSH forwarding port must be unprivileged.")
    return [
        "qemu-system-x86_64",
        "-machine",
        "accel=kvm",
        "-cpu",
        "host",
        "-smp",
        "2",
        "-m",
        str(GUEST_MEMORY_MIB),
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        f"file:{directory / 'serial.log'}",
        "-drive",
        f"file={directory / 'guest.qcow2'},if=virtio,format=qcow2,cache=none",
        "-drive",
        f"file={directory / 'seed.img'},if=virtio,format=raw,readonly=on",
        "-netdev",
        f"user,id=net0,restrict={'on' if offline else 'off'},ipv6=off,hostfwd=tcp:127.0.0.1:{port}-:22",
        "-device",
        "virtio-net-pci,netdev=net0",
    ]


def require_changed_boot_id(before: str, after: str) -> None:
    if not _BOOT_ID.fullmatch(before) or not _BOOT_ID.fullmatch(after) or before == after:
        raise AssertionError("Qualification requires two valid, different Linux boot IDs.")


class Guest:
    def __init__(self, directory: Path, *, image: Path, image_sha256: str, ubuntu: str):
        self.directory = directory
        self.ubuntu = ubuntu
        self.image_sha256 = image_sha256
        self.process: subprocess.Popen | None = None
        self.log: IO | None = None
        self.token = str(uuid.uuid4())
        self.boot_ids: list[str] = []
        directory.mkdir(mode=0o700)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.key = directory / "ssh-key"
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)])
        public_key = self.key.with_suffix(".pub").read_text().strip()
        seed = {
            "users": [
                {
                    "name": "qualifier",
                    "sudo": "ALL=(ALL) NOPASSWD:ALL",
                    "shell": "/bin/bash",
                    "ssh_authorized_keys": [public_key],
                }
            ],
            "ssh_pwauth": False,
            "disable_root": True,
            "write_files": [
                {
                    "path": GUEST_MARKER,
                    "owner": "root:root",
                    "permissions": "0600",
                    "content": self.token,
                }
            ],
        }
        (directory / "user-data").write_text("#cloud-config\n" + json.dumps(seed))
        (directory / "meta-data").write_text(
            json.dumps({"instance-id": self.token, "local-hostname": "rcp-qualification"})
        )
        run(
            [
                "cloud-localds",
                str(directory / "seed.img"),
                str(directory / "user-data"),
                str(directory / "meta-data"),
            ]
        )
        run(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(image.resolve()),
                str(directory / "guest.qcow2"),
                f"{GUEST_DISK_GIB}G",
            ]
        )

    def _ssh_options(self) -> list[str]:
        return [
            "-F",
            "/dev/null",
            "-i",
            str(self.key),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={self.directory / 'known-hosts'}",
            "-o",
            "LogLevel=ERROR",
        ]

    def ssh(
        self, argv: list[str], *, timeout: int = COMMAND_TIMEOUT, check: bool = True
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [
                    "ssh",
                    *self._ssh_options(),
                    "-p",
                    str(self.port),
                    "qualifier@127.0.0.1",
                    shlex.join(argv),
                ],
                check=check,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr:
                exc.add_note("Guest SSH stderr:\n" + exc.stderr[-20000:])
            raise

    def copy(self, source: Path, destination: str) -> None:
        if (
            not destination.startswith("/home/qualifier/")
            or ".." in Path(destination).parts
            or not re.fullmatch(r"[a-zA-Z0-9/_.-]+", destination)
        ):
            raise ValueError("Guest uploads must name a plain path under /home/qualifier/.")
        run(
            [
                "scp",
                *self._ssh_options(),
                "-P",
                str(self.port),
                str(source),
                f"qualifier@127.0.0.1:{destination}",
            ]
        )

    def install_payload(self, source: Path) -> None:
        self.copy(source, "/home/qualifier/payload.tar.gz")
        self.ssh(["sudo", "-n", "mkdir", "-m", "0755", GUEST_ROOT])
        self.ssh(
            [
                "sudo",
                "-n",
                "tar",
                "--extract",
                "--gzip",
                "--file",
                "/home/qualifier/payload.tar.gz",
                "--directory",
                GUEST_ROOT,
                "--no-same-owner",
                "--no-same-permissions",
            ]
        )

    def start(self, *, offline: bool = False) -> str:
        from tests.helpers import wait_until

        if self.process is not None:
            raise RuntimeError("The owned guest is already running.")
        self.log = (self.directory / "qemu.log").open("a")
        self.process = subprocess.Popen(
            qemu_command(self.directory, self.port, offline=offline),
            stdin=subprocess.DEVNULL,
            stdout=self.log,
            stderr=self.log,
        )

        def ready():
            assert self.process is not None
            if self.process.poll() is not None:
                raise RuntimeError("The owned QEMU guest exited; inspect qemu.log and serial.log.")
            try:
                result = self.ssh(
                    ["cat", "/proc/sys/kernel/random/boot_id"], timeout=10, check=False
                )
            except subprocess.TimeoutExpired:
                return None
            return result.stdout.strip() if result.returncode == 0 else None

        boot_id = wait_until(
            ready,
            timeout=BOOT_TIMEOUT,
            interval=2,
            detail="The guest did not boot within the qualification deadline.",
        )
        self.ssh(["cloud-init", "status", "--wait"], timeout=BOOT_TIMEOUT)
        marker = self.ssh(["sudo", "-n", "cat", GUEST_MARKER]).stdout
        if marker != self.token:
            raise RuntimeError(
                "The responding guest does not have this controller's disposable identity."
            )
        release = {}
        for line in self.ssh(["cat", "/etc/os-release"]).stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"ID", "VERSION_ID"}:
                release[key] = shlex.split(value)[0]
        if release != {"ID": "ubuntu", "VERSION_ID": self.ubuntu}:
            raise RuntimeError("The guest operating system differs from the requested matrix.")
        if self.boot_ids:
            require_changed_boot_id(self.boot_ids[-1], boot_id)
        self.boot_ids.append(boot_id)
        (self.directory / "guest.json").write_text(
            json.dumps({"release": release, "boot_ids": self.boot_ids}, indent=2) + "\n"
        )
        return boot_id

    def power_off(self) -> None:
        """Simulate abrupt power loss, never a guest-controlled graceful stop."""
        if self.process is not None:
            self.process.kill()
            self.process.wait(timeout=15)
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def power_cycle(self, *, offline: bool) -> str:
        self.power_off()
        return self.start(offline=offline)

    def save_baseline(self) -> None:
        self.power_off()
        run(
            [
                "qemu-img",
                "snapshot",
                "-c",
                "qualification-baseline",
                str(self.directory / "guest.qcow2"),
            ]
        )

    def reset(self) -> str:
        self.power_off()
        run(
            [
                "qemu-img",
                "snapshot",
                "-a",
                "qualification-baseline",
                str(self.directory / "guest.qcow2"),
            ]
        )
        return self.start()

    def collect(self, name: str, argv: list[str]) -> None:
        result = self.ssh(argv, check=False)
        (self.directory / name).write_text(result.stdout + result.stderr)
