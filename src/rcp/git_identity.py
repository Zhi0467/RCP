"""Member Git defaults; this stdlib-only module also executes on remote accounts."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitIdentity:
    user_id: str
    display_name: str


def _quoted(value: str) -> str:
    if any(character in value for character in "\r\n\0"):
        raise ValueError("Git identity must not contain newlines or NUL characters.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t") + '"'


def write_git_identity(
    data_dir: Path, identity: GitIdentity, *, git_path: str | None = None
) -> Path:
    """Write one member's lowest-precedence config on the execution machine."""
    # The host's system config comes first: within one file the later value
    # wins, so the member default must follow any system-wide identity.
    content = '[include]\n\tpath = "/etc/gitconfig"\n' if Path("/etc/gitconfig").is_file() else ""
    content += (
        f"[user]\n\tname = {_quoted(identity.display_name)}\n"
        f"\temail = {_quoted(identity.user_id + '@members.rcp.invalid')}\n"
    )
    try:
        version = subprocess.run(
            ["git", "--version"],
            env={**os.environ, "PATH": git_path} if git_path is not None else None,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            "Git 2.32 or newer is required for the member's default identity."
        ) from exc
    match = re.search(r"git version (\d+)\.(\d+)", version)
    if match is None or tuple(map(int, match.groups())) < (2, 32):
        raise ValueError("Git 2.32 or newer is required for the member's default identity.")
    # Regenerated launch configuration belongs with account credentials, which
    # the protected backup inventory already excludes.
    directory = data_dir.expanduser() / "providers" / "git-identities"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(identity.user_id.encode()).hexdigest() + ".gitconfig")
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".identity-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


if __name__ == "__main__":
    try:
        print(write_git_identity(Path(sys.argv[1]), GitIdentity(sys.argv[2], sys.argv[3])))
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
