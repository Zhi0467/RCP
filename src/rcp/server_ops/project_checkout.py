"""Prepare one exact server-managed Git checkout for project provisioning."""

from __future__ import annotations

import importlib.resources
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Literal, cast

from rcp.limits import SERVER_PROJECT_CHECKOUT_TIMEOUT_SECONDS
from rcp.server_ops._local_primitives import canonical_json_text
from rcp.server_ops.backup_models import (
    BACKUP_MATERIALIZED_NAMES,
    BACKUP_RESEARCH_CANONICAL_ROOTS,
    BACKUP_RESEARCH_DELEGATED_ROOTS,
    BACKUP_RESEARCH_EXCLUSIONS,
)
from rcp.server_ops.git_credentials import (
    CommandRunner,
    DeployKeyMaterial,
    GitCredentialManager,
    GitCredentialRefused,
    GitWriteProbe,
    _parse_remote_refs,
    _probe_failure,
    deploy_key_ssh_command,
    run_bounded_process,
    target_account_argv,
)
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT, ServerLayout, remote_projects_root
from rcp.server_ops.models import (
    OPERATOR_SHELL,
    ExternalAction,
    MachineTarget,
    NonsecretField,
    ServerStep,
    canonical_uuid4,
)
from rcp.storage import (
    ProjectProvisioningCheckoutDisposition,
    ProjectProvisioningKind,
    ProjectProvisioningMachineIntent,
)

_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_MAX_OUTPUT_BYTES = 256 * 1024
_MAX_HELPER_OUTPUT_BYTES = 64 * 1024
_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

ProjectCheckoutFailureKind = Literal[
    "account_or_path",
    "git_access",
    "checkout_conflict",
    "retained_research",
]


@dataclass(frozen=True)
class RetainedResearchState:
    retained: bool
    patch_history: bool
    project_id: str | None
    home_space_id: str | None


@dataclass(frozen=True)
class ProjectCheckoutResult:
    machine_alias: str
    repository_alias: str
    central_root: str
    repository_path: str
    checkout_disposition: ProjectProvisioningCheckoutDisposition
    commit: str
    retained_research: RetainedResearchState


