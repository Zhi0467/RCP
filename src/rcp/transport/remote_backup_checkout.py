"""Checkout identity proof and empty server-only restore on local or SSH accounts."""

from __future__ import annotations

import json
import os
import pwd
import re
import stat
import subprocess
import sys
from pathlib import Path

_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_MAX_GIT_OUTPUT_BYTES = 64 * 1024
_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class CheckoutInspectionError(RuntimeError):
    """The checkout no longer matches its captured nonsecret identity."""


def _git_result(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        "HOME": str(Path.home()),
        "USER": pwd.getpwuid(os.geteuid()).pw_name,
        "LOGNAME": pwd.getpwuid(os.geteuid()).pw_name,
        "PATH": _SAFE_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        result = subprocess.run(
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(repository),
                *arguments,
            ),
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
            env=environment,
            umask=0o077,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckoutInspectionError("The central checkout Git metadata is unavailable.") from exc
    if (
        len(result.stdout.encode("utf-8", errors="replace")) > _MAX_GIT_OUTPUT_BYTES
        or len(result.stderr.encode("utf-8", errors="replace")) > _MAX_GIT_OUTPUT_BYTES
    ):
        raise CheckoutInspectionError("The central checkout returned too much Git output.")
    return result


def _git(repository: Path, *arguments: str) -> str:
    result = _git_result(repository, *arguments)
    if result.returncode != 0:
        raise CheckoutInspectionError("The central checkout Git identity could not be read.")
    return result.stdout.strip()


def _optional_git(repository: Path, *arguments: str) -> str | None:
    result = _git_result(repository, *arguments)
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return None
    if result.returncode != 0:
        raise CheckoutInspectionError("The central checkout Git identity could not be read.")
    return result.stdout.strip()


def inspect_checkout(
    *,
    os_account: str,
    repository_path: str,
    expected_origin: str,
    recorded_commit: str,
) -> dict[str, str]:
    """Verify exact account, path, origin, and retained provisioning commit."""

    if pwd.getpwuid(os.geteuid()).pw_name != os_account:
        raise CheckoutInspectionError("The checkout is not being read as its configured account.")
    repository = Path(repository_path)
    if (
        not repository.is_absolute()
        or repository == Path("/")
        or ".." in repository.parts
        or _FULL_COMMIT.fullmatch(recorded_commit) is None
    ):
        raise CheckoutInspectionError("The captured checkout identity is invalid.")
    try:
        metadata = repository.lstat()
    except OSError as exc:
        raise CheckoutInspectionError("The central checkout is unavailable.") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise CheckoutInspectionError("The central checkout is not a safe directory.")

    top_level = _git(repository, "rev-parse", "--show-toplevel")
    if top_level != str(repository):
        raise CheckoutInspectionError("The configured path is not the exact Git checkout root.")
    remotes = _git(repository, "remote").splitlines()
    origin = ""
    if not expected_origin:
        if remotes:
            raise CheckoutInspectionError("A server-only checkout acquired a remote.")
    else:
        if remotes != ["origin"]:
            raise CheckoutInspectionError("The central checkout does not have one exact origin.")
        origin = _git(repository, "config", "--local", "--get-all", "remote.origin.url")
        push_origin = _optional_git(
            repository,
            "config",
            "--local",
            "--get-all",
            "remote.origin.pushurl",
        )
        if origin != expected_origin or push_origin not in {None, expected_origin}:
            raise CheckoutInspectionError("The central checkout origin identity changed.")
    head = _git(repository, "rev-parse", "--verify", "HEAD^{commit}")
    if _FULL_COMMIT.fullmatch(head) is None:
        raise CheckoutInspectionError("The central checkout HEAD is invalid.")
    retained = _git(repository, "rev-parse", "--verify", f"{recorded_commit}^{{commit}}")
    if retained != recorded_commit:
        raise CheckoutInspectionError("The provisioning commit is absent from the checkout.")
    return {
        "account": os_account,
        "repository_path": str(repository),
        "origin": origin,
        "head": head,
        "recorded_commit": retained,
    }


def restore_server_only(
    *, os_account: str, central_root: str, repository_path: str, recorded_commit: str = ""
) -> dict[str, str]:
    """Create an empty replacement; retry only our empty, remote-free history."""
    if pwd.getpwuid(os.geteuid()).pw_name != os_account:
        raise CheckoutInspectionError("Restore account differs from its configured owner.")
    root, repository = Path(central_root), Path(repository_path)
    if (
        not root.is_absolute()
        or root == Path("/")
        or ".." in repository.parts
        or not repository.is_relative_to(root)
        or len(repository.relative_to(root).parts) != 3
        or repository.parent.name != "repositories"
    ):
        raise CheckoutInspectionError("Invalid replacement checkout path.")
    current = Path("/")
    for part in repository.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not current.is_relative_to(root):
                raise CheckoutInspectionError("Central root ancestry is absent.") from None
            current.mkdir(mode=0o700)
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or (
            current.is_relative_to(root)
            and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077)
        ):
            raise CheckoutInspectionError("Unsafe replacement checkout ancestry.")
    if recorded_commit:
        return inspect_checkout(
            os_account=os_account,
            repository_path=repository_path,
            expected_origin="",
            recorded_commit=recorded_commit,
        )
    if set(p.name for p in repository.iterdir()) - {".git"}:
        raise CheckoutInspectionError("Replacement checkout contains existing work.")
    git_dir = repository / ".git"
    if git_dir.exists() or git_dir.is_symlink():
        if not stat.S_ISDIR(git_dir.lstat().st_mode):
            raise CheckoutInspectionError("Replacement Git directory is unsafe.")
    else:
        _git(repository, "init", "-b", "main", "--template=")
    if _git(repository, "remote"):
        raise CheckoutInspectionError("Replacement checkout has a remote.")
    if _git(repository, "ls-files", "--stage"):
        raise CheckoutInspectionError("Replacement index is not empty.")
    head = _git_result(repository, "rev-parse", "--verify", "HEAD^{commit}")
    if head.returncode != 0:
        _git(
            repository,
            "-c",
            "user.name=RCP",
            "-c",
            "user.email=rcp@rcp.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--allow-empty",
            "-m",
            "Start RCP project",
        )
    commit = _git(repository, "rev-parse", "--verify", "HEAD^{commit}")
    if (
        _git(repository, "symbolic-ref", "HEAD") != "refs/heads/main"
        or _git(repository, "rev-list", "--count", "HEAD") != "1"
        or _git(repository, "ls-tree", "HEAD")
        or _git(repository, "show", "-s", "--format=%an <%ae>%n%B", "HEAD")
        != "RCP <rcp@rcp.invalid>\nStart RCP project"
    ):
        raise CheckoutInspectionError("Existing replacement history is not the RCP first commit.")
    _git(repository, "config", "--local", "core.hooksPath", "/dev/null")
    return inspect_checkout(
        os_account=os_account,
        repository_path=repository_path,
        expected_origin="",
        recorded_commit=commit,
    )


def main(argv: list[str]) -> int:
    if len(argv) == 6 and argv[1] == "restore-server-only":
        try:
            payload = restore_server_only(
                os_account=argv[2],
                central_root=argv[3],
                repository_path=argv[4],
                recorded_commit=argv[5],
            )
        except (CheckoutInspectionError, OSError, ValueError):
            return 3
        payload["account_home"] = pwd.getpwuid(os.geteuid()).pw_dir
        sys.stdout.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
        return 0
    if len(argv) != 5:
        return 2
    try:
        payload = inspect_checkout(
            os_account=argv[1],
            repository_path=argv[2],
            expected_origin=argv[3],
            recorded_commit=argv[4],
        )
    except CheckoutInspectionError:
        return 3
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through SSH source execution
    raise SystemExit(main(sys.argv))
