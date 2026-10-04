from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.transport import conversation_worktree


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(scope="module")
def repository_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("template") / "shared checkout"
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


@pytest.fixture
def repository(repository_template: Path, tmp_path: Path) -> Path:
    # The template holds no absolute paths, so a copy equals a fresh build.
    return Path(shutil.copytree(repository_template, tmp_path / "shared checkout", symlinks=True))


def run(operation: str, **kwargs: object) -> dict:
    return conversation_worktree.execute({"operation": operation, "timeout_seconds": 10, **kwargs})


def bind(repository: Path, chat_id: str = "chat-1") -> dict:
    binding = run("plan", shared_path=str(repository), chat_id=chat_id)
    run("create", binding=binding)
    return binding


@pytest.mark.parametrize("name", ["shared checkout", 'shared café\t"\\ checkout'])
@pytest.mark.parametrize("legacy_output", [False, True])
def test_worktree_lifecycle_without_null_terminated_list_support(
    repository: Path, monkeypatch, name: str, legacy_output: bool
) -> None:
    repository = repository.rename(repository.with_name(name))
    original_run = subprocess.run

    def older_git(arguments, **kwargs):
        if "worktree" in arguments and "list" in arguments:
            if "-z" in arguments:
                return subprocess.CompletedProcess(arguments, 129, "", "error: unknown switch 'z'")
            if legacy_output:
                # Git 2.34 prints raw paths; newer Git C-quotes special characters.
                result = original_run([*arguments, "-z"], **kwargs)
                result.stdout = result.stdout.replace("\0", "\n")
                return result
        return original_run(arguments, **kwargs)

    monkeypatch.setattr(conversation_worktree.subprocess, "run", older_git)
    binding = bind(repository)
    assert run("inspect", binding=binding)["shared_branch"] == "research"
    assert run("preflight", binding=binding, target_branch="research")["target_checked_out"]
    assert run("remove", binding=binding)["removed"]
    assert git(repository, "rev-parse", binding["branch"]) == binding["starting_commit"]


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


def test_registration_cannot_disguise_a_changed_checkout_branch(
    repository: Path, monkeypatch
) -> None:
    binding = bind(repository)
    registrations = conversation_worktree._registered(repository, timeout=10)
    git(Path(binding["worktree_path"]), "checkout", "release")
    monkeypatch.setattr(
        conversation_worktree, "_registered", lambda *_args, **_kwargs: registrations
    )
    with pytest.raises(ValueError, match="checked-out branch changed"):
        run("inspect", binding=binding)


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


def test_remote_worktree_loads_package_resource_without_module_source_path(
    repository: Path, monkeypatch
) -> None:
    from rcp import conversation_worktrees
    from rcp.transport.state import _remote_script

    # Frozen modules have importable code, but their __file__ is not a source
    # file. Exercise the actual remote command path through an owned local pipe.
    monkeypatch.setattr(conversation_worktree, "__file__", "/not-a-source-file/worktree.pyc")
    commands = []

    def local_pipe(host, command):
        assert host == "fixture-only"
        arguments = shlex.split(command)
        assert arguments[:2] == ["python3", "-c"]
        assert arguments[2] == _remote_script("conversation_worktree.py")
        commands.append(arguments)
        return [sys.executable, "-I", *arguments[1:]]

    monkeypatch.setattr(conversation_worktrees, "ssh_arguments", local_pipe)
    result = conversation_worktrees.worktree_command(
        SimpleNamespace(space_kind="personal"),
        host="fixture-only",
        operation="plan",
        shared_path=str(repository),
        chat_id="packaged-resource",
    )
    assert result["starting_branch"] == "research"
    assert len(commands) == 1


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
    assert run("preflight", binding=binding, allowed_branch="release", target_branch="release")[
        "starting_branch_exists"
    ]
    with pytest.raises(ValueError, match="exact admitted integration target"):
        run("preflight", binding=binding, allowed_branch="release", target_branch="research")
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


def test_episode_uses_shared_worktree_lifecycle_and_fails_closed(repository: Path) -> None:
    binding = run("plan", shared_path=str(repository), owner_episode_id="owner")
    assert binding["owner_episode_id"] == "owner"
    assert "chat_id" not in binding
    run("create", binding=binding)
    assert run("inspect", binding=binding)["ahead_count"] == 0
    worktree = Path(binding["worktree_path"])
    (worktree / "notes.txt").write_text("episode edit\n")
    assert (repository / "notes.txt").read_text() == "initial\n"
    assert run("create", binding=binding)["dirty_worktree"] == [" M notes.txt"]
    worktree.rename(worktree.with_name("moved"))
    with pytest.raises(ValueError):
        run("inspect", binding=binding)


