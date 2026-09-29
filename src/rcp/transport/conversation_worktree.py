"""Git worktree operations, shipped unchanged to the execution account.

The caller persists ``plan``'s binding before ``create``. Existing paths are
accepted only when that durable binding agrees with Git's registration.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
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


def _names(shared: Path, owner: dict) -> tuple[Path, str]:
    episode_id = owner.get("owner_episode_id")
    owner_id = episode_id or owner.get("chat_id")
    if not owner_id or (episode_id and owner.get("chat_id")):
        raise ValueError("Worktree binding requires exactly one owner")
    kind = "episode" if episode_id else "chat"
    identity = f"episode:{owner_id}" if episode_id else owner_id
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return shared.parent / f"{shared.name}-rcp-{digest}", f"rcp/{kind}-{digest}"


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
    # Git 2.34 (Ubuntu 22.04) has porcelain output but no worktree-list -z.
    # Keep Unicode literal so Python can decode newer Git's C-quoted paths.
    output = _git(
        root, "-c", "core.quotePath=false", "worktree", "list", "--porcelain", timeout=timeout
    )
    result: dict[str, dict[str, str]] = {}
    for record in output.split("\n\n"):
        current: dict[str, str] = {}
        for field in record.split("\n"):
            key, _, value = field.partition(" ")
            if (
                key not in {"worktree", "HEAD", "branch", "bare", "detached", "locked", "prunable"}
                or key in current
            ):
                raise ValueError("Git worktree registration could not be read safely.")
            current[key] = value
        path = current.get("worktree", "")
        if path.startswith('"'):
            try:
                path = ast.literal_eval(path)
            except (SyntaxError, ValueError) as exc:
                raise ValueError("Git worktree path could not be read safely.") from exc
        if not isinstance(path, str) or not Path(path).is_absolute() or path in result:
            raise ValueError("Git worktree registration could not be read safely.")
        current["worktree"] = path
        result[path] = current
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
    expected_path, expected_branch = _names(shared, binding)
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
    actual_branch = _git(
        worktree, "symbolic-ref", "--quiet", "HEAD", timeout=timeout, optional=True
    )
    if (
        not registration
        or registration.get("branch") not in branches
        or actual_branch not in branches
    ):
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


class WorktreeValidationError(ValueError):
    """A refusal whose code survives the execution-host JSON boundary."""

    def __init__(self, code: str, details: object = None):
        self.code = code
        super().__init__(code if details is None else f"{code}: {details}")


def _ref_commit(root: Path, branch: str, timeout: float) -> str:
    if not _branch_exists(root, branch, timeout=timeout):
        raise WorktreeValidationError("target_missing", branch)
    return _git(root, "rev-parse", f"refs/heads/{branch}", timeout=timeout)


def _interrupted(root: Path, timeout: float) -> None:
    for name in (
        "MERGE_HEAD",
        "rebase-merge",
        "rebase-apply",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "sequencer",
    ):
        path = _git(root, "rev-parse", "--git-path", name, timeout=timeout)
        if (root / path).exists():
            raise WorktreeValidationError("interrupted_git_state", name)
    if _git(root, "ls-files", "--unmerged", timeout=timeout):
        raise WorktreeValidationError("interrupted_git_state", "unmerged_index")
    for line in _git(
        root, "status", "--porcelain=v2", "--ignore-submodules=none", timeout=timeout
    ).splitlines():
        parts = line.split(" ")
        if parts[0] in {"1", "2"} and parts[2].startswith("S") and parts[2][2:] != "..":
            raise WorktreeValidationError("interrupted_git_state", "dirty_submodule")


def _leftovers(root: Path, timeout: float) -> list[str]:
    # NUL records preserve filenames, including whitespace and newlines.
    return list(
        dict.fromkeys(
            filter(
                None,
                _git(
                    root,
                    "ls-files",
                    "-z",
                    "--modified",
                    "--deleted",
                    "--others",
                    "--exclude-standard",
                    timeout=timeout,
                ).split("\0")
                + _git(root, "diff", "--cached", "--name-only", "-z", timeout=timeout).split("\0"),
            )
        )
    )


def _merge_inputs(payload: dict, timeout: float, require_owner: bool) -> tuple[Path, Path, dict]:
    binding = payload["binding"]
    if not execute({"operation": "git_version", "timeout_seconds": timeout})["supported"]:
        raise WorktreeValidationError("git_version_unsupported")
    shared, worktree = _validate(binding, timeout=timeout, require_owner=require_owner)
    _interrupted(worktree, timeout)
    target = payload.get("target_branch") or binding["starting_branch"]
    if target == binding["branch"]:
        raise WorktreeValidationError("target_is_episode_branch")
    target_commit = _ref_commit(shared, target, timeout)
    registrations = [
        entry
        for entry in _registered(shared, timeout=timeout).values()
        if entry.get("branch") == f"refs/heads/{target}"
    ]
    if any(Path(entry["worktree"]).resolve() != shared for entry in registrations):
        raise WorktreeValidationError("target_checked_out_elsewhere")
    shared_branch = _git(
        shared, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=timeout, optional=True
    )
    checked_out = shared_branch == target
    if checked_out:
        _interrupted(shared, timeout)
        dirty = _git(
            shared,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
            timeout=timeout,
        )
        if dirty:
            raise WorktreeValidationError("target_dirty", dirty)
    source_commit = _git(worktree, "rev-parse", "HEAD", timeout=timeout)
    return (
        shared,
        worktree,
        {
            "source_commit": source_commit,
            "target_commit": target_commit,
            "target_branch": target,
            "target_checked_out": checked_out,
            "shared_branch": shared_branch,
            "leftover_files": _leftovers(worktree, timeout),
        },
    )


def _contains(root: Path, ancestor: str, target: str, timeout: float) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, target],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise ValueError(result.stderr.strip())
    return result.returncode == 0


def _merge_tree(root: Path, target: str, source: str, timeout: float) -> dict:
    result = subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "-C",
            str(root),
            "merge-tree",
            "--write-tree",
            "--name-only",
            "-z",
            target,
            source,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise ValueError(result.stderr.strip())
    fields = result.stdout.split("\0")
    conflicts = fields[1 : fields.index("", 1)] if result.returncode else []
    return {
        "tree": fields[0].strip(),
        "status": "conflict" if result.returncode else "clean",
        "conflict_files": conflicts,
        "merge_tree_output": result.stdout,
    }


def _shared_binding(binding: dict, timeout: float, require_owner: bool) -> Path:
    shared = _directory(binding["shared_path"], require_owner=require_owner)
    _checkout(shared, timeout=timeout)
    _bound_common_dir(shared, binding, timeout=timeout)
    path, branch = _names(shared, binding)
    if (str(shared), str(path), branch) != (
        binding["shared_path"],
        binding["worktree_path"],
        binding["branch"],
    ):
        raise WorktreeValidationError("binding_changed")
    return shared


def _commit_command(root: Path, arguments: list[str], timestamp: str, timeout: float) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "-C",
            str(root),
            *arguments,
        ],
        env={**os.environ, "GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip())
    return result.stdout.strip()


def _episode_operation(payload: dict, timeout: float, require_owner: bool) -> dict:
    operation, binding = payload["operation"], payload["binding"]
    target = payload.get("target_branch") or binding["starting_branch"]
    if operation in {"verify_landing", "remove_episode_worktree", "delete_branch"}:
        shared = _shared_binding(binding, timeout, require_owner)
        if operation == "remove_episode_worktree":
            path = Path(binding["worktree_path"])
            if (
                not path.exists()
                and not path.is_symlink()
                and str(path) not in _registered(shared, timeout=timeout)
            ):
                return {"removed": True}
            _validate(binding, timeout=timeout, require_owner=require_owner)
            expected_source = payload.get("source_commit")
            if (
                expected_source is not None
                and _ref_commit(shared, binding["branch"], timeout) != expected_source
            ):
                raise WorktreeValidationError("source_changed")
            _interrupted(path, timeout)
            arguments = ["worktree", "remove"]
            if payload.get("discard"):
                arguments.append("--force")
            _git(shared, *arguments, str(path), timeout=timeout)
            return {"removed": True}
        target_commit = _ref_commit(shared, target, timeout)
        source = payload["source_commit"]
        squash = payload.get("squash_commit")
        verified = _contains(shared, squash or source, target_commit, timeout)
        if operation == "verify_landing":
            return {"verified": verified, "landed_commit": squash or source}
        if Path(binding["worktree_path"]).exists() or str(
            Path(binding["worktree_path"])
        ) in _registered(shared, timeout=timeout):
            raise WorktreeValidationError("worktree_removal_required")
        if not verified:
            raise WorktreeValidationError("code_not_delivered")
        if not _branch_exists(shared, binding["branch"], timeout=timeout):
            return {"deleted": True}
        tip = _ref_commit(shared, binding["branch"], timeout)
        if tip != source or any(
            entry.get("branch") == f"refs/heads/{binding['branch']}"
            for entry in _registered(shared, timeout=timeout).values()
        ):
            raise WorktreeValidationError("source_changed")
        _git(shared, "update-ref", "-d", f"refs/heads/{binding['branch']}", source, timeout=timeout)
        return {"deleted": True}
    shared, worktree, facts = _merge_inputs(payload, timeout, require_owner)
    if operation == "commit_leftovers":
        if facts["leftover_files"]:
            _git(worktree, "add", "-A", timeout=timeout)
            if _git(worktree, "diff", "--cached", "--name-only", timeout=timeout):
                _git(
                    worktree,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "-m",
                    "RCP episode leftovers",
                    timeout=timeout,
                )
        return {
            "source_commit": _git(worktree, "rev-parse", "HEAD", timeout=timeout),
            "leftover_files": facts["leftover_files"],
        }
    if operation == "merge_preview":
        result = _merge_tree(shared, facts["target_commit"], facts["source_commit"], timeout)
        if _contains(shared, facts["source_commit"], facts["target_commit"], timeout):
            result["status"] = "already_merged"
        return {
            **facts,
            **result,
            "commits_ahead": int(
                _git(
                    shared,
                    "rev-list",
                    "--count",
                    f"{facts['target_commit']}..{facts['source_commit']}",
                    timeout=timeout,
                )
            ),
        }
    for key in ("source_commit", "target_commit"):
        if payload[key] != facts[key]:
            raise WorktreeValidationError(f"{key.removesuffix('_commit')}_changed")
    if operation == "land" and payload["expected_shared_branch"] != facts["shared_branch"]:
        raise WorktreeValidationError("shared_checkout_changed")
    if facts["leftover_files"]:
        raise WorktreeValidationError("source_dirty")
    tree = _merge_tree(shared, facts["target_commit"], facts["source_commit"], timeout)
    if tree["status"] == "conflict":
        raise WorktreeValidationError("code_residue_needs_merge_task")
    if tree["tree"] != payload["tree"]:
        raise WorktreeValidationError("merge_tree_changed")
    mode = payload.get("history_mode", "merge")
    if mode not in {"merge", "squash"}:
        raise WorktreeValidationError("invalid_history_mode")
    timestamp = payload.get("commit_timestamp") or f"{int(time.time())} +0000"
    message = f"RCP episode {mode}: {binding['branch']}"
    parents = ["-p", facts["target_commit"]]
    if mode == "merge":
        parents += ["-p", facts["source_commit"]]
    expected = _commit_command(
        shared, ["commit-tree", tree["tree"], *parents, "-m", message], timestamp, timeout
    )
    result = {
        "landed_commit": expected,
        "squash_commit": expected if mode == "squash" else None,
        "commit_timestamp": timestamp,
        "expected_shared_branch": facts["shared_branch"],
    }
    if operation == "prepare_landing":
        return result
    if payload["landed_commit"] != expected:
        raise WorktreeValidationError("landing_commit_changed")
    if facts["target_checked_out"]:
        # One fast-forward to the prebuilt commit moves the ref, index, and files together;
        # there is no second step for a crash to fall between.
        _git(
            shared,
            "-c",
            "core.hooksPath=/dev/null",
            "merge",
            "--ff-only",
            "--",
            expected,
            timeout=timeout,
        )
        if _git(shared, "rev-parse", "HEAD", timeout=timeout) != expected:
            raise WorktreeValidationError("landing_commit_changed")
    else:
        _git(
            shared,
            "update-ref",
            f"refs/heads/{target}",
            expected,
            facts["target_commit"],
            timeout=timeout,
        )
    return result


def execute(payload: dict) -> dict:
    """Run binding lifecycle operations and explicitly dispatched episode merges.

    ``timeout_seconds`` is supplied from the application's limits owner. Binding
    metadata (alias/machine/host) is owned and checked by the calling service.
    ``target_branch`` on preflight selects a local merge; its absence selects PR.
    """
    timeout = float(payload["timeout_seconds"])
    if timeout <= 0:
        raise ValueError("Git timeout must be positive")
    require_owner = bool(payload.get("require_owner", False))
    operation = payload["operation"]
    if operation == "git_version":
        result = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=timeout, check=False
        )
        if result.returncode:
            raise ValueError(result.stderr.strip() or "Git version is unavailable")
        match = re.search(r"git version (\d+)\.(\d+)(?:\.(\d+))?", result.stdout)
        if match is None:
            raise ValueError("Git version is unavailable")
        version = [int(part or 0) for part in match.groups()]
        return {"version": version, "supported": version >= [2, 38, 0]}
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
        path, name = _names(shared, payload)
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
            **(
                {"owner_episode_id": payload["owner_episode_id"]}
                if payload.get("owner_episode_id")
                else {"chat_id": payload["chat_id"]}
            ),
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
        path, branch = _names(shared, binding)
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
    if operation in {
        "merge_preview",
        "commit_leftovers",
        "prepare_landing",
        "land",
        "verify_landing",
        "remove_episode_worktree",
        "delete_branch",
    }:
        return _episode_operation(payload, timeout, require_owner)
    if operation not in {"inspect", "preflight", "remove"}:
        raise ValueError(f"Unknown conversation worktree operation: {operation}")
    if operation == "remove" and binding.get("status") == "removing":
        shared = _directory(binding["shared_path"], require_owner=require_owner)
        _checkout(shared, timeout=timeout)
        _bound_common_dir(shared, binding, timeout=timeout)
        path, branch = _names(shared, binding)
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
    allowed_branch = payload.get("allowed_branch")
    if (
        operation == "preflight"
        and allowed_branch is not None
        and allowed_branch != payload.get("target_branch")
    ):
        raise ValueError("Recovery must use its exact admitted integration target")
    result = _inspect(
        binding,
        timeout=timeout,
        require_owner=require_owner,
        allowed_branch=allowed_branch if operation in {"inspect", "preflight"} else None,
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
        if isinstance(exc, WorktreeValidationError):
            result["code"] = exc.code
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through shipped source
    raise SystemExit(main(sys.argv))
