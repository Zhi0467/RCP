from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.server_ops import remote_project_checkout
from rcp.server_ops.git_credentials import DeployKeyMaterial, _run_process
from rcp.server_ops.github import GitHubRepositoryRef
from rcp.server_ops.layout import ServerLayout
from rcp.server_ops.project_checkout import (
    ProjectCheckoutManager,
    ProjectCheckoutRefused,
    retained_research_operator_step,
)
from rcp.storage import ProjectProvisioningMachineIntent

SPACE_ID = "7eb4ea9d-cccf-42fd-abfe-09f71f4b8cd2"
PROJECT_ID = "2ad064a6-f015-4703-a223-1d64cde75cc8"
REQUEST_ID = "a29ddba0-a0a7-46be-ab7a-7a6d77644ea5"
ALIAS = "paper"
REPOSITORY = GitHubRepositoryRef(identity="zhi0467/rcp-checkout-live-test")
HELPER = Path(__file__).parents[1] / "src" / "rcp" / "server_ops" / "remote_project_checkout.py"


def _layout(tmp_path: Path) -> ServerLayout:
    account = pwd.getpwuid(os.getuid())
    root = tmp_path / "rcp-server"
    return ServerLayout(
        service_account=account.pw_name,
        service_home=Path(account.pw_dir),
        server_root=root,
        source_checkout=root / "source",
        releases_root=root / "releases",
        data_dir=root / "data",
        projects_root=root / "projects",
        credentials_root=root / "credentials",
        update_checkpoints_root=root / "update-checkpoints",
        restore_operations_root=root / "restore-operations",
        codex_state_root=Path(account.pw_dir) / ".codex",
        claude_state_root=Path(account.pw_dir) / ".claude",
        ssh_state_root=Path(account.pw_dir) / ".ssh",
        config_path=tmp_path / "etc" / "rcp" / "server.toml",
        current_release=tmp_path / "etc" / "rcp" / "current",
        runtime_dir=tmp_path / "run" / "rcp",
        control_socket=tmp_path / "run" / "rcp" / "control.sock",
        cli_wrapper=tmp_path / "usr" / "local" / "bin" / "rcp",
        systemd_unit=tmp_path / "etc" / "systemd" / "system" / "rcp.service",
        service_unit_name="rcp.service",
    )


def _machine(layout: ServerLayout) -> ProjectProvisioningMachineIntent:
    return ProjectProvisioningMachineIntent.model_construct(
        alias="server",
        location="local",
        host="",
        os_account=layout.service_account,
        central_root=str(layout.projects_root),
    )


def _material(layout: ServerLayout) -> DeployKeyMaterial:
    return DeployKeyMaterial(
        space_id=SPACE_ID,
        project_id=PROJECT_ID,
        repository_alias=ALIAS,
        repository=REPOSITORY,
        machine_alias="server",
        location="local",
        host="",
        os_account=layout.service_account,
        central_root=str(layout.projects_root),
        account_home=str(layout.service_home),
        credentials_root=str(layout.credentials_root),
        private_key_path=str(layout.project_deploy_key_path(PROJECT_ID, ALIAS)),
        label=f"rcp:{SPACE_ID}:{PROJECT_ID}:{ALIAS}",
        public_key="ssh-ed25519 AAAA fixture",
        public_key_fingerprint="SHA256:" + ("A" * 43),
        created=False,
    )


class _CurrentAccountRunner:
    def __init__(self, account: str) -> None:
        self.account = account
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        expected = ("runuser", "--user", self.account, "--")
        assert argv[:4] == expected
        self.calls.append(argv)
        return _run_process(tuple(argv[4:]), timeout=timeout)


class _StaticCredentialManager:
    def inspect_key(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial,
    ) -> DeployKeyMaterial:
        assert machine.alias == material.machine_alias
        return material