@pytest.mark.parametrize("version,supported", [("2.37.9", False), ("2.38.0", True)])
def test_episode_git_version_probe_runs_on_execution_host(monkeypatch, version, supported) -> None:
    monkeypatch.setattr(conversation_worktree, "_GIT_VERSION", None)
    commands = []

    def probe(arguments, **kwargs):
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, f"git version {version}\n", "")

    monkeypatch.setattr(conversation_worktree.subprocess, "run", probe)
    assert run("git_version") == {
        "version": [int(part) for part in version.split(".")],
        "supported": supported,
    }
    assert commands == [["git", "--version"]]


def episode_binding(repository: Path) -> dict:
    binding = run("plan", shared_path=str(repository), owner_episode_id="merge-owner")
    run("create", binding=binding)
    return binding


def episode_edit(binding: dict, name: str = "episode.txt", text: str = "episode\n") -> Path:
    worktree = Path(binding["worktree_path"])
    (worktree / name).write_text(text)
    return worktree


def land_episode(binding: dict, target: str, mode: str = "merge") -> dict:
    run("commit_leftovers", binding=binding, target_branch=target)
    preview = run("merge_preview", binding=binding, target_branch=target)
    payload = {**preview, "binding": binding, "history_mode": mode}
    prepared = run("prepare_landing", **payload)
    assert run("land", **payload, **prepared) == prepared
    assert run("verify_landing", **payload, **prepared)["verified"]
    return {**payload, **prepared}


@pytest.mark.parametrize("target", ["research", "release"])
@pytest.mark.parametrize("mode", ["merge", "squash"])
def test_episode_merge_lands_verified_commit_and_retries_cleanup(repository, target, mode):
    binding = episode_binding(repository)
    episode_edit(binding)
    result = land_episode(binding, target, mode)
    assert git(repository, "rev-parse", target) == result["landed_commit"]
    assert len(git(repository, "show", "-s", "--format=%P", target).split()) == (
        2 if mode == "merge" else 1
    )
    assert (repository / "episode.txt").exists() == (target == "research")
    for _ in range(2):
        assert run("remove_episode_worktree", binding=binding)["removed"]
        assert run("delete_branch", **result)["deleted"]
    assert not Path(binding["worktree_path"]).exists()


def test_episode_landing_that_drops_target_history_is_unverified(repository):
    binding = episode_binding(repository)
    episode_edit(binding)
    (repository / "human.txt").write_text("human\n")
    git(repository, "add", "human.txt")
    git(repository, "commit", "-m", "Target moves after the episode starts")
    run("commit_leftovers", binding=binding, target_branch="release")
    git(repository, "branch", "-f", "release", "research")
    preview = run("merge_preview", binding=binding, target_branch="release")
    # A provider that resets the target to the source contains the source but drops the target.
    git(repository, "branch", "-f", "release", preview["source_commit"])
    payload = {"binding": binding, "target_branch": "release", **preview}
    assert run("verify_landing", **{**payload, "target_commit": None})["verified"]
    assert not run("verify_landing", **payload)["verified"]


def test_episode_merge_commits_without_an_account_git_identity(repository, monkeypatch):
    binding = episode_binding(repository)
    episode_edit(binding)
    git(repository, "config", "--unset", "user.name")
    git(repository, "config", "--unset", "user.email")
    git(repository, "config", "user.useConfigOnly", "true")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    result = land_episode(binding, "release")
    for commit in (result["landed_commit"], result["source_commit"]):
        assert git(repository, "show", "-s", "--format=%an %cn", commit) == "RCP RCP"


