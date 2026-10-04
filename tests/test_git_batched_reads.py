from __future__ import annotations

from pathlib import Path

import pytest

from rcp.server_ops.project_checkout import ProjectCheckoutRefused

from .test_project_checkout import ALIAS, PROJECT_ID, REPOSITORY, _git_command, _manager, _origin


@pytest.mark.parametrize(
    "configuration,error",
    [
        ("[unrelated]\n value = " + "x" * (300 * 1024) + "\n", None),
        ('[REMOTE "origin"]\n URL = ' + REPOSITORY.ssh_clone_url + "\n", "origin does not match"),
        ("[Core]\n HooksPath = /dev/null\n hookspath = /dev/null\n", "repository hook path"),
        ('[Core]\n HooksPath = "/dev/null\\nextra"\n', "repository hook path"),
        ("[Core]\n HooksPath\n", "repository hook path"),
        (
            "[Include]\n path = /nonexistent\n[Core]\n HooksPath = /unsafe\n",
            "unsafe local Git execution",
        ),
        ('[Core "different"]\n HooksPath = /unsafe\n[Core]\n HooksPath = /dev/null\n', None),
    ],
)
def test_checkout_batched_config_preserves_values_case_and_refusal_order(
    tmp_path: Path, configuration: str, error: str | None
) -> None:
    origin, commit = _origin(tmp_path)
    manager, layout, machine, material, _runner = _manager(tmp_path, origin)
    checkout = layout.projects_root / PROJECT_ID / "repositories" / ALIAS
    checkout.parent.mkdir(parents=True)
    _git_command("clone", "--quiet", str(origin), str(checkout))
    _git_command("remote", "set-url", "origin", REPOSITORY.ssh_clone_url, cwd=checkout)
    config = checkout / ".git" / "config"
    with config.open("a") as stream:
        stream.write(configuration)
    before = config.read_bytes()

    def prepare():
        return manager.prepare(
            machine,
            material,
            request_kind="create_team_project",
            project_id=PROJECT_ID,
            repository_alias=ALIAS,
            state_repository=False,
            expected_commit=commit,
        )

    if error is None:
        assert prepare().commit == commit
    else:
        with pytest.raises(ProjectCheckoutRefused, match=error) as refusal:
            prepare()
        assert refusal.value.kind == "checkout_conflict"
        assert config.read_bytes() == before


@pytest.mark.parametrize("changed_branch", [False, True])
def test_batched_common_directory_failure_keeps_branch_error_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_branch: bool
) -> None:
    from rcp.transport import conversation_worktree

    from .test_conversation_worktree_git import bind, git, run

    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "Initial",
    )
    binding = bind(root)
    if changed_branch:
        git(Path(binding["worktree_path"]), "checkout", "-b", "changed")
    original = conversation_worktree.subprocess.run

    def common_directory_failure(arguments, **kwargs):
        result = original(arguments, **kwargs)
        if "--git-common-dir" in arguments:
            result.returncode = 1
            result.stdout = f"{arguments[arguments.index('-C') + 1]}\n"
            result.stderr = "common directory unavailable"
        return result

    monkeypatch.setattr(conversation_worktree.subprocess, "run", common_directory_failure)
    message = "checked-out branch changed" if changed_branch else "common directory unavailable"
    with pytest.raises(ValueError, match=message):
        run("inspect", binding=binding)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("state", ["clean", "shared_dirty", "worktree_dirty", "both_dirty"])
def test_batch_preflight_matches_single_target_results_and_error_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: bool, state: str
) -> None:
    import shlex
    import sys
    from types import SimpleNamespace

    from rcp import conversation_worktrees

    from .test_conversation_worktree_git import bind, git

    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "Initial",
    )
    git(root, "branch", "release")
    binding = bind(root)
    if state in {"shared_dirty", "both_dirty"}:
        (root / "shared.txt").write_text("shared edit\n")
    if state in {"worktree_dirty", "both_dirty"}:
        (Path(binding["worktree_path"]) / "worktree.txt").write_text("worktree edit\n")
    if remote:
        monkeypatch.setattr(
            conversation_worktrees,
            "ssh_arguments",
            lambda host, command: [sys.executable, "-I", *shlex.split(command)[1:]],
        )
    store = SimpleNamespace(space_kind="personal")
    host = "fixture-only" if remote else ""

    stored_binding = SimpleNamespace(model_dump=lambda **kwargs: binding)

    def command(operation, **values):
        return conversation_worktrees.worktree_command(
            store, host=host, operation=operation, binding=stored_binding, **values
        )

    targets = ["missing", "main", None, "release", "bad branch", None]
    outcomes = command("preflight_many", target_branches=targets)["results"]
    for target, outcome in zip(targets, outcomes, strict=True):
        try:
            expected = command("preflight", target_branch=target)
        except ValueError as exc:
            actual = conversation_worktrees._worktree_command_error(outcome, host=host)
            assert type(actual) is type(exc)
            assert str(actual) == str(exc)
        else:
            assert outcome == {"facts": expected}