class _LocalOriginCheckoutManager(ProjectCheckoutManager):
    """Exercise production checkout logic while replacing only GitHub I/O with a bare repo."""

    def __init__(self, *args: object, origin: Path, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.origin = origin

    def _clone(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial,
        repository_path: str,
    ) -> None:
        cloned = self._git(
            machine,
            material,
            (
                "git",
                "clone",
                "--no-tags",
                "--no-recurse-submodules",
                "--origin",
                "origin",
                "--template=",
                "--config",
                "core.hooksPath=/dev/null",
                "--",
                str(self.origin),
                repository_path,
            ),
        )
        assert cloned.returncode == 0, cloned.stderr
        configured = self._git_at(
            machine,
            material,
            repository_path,
            ("remote", "set-url", "origin", material.repository.ssh_clone_url),
        )
        assert configured.returncode == 0, configured.stderr

    def _git(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial,
        argv: tuple[str, ...],
    ) -> subprocess.CompletedProcess[str]:
        mapped = list(argv)
        if len(mapped) >= 2 and mapped[-2:] == ["origin", "HEAD"]:
            mapped[-2] = str(self.origin)
        elif len(mapped) >= 1 and mapped[-1] == "origin" and "fetch" in mapped:
            mapped[-1] = str(self.origin)
        return super()._git(machine, material, tuple(mapped))


def _helper(operation: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, str(HELPER), operation, *arguments),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _prepare_helper(root: Path) -> subprocess.CompletedProcess[str]:
    account = pwd.getpwuid(os.getuid())
    return _helper(
        "prepare",
        account.pw_name,
        account.pw_dir,
        "local",
        str(root),
        PROJECT_ID,
        ALIAS,
    )


def _git_command(*argv: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ("git", *argv),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _origin(tmp_path: Path, *, retained: bool = False) -> tuple[Path, str]:
    source = tmp_path / "source"
    bare = tmp_path / "origin.git"
    source.mkdir()
    _git_command("init", "--quiet", cwd=source)
    _git_command("config", "user.name", "RCP test", cwd=source)
    _git_command("config", "user.email", "rcp@example.invalid", cwd=source)
    (source / "README.md").write_text("central checkout fixture\n", encoding="utf-8")
    if retained:
        patches = source / ".research" / "patches"
        patches.mkdir(parents=True)
        (patches / "000001.json").write_text(
            json.dumps(
                {
                    "kind": "identity",
                    "project_identity": {
                        "project_id": "651f8a95-c12d-46ef-9ac2-df13e9c96ee2",
                        "home_space_id": "2f8dfa3b-d91e-4d5e-a622-6e35395bdfe7",
                    },
                }
            ),
            encoding="utf-8",
        )
    _git_command("add", ".", cwd=source)
    _git_command("commit", "--quiet", "-m", "fixture", cwd=source)
    commit = _git_command("rev-parse", "HEAD", cwd=source)
    _git_command("init", "--quiet", "--bare", str(bare))
    _git_command("remote", "add", "origin", str(bare), cwd=source)
    _git_command("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=source)
    _git_command("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)
    return bare, commit


def _manager(
    tmp_path: Path, origin: Path
) -> tuple[
    _LocalOriginCheckoutManager,
    ServerLayout,
    ProjectProvisioningMachineIntent,
    DeployKeyMaterial,
    _CurrentAccountRunner,
]:
    layout = _layout(tmp_path)
    layout.projects_root.mkdir(parents=True, mode=0o700)
    layout.projects_root.chmod(0o700)
    runner = _CurrentAccountRunner(layout.service_account)
    machine = _machine(layout)
    material = _material(layout)
    manager = _LocalOriginCheckoutManager(
        layout,
        runner=runner,
        credential_manager=_StaticCredentialManager(),  # type: ignore[arg-type]
        origin=origin,
    )
    return manager, layout, machine, material, runner


def test_shipped_helper_creates_and_reuses_only_the_exact_checkout(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir(mode=0o700)

    first = _prepare_helper(root)
    assert first.returncode == 0, first.stderr
    receipt = json.loads(first.stdout)
    checkout = root / PROJECT_ID / "repositories" / ALIAS
    assert receipt == {
        "account": pwd.getpwuid(os.getuid()).pw_name,
        "home": pwd.getpwuid(os.getuid()).pw_dir,
        "central_root": str(root),
        "repository_path": str(checkout),
        "disposition": "request_created",
        "empty": True,
    }
    assert checkout.is_dir()

    sentinel = checkout / "keep-me"
    sentinel.write_text("preserve", encoding="utf-8")
    second = _prepare_helper(root)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout) == {
        **receipt,
        "disposition": "reused_existing",
        "empty": False,
    }
    assert sentinel.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("unsafe", ["symlink", "mode"])
def test_shipped_helper_refuses_unsafe_central_root(tmp_path: Path, unsafe: str) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    root = tmp_path / "projects"
    if unsafe == "symlink":
        root.symlink_to(actual, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        root.chmod(0o777)

    refused = _prepare_helper(root)

    assert refused.returncode == 2
    assert not (actual / PROJECT_ID).exists()


def test_manager_refuses_a_symlinked_git_directory_before_running_git(tmp_path: Path) -> None:
    origin, commit = _origin(tmp_path)
    manager, layout, machine, material, _runner = _manager(tmp_path, origin)
    actual = tmp_path / "actual-checkout"
    _git_command("clone", "--quiet", str(origin), str(actual))
    checkout = layout.projects_root / PROJECT_ID / "repositories" / ALIAS
    checkout.mkdir(parents=True)
    (checkout / "README.md").write_text("do not follow .git\n", encoding="utf-8")
    (checkout / ".git").symlink_to(actual / ".git", target_is_directory=True)

    with pytest.raises(ProjectCheckoutRefused, match="checkout helper refused") as error:
        manager.prepare(
            machine,
            material,
            request_kind="create_team_project",
            project_id=PROJECT_ID,
            repository_alias=ALIAS,
            state_repository=False,
            expected_commit=commit,
        )

    assert error.value.checkout_disposition == "reused_existing"
    assert (checkout / ".git").is_symlink()


def test_shipped_helper_resolves_default_remote_root_from_verified_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "remote-home"
    home.mkdir(mode=0o700)
    account = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setattr(
        remote_project_checkout.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_name=account, pw_dir=str(home), pw_uid=os.getuid()),
    )

    receipt = remote_project_checkout._prepare(
        account,
        str(home),
        "ssh",
        "-",
        PROJECT_ID,
        ALIAS,
    )

    expected_root = home / ".local" / "share" / "rcp" / "projects"
    assert receipt["central_root"] == str(expected_root)
    assert receipt["repository_path"] == str(expected_root / PROJECT_ID / "repositories" / ALIAS)


def test_retained_patch_scan_has_one_cumulative_entry_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patches = tmp_path / "patches"
    for batch in ("batch-a", "batch-b"):
        directory = patches / batch
        directory.mkdir(parents=True)
        (directory / "000001.json").write_text("{}", encoding="utf-8")
        (directory / "000002.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(remote_project_checkout, "MAX_RESEARCH_ENTRIES", 4)
    descriptor = os.open(patches, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert remote_project_checkout._patch_names(descriptor) == ["too-many"]
    finally:
        os.close(descriptor)


def test_manager_clones_verifies_and_recovers_without_renaming(tmp_path: Path) -> None:
    origin, commit = _origin(tmp_path)
    manager, layout, machine, material, runner = _manager(tmp_path, origin)

    first = manager.prepare(
        machine,
        material,
        request_kind="create_team_project",
        project_id=PROJECT_ID,
        repository_alias=ALIAS,
        state_repository=True,
        expected_commit=commit,
    )
    second = manager.prepare(
        machine,
        material,
        request_kind="create_team_project",
        project_id=PROJECT_ID,
        repository_alias=ALIAS,
        state_repository=True,
        expected_commit=commit,
    )

    expected_path = layout.projects_root / PROJECT_ID / "repositories" / ALIAS
    assert first.repository_path == str(expected_path)
    assert first.checkout_disposition == "request_created"
    assert first.commit == commit
    assert first.retained_research.retained is False
    assert second.checkout_disposition == "reused_existing"
    assert _git_command("config", "--local", "--get", "remote.origin.url", cwd=expected_path) == (
        REPOSITORY.ssh_clone_url
    )
    assert _git_command("config", "--local", "--get", "core.hooksPath", cwd=expected_path) == (
        "/dev/null"
    )
    assert runner.calls
    assert all(
        call[:4] == ("runuser", "--user", layout.service_account, "--") for call in runner.calls
    )
    git_calls = [call for call in runner.calls if "GIT_CONFIG_GLOBAL=/dev/null" in call]
    assert git_calls
    assert all("GIT_TERMINAL_PROMPT=0" in call for call in git_calls)
    assert all(
        not any(token in {"reset", "clean", "stash"} for token in call) for call in git_calls
    )


def test_manager_refuses_wrong_origin_without_rewriting_it(tmp_path: Path) -> None:
    origin, commit = _origin(tmp_path)
    manager, layout, machine, material, _runner = _manager(tmp_path, origin)
    checkout = layout.projects_root / PROJECT_ID / "repositories" / ALIAS
    checkout.parent.mkdir(parents=True)
    _git_command("clone", "--quiet", str(origin), str(checkout))
    wrong = "git@github.com:someone/else.git"
    _git_command("remote", "set-url", "origin", wrong, cwd=checkout)

    with pytest.raises(ProjectCheckoutRefused, match="canonical GitHub repository") as error:
        manager.prepare(
            machine,
            material,
            request_kind="create_team_project",
            project_id=PROJECT_ID,
            repository_alias=ALIAS,
            state_repository=False,
            expected_commit=commit,
        )

    assert error.value.kind == "checkout_conflict"
    assert error.value.checkout_disposition == "reused_existing"
    assert _git_command("config", "--local", "--get", "remote.origin.url", cwd=checkout) == wrong


def test_manager_refuses_local_git_execution_and_url_overrides(tmp_path: Path) -> None:
    origin, commit = _origin(tmp_path)
    manager, _layout_value, machine, material, _runner = _manager(tmp_path, origin)
    prepared = manager.prepare(
        machine,
        material,
        request_kind="create_team_project",
        project_id=PROJECT_ID,
        repository_alias=ALIAS,
        state_repository=False,
        expected_commit=commit,
    )
    checkout = Path(prepared.repository_path)
    _git_command(
        "config",
        "url.file:///tmp/not-the-reviewed-origin.insteadOf",
        REPOSITORY.ssh_clone_url,
        cwd=checkout,
    )

    with pytest.raises(ProjectCheckoutRefused, match="unsafe local Git"):
        manager.prepare(
            machine,
            material,
            request_kind="create_team_project",
            project_id=PROJECT_ID,
            repository_alias=ALIAS,
            state_repository=False,
            expected_commit=commit,
        )


def test_manager_refuses_an_origin_fetch_mapping_that_can_rewrite_local_branches(
    tmp_path: Path,
) -> None:
    origin, commit = _origin(tmp_path)
    manager, layout, machine, material, _runner = _manager(tmp_path, origin)
    checkout = layout.projects_root / PROJECT_ID / "repositories" / ALIAS
    checkout.parent.mkdir(parents=True)
    _git_command("clone", "--quiet", str(origin), str(checkout))
    _git_command("remote", "set-url", "origin", REPOSITORY.ssh_clone_url, cwd=checkout)
    unsafe_refspec = "+refs/heads/*:refs/heads/*"
    _git_command("config", "remote.origin.fetch", unsafe_refspec, cwd=checkout)

    with pytest.raises(ProjectCheckoutRefused, match="unsafe origin fetch mapping"):
        manager.prepare(
            machine,
            material,
            request_kind="create_team_project",
            project_id=PROJECT_ID,
            repository_alias=ALIAS,
            state_repository=False,
            expected_commit=commit,
        )

    assert _git_command("config", "--local", "--get", "remote.origin.fetch", cwd=checkout) == (
        unsafe_refspec
    )


def test_manager_preserves_dirty_and_divergent_existing_checkout(tmp_path: Path) -> None:
    origin, commit = _origin(tmp_path)
    manager, layout, machine, material, _runner = _manager(tmp_path, origin)
    prepared = manager.prepare(
        machine,
        material,
        request_kind="create_team_project",
        project_id=PROJECT_ID,
        repository_alias=ALIAS,
        state_repository=False,
        expected_commit=commit,
    )
    checkout = Path(prepared.repository_path)
    dirty = checkout / "untracked.txt"
    dirty.write_text("do not remove", encoding="utf-8")

    with pytest.raises(ProjectCheckoutRefused, match="uncommitted or untracked"):
        manager.prepare(
            machine,
            material,
            request_kind="create_team_project",
            project_id=PROJECT_ID,
            repository_alias=ALIAS,
            state_repository=False,
            expected_commit=commit,
        )
    assert dirty.read_text(encoding="utf-8") == "do not remove"

    dirty.unlink()
    _git_command("config", "user.name", "RCP test", cwd=checkout)
    _git_command("config", "user.email", "rcp@example.invalid", cwd=checkout)
    (checkout / "local-only.txt").write_text("preserve commit\n", encoding="utf-8")
    _git_command("add", "local-only.txt", cwd=checkout)
    _git_command("commit", "--quiet", "-m", "local only", cwd=checkout)
    local_commit = _git_command("rev-parse", "HEAD", cwd=checkout)

    with pytest.raises(ProjectCheckoutRefused, match="differ"):
        manager.prepare(
            machine,
            material,
            request_kind="create_team_project",
            project_id=PROJECT_ID,
            repository_alias=ALIAS,
            state_repository=False,
            expected_commit=commit,
        )
    assert _git_command("rev-parse", "HEAD", cwd=checkout) == local_commit


def test_manager_does_not_rewrite_hooks_before_refusing_existing_dirty_work(
    tmp_path: Path,
) -> None:
    origin, commit = _origin(tmp_path)
    manager, layout, machine, material, _runner = _manager(tmp_path, origin)
    checkout = layout.projects_root / PROJECT_ID / "repositories" / ALIAS
    checkout.parent.mkdir(parents=True)
    _git_command("clone", "--quiet", str(origin), str(checkout))
    _git_command("remote", "set-url", "origin", REPOSITORY.ssh_clone_url, cwd=checkout)
    dirty = checkout / "preserve.txt"
    dirty.write_text("do not rewrite config\n", encoding="utf-8")

    with pytest.raises(ProjectCheckoutRefused, match="uncommitted or untracked"):
        manager.prepare(
            machine,
            material,
            request_kind="create_team_project",
            project_id=PROJECT_ID,
            repository_alias=ALIAS,
            state_repository=False,
            expected_commit=commit,
        )

    hooks = subprocess.run(
        ("git", "config", "--local", "--get-all", "core.hooksPath"),
        cwd=checkout,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert hooks.returncode == 1
    assert hooks.stdout == ""
    assert dirty.read_text(encoding="utf-8") == "do not rewrite config\n"


def test_manager_refuses_existing_hook_path_without_rewriting_it(tmp_path: Path) -> None:
    origin, commit = _origin(tmp_path)
    manager, layout, machine, material, _runner = _manager(tmp_path, origin)
    checkout = layout.projects_root / PROJECT_ID / "repositories" / ALIAS
    checkout.parent.mkdir(parents=True)
    _git_command("clone", "--quiet", str(origin), str(checkout))
    _git_command("remote", "set-url", "origin", REPOSITORY.ssh_clone_url, cwd=checkout)
    _git_command("config", "core.hooksPath", "/tmp/operator-hooks", cwd=checkout)

    with pytest.raises(ProjectCheckoutRefused, match="unsafe repository hook path"):
        manager.prepare(
            machine,
            material,
            request_kind="create_team_project",
            project_id=PROJECT_ID,
            repository_alias=ALIAS,
            state_repository=False,
            expected_commit=commit,
        )

    assert _git_command("config", "--local", "--get", "core.hooksPath", cwd=checkout) == (
        "/tmp/operator-hooks"
    )


def test_direct_creation_stops_on_retained_research_but_transfer_reports_it(
    tmp_path: Path,
) -> None:
    origin, commit = _origin(tmp_path, retained=True)
    manager, _layout_value, machine, material, _runner = _manager(tmp_path, origin)

    with pytest.raises(ProjectCheckoutRefused, match="Move to team space") as error:
        manager.prepare(
            machine,
            material,
            request_kind="create_team_project",
            project_id=PROJECT_ID,
            repository_alias=ALIAS,
            state_repository=True,
            expected_commit=commit,
        )

    refusal = error.value
    assert refusal.kind == "retained_research"
    assert refusal.checkout_disposition == "request_created"
    assert refusal.retained_research is not None
    assert refusal.retained_research.patch_history is True
    assert refusal.retained_research.project_id == "651f8a95-c12d-46ef-9ac2-df13e9c96ee2"
    step = retained_research_operator_step(
        machine,
        refusal,
        number=4,
        request_id=REQUEST_ID,
        resume_argv=("rcp", "server", "project", "provision", REQUEST_ID),
        local_host="team-server",
    )
    assert step.state == "operator_action_needed"
    assert step.target.kind == "machine"
    assert step.fields[0].value == refusal.repository_path
    assert "Move to team space" in step.actions[0].instruction

    transferred = manager.prepare(
        machine,
        material,
        request_kind="incoming_transfer",
        project_id=PROJECT_ID,
        repository_alias=ALIAS,
        state_repository=True,
        expected_commit=commit,
    )
    assert transferred.checkout_disposition == "reused_existing"
    assert transferred.retained_research.patch_history is True


def test_reused_personal_research_is_refused_before_git_config_changes(tmp_path: Path) -> None:
    origin, commit = _origin(tmp_path, retained=True)
    manager, layout, machine, material, _runner = _manager(tmp_path, origin)
    checkout = layout.projects_root / PROJECT_ID / "repositories" / ALIAS
    checkout.parent.mkdir(parents=True)
    _git_command("clone", "--quiet", str(origin), str(checkout))
    _git_command("remote", "set-url", "origin", REPOSITORY.ssh_clone_url, cwd=checkout)

    with pytest.raises(ProjectCheckoutRefused, match="Move to team space") as error:
        manager.prepare(
            machine,
            material,
            request_kind="create_team_project",
            project_id=PROJECT_ID,
            repository_alias=ALIAS,
            state_repository=True,
            expected_commit=commit,
        )

    hooks = subprocess.run(
        ("git", "config", "--local", "--get-all", "core.hooksPath"),
        cwd=checkout,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert error.value.checkout_disposition == "reused_existing"
    assert hooks.returncode == 1
    assert hooks.stdout == ""