def test_episode_leftovers_respect_ignore_and_do_not_commit_shared_changes(repository):
    binding = episode_binding(repository)
    worktree = episode_edit(binding)
    (worktree / ".gitignore").write_text("ignored\n")
    (worktree / "ignored").write_text("private\n")
    (repository / "shared.txt").write_text("human\n")
    result = run("commit_leftovers", binding=binding, target_branch="release")
    assert set(result["leftover_files"]) == {"episode.txt", ".gitignore"}
    assert git(worktree, "rev-list", "--count", f"{binding['starting_commit']}..HEAD") == "1"
    assert git(worktree, "status", "--porcelain") == ""
    assert git(repository, "status", "--porcelain") == "?? shared.txt"
    assert (
        run("commit_leftovers", binding=binding, target_branch="release")["source_commit"]
        == result["source_commit"]
    )


@pytest.mark.parametrize(
    "rule,code",
    [
        ("missing", "target_missing"),
        ("same", "target_is_episode_branch"),
        ("other", "target_checked_out_elsewhere"),
        ("late_other", "target_checked_out_elsewhere"),
        ("dirty", "target_dirty"),
    ],
)
def test_episode_target_refusals_preserve_leftovers(repository, monkeypatch, rule, code):
    binding = episode_binding(repository)
    worktree = episode_edit(binding)
    target = "research"
    other = repository.parent / "other"
    if rule == "missing":
        target = "missing"
    elif rule == "same":
        target = binding["branch"]
    elif rule == "other":
        target = "release"
        git(repository, "worktree", "add", str(other), target)
    elif rule == "late_other":
        # Checked out only after validation listed the registrations.
        target = "release"
        interrupted = conversation_worktree._interrupted

        def check_out_target_late(root, timeout):
            interrupted(root, timeout)
            if not other.exists():
                git(repository, "worktree", "add", str(other), target)

        monkeypatch.setattr(conversation_worktree, "_interrupted", check_out_target_late)
    else:
        (repository / "human.txt").write_text("human\n")
    with pytest.raises(conversation_worktree.WorktreeValidationError) as exc:
        run("commit_leftovers", binding=binding, target_branch=target)
    assert exc.value.code == code
    assert git(worktree, "rev-parse", "HEAD") == binding["starting_commit"]


@pytest.mark.parametrize("marker", ["MERGE_HEAD", "rebase-merge"])
def test_episode_interrupted_git_is_not_leftovers(repository, marker):
    binding = episode_binding(repository)
    worktree = episode_edit(binding)
    marker_path = Path(git(worktree, "rev-parse", "--git-path", marker))
    if marker.startswith("rebase"):
        marker_path.mkdir()
    else:
        marker_path.write_text(binding["starting_commit"] + "\n")
    with pytest.raises(conversation_worktree.WorktreeValidationError) as exc:
        run("commit_leftovers", binding=binding)
    assert exc.value.code == "interrupted_git_state"
    assert git(worktree, "rev-parse", "HEAD") == binding["starting_commit"]


def test_episode_conflict_probe_moves_no_refs_or_checkout(repository):
    binding = episode_binding(repository)
    episode_edit(binding, "notes.txt", "episode\n")
    run("commit_leftovers", binding=binding)
    (repository / "notes.txt").write_text("human\n")
    git(repository, "commit", "-am", "Human work")
    before = git(repository, "rev-parse", "HEAD")
    preview = run("merge_preview", binding=binding)
    assert preview["status"] == "conflict"
    assert preview["conflict_files"] == ["notes.txt"]
    assert git(repository, "rev-parse", "HEAD") == before
    assert git(repository, "status", "--porcelain") == ""


@pytest.mark.parametrize("moved", ["source", "target"])
def test_episode_landing_refuses_moved_input(repository, moved):
    binding = episode_binding(repository)
    worktree = episode_edit(binding)
    run("commit_leftovers", binding=binding)
    preview = run("merge_preview", binding=binding)
    prepared = run("prepare_landing", binding=binding, **preview)
    root = worktree if moved == "source" else repository
    (root / "moved.txt").write_text("later\n")
    git(root, "add", "moved.txt")
    git(root, "commit", "-m", "Moved input")
    with pytest.raises(conversation_worktree.WorktreeValidationError) as exc:
        run("land", binding=binding, **preview, **prepared)
    assert exc.value.code == f"{moved}_changed"


def test_episode_branch_deletion_requires_delivery_and_removed_worktree(repository):
    binding = episode_binding(repository)
    episode_edit(binding)
    source = run("commit_leftovers", binding=binding)["source_commit"]
    with pytest.raises(conversation_worktree.WorktreeValidationError) as exc:
        run("delete_branch", binding=binding, source_commit=source)
    assert exc.value.code == "worktree_removal_required"
    run("remove_episode_worktree", binding=binding)
    with pytest.raises(conversation_worktree.WorktreeValidationError) as exc:
        run("delete_branch", binding=binding, source_commit=source)
    assert exc.value.code == "code_not_delivered"


