from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rcp.transport import conversation_worktree


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "shared checkout"
    root.mkdir()
    git(root, "init", "--initial-branch=research")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Worktree fixture")
    (root / "notes.txt").write_text("initial\n")
    git(root, "add", "notes.txt")
    git(root, "commit", "-m", "Initial fixture")
    git(root, "branch", "release")
    git(root, "update-ref", "refs/remotes/origin/release", "HEAD")
    git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/release")
    return root


def run(operation: str, **kwargs: object) -> dict:
    return conversation_worktree.execute({"operation": operation, "timeout_seconds": 10, **kwargs})


def bind(repository: Path, chat_id: str = "chat-1") -> dict:
    binding = run("plan", shared_path=str(repository), chat_id=chat_id)
    run("create", binding=binding)
    return binding


def test_binding_isolates_uncommitted_work_and_survives_reexecution(repository: Path) -> None:
    (repository / "notes.txt").write_text("shared dirty\n")
    (repository / "untracked.txt").write_text("shared only\n")
    binding = bind(repository)
    worktree = Path(binding["worktree_path"])
    assert worktree.parent == repository.parent
    assert binding["starting_branch"] == "research"
    assert (worktree / "notes.txt").read_text() == "initial\n"
    assert not (worktree / "untracked.txt").exists()
    (worktree / "notes.txt").write_text("chat edit\n")
    assert (repository / "notes.txt").read_text() == "shared dirty\n"
    state = run("create", binding=binding)
    assert state["dirty_worktree"] == [" M notes.txt"]
    assert state["default_branch"] == "release"
    assert state["shared_branch"] == "research"
    assert run("inspect", binding=binding) == state
    other = bind(repository, "chat-2")
    assert other["worktree_path"] != str(worktree)
    assert (Path(other["worktree_path"]) / "notes.txt").read_text() == "initial\n"
    with pytest.raises(ValueError, match="without a binding"):
        run("plan", shared_path=str(repository), chat_id="chat-1")


def test_preflight_preserves_changes_and_allows_pr_with_dirty_destination(repository: Path) -> None:
    binding = bind(repository)
    worktree = Path(binding["worktree_path"])
    (worktree / "new.txt").write_text("uncommitted\n")
    for target in (None, "research", "release"):
        with pytest.raises(
            ValueError,
            match="Worktree has uncommitted changes.*",
        ):
            run("preflight", binding=binding, target_branch=target)
    with pytest.raises(ValueError, match="Worktree has uncommitted changes"):
        run("remove", binding=binding)
    assert (worktree / "new.txt").read_text() == "uncommitted\n"
    git(worktree, "add", "new.txt")
    git(worktree, "commit", "-m", "Chat edit")
    (repository / "untracked.txt").write_text("shared edit\n")
    assert run("preflight", binding=binding)["dirty_shared"] == ["?? untracked.txt"]
    for target in ("research", "release"):
        with pytest.raises(ValueError, match="Shared checkout has uncommitted changes"):
            run("preflight", binding=binding, target_branch=target)
    git(repository, "add", "untracked.txt")
    git(repository, "commit", "-m", "Shared edit")
    assert run("preflight", binding=binding, target_branch="research")["target_checked_out"]
    assert not run("preflight", binding=binding, target_branch="release")["target_checked_out"]
    with pytest.raises(ValueError, match="target branch does not exist"):
        run("preflight", binding=binding, target_branch="missing")


def test_remove_keeps_unmerged_branch(repository: Path) -> None:
    binding = bind(repository)
    worktree = Path(binding["worktree_path"])
    (worktree / "notes.txt").write_text("committed chat\n")
    git(worktree, "add", "notes.txt")
    git(worktree, "commit", "-m", "Chat work")
    state = run("inspect", binding=binding)
    assert state["ahead_count"] == 1
    state = run("remove", binding=binding)
    assert state["removed"] is True
    assert not worktree.exists()
    assert git(repository, "show", f"{binding['branch']}:notes.txt") == "committed chat"
    with pytest.raises(ValueError, match="unavailable"):
        run("inspect", binding=binding)
    with pytest.raises(ValueError, match="checkout is missing"):
        run("create", binding=binding)


def test_missing_or_relocated_worktree_and_changed_branch_fail_closed(repository: Path) -> None:
    binding = bind(repository)
    worktree = Path(binding["worktree_path"])
    git(worktree, "checkout", "--detach")
    with pytest.raises(ValueError, match="checked-out branch changed"):
        run("inspect", binding=binding)
    git(worktree, "checkout", binding["branch"])
    relocated = worktree.with_name("relocated")
    git(repository, "worktree", "move", str(worktree), str(relocated))
    with pytest.raises(ValueError, match="unavailable"):
        run("inspect", binding=binding)
    with pytest.raises(ValueError, match="relocated"):
        run("inspect", binding={**binding, "worktree_path": str(relocated)})