class ProjectCheckoutRefused(RuntimeError):
    """Checkout preparation stopped without rewriting or deleting repository state."""

    def __init__(
        self,
        kind: ProjectCheckoutFailureKind,
        message: str,
        *,
        central_root: str | None = None,
        repository_path: str | None = None,
        checkout_disposition: ProjectProvisioningCheckoutDisposition | None = None,
        retained_research: RetainedResearchState | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.central_root = central_root
        self.repository_path = repository_path
        self.checkout_disposition = checkout_disposition
        self.retained_research = retained_research


@dataclass(frozen=True)
class CheckoutAccount:
    account_home: str
    os_account: str
    central_root: str


@dataclass(frozen=True)
class _PathReceipt:
    central_root: str
    repository_path: str
    checkout_disposition: ProjectProvisioningCheckoutDisposition
    empty: bool


class ProjectCheckoutManager:
    """Clone or verify one checkout through the same exact-account transport as P3."""

    def __init__(
        self,
        layout: ServerLayout = DEFAULT_SERVER_LAYOUT,
        *,
        runner: CommandRunner | None = None,
        credential_manager: GitCredentialManager | None = None,
    ) -> None:
        self.layout = layout
        self._runner = runner or run_bounded_process
        self._credential_manager = credential_manager or GitCredentialManager(
            layout,
            runner=self._runner,
        )

    def prepare(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial,
        *,
        request_kind: ProjectProvisioningKind,
        project_id: str,
        repository_alias: str,
        state_repository: bool,
        expected_commit: str | None = None,
    ) -> ProjectCheckoutResult:
        if request_kind not in {"create_team_project", "incoming_transfer", "add_repository"}:
            raise ValueError("project checkout request kind is invalid")
        if not isinstance(state_repository, bool):
            raise ValueError("state-repository selection must be explicit")
        if (
            project_id != material.project_id
            or repository_alias != material.repository_alias
            or material.machine_alias != machine.alias
        ):
            raise ProjectCheckoutRefused(
                "account_or_path",
                "The deploy key and checkout request name different machine or project targets.",
            )
        if expected_commit is not None and _FULL_COMMIT.fullmatch(expected_commit) is None:
            raise ValueError("expected checkout commit must be one full lowercase Git object id")
        try:
            observed_material = self._credential_manager.inspect_key(machine, material)
        except GitCredentialRefused as exc:
            raise ProjectCheckoutRefused(
                "git_access",
                "The exact repository deploy key could not be revalidated on its execution account.",
            ) from exc

        receipt = self._prepare_path(
            machine,
            observed_material,
            project_id=project_id,
            repository_alias=repository_alias,
        )
        try:
            if receipt.empty:
                self._clone(machine, observed_material, receipt.repository_path)
                self._seal_new_git_directory(
                    machine,
                    observed_material,
                    receipt.repository_path,
                )
            self._verify_git_directory(
                machine,
                observed_material,
                receipt.repository_path,
            )
            retained = (
                self._retained_research(machine, observed_material, receipt.repository_path)
                if state_repository
                else RetainedResearchState(False, False, None, None)
            )
            if request_kind == "create_team_project" and retained.retained:
                raise ProjectCheckoutRefused(
                    "retained_research",
                    (
                        "The state repository already contains retained RCP research. If this is "
                        "a personal project, use Move to team space; otherwise clean or choose the "
                        "repository outside this provisioning request, then resume."
                    ),
                    retained_research=retained,
                )
            commit = self._verify_repository(
                machine,
                observed_material,
                receipt.repository_path,
                expected_commit=expected_commit,
            )
        except ProjectCheckoutRefused as exc:
            raise ProjectCheckoutRefused(
                exc.kind,
                str(exc),
                central_root=receipt.central_root,
                repository_path=receipt.repository_path,
                checkout_disposition=receipt.checkout_disposition,
                retained_research=exc.retained_research,
            ) from exc
        return ProjectCheckoutResult(
            machine_alias=machine.alias,
            repository_alias=repository_alias,
            central_root=receipt.central_root,
            repository_path=receipt.repository_path,
            checkout_disposition=receipt.checkout_disposition,
            commit=commit,
            retained_research=retained,
        )

    def prepare_server_only(
        self,
        machine: ProjectProvisioningMachineIntent,
        *,
        request_kind: ProjectProvisioningKind,
        project_id: str,
        repository_alias: str,
        state_repository: bool,
        expected_commit: str | None = None,
    ) -> ProjectCheckoutResult:
        account = self._helper(machine, ("account", machine.os_account))
        home = account.get("home")
        if account.get("account") != machine.os_account or not isinstance(home, str):
            raise ProjectCheckoutRefused("account_or_path", "Invalid execution account receipt.")
        context = CheckoutAccount(
            home,
            machine.os_account,
            machine.central_root or str(remote_projects_root(home)),
        )
        return self._prepare_initial(
            machine,
            context,
            request_kind=request_kind,
            project_id=project_id,
            repository_alias=repository_alias,
            state_repository=state_repository,
            expected_commit=expected_commit,
        )

    def prepare_initial(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial,
        *,
        request_kind: ProjectProvisioningKind,
        project_id: str,
        repository_alias: str,
        state_repository: bool,
        expected_commit: str | None = None,
    ) -> ProjectCheckoutResult:
        if (material.project_id, material.repository_alias, material.machine_alias) != (
            project_id,
            repository_alias,
            machine.alias,
        ):
            raise ProjectCheckoutRefused(
                "account_or_path", "The checkout target differs from its key."
            )
        material = self._credential_manager.inspect_key(machine, material)
        return self._prepare_initial(
            machine,
            material,
            request_kind=request_kind,
            project_id=project_id,
            repository_alias=repository_alias,
            state_repository=state_repository,
            expected_commit=expected_commit,
        )

    def _prepare_initial(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial | CheckoutAccount,
        *,
        request_kind: ProjectProvisioningKind,
        project_id: str,
        repository_alias: str,
        state_repository: bool,
        expected_commit: str | None,
    ) -> ProjectCheckoutResult:
        if expected_commit is not None and _FULL_COMMIT.fullmatch(expected_commit) is None:
            raise ValueError("expected checkout commit must be a full Git object id")
        receipt = self._prepare_path(
            machine, material, project_id=project_id, repository_alias=repository_alias
        )
        path = receipt.repository_path

        def run(*arguments: str) -> subprocess.CompletedProcess[str]:
            return self._git_at(machine, material, path, arguments)

        if receipt.empty:
            if expected_commit is not None or request_kind == "connect_repository":
                raise self._checkout_conflict(material, path, "The recorded checkout is missing.")
            initialized = run("init", "-b", "main", "--template=")
            if initialized.returncode != 0:
                raise self._checkout_conflict(
                    material, path, "Cannot initialize the central checkout."
                )
            self._seal_new_git_directory(machine, material, path)
        self._verify_git_directory(machine, material, path)
        branch = run("symbolic-ref", "--quiet", "HEAD")
        if branch.returncode != 0 or branch.stdout.strip() != "refs/heads/main":
            raise self._checkout_conflict(material, path, "The central checkout must be on main.")
        # Check all execution configuration and cleanliness before making any commit.
        account = CheckoutAccount(material.account_home, material.os_account, material.central_root)
        remotes = run("remote")
        no_origin = remotes.returncode == 0 and not remotes.stdout.strip()
        context = account if no_origin else material
        head = run("rev-parse", "--verify", "HEAD")
        if head.returncode != 0:
            if expected_commit is not None or request_kind == "connect_repository":
                raise self._checkout_conflict(
                    material, path, "The recorded main commit is missing."
                )
            config = run(
                "config",
                "--local",
                "--name-only",
                "--get-regexp",
                "^(include|includeif|filter|url|remote|core\\.(sshcommand|hookspath|fsmonitor))",
            )
            if config.returncode not in {0, 1} or config.stdout:
                raise self._checkout_conflict(
                    material, path, "The unborn checkout contains unsafe configuration."
                )
            clean = run("status", "--porcelain=v1", "--untracked-files=all")
            if clean.returncode != 0 or clean.stdout:
                raise self._checkout_conflict(material, path, "The unborn checkout contains work.")
            committed = run(
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
            if committed.returncode != 0:
                raise self._checkout_conflict(material, path, "Cannot create the RCP first commit.")
        retained = (
            self._retained_research(machine, material, path)
            if state_repository
            else RetainedResearchState(False, False, None, None)
        )
        if request_kind in {"create_team_project", "add_repository"} and retained.retained:
            raise ProjectCheckoutRefused(
                "retained_research",
                "The checkout contains retained research.",
                retained_research=retained,
                central_root=receipt.central_root,
                repository_path=path,
                checkout_disposition=receipt.checkout_disposition,
            )
        if request_kind == "connect_repository" and expected_commit is not None:
            if run("merge-base", "--is-ancestor", expected_commit, "HEAD").returncode != 0:
                raise self._checkout_conflict(
                    material, path, "The server main no longer contains the recorded commit."
                )
            expected_commit = None
        commit = self._verify_repository(
            machine,
            context,
            path,
            expected_commit=expected_commit,
            unpublished=True,
            allow_dirty=request_kind == "connect_repository",
        )
        if isinstance(material, DeployKeyMaterial) and no_origin:
            added = run("remote", "add", "origin", material.repository.ssh_clone_url)
            if added.returncode != 0:
                raise self._checkout_conflict(
                    material, path, "Cannot add the reviewed GitHub origin."
                )
            self._verify_repository(
                machine,
                material,
                path,
                expected_commit=commit,
                unpublished=True,
                allow_dirty=request_kind == "connect_repository",
            )
        return ProjectCheckoutResult(
            machine.alias,
            repository_alias,
            receipt.central_root,
            path,
            receipt.checkout_disposition,
            commit,
            retained,
        )

    def publish_initial(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial,
        *,
        repository_path: str,
        expected_commit: str,
        request_kind: ProjectProvisioningKind = "create_team_project",
    ) -> GitWriteProbe:
        """Push only an already durably recorded local commit, then reconcile the receipt."""
        if _FULL_COMMIT.fullmatch(expected_commit) is None:
            raise ValueError("first push requires a recorded full commit")
        material = self._credential_manager.inspect_key(machine, material)
        exact_path = str(
            PurePosixPath(material.central_root)
            / material.project_id
            / "repositories"
            / material.repository_alias
        )
        if repository_path != exact_path:
            raise ProjectCheckoutRefused(
                "account_or_path", "The push must use the exact central checkout."
            )
        self._verify_git_directory(machine, material, repository_path)
        self._verify_repository(
            machine,
            material,
            repository_path,
            expected_commit=expected_commit,
            unpublished=True,
            allow_dirty=request_kind == "connect_repository",
        )

        def run(*arguments: str) -> subprocess.CompletedProcess[str]:
            return self._git_at(machine, material, repository_path, arguments)

        advertised = run("ls-remote", "origin", "refs/heads/*")
        if advertised.returncode != 0:
            return _probe_failure(advertised, operation="read", stage="main lookup")
        refs = _parse_remote_refs(advertised.stdout)
        remote = refs.get("refs/heads/main")
        divergence = GitWriteProbe(
            "failed",
            expected_commit,
            None,
            "Merge GitHub's main into the server repository, then resume. RCP never force-pushes or merges.",
        )
        if remote is None and refs and request_kind != "connect_repository":
            return divergence
        if remote is not None:
            fetched = run("fetch", "--no-tags", "origin", "refs/heads/main")
            if fetched.returncode != 0:
                return _probe_failure(fetched, operation="read", stage="main fetch")
            fetched_head = run("rev-parse", "FETCH_HEAD")
            if fetched_head.stdout.strip() != remote:
                return divergence
            ancestor = run("merge-base", "--is-ancestor", remote, expected_commit)
            if ancestor.returncode != 0:
                return divergence
        pushed = run("push", "--porcelain", "origin", f"{expected_commit}:refs/heads/main")
        observed = run("ls-remote", "origin", "refs/heads/main")
        if observed.returncode == 0:
            current = _parse_remote_refs(observed.stdout).get("refs/heads/main")
            if current == expected_commit:
                return GitWriteProbe(
                    "ready",
                    expected_commit,
                    None,
                    "The recorded main commit was read back from GitHub.",
                )
            if current is not None and current != remote:
                return divergence
        return _probe_failure(
            pushed if pushed.returncode else observed, operation="write", stage="main push readback"
        )

    def prepare_recovery(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial,
        *,
        project_id: str,
        repository_alias: str,
        state_repository: bool,
        expected_head: str,
        retained_provisioning_commit: str,
        archived_research: Mapping[str, tuple[str, int]],
    ) -> ProjectCheckoutResult:
        """Reconstruct one replacement checkout without adopting newer research input."""

        if _FULL_COMMIT.fullmatch(retained_provisioning_commit) is None:
            raise ValueError("retained provisioning commit must be one full Git object id")
        result = self.prepare(
            machine,
            material,
            request_kind="incoming_transfer",
            project_id=project_id,
            repository_alias=repository_alias,
            state_repository=state_repository,
            expected_commit=expected_head,
        )
        retained = self._git_at(
            machine,
            material,
            result.repository_path,
            ("rev-parse", "--verify", f"{retained_provisioning_commit}^{{commit}}"),
        )
        if retained.returncode != 0 or retained.stdout.strip() != retained_provisioning_commit:
            raise ProjectCheckoutRefused(
                "git_access",
                "The reconstructed checkout no longer contains its captured provisioning commit.",
                central_root=result.central_root,
                repository_path=result.repository_path,
                checkout_disposition=result.checkout_disposition,
            )
        self.verify_recovery_research(
            machine,
            result.repository_path,
            account_home=material.account_home,
            central_root=material.central_root,
            archived_research=archived_research,
        )
        return result

    def verify_recovery_research(
        self,
        machine: ProjectProvisioningMachineIntent,
        repository_path: str,
        *,
        account_home: str,
        central_root: str,
        archived_research: Mapping[str, tuple[str, int]],
    ) -> None:
        for path, proof in archived_research.items():
            parsed = PurePosixPath(path) if isinstance(path, str) else None
            if (
                not isinstance(path, str)
                or not path.startswith(".research/")
                or parsed is None
                or parsed.is_absolute()
                or ".." in parsed.parts
                or parsed.as_posix() != path
                or not isinstance(proof, tuple)
                or len(proof) != 2
                or not isinstance(proof[0], str)
                or re.fullmatch(r"[0-9a-f]{64}", proof[0]) is None
                or not isinstance(proof[1], int)
                or isinstance(proof[1], bool)
                or proof[1] < 0
            ):
                raise ValueError("archived recovery research proof is invalid")
        observed: dict[str, tuple[str, int]] = {}
        stable: tuple[bool, str, int] | None = None
        offset = 0
        while True:
            policy = canonical_json_text(
                {
                    "durable_roots": sorted(
                        BACKUP_RESEARCH_CANONICAL_ROOTS | BACKUP_RESEARCH_DELEGATED_ROOTS
                    ),
                    "excluded_direct": sorted(BACKUP_RESEARCH_EXCLUSIONS),
                    "excluded_names": sorted(BACKUP_MATERIALIZED_NAMES),
                    "excluded_prefixes": [".batch-", ".unconfirmed-"],
                    "offset": offset,
                    "page_size": 8,
                },
            )
            try:
                payload = self._helper(
                    machine,
                    (
                        "recovery-research",
                        machine.os_account,
                        account_home,
                        repository_path,
                        policy,
                    ),
                )
            except ProjectCheckoutRefused as exc:
                raise ProjectCheckoutRefused(
                    "retained_research",
                    "The reconstructed checkout contains unsafe or unclassified .research input.",
                    central_root=central_root,
                    repository_path=repository_path,
                ) from exc
            if set(payload) != {
                "files",
                "inventory_sha256",
                "next_offset",
                "research_present",
                "total_files",
            }:
                raise self._recovery_inventory_refused(central_root, repository_path)
            files = payload["files"]
            next_offset = payload["next_offset"]
            signature = (
                payload["research_present"],
                payload["inventory_sha256"],
                payload["total_files"],
            )
            if (
                not isinstance(signature[0], bool)
                or not isinstance(signature[1], str)
                or re.fullmatch(r"[0-9a-f]{64}", signature[1]) is None
                or not isinstance(signature[2], int)
                or isinstance(signature[2], bool)
                or signature[2] < 0
                or not isinstance(files, list)
                or not (next_offset is None or isinstance(next_offset, int))
                or isinstance(next_offset, bool)
                or stable not in {None, signature}
            ):
                raise self._recovery_inventory_refused(central_root, repository_path)
            stable = signature
            for item in files:
                item_path = item.get("path") if isinstance(item, dict) else None
                item_sha256 = item.get("sha256") if isinstance(item, dict) else None
                item_size = item.get("size_bytes") if isinstance(item, dict) else None
                if (
                    not isinstance(item, dict)
                    or set(item) != {"path", "sha256", "size_bytes"}
                    or not isinstance(item_path, str)
                    or not item_path.startswith(".research/")
                    or PurePosixPath(item_path).is_absolute()
                    or ".." in PurePosixPath(item_path).parts
                    or PurePosixPath(item_path).as_posix() != item_path
                    or not isinstance(item_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", item_sha256) is None
                    or not isinstance(item_size, int)
                    or isinstance(item_size, bool)
                    or item_size < 0
                    or item_path in observed
                ):
                    raise self._recovery_inventory_refused(central_root, repository_path)
                observed[item_path] = (item_sha256, item_size)
            if next_offset is None:
                if len(observed) != signature[2]:
                    raise self._recovery_inventory_refused(central_root, repository_path)
                break
            if next_offset != offset + len(files) or next_offset <= offset:
                raise self._recovery_inventory_refused(central_root, repository_path)
            offset = next_offset
        if any(archived_research.get(path) != proof for path, proof in observed.items()):
            raise ProjectCheckoutRefused(
                "retained_research",
                (
                    "The reconstructed checkout contains retained .research input that is newer, "
                    "unknown, or different from the validated archive. RCP left it intact."
                ),
                central_root=central_root,
                repository_path=repository_path,
            )

    @staticmethod
    def _recovery_inventory_refused(
        central_root: str,
        repository_path: str,
    ) -> ProjectCheckoutRefused:
        return ProjectCheckoutRefused(
            "retained_research",
            "The reconstructed checkout returned an invalid retained-research inventory.",
            central_root=central_root,
            repository_path=repository_path,
        )

    def _prepare_path(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial | CheckoutAccount,
        *,
        project_id: str,
        repository_alias: str,
    ) -> _PathReceipt:
        payload = self._helper(
            machine,
            (
                "prepare",
                machine.os_account,
                material.account_home,
                machine.location,
                machine.central_root or "-",
                project_id,
                repository_alias,
            ),
        )
        expected_keys = {
            "account",
            "home",
            "central_root",
            "repository_path",
            "disposition",
            "empty",
        }
        if set(payload) != expected_keys:
            raise ProjectCheckoutRefused(
                "account_or_path",
                "The central-checkout helper returned an unexpected receipt shape.",
            )
        string_fields = expected_keys - {"empty"}
        if not all(isinstance(payload[field], str) for field in string_fields) or not isinstance(
            payload["empty"], bool
        ):
            raise ProjectCheckoutRefused(
                "account_or_path",
                "The central-checkout helper returned invalid receipt fields.",
            )
        central_root = str(payload["central_root"])
        expected_root = (
            machine.central_root
            if machine.central_root is not None
            else str(remote_projects_root(material.account_home))
        )
        expected_path = str(
            PurePosixPath(expected_root) / project_id / "repositories" / repository_alias
        )
        disposition = str(payload["disposition"])
        if (
            payload["account"] != machine.os_account
            or payload["home"] != material.account_home
            or central_root != expected_root
            or payload["repository_path"] != expected_path
            or disposition not in {"request_created", "reused_existing"}
            or (disposition == "request_created" and not payload["empty"])
        ):
            raise ProjectCheckoutRefused(
                "account_or_path",
                "The central-checkout receipt does not match the reviewed machine and project.",
            )
        return _PathReceipt(
            central_root=central_root,
            repository_path=expected_path,
            checkout_disposition=cast(ProjectProvisioningCheckoutDisposition, disposition),
            empty=bool(payload["empty"]),
        )

    def _verify_git_directory(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial | CheckoutAccount,
        repository_path: str,
    ) -> None:
        payload = self._helper(
            machine,
            ("git-directory", machine.os_account, material.account_home, repository_path),
        )
        if set(payload) != {"repository_path", "safe"} or payload != {
            "repository_path": repository_path,
            "safe": True,
        }:
            raise ProjectCheckoutRefused(
                "account_or_path",
                "The central checkout returned an invalid Git-directory receipt.",
            )

    def _seal_new_git_directory(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial | CheckoutAccount,
        repository_path: str,
    ) -> None:
        payload = self._helper(
            machine,
            ("seal-git-directory", machine.os_account, material.account_home, repository_path),
        )
        if set(payload) != {"repository_path", "sealed"} or payload != {
            "repository_path": repository_path,
            "sealed": True,
        }:
            raise ProjectCheckoutRefused(
                "account_or_path",
                "The new central checkout returned an invalid Git-directory seal receipt.",
            )

    def _clone(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial,
        repository_path: str,
    ) -> None:
        result = self._git(
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
                material.repository.ssh_clone_url,
                repository_path,
            ),
        )
        if result.returncode != 0:
            raise ProjectCheckoutRefused(
                "git_access",
                "Git could not clone the canonical repository with its exact deploy key.",
                central_root=material.central_root,
                repository_path=repository_path,
            )

    def _verify_repository(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial | CheckoutAccount,
        repository_path: str,
        *,
        expected_commit: str | None,
        unpublished: bool = False,
        allow_dirty: bool = False,
    ) -> str:
        checkout = self._git_at(
            machine,
            material,
            repository_path,
            ("rev-parse", "--is-inside-work-tree", "--show-toplevel"),
        )
        if checkout.returncode != 0 or checkout.stdout.strip() != f"true\n{repository_path}":
            raise self._checkout_conflict(
                material,
                repository_path,
                "The central path is not the exact non-bare Git working tree.",
            )
        remotes = self._git_at(machine, material, repository_path, ("remote",))
        if remotes.returncode != 0 or remotes.stdout.splitlines() != (
            ["origin"] if isinstance(material, DeployKeyMaterial) else []
        ):
            raise self._checkout_conflict(
                material,
                repository_path,
                "The central checkout must have exactly one remote named origin.",
            )
        unsafe_config = re.compile(
            r"^(include(\..*)?|includeif\..*|"
            r"url\..*\.(insteadof|pushinsteadof)|"
            r"remote\..*\.(uploadpack|receivepack)|"
            r"core\.fsmonitor|"
            r"filter\..*\.(clean|smudge|process|required))$",
            re.DOTALL,
        )
        # Keep unrelated values out of the bounded helper response. A complete
        # --list can exceed that bound even when every checked setting is safe.
        local_config = self._git_at(
            machine,
            material,
            repository_path,
            (
                "config",
                "--local",
                "--null",
                "--get-regexp",
                unsafe_config.pattern
                + r"|^(remote\.origin\.(url|pushurl|fetch)|core\.(sshcommand|hookspath))$",
            ),
        )
        if local_config.returncode not in {0, 1}:
            raise self._checkout_conflict(
                material, repository_path, "Cannot inspect local Git configuration."
            )
        config: dict[str, list[str]] = {}
        for record in local_config.stdout.split("\0"):
            if record:
                key, _, value = record.partition("\n")
                # Git normalizes section/variable case, but preserves subsection
                # case. Reproduce get-all's line output, including empty values.
                config.setdefault(key, []).extend((value + "\n").splitlines())
        if isinstance(material, DeployKeyMaterial) and (
            local_config.returncode != 0
            or config.get("remote.origin.url") != [material.repository.ssh_clone_url]
            or config.get("remote.origin.pushurl", [material.repository.ssh_clone_url])
            != [material.repository.ssh_clone_url]
        ):
            raise self._checkout_conflict(
                material,
                repository_path,
                "The central checkout origin does not match the canonical GitHub repository.",
            )
        if isinstance(material, DeployKeyMaterial) and config.get("remote.origin.fetch") != [
            "+refs/heads/*:refs/remotes/origin/*"
        ]:
            raise self._checkout_conflict(
                material,
                repository_path,
                "The central checkout has an unsafe origin fetch mapping; RCP left it intact.",
            )
        if any(unsafe_config.search(key) for key in config):
            raise self._checkout_conflict(
                material,
                repository_path,
                "The central checkout contains unsafe local Git execution or URL configuration; "
                "RCP left it intact.",
            )
        ssh_command = (
            deploy_key_ssh_command(material) if isinstance(material, DeployKeyMaterial) else None
        )
        stored_ssh = config.get("core.sshcommand")
        if stored_ssh is not None and stored_ssh != [ssh_command]:
            raise self._checkout_conflict(
                material,
                repository_path,
                "The central checkout contains unsafe local Git SSH configuration; RCP left it intact.",
            )
        stored_hooks = config.get("core.hookspath")
        if stored_hooks is not None and stored_hooks != ["/dev/null"]:
            raise self._checkout_conflict(
                material,
                repository_path,
                "The central checkout contains an unsafe repository hook path; RCP left it intact.",
            )
        clean = self._git_at(
            machine,
            material,
            repository_path,
            ("status", "--porcelain=v1", "--untracked-files=all"),
        )
        if clean.returncode != 0 or (clean.stdout and not allow_dirty):
            raise self._checkout_conflict(
                material,
                repository_path,
                "The central checkout contains uncommitted or untracked work; RCP left it intact.",
            )
        remote_commit = expected_commit
        if not unpublished:
            assert isinstance(material, DeployKeyMaterial)
            fetched = self._git_at(
                machine,
                material,
                repository_path,
                ("fetch", "--no-tags", "origin"),
            )
            if fetched.returncode != 0:
                raise ProjectCheckoutRefused(
                    "git_access",
                    "The central checkout could not fetch its canonical GitHub origin.",
                    central_root=material.central_root,
                    repository_path=repository_path,
                )
            advertised = self._git_at(
                machine,
                material,
                repository_path,
                ("ls-remote", "origin", "HEAD"),
            )
            remote_commit = _one_head(advertised)
        local = self._git_at(machine, material, repository_path, ("rev-parse", "HEAD"))
        local_commit = local.stdout.strip()
        if local.returncode != 0 or _FULL_COMMIT.fullmatch(local_commit) is None:
            raise self._checkout_conflict(
                material,
                repository_path,
                "The central checkout HEAD is unavailable or invalid.",
            )
        if unpublished:
            remote_commit = local_commit
        if remote_commit != local_commit:
            raise self._checkout_conflict(
                material,
                repository_path,
                "The clean central checkout and GitHub HEAD differ; RCP did not reset either one.",
            )
        if expected_commit is not None and remote_commit != expected_commit:
            raise ProjectCheckoutRefused(
                "git_access",
                "GitHub HEAD changed after the write proof; rerun the same provisioning request.",
                central_root=material.central_root,
                repository_path=repository_path,
            )
        final_clean = self._git_at(
            machine,
            material,
            repository_path,
            ("status", "--porcelain=v1", "--untracked-files=all"),
        )
        if final_clean.returncode != 0 or (final_clean.stdout and not allow_dirty):
            raise self._checkout_conflict(
                material,
                repository_path,
                "The central checkout changed during verification; RCP left it intact.",
            )
        if stored_ssh is None and ssh_command is not None:
            configured = self._git_at(
                machine,
                material,
                repository_path,
                ("config", "--local", "core.sshCommand", ssh_command),
            )
            if configured.returncode != 0:
                raise self._checkout_conflict(
                    material,
                    repository_path,
                    "The central checkout could not configure its repository deploy key.",
                )
        if stored_hooks is None:
            configured = self._git_at(
                machine,
                material,
                repository_path,
                ("config", "--local", "core.hooksPath", "/dev/null"),
            )
            if configured.returncode != 0:
                raise self._checkout_conflict(
                    material,
                    repository_path,
                    "The central checkout could not disable repository hooks.",
                )
        hooks = self._git_at(
            machine,
            material,
            repository_path,
            ("config", "--local", "--get", "core.hooksPath"),
        )
        if hooks.returncode != 0 or hooks.stdout.strip() != "/dev/null":
            raise self._checkout_conflict(
                material,
                repository_path,
                "The central checkout hook fence could not be read back.",
            )
        return local_commit

    def _retained_research(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial | CheckoutAccount,
        repository_path: str,
    ) -> RetainedResearchState:
        payload = self._helper(
            machine,
            ("retained", machine.os_account, material.account_home, repository_path),
        )
        if set(payload) != {"retained", "patch_history", "project_id", "home_space_id"}:
            raise ProjectCheckoutRefused(
                "account_or_path",
                "The retained-research helper returned an unexpected receipt shape.",
                central_root=material.central_root,
                repository_path=repository_path,
            )
        if (
            not isinstance(payload["retained"], bool)
            or not isinstance(payload["patch_history"], bool)
            or not (payload["project_id"] is None or isinstance(payload["project_id"], str))
            or not (payload["home_space_id"] is None or isinstance(payload["home_space_id"], str))
            or (payload["project_id"] is None) != (payload["home_space_id"] is None)
            or (payload["patch_history"] and not payload["retained"])
        ):
            raise ProjectCheckoutRefused(
                "account_or_path",
                "The retained-research helper returned invalid receipt fields.",
                central_root=material.central_root,
                repository_path=repository_path,
            )
        return RetainedResearchState(
            retained=bool(payload["retained"]),
            patch_history=bool(payload["patch_history"]),
            project_id=(str(payload["project_id"]) if payload["project_id"] is not None else None),
            home_space_id=(
                str(payload["home_space_id"]) if payload["home_space_id"] is not None else None
            ),
        )

    def _helper(
        self,
        machine: ProjectProvisioningMachineIntent,
        arguments: tuple[str, ...],
    ) -> dict[str, object]:
        operation = arguments[0]
        result = self._target(
            machine,
            ("python3", "-c", _remote_helper_source(), *arguments),
        )
        if result.returncode != 0:
            raise ProjectCheckoutRefused(
                "account_or_path",
                f"The checkout helper refused the {operation} operation's exact account, path, "
                "ownership, or mode.",
            )
        if len(result.stdout.encode("utf-8", errors="replace")) > _MAX_HELPER_OUTPUT_BYTES:
            raise ProjectCheckoutRefused(
                "account_or_path",
                "The checkout helper returned too much output.",
            )
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ProjectCheckoutRefused(
                "account_or_path",
                "The checkout helper returned invalid JSON.",
            ) from exc
        if not isinstance(payload, dict):
            raise ProjectCheckoutRefused(
                "account_or_path",
                "The checkout helper returned an invalid receipt.",
            )
        return payload

    def _git_at(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial | CheckoutAccount,
        repository_path: str,
        arguments: tuple[str, ...],
    ) -> subprocess.CompletedProcess[str]:
        return self._git(
            machine,
            material,
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-C",
                repository_path,
                *arguments,
            ),
        )

    def _git(
        self,
        machine: ProjectProvisioningMachineIntent,
        material: DeployKeyMaterial | CheckoutAccount,
        argv: tuple[str, ...],
    ) -> subprocess.CompletedProcess[str]:
        return self._target(
            machine,
            (
                "env",
                "-i",
                f"HOME={material.account_home}",
                f"USER={material.os_account}",
                f"LOGNAME={material.os_account}",
                f"PATH={_SAFE_PATH}",
                "LANG=C.UTF-8",
                "LC_ALL=C.UTF-8",
                "GIT_CONFIG_GLOBAL=/dev/null",
                "GIT_CONFIG_NOSYSTEM=1",
                "GIT_TERMINAL_PROMPT=0",
                "GIT_SSH_VARIANT=ssh",
                *(
                    (f"GIT_SSH_COMMAND={deploy_key_ssh_command(material)}",)
                    if isinstance(material, DeployKeyMaterial)
                    else ()
                ),
                "python3",
                "-c",
                _remote_private_exec_source(),
                *argv,
            ),
        )

    def _target(
        self,
        machine: ProjectProvisioningMachineIntent,
        command: tuple[str, ...],
    ) -> subprocess.CompletedProcess[str]:
        try:
            argv = target_account_argv(self.layout, machine, command)
            result = self._runner(
                argv,
                timeout=SERVER_PROJECT_CHECKOUT_TIMEOUT_SECONDS,
            )
        except (GitCredentialRefused, OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectCheckoutRefused(
                "account_or_path",
                "The exact checkout execution account or transport was unavailable.",
            ) from exc
        if (
            len(result.stdout.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES
            or len(result.stderr.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES
        ):
            return subprocess.CompletedProcess(argv, 126, "", "output exceeded the bound")
        return result

    @staticmethod
    def _checkout_conflict(
        material: DeployKeyMaterial | CheckoutAccount,
        repository_path: str,
        message: str,
    ) -> ProjectCheckoutRefused:
        return ProjectCheckoutRefused(
            "checkout_conflict",
            message,
            central_root=material.central_root,
            repository_path=repository_path,
        )


def retained_research_operator_step(
    machine: ProjectProvisioningMachineIntent,
    refusal: ProjectCheckoutRefused,
    *,
    number: int,
    request_id: str,
    resume_argv: tuple[str, ...],
    local_host: str,
) -> ServerStep:
    _require_project_resume(resume_argv, request_id)
    if (
        refusal.kind != "retained_research"
        or refusal.repository_path is None
        or refusal.retained_research is None
    ):
        raise ValueError("retained-research action requires one exact retained checkout")
    return ServerStep(
        number=number,
        title="Resolve retained RCP research",
        purpose="Prevent a new team project from adopting or overwriting existing RCP history.",
        performed_by="human",
        target=MachineTarget(
            host=machine.host or local_host,
            os_account=machine.os_account,
        ),
        phase="retained_research",
        state="operator_action_needed",
        expected_success=(
            "The repository has no retained .research state, or a separate Move to team request "
            "owns the existing personal project."
        ),
        message=str(refusal),
        actions=(
            ExternalAction(
                instruction=(
                    "If this repository belongs to a personal RCP project, cancel this new-project "
                    "request and choose Move to team space. Otherwise clean or choose the repository "
                    "outside RCP, then resume this exact request."
                )
            ),
        ),
        fields=(
            NonsecretField(name="repository_path", value=refusal.repository_path),
            NonsecretField(
                name="patch_history",
                value=refusal.retained_research.patch_history,
            ),
        ),
        resume_argv=resume_argv,
        resume_execution=OPERATOR_SHELL,
    )


def _require_project_resume(resume_argv: tuple[str, ...], request_id: str) -> None:
    canonical_uuid4(request_id, label="provisioning request identity")
    meaningful = resume_argv[:-1] if resume_argv[-1:] == ("--machine-readable",) else resume_argv
    if meaningful[-4:] != ("server", "project", "provision", request_id):
        raise ValueError("resume argv must name this exact provisioning request")


def _one_head(result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode != 0:
        raise ProjectCheckoutRefused(
            "git_access",
            "GitHub did not return the canonical repository HEAD.",
        )
    lines = result.stdout.splitlines()
    if len(lines) != 1:
        raise ProjectCheckoutRefused(
            "git_access",
            "GitHub did not advertise exactly one repository HEAD.",
        )
    parts = lines[0].split("\t")
    if len(parts) != 2 or parts[1] != "HEAD" or _FULL_COMMIT.fullmatch(parts[0]) is None:
        raise ProjectCheckoutRefused(
            "git_access",
            "GitHub returned an invalid repository HEAD record.",
        )
    return parts[0]


@lru_cache(maxsize=1)
def _remote_helper_source() -> str:
    return (
        importlib.resources.files("rcp.server_ops")
        .joinpath("remote_project_checkout.py")
        .read_text(encoding="utf-8")
    )


@lru_cache(maxsize=1)
def _remote_private_exec_source() -> str:
    return (
        importlib.resources.files("rcp.server_ops")
        .joinpath("remote_private_exec.py")
        .read_text(encoding="utf-8")
    )


__all__ = [
    "ProjectCheckoutManager",
    "ProjectCheckoutRefused",
    "ProjectCheckoutResult",
    "RetainedResearchState",
    "retained_research_operator_step",
]