def test_episode_old_git_refuses_merge_before_writes(repository, monkeypatch):
    binding = episode_binding(repository)
    worktree = episode_edit(binding)
    original = subprocess.run

    def old_git(arguments, **kwargs):
        if arguments == ["git", "--version"]:
            return subprocess.CompletedProcess(arguments, 0, "git version 2.37.0\n", "")
        return original(arguments, **kwargs)

    monkeypatch.setattr(conversation_worktree, "_GIT_VERSION", None)
    monkeypatch.setattr(conversation_worktree.subprocess, "run", old_git)
    with pytest.raises(conversation_worktree.WorktreeValidationError) as exc:
        run("commit_leftovers", binding=binding)
    assert exc.value.code == "git_version_unsupported"
    assert git(worktree, "rev-parse", "HEAD") == binding["starting_commit"]


def test_episode_landing_rechecks_checkout_identity(repository):
    binding = episode_binding(repository)
    episode_edit(binding)
    run("commit_leftovers", binding=binding)
    preview = run("merge_preview", binding=binding)
    prepared = run("prepare_landing", binding=binding, **preview)
    git(repository, "checkout", "release")
    with pytest.raises(conversation_worktree.WorktreeValidationError) as exc:
        run("land", binding=binding, **preview, **prepared)
    assert exc.value.code == "shared_checkout_changed"


def test_episode_dirty_submodule_cannot_be_committed_as_leftovers(repository):
    submodule = repository.parent / "submodule"
    submodule.mkdir()
    git(submodule, "init", "--initial-branch=main")
    git(submodule, "config", "user.email", "fixture@example.invalid")
    git(submodule, "config", "user.name", "Fixture")
    (submodule / "file").write_text("initial\n")
    git(submodule, "add", ".")
    git(submodule, "commit", "-m", "Initial")
    binding = episode_binding(repository)
    worktree = Path(binding["worktree_path"])
    git(worktree, "-c", "protocol.file.allow=always", "submodule", "add", str(submodule), "sub")
    git(worktree, "commit", "-am", "Submodule")
    (worktree / "sub" / "file").write_text("dirty\n")
    with pytest.raises(conversation_worktree.WorktreeValidationError) as exc:
        run("commit_leftovers", binding=binding)
    assert exc.value.code == "interrupted_git_state"


def test_episode_unmerged_index_without_operation_marker_refuses_leftovers(repository):
    binding = episode_binding(repository)
    worktree = episode_edit(binding, "notes.txt", "episode\n")
    run("commit_leftovers", binding=binding)
    (repository / "notes.txt").write_text("human\n")
    git(repository, "commit", "-am", "Human")
    result = subprocess.run(["git", "-C", str(worktree), "merge", "research"], capture_output=True)
    assert result.returncode == 1
    Path(git(worktree, "rev-parse", "--git-path", "MERGE_HEAD")).unlink()
    with pytest.raises(conversation_worktree.WorktreeValidationError) as exc:
        run("commit_leftovers", binding=binding)
    assert exc.value.code == "interrupted_git_state"


def test_episode_leftovers_with_reverted_staging_need_no_empty_commit(repository):
    binding = episode_binding(repository)
    worktree = episode_edit(binding, "notes.txt", "staged\n")
    git(worktree, "add", "notes.txt")
    (worktree / "notes.txt").write_text("initial\n")
    result = run("commit_leftovers", binding=binding)
    assert result["source_commit"] == binding["starting_commit"]
    assert git(worktree, "status", "--porcelain") == ""


def test_episode_removal_refuses_code_added_after_delivery(repository):
    binding = episode_binding(repository)
    worktree = episode_edit(binding)
    landed = land_episode(binding, "research")
    episode_edit(binding, "later.txt", "later\n")
    run("commit_leftovers", binding=binding)
    with pytest.raises(conversation_worktree.WorktreeValidationError) as exc:
        run("remove_episode_worktree", binding=binding, source_commit=landed["source_commit"])
    assert exc.value.code == "source_changed"
    assert worktree.exists()