def test_plan_refuses_detached_head_and_does_not_guess_default_branch(repository: Path) -> None:
    git(repository, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    binding = bind(repository)
    assert run("inspect", binding=binding)["default_branch"] is None
    git(repository, "checkout", "--detach")
    with pytest.raises(ValueError, match="detached HEAD"):
        run("plan", shared_path=str(repository), chat_id="detached")


def test_creation_uses_durable_commit_even_if_shared_head_moves(repository: Path) -> None:
    binding = run("plan", shared_path=str(repository), chat_id="planned")
    (repository / "later.txt").write_text("later\n")
    git(repository, "add", "later.txt")
    git(repository, "commit", "-m", "Moved after planning")
    run("create", binding=binding)
    assert git(Path(binding["worktree_path"]), "rev-parse", "HEAD") == binding["starting_commit"]
    assert not (Path(binding["worktree_path"]) / "later.txt").exists()


def test_shipped_source_has_same_behavior_without_package_imports(repository: Path) -> None:
    payload = {
        "operation": "plan",
        "shared_path": str(repository),
        "chat_id": "shipped",
        "timeout_seconds": 10,
    }
    result = subprocess.run(
        [
            "python3",
            "-I",
            "-c",
            Path(conversation_worktree.__file__).read_text(),
            json.dumps(payload),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == conversation_worktree.execute(payload)


def test_removal_reconciles_only_persisted_removal_intent(repository: Path) -> None:
    binding = bind(repository)
    run("remove", binding={**binding, "status": "removing"})
    assert run("remove", binding={**binding, "status": "removing"}) == {"removed": True}
    with pytest.raises(ValueError, match="unavailable"):
        run("remove", binding=binding)


def test_inspection_rejects_replacement_repository(repository: Path) -> None:
    binding = bind(repository)
    worktree = Path(binding["worktree_path"])
    git(repository, "worktree", "remove", str(worktree))
    worktree.mkdir()
    git(worktree, "init", "--initial-branch=imposter")
    with pytest.raises(ValueError, match="missing or its checked-out branch changed"):
        run("inspect", binding=binding)


def test_missing_starting_branch_does_not_hide_retained_work(repository: Path) -> None:
    binding = bind(repository)
    git(repository, "checkout", "release")
    git(repository, "branch", "-d", "research")
    result = run("inspect", binding=binding)
    assert result["starting_branch_exists"] is False
    assert result["ahead_count"] is None
    assert run("remove", binding=binding)["removed"] is True


def test_bound_common_git_directory_is_checked_before_creation_and_inspection(
    repository: Path,
) -> None:
    binding = run("plan", shared_path=str(repository), chat_id="identity")
    assert binding["git_common_dir"] == str(repository / ".git")
    wrong = {**binding, "git_common_dir": str(repository.parent / "different.git")}
    with pytest.raises(ValueError, match="Git metadata moved"):
        run("create", binding=wrong)
    assert not Path(binding["worktree_path"]).exists()
    run("create", binding=binding)
    with pytest.raises(ValueError, match="Git metadata moved"):
        run("inspect", binding=wrong)


def test_inspect_allows_only_explicit_integration_continuation_target(repository: Path) -> None:
    # release predates the binding's start; temporary target checkout is not the
    # chat branch's ancestry, which must still be verified independently.
    (repository / "later.txt").write_text("new starting commit\n")
    git(repository, "add", "later.txt")
    git(repository, "commit", "-m", "New start")
    binding = bind(repository)
    worktree = Path(binding["worktree_path"])
    git(worktree, "checkout", "release")
    with pytest.raises(ValueError, match="checked-out branch changed"):
        run("inspect", binding=binding)
    assert run("inspect", binding=binding, allowed_branch="release")["starting_branch_exists"]
    with pytest.raises(ValueError, match="checked-out branch changed"):
        run("inspect", binding=binding, allowed_branch="research")
    with pytest.raises(ValueError, match="checked-out branch changed"):
        run("preflight", binding=binding, allowed_branch="release", target_branch="release")
    with pytest.raises(ValueError, match="checked-out branch changed"):
        run("remove", binding=binding, allowed_branch="release")


def test_canonicalize_is_read_only_and_only_prospective_path_may_be_missing(
    repository: Path,
) -> None:
    prospective = repository.parent / "prospective"
    alias = repository.parent / "alias"
    alias.symlink_to(repository, target_is_directory=True)
    result = run(
        "canonicalize",
        paths=[str(alias)],
        shared_path=str(repository),
        prospective_worktree_path=str(prospective),
    )
    assert result["canonical"] == {
        str(alias): str(repository),
        str(repository): str(repository),
        str(prospective): str(prospective),
    }
    assert result["account_home"] == str(Path.home().resolve())
    assert not prospective.exists()
    with pytest.raises(ValueError, match="unavailable"):
        run("canonicalize", paths=[str(prospective)], shared_path=str(repository))
    with pytest.raises(ValueError, match="unavailable"):
        run(
            "canonicalize",
            paths=[],
            shared_path=str(repository),
            prospective_worktree_path=str(prospective / "nested"),
        )


def test_remote_branch_lookup_uses_local_bare_remote_and_reports_unavailability(
    repository: Path,
) -> None:
    binding = bind(repository)
    remote = repository.parent / "origin.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(repository, "remote", "add", "origin", str(remote))
    # A stale tracking ref cannot establish that the actual remote has a branch.
    git(repository, "update-ref", f"refs/remotes/origin/{binding['branch']}", binding["branch"])
    assert run("remote_branch", binding=binding) == {
        "remote_branch_exists": False,
        "remote_branch_reason": None,
    }
    git(repository, "push", "origin", binding["branch"])
    assert run("remote_branch", binding=binding) == {
        "remote_branch_exists": True,
        "remote_branch_reason": None,
    }
    git(repository, "remote", "set-url", "origin", str(repository.parent / "missing.git"))
    result = run("remote_branch", binding=binding)
    assert result["remote_branch_exists"] is None
    assert result["remote_branch_reason"]
