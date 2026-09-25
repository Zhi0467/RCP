"""Repository Git transport shared by provisioning and local or shipped launches."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


def deploy_key_ssh_command(key: str, account_home: str) -> str:
    """Pin Git transport to one repository key and the execution account's trust."""
    return shlex.join(
        (
            "ssh",
            "-F",
            "/dev/null",
            "-i",
            key,
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={Path(account_home) / '.ssh' / 'known_hosts'}",
        )
    )


def missing_deploy_key(repository_path: str) -> str:
    return (
        f"Repository {repository_path} has no deploy key, so Git cannot fetch or push "
        "there. Ask the server operator to provision it with "
        "rcp server project provision <request-id>."
    )


def ensure_checkout_git_access(
    repository_path: str,
    key_path: str,
    account_home: str | None = None,
    *,
    timeout: float,
) -> str | None:
    """Backfill the selected team checkout; None when it has no deploy key."""
    home = Path(account_home) if account_home is not None else Path.home()
    key = Path(key_path).expanduser()
    if not key.is_absolute():
        key = home / key
    if not key.is_file():
        return None
    command = deploy_key_ssh_command(str(key), str(home))
    environment = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    arguments = [
        "git",
        "-C",
        str(Path(repository_path).expanduser()),
        "config",
        "--local",
        "--no-includes",
    ]
    current = subprocess.run(
        [*arguments, "--get-all", "core.sshCommand"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if current.returncode == 0 and current.stdout == command + "\n":
        return str(key)
    result = subprocess.run(
        [*arguments, "--replace-all", "core.sshCommand", command],
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Could not configure the deploy key for repository {repository_path}.")

    return str(key)


def main() -> None:
    try:
        print(
            ensure_checkout_git_access(sys.argv[1], sys.argv[2], timeout=float(sys.argv[3])) or ""
        )
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
