"""Real agent tools and filesystem assertions for the published-release upgrade gate."""

from __future__ import annotations

import hashlib
import os
import socket
import stat
import subprocess
import sys
import zipfile
from pathlib import Path


def build_scratch_wheel(directory: Path) -> Path:
    """Build a dependency-free wheel once; installation still goes through real uv."""
    wheel = directory / "upgrade_probe-1.0-py3-none-any.whl"
    files = {
        "upgrade_probe.py": b"VALUE = 'rollback-works'\n",
        "upgrade_probe-1.0.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: upgrade-probe\nVersion: 1.0\n"
        ),
        "upgrade_probe-1.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: upgrade-gate\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = "upgrade_probe-1.0.dist-info/RECORD"
    files[record] = "".join(f"{name},,\n" for name in (*files, record)).encode()
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return wheel


def populate_agent_scratch(data: Path, projects: Path, wheel: Path) -> Path:
    scratch = data / "run-stage" / "real-tools"
    scratch.mkdir(parents=True)
    environment = dict(os.environ, UV_LINK_MODE="hardlink", UV_CACHE_DIR=str(scratch / "cache"))
    subprocess.run(
        ["uv", "venv", "--offline", "--python", sys.executable, str(scratch / "venv")],
        check=True,
        env=environment,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            str(scratch / "venv/bin/python"),
            str(wheel),
        ],
        check=True,
        env=environment,
    )
    installed = next((scratch / "venv/lib").glob("python*/site-packages/upgrade_probe.py"))
    assert installed.stat().st_nlink > 1
    locks = list(scratch.rglob(".lock"))
    assert locks
    for lock in locks:
        lock.chmod(0o666)  # uv releases differ in how their lock mode respects umask.
    # A real uv-created hardlink also crosses the two captured roots.
    os.link(installed, projects / "scratch-package-link")
    repository = scratch / "repo"
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    (repository / "result.txt").write_text("retained research\n")
    subprocess.run(["git", "-C", str(repository), "add", "result.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Upgrade Test",
            "-c",
            "user.email=upgrade@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "record result",
        ],
        check=True,
    )
    os.mkfifo(scratch / "fifo")
    # Bind relative to cwd: macOS limits Unix socket addresses to 104 bytes.
    with socket.socket(socket.AF_UNIX) as listener:
        previous = Path.cwd()
        try:
            os.chdir(scratch)
            listener.bind("socket")
        finally:
            os.chdir(previous)
    (scratch / "unreadable-file").write_bytes(b"still retained\n")
    (scratch / "unreadable-file").chmod(0)
    (scratch / "unreadable-directory").mkdir(mode=0)
    (scratch / "dangling").symlink_to("missing-target")
    (scratch / "empty").mkdir()
    (scratch / "odd\\name\n").mkdir()  # Agent filenames are not manifest keys.
    with (scratch / "sparse").open("wb") as stream:
        stream.seek(16 * 1024 * 1024)
        stream.write(b"end")
    return scratch


def tree_state(roots: tuple[Path, ...]) -> tuple[dict, list]:
    """Compare user-visible paths, metadata, bytes and in-set hardlink groups."""
    entries = {}
    links: dict[tuple[int, int], list[str]] = {}

    def visit(path: Path) -> None:
        info = path.lstat()
        kind, mode = stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode)
        content = os.readlink(path) if stat.S_ISLNK(kind) else None
        if stat.S_ISREG(kind) or stat.S_ISDIR(kind):
            # The owner may inspect mode-000 fixtures, then put their modes back.
            try:
                path.chmod(mode | (0o500 if stat.S_ISDIR(kind) else 0o400))
                if stat.S_ISDIR(kind):
                    for child in sorted(path.iterdir()):
                        visit(child)
                else:
                    with path.open("rb") as stream:
                        content = hashlib.file_digest(stream, "sha256").hexdigest()
                    links.setdefault((info.st_dev, info.st_ino), []).append(str(path))
            finally:
                path.chmod(mode)
        entries[str(path)] = (
            kind,
            mode,
            info.st_uid,
            info.st_gid,
            info.st_mtime_ns,
            content,
            # Production GNU preservation is qualified on Linux; the managed
            # macOS Python does not expose these syscalls.
            {
                name: os.getxattr(path, name, follow_symlinks=False).hex()
                for name in os.listxattr(path, follow_symlinks=False)
            }
            if sys.platform == "linux"
            else None,
        )

    for root in roots:
        visit(root)
    return entries, sorted(sorted(group) for group in links.values() if len(group) > 1)


def assert_scratch_usable(scratch: Path) -> None:
    sparse = (scratch / "sparse").stat()
    assert sparse.st_blocks * 512 < sparse.st_size
    subprocess.run(
        [
            str(scratch / "venv/bin/python"),
            "-c",
            "import upgrade_probe; assert upgrade_probe.VALUE == 'rollback-works'",
        ],
        check=True,
    )
    result = subprocess.run(
        ["git", "-C", str(scratch / "repo"), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not result.stdout
