"""Git worktree operations, shipped unchanged to the execution account.

The caller persists ``plan``'s binding before ``create``. Existing paths are
accepted only when that durable binding agrees with Git's registration.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def _git(root: Path, *arguments: str, timeout: float, optional: bool = False) -> str:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        if optional and result.returncode == 1:
            return ""
        raise ValueError(result.stderr.strip() or f"Git {' '.join(arguments)} failed")
    return result.stdout.rstrip("\n")


def _directory(value: str, *, require_owner: bool) -> Path:
    root = Path(value)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError(f"Repository directory is unavailable: {value}")
    resolved = root.resolve()
    if require_owner and resolved.stat().st_uid != os.geteuid():
        raise ValueError(f"Repository directory has the wrong execution-account owner: {value}")
    if not os.access(resolved, os.W_OK | os.X_OK):
        raise ValueError(f"Repository directory is not writable: {value}")
    return resolved


def _checkout(root: Path, *, timeout: float) -> None:
    actual = _git(root, "rev-parse", "--show-toplevel", timeout=timeout)
    if Path(actual).resolve() != root:
        raise ValueError(f"Registered repository must be a Git checkout root: {root}")


def _names(shared: Path, chat_id: str) -> tuple[Path, str]:
    if not chat_id:
        raise ValueError("Worktree binding requires a chat id")
    digest = hashlib.sha256(chat_id.encode()).hexdigest()[:24]
    return shared.parent / f"{shared.name}-rcp-{digest}", f"rcp/chat-{digest}"


def _branch_exists(root: Path, branch: str, *, timeout: float) -> bool:
    _git(root, "check-ref-format", f"refs/heads/{branch}", timeout=timeout)
    return bool(
        _git(
            root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            timeout=timeout,
            optional=True,
        )
    )


def _registered(root: Path, *, timeout: float) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    for field in _git(root, "worktree", "list", "--porcelain", "-z", timeout=timeout).split("\0"):
        if not field:
            if "worktree" in current:
                result[current["worktree"]] = current
            current = {}
        else:
            key, _, value = field.partition(" ")
            current[key] = value
    return result


def _common_dir(root: Path, *, timeout: float) -> str:
    return str(
        Path(
            _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", timeout=timeout)
        ).resolve()
    )


def _bound_common_dir(root: Path, binding: dict, *, timeout: float) -> None:
    if _common_dir(root, timeout=timeout) != binding["git_common_dir"]:
        raise ValueError(
            "Bound worktree Git metadata moved or belongs to a different Git repository"
        )


def _validate(
    binding: dict, *, timeout: float, require_owner: bool, allowed_branch: str | None = None
) -> tuple[Path, Path]:
    shared = _directory(binding["shared_path"], require_owner=require_owner)
    worktree = _directory(binding["worktree_path"], require_owner=require_owner)
    expected_path, expected_branch = _names(shared, binding["chat_id"])
    if (
        str(shared) != binding["shared_path"]
        or worktree != expected_path
        or str(worktree) != binding["worktree_path"]
    ):
        raise ValueError("Bound repository or worktree is relocated")
    if binding["branch"] != expected_branch:
        raise ValueError("Worktree branch does not match its chat binding")
    for root in (shared, worktree):
        _checkout(root, timeout=timeout)
    registration = _registered(shared, timeout=timeout).get(str(worktree))
    branches = {f"refs/heads/{binding['branch']}"}
    if allowed_branch is not None:
        _git(shared, "check-ref-format", f"refs/heads/{allowed_branch}", timeout=timeout)
        branches.add(f"refs/heads/{allowed_branch}")
    if not registration or registration.get("branch") not in branches:
        raise ValueError("Bound worktree is missing or its checked-out branch changed")
    for root in (shared, worktree):
        _bound_common_dir(root, binding, timeout=timeout)
    _git(
        worktree,
        "merge-base",
        "--is-ancestor",
        binding["starting_commit"],
        f"refs/heads/{binding['branch']}",
        timeout=timeout,
    )
    return shared, worktree


def _inspect(
    binding: dict, *, timeout: float, require_owner: bool, allowed_branch: str | None = None
) -> dict:
    shared, worktree = _validate(
        binding, timeout=timeout, require_owner=require_owner, allowed_branch=allowed_branch
    )
    default_ref = _git(
        shared,
        "symbolic-ref",
        "--quiet",
        "refs/remotes/origin/HEAD",
        timeout=timeout,
        optional=True,
    )
    default_branch = default_ref.removeprefix("refs/remotes/origin/") if default_ref else None
    starting_exists = _branch_exists(shared, binding["starting_branch"], timeout=timeout)
    return {
        "dirty_worktree": _git(
            worktree,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
            timeout=timeout,
        ).splitlines(),
        "dirty_shared": _git(
            shared,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
            timeout=timeout,
        ).splitlines(),
        "default_branch": default_branch,
        "starting_branch_exists": starting_exists,
        "ahead_count": int(
            _git(
                shared,
                "rev-list",
                "--count",
                f"refs/heads/{binding['starting_branch']}..refs/heads/{binding['branch']}",
                timeout=timeout,
            )
        )
        if starting_exists
        else None,
        "shared_branch": _git(
            shared, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=timeout, optional=True
        )
        or None,
    }


def execute(payload: dict) -> dict:
    """Plan/create/inspect/preflight/remove without performing integration.

    ``timeout_seconds`` is supplied from the application's limits owner. Binding
    metadata (alias/machine/host) is owned and checked by the calling service.
    ``target_branch`` on preflight selects a local merge; its absence selects PR.
    """
    timeout = float(payload["timeout_seconds"])
    if timeout <= 0:
        raise ValueError("Git timeout must be positive")
    require_owner = bool(payload.get("require_owner", False))
    operation = payload["operation"]
    if operation == "canonicalize":
        canonical = {}
        for value in payload["paths"]:
            root = Path(value).expanduser()
            if not root.is_absolute() or not root.is_dir():
                raise ValueError(f"Repository directory is unavailable: {value}")
            canonical[value] = str(root.resolve())
        shared_path = payload["shared_path"]
        canonical[shared_path] = str(_directory(shared_path, require_owner=require_owner))
        prospective = payload.get("prospective_worktree_path")
        if prospective is not None:
            root = Path(prospective)
            if not root.is_absolute():
                raise ValueError("Prospective worktree path must be absolute")
            _directory(str(root.parent), require_owner=False)
            canonical[prospective] = str(root.resolve())
        return {"canonical": canonical, "account_home": str(Path.home().resolve())}
    if operation == "plan":
        shared = _directory(payload["shared_path"], require_owner=require_owner)
        _checkout(shared, timeout=timeout)
        branch = _git(
            shared, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=timeout, optional=True
        )
        if not branch:
            raise ValueError("Cannot bind a worktree from a detached HEAD")
        path, name = _names(shared, payload["chat_id"])
        if (
            path.exists()
            or path.is_symlink()
            or str(path) in _registered(shared, timeout=timeout)
            or _branch_exists(shared, name, timeout=timeout)
        ):
            raise ValueError(
                "Conversation worktree path or branch already exists without a binding"
            )
        return {
            "chat_id": payload["chat_id"],
            "shared_path": str(shared),
            "worktree_path": str(path),
            "branch": name,
            "starting_branch": branch,
            "starting_commit": _git(shared, "rev-parse", "HEAD", timeout=timeout),
            "git_common_dir": _common_dir(shared, timeout=timeout),
        }
    binding = payload["binding"]
    if operation == "create":
        shared = _directory(binding["shared_path"], require_owner=require_owner)
        _checkout(shared, timeout=timeout)
        _bound_common_dir(shared, binding, timeout=timeout)
        path, branch = _names(shared, binding["chat_id"])
        if (str(shared), str(path), branch) != (
            binding["shared_path"],
            binding["worktree_path"],
            binding["branch"],
        ):
            raise ValueError("Worktree creation does not match its durable binding")
        if (
            not path.exists()
            and not path.is_symlink()
            and str(path) not in _registered(shared, timeout=timeout)
        ):
            if _branch_exists(shared, branch, timeout=timeout):
                raise ValueError("Worktree branch exists but its bound checkout is missing")
            _git(
                shared,
                "-c",
                "core.hooksPath=/dev/null",
                "worktree",
                "add",
                "-b",
                branch,
                str(path),
                binding["starting_commit"],
                timeout=timeout,
            )
        return _inspect(binding, timeout=timeout, require_owner=require_owner)
    if operation == "remote_branch":
        shared, _worktree = _validate(binding, timeout=timeout, require_owner=require_owner)
        try:
            result = subprocess.run(
                [
                    "git",
                    "--no-optional-locks",
                    "-C",
                    str(shared),
                    "ls-remote",
                    "--exit-code",
                    "--heads",
                    "origin",
                    f"refs/heads/{binding['branch']}",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"remote_branch_exists": None, "remote_branch_reason": str(exc)}
        if result.returncode not in {0, 2}:
            return {
                "remote_branch_exists": None,
                "remote_branch_reason": result.stderr.strip() or "Remote branch lookup failed",
            }
        return {"remote_branch_exists": result.returncode == 0, "remote_branch_reason": None}
    if operation not in {"inspect", "preflight", "remove"}:
        raise ValueError(f"Unknown conversation worktree operation: {operation}")
    if operation == "remove" and binding.get("status") == "removing":
        shared = _directory(binding["shared_path"], require_owner=require_owner)
        _checkout(shared, timeout=timeout)
        _bound_common_dir(shared, binding, timeout=timeout)
        path, branch = _names(shared, binding["chat_id"])
        if (str(shared), str(path), branch) != (
            binding["shared_path"],
            binding["worktree_path"],
            binding["branch"],
        ):
            raise ValueError("Worktree removal does not match its durable binding")
        if (
            not path.exists()
            and not path.is_symlink()
            and str(path) not in _registered(shared, timeout=timeout)
        ):
            if not _branch_exists(shared, branch, timeout=timeout):
                raise ValueError("Removed worktree branch is missing")
            return {"removed": True}
    result = _inspect(
        binding,
        timeout=timeout,
        require_owner=require_owner,
        allowed_branch=payload.get("allowed_branch") if operation == "inspect" else None,
    )
    if operation in {"preflight", "remove"} and result["dirty_worktree"]:
        raise ValueError(
            "Worktree has uncommitted changes:\n" + "\n".join(result["dirty_worktree"])
        )
    if operation == "preflight":
        target = payload.get("target_branch")
        if target is not None:
            if result["dirty_shared"]:
                raise ValueError(
                    "Shared checkout has uncommitted changes:\n" + "\n".join(result["dirty_shared"])
                )
            if not _branch_exists(Path(binding["shared_path"]), target, timeout=timeout):
                raise ValueError(f"Integration target branch does not exist: {target}")
            result["target_checked_out"] = result["shared_branch"] == target
    if operation == "remove":
        _git(
            Path(binding["shared_path"]),
            "worktree",
            "remove",
            binding["worktree_path"],
            timeout=timeout,
        )
        result["removed"] = True
    return result


def main(argv: list[str]) -> int:
    try:
        if len(argv) != 2:
            raise ValueError("Expected one JSON worktree request")
        result = execute(json.loads(argv[1]))
    except (KeyError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result = {"error": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through shipped source
    raise SystemExit(main(sys.argv))
