"""Provider-native CLI maintenance under the installed service account."""

from __future__ import annotations

import contextlib
import os
import pwd
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from rcp.limits import PROVIDER_UPDATE_PROBE_TIMEOUT_SECONDS, PROVIDER_UPDATE_TIMEOUT_SECONDS
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.providers import ProviderId, profile_for
from rcp.server_ops.cli import CallerIdentity, PreparedServerCommand, ServerEventEmitter
from rcp.server_ops.install import _account_environment_argv, _run_as_account, _run_process
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT, ServerLayout
from rcp.server_ops.models import (
    MachineTarget,
    NonsecretField,
    ServerCommandRequest,
    ServerPlanEvent,
    ServerStep,
    redact_server_text,
)
from rcp.transport.ssh import ssh_arguments

ProviderProcessRunner = Callable[
    [pwd.struct_passwd, tuple[str, ...], float], subprocess.CompletedProcess[str]
]

_SAFE_VERSION = re.compile(r"[ -~]{1,120}")


class ProviderUpdateRefused(RuntimeError):
    """The provider update cannot safely run on this installed server."""


class UpdateTarget(Protocol):
    @property
    def home(self) -> str: ...

    def run(self, argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]: ...

    def discover(self, provider: ProviderId) -> str | None: ...

    def temp_dir(self) -> str: ...

    def remove(self, path: str) -> None: ...


@dataclass
class LocalUpdateTarget:
    account: pwd.struct_passwd
    runner: ProviderProcessRunner
    _temporary: set[str] = field(default_factory=set, init=False, repr=False)

    @property
    def home(self) -> str:
        return self.account.pw_dir

    def run(self, argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
        return self.runner(self.account, argv, timeout)

    def discover(self, provider: ProviderId) -> str | None:
        binary = _discover_provider(self.account, provider)
        return str(binary) if binary is not None else None

    def temp_dir(self) -> str:
        path = tempfile.mkdtemp(prefix="rcp-provider-", dir=_account_temp_dir(self.account))
        self._temporary.add(path)
        try:
            os.chown(path, self.account.pw_uid, self.account.pw_gid)
            os.chmod(path, 0o700)
            info = Path(path).lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != self.account.pw_uid
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise ProviderUpdateRefused(f"RCP's temporary directory is unsafe: {path}")
        except (OSError, ProviderUpdateRefused):
            self.remove(path)
            raise
        return path

    def remove(self, path: str) -> None:
        _require_temp_path(self.home, path)
        if path not in self._temporary:
            raise ProviderUpdateRefused("The directory does not belong to this update.")
        shutil.rmtree(path)
        self._temporary.remove(path)


TransportRunner = Callable[[tuple[str, ...], float], subprocess.CompletedProcess[str]]


class RemoteUpdateTarget:
    def __init__(
        self, host: str, os_account: str, *, runner: TransportRunner | None = None
    ) -> None:
        if os_account and not re.fullmatch(r"[A-Za-z0-9_.-]+", os_account):
            raise ProviderUpdateRefused("The remote machine needs a valid OS account.")
        self.host = f"{os_account}@{host.rsplit('@', 1)[-1]}" if os_account else host
        self._account_resolved = bool(os_account)
        self._runner = runner
        self._home: str | None = None
        self._temporary: set[str] = set()

    def run(self, argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
        if not self._account_resolved:
            result = self._transport(("id", "-un"), PROVIDER_UPDATE_PROBE_TIMEOUT_SECONDS)
            account = result.stdout.strip()
            if result.returncode or not re.fullmatch(r"[A-Za-z0-9_.-]+", account):
                raise ProviderUpdateRefused("The remote machine could not resolve its OS account.")
            self.host = f"{account}@{self.host.rsplit('@', 1)[-1]}"
            self._account_resolved = True
        return self._transport(argv, timeout)

    def _transport(self, argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
        command = tuple(ssh_arguments(self.host, shlex.join(argv)))
        if self._runner is not None:
            return self._runner(command, timeout)
        return _run_process(command, timeout=timeout)

    def _checked(self, argv: tuple[str, ...]) -> str:
        result = self.run(argv, PROVIDER_UPDATE_PROBE_TIMEOUT_SECONDS)
        if result.returncode:
            raise ProviderUpdateRefused(_bounded_diagnostic(result))
        return result.stdout.strip()

    @property
    def home(self) -> str:
        if self._home is None:
            home = self._checked(("printenv", "HOME"))
            if (
                not home.startswith("/")
                or home == "/"
                or "\n" in home
                or ".." in PurePosixPath(home).parts
            ):
                raise ProviderUpdateRefused("The remote account returned an unsafe home directory.")
            self._home = home
        return self._home

    def discover(self, provider: ProviderId) -> str | None:
        result = self.run(
            ("sh", "-lc", f"command -v {shlex.quote(provider)}"),
            PROVIDER_UPDATE_PROBE_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and result.stdout.strip().startswith("/"):
            return result.stdout.strip()
        for relative in profile_for(provider).install_paths:
            candidate = str(PurePosixPath(self.home) / relative)
            if (
                self.run(
                    ("test", "-x", candidate), PROVIDER_UPDATE_PROBE_TIMEOUT_SECONDS
                ).returncode
                == 0
            ):
                return candidate
        return None

    def temp_dir(self) -> str:
        uid = self._checked(("id", "-u"))
        system = self._checked(("uname", "-s"))
        parent = PurePosixPath(self.home)
        for name in (".rcp", "tmp"):
            parent /= name
            self._checked(("mkdir", "-p", "-m", "700", str(parent)))
            self._check_directory(str(parent), uid, system)
        path = self._checked(("mktemp", "-d", str(parent / "rcp-provider-XXXXXXXX")))
        _require_temp_path(self.home, path)
        self._temporary.add(path)
        try:
            self._check_directory(path, uid, system, private=True)
        except ProviderUpdateRefused:
            self.remove(path)
            raise
        return path

    def _check_directory(self, path: str, uid: str, system: str, *, private: bool = False) -> None:
        stat_args = (
            ("stat", "-f", "%u:%Lp:%HT") if system == "Darwin" else ("stat", "-c", "%u:%a:%F")
        )
        fields = self._checked((*stat_args, path)).split(":")
        if len(fields) != 3 or fields[0] != uid or fields[2].lower() != "directory":
            raise ProviderUpdateRefused(f"RCP's temporary directory is unsafe: {path}")
        try:
            mode = int(fields[1], 8)
        except ValueError as exc:
            raise ProviderUpdateRefused(f"RCP's temporary directory is unsafe: {path}") from exc
        if mode & 0o022 or (private and mode != 0o700):
            raise ProviderUpdateRefused(f"RCP's temporary directory is unsafe: {path}")

    def remove(self, path: str) -> None:
        _require_temp_path(self.home, path)
        if path not in self._temporary:
            raise ProviderUpdateRefused("The directory does not belong to this update.")
        self._checked(("rm", "-rf", "--", path))
        self._temporary.remove(path)


def _require_temp_path(home: str, path: str) -> None:
    candidate = PurePosixPath(path)
    if candidate.parent != PurePosixPath(home) / ".rcp/tmp" or not candidate.name.startswith(
        "rcp-provider-"
    ):
        raise ProviderUpdateRefused("The temporary directory is outside the update boundary.")


def _require_home(target: UpdateTarget, binary: str) -> None:
    path = PurePosixPath(binary)
    if ".." in path.parts or not path.is_relative_to(target.home):
        raise ProviderUpdateRefused(
            "The provider executable is outside the account home; update it with its system package manager."
        )


def prepare_provider_update_command(
    request: ServerCommandRequest,
    identity: CallerIdentity,
    *,
    runner: ProviderProcessRunner | None = None,
    layout: ServerLayout = DEFAULT_SERVER_LAYOUT,
) -> PreparedServerCommand:
    """Prepare one bounded provider-native update and post-update readiness check."""

    if request.command != "server provider update" or request.provider_update_provider is None:
        raise ValueError("prepare_provider_update_command requires one provider update")
    provider = request.provider_update_provider
    target = MachineTarget(host=identity.host, os_account=layout.service_account)
    pending = _pending_steps(provider, target)
    process_runner = runner or _run_provider_process

    def execute(emitter: ServerEventEmitter, _input_stream: BinaryIO) -> None:
        account: pwd.struct_passwd
        before_path: str | None
        before_version: str | None
        emitter.emit_step(_running(pending[0], "Inspecting the installed provider as rcp."))
        try:
            account = _installed_service_account(layout)
            update_target = LocalUpdateTarget(account, process_runner)
            _refuse_while_turns_run(layout.data_dir / "rcp.sqlite3", provider)
            before_path = update_target.discover(provider)
            if before_path is not None:
                _require_home(update_target, before_path)
            before_version = (
                _provider_version(update_target, provider, before_path)
                if before_path is not None
                else None
            )
            if profile_for(provider).native_update.requires_installed and before_path is None:
                raise ProviderUpdateRefused(
                    f"{profile_for(provider).label} is not installed for rcp. Use the documented "
                    "provider install command, then rerun this update."
                )
        except (KeyError, OSError, ProviderUpdateRefused) as exc:
            emitter.emit_step(_failed(pending[0], str(exc)))
            return
        inspect_fields = [NonsecretField(name="provider", value=provider)]
        if before_path is not None:
            inspect_fields.append(NonsecretField(name="executable_before", value=str(before_path)))
        if before_version is not None:
            inspect_fields.append(NonsecretField(name="version_before", value=before_version))
        emitter.emit_step(
            pending[0].model_copy(
                update={
                    "state": "succeeded",
                    "message": "The provider installation boundary is safe to update.",
                    "fields": tuple(inspect_fields),
                }
            )
        )

        emitter.emit_step(_running(pending[1], f"Running {provider}'s native update as rcp."))
        try:
            _update_provider(update_target, provider, before_path)
        except (OSError, ProviderUpdateRefused) as exc:
            emitter.emit_step(_failed(pending[1], str(exc)))
            return
        emitter.emit_step(
            pending[1].model_copy(
                update={
                    "state": "succeeded",
                    "message": f"{profile_for(provider).label}'s native updater completed.",
                }
            )
        )

        emitter.emit_step(_running(pending[2], "Checking the updated executable and version."))
        try:
            after_path = update_target.discover(provider)
            if after_path is None:
                raise ProviderUpdateRefused(
                    "The updater completed but no provider executable is discoverable as rcp."
                )
            after_version = _provider_version(update_target, provider, after_path)
        except (OSError, ProviderUpdateRefused) as exc:
            emitter.emit_step(_failed(pending[2], str(exc)))
            return
        emitter.emit_step(
            pending[2].model_copy(
                update={
                    "state": "succeeded",
                    "message": _success_message(provider, before_path, after_path),
                    "fields": (
                        NonsecretField(name="executable_after", value=str(after_path)),
                        NonsecretField(name="version_after", value=after_version),
                        NonsecretField(
                            name="command_path_changed",
                            value="yes" if before_path not in {None, after_path} else "no",
                        ),
                        NonsecretField(name="authentication", value="unchanged by this update"),
                    ),
                }
            )
        )

    return PreparedServerCommand(
        plan=ServerPlanEvent(command=request.command, timestamp=datetime.now(UTC), steps=pending),
        execute=execute,
    )


@dataclass(frozen=True)
class ProviderVersionChange:
    """The version one native update started from and the version it left installed."""

    before: str
    after: str


def update_installed_provider(
    target: UpdateTarget,
    provider: ProviderId,
    binary: str,
) -> ProviderVersionChange:
    """Update a home-owned executable and verify the newly discovered command."""
    _require_home(target, binary)
    before = _provider_version(target, provider, binary)
    _update_provider(target, provider, binary)
    after_binary = target.discover(provider)
    if after_binary is None:
        raise ProviderUpdateRefused("The updated provider executable cannot be discovered.")
    _require_home(target, after_binary)
    return ProviderVersionChange(
        before=before, after=_provider_version(target, provider, after_binary)
    )


def run_provider_process_as_self(
    account: pwd.struct_passwd,
    argv: tuple[str, ...],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run a provider command as the calling account, which must be `account`; needs no root."""
    return _run_process(
        (*_account_environment_argv(account), *argv), cwd=Path(account.pw_dir), timeout=timeout
    )


def _pending_steps(provider: ProviderId, target: MachineTarget) -> tuple[ServerStep, ...]:
    label = profile_for(provider).label
    common = {"performed_by": "system", "target": target, "state": "pending"}
    return (
        ServerStep(
            number=1,
            title=f"Inspect {label}",
            purpose="Resolve the current provider installation under the exact service account.",
            phase="provider_update_inspect",
            expected_success="The installed server and provider update boundary are safe.",
            message=f"RCP will inspect {label} as rcp.",
            **common,
        ),
        ServerStep(
            number=2,
            title=f"Update {label}",
            purpose="Run only the provider's supported native update under the rcp home.",
            phase="provider_update_run",
            expected_success=f"{label}'s native update command exits successfully.",
            message=f"RCP will update {label} as rcp.",
            **common,
        ),
        ServerStep(
            number=3,
            title=f"Verify {label}",
            purpose="Prove the updated executable and its version under the service account.",
            phase="provider_update_verify",
            expected_success=f"{label} is executable under the rcp account; its login is untouched.",
            message=f"RCP will verify the updated {label} installation.",
            **common,
        ),
    )


def _installed_service_account(layout: ServerLayout) -> pwd.struct_passwd:
    if not layout.config_path.is_file() or not layout.current_release.exists():
        raise ProviderUpdateRefused(
            "No installed RCP team server is present. Run server install before provider update."
        )
    try:
        return pwd.getpwnam(layout.service_account)
    except KeyError as exc:
        raise ProviderUpdateRefused("The installed rcp service account is missing.") from exc


def backend_turns_using(database: Path, provider: ProviderId, *, queued: bool = False) -> int:
    """Count backend turns running this provider, and with `queued` those waiting to start.

    An unreadable database counts none.
    """
    statuses = ("queued", "running", "pausing") if queued else ("running", "pausing")
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        (count,) = connection.execute(
            "SELECT COUNT(*) FROM graph_runs "
            "WHERE json_extract(request_json, '$.provider')=? "
            "AND COALESCE(NULLIF(stage_machine_id,''),?)=? "
            f"AND status IN ({','.join('?' * len(statuses))})",
            (provider, BACKEND_MACHINE_ID, BACKEND_MACHINE_ID, *statuses),
        ).fetchone()
    except sqlite3.Error:
        return 0
    finally:
        connection.close()
    return count


def _refuse_while_turns_run(database: Path, provider: ProviderId) -> None:
    """Refuse while a backend turn runs this provider; an unreadable database does not block."""
    running = backend_turns_using(database, provider)
    if running:
        raise ProviderUpdateRefused(
            f"{running} agent turn(s) are running {profile_for(provider).label} on the backend. "
            "Wait for them to finish or stop them, then rerun this update."
        )


def _discover_provider(account: pwd.struct_passwd, provider: ProviderId) -> Path | None:
    """Find the account's executable as root, with fixed directories standing in for its PATH."""
    home = Path(account.pw_dir)
    candidates = (
        home / ".local" / "bin" / provider,
        Path("/usr/local/sbin") / provider,
        Path("/usr/local/bin") / provider,
        Path("/usr/sbin") / provider,
        Path("/usr/bin") / provider,
        Path("/sbin") / provider,
        Path("/bin") / provider,
        # Native locations are fallback candidates, as in readiness discovery.
        *(home / relative for relative in profile_for(provider).install_paths),
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _command_prefix(target: UpdateTarget, provider: ProviderId) -> tuple[str, ...]:
    if isinstance(target, RemoteUpdateTarget) and provider == "agy":
        return ("env", "BROWSER=/bin/false")
    return profile_for(provider).authentication.unmanaged_environment("").command_prefix


def _provider_version(target: UpdateTarget, provider: ProviderId, binary: str) -> str:
    prefix = _command_prefix(target, provider)
    result = target.run((*prefix, binary, "--version"), PROVIDER_UPDATE_PROBE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise ProviderUpdateRefused(
            f"The provider version probe failed: {_bounded_diagnostic(result)}"
        )
    lines = (result.stdout or result.stderr).strip().splitlines()
    version = lines[-1].strip() if lines else ""
    if _SAFE_VERSION.fullmatch(version) is None:
        raise ProviderUpdateRefused("The provider returned no safe bounded version string.")
    return version


def _update_provider(target: UpdateTarget, provider: ProviderId, before_path: str | None) -> None:
    profile = profile_for(provider)
    update = profile.native_update
    if before_path is not None:
        _require_home(target, before_path)
    if update.self_update_args is not None:
        assert before_path is not None
        prefix = _command_prefix(target, provider)
        result = target.run(
            (*prefix, before_path, *update.self_update_args), PROVIDER_UPDATE_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            raise ProviderUpdateRefused(
                f"{profile.label}'s native update failed: {_bounded_diagnostic(result)}"
            )
        return
    assert update.installer_url is not None
    temporary = target.temp_dir()
    try:
        installer = str(PurePosixPath(temporary) / "install.sh")
        downloaded = target.run(
            ("/usr/bin/curl", "-fsSL", update.installer_url, "-o", installer),
            PROVIDER_UPDATE_TIMEOUT_SECONDS,
        )
        if downloaded.returncode != 0:
            raise ProviderUpdateRefused(
                f"{profile.label}'s official installer download failed: "
                f"{_bounded_diagnostic(downloaded)}"
            )
        installed = target.run(
            ("/usr/bin/env", *update.installer_env, "/bin/sh", installer),
            PROVIDER_UPDATE_TIMEOUT_SECONDS,
        )
        if installed.returncode != 0:
            raise ProviderUpdateRefused(
                f"{profile.label}'s official installer failed: {_bounded_diagnostic(installed)}"
            )
    finally:
        target.remove(temporary)


def _account_temp_dir(account: pwd.struct_passwd) -> Path:
    directory = Path(account.pw_dir)
    for name in (".rcp", "tmp"):
        directory = directory / name
        with contextlib.suppress(FileExistsError):
            directory.mkdir(mode=0o700)
            os.chown(directory, account.pw_uid, account.pw_gid)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid or info.st_mode & 0o022:
            raise ProviderUpdateRefused(f"RCP's temporary directory is unsafe: {directory}")
    return directory


def _run_provider_process(
    account: pwd.struct_passwd,
    argv: tuple[str, ...],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return _run_as_account(account, argv, timeout=timeout)


def _bounded_diagnostic(result: subprocess.CompletedProcess[str]) -> str:
    raw = (result.stderr or result.stdout or "no diagnostic output").strip()
    single_line = " ".join(raw.split())[-1000:]
    return redact_server_text(single_line) or "no diagnostic output"


def _success_message(
    provider: ProviderId,
    before_path: str | None,
    after_path: str,
) -> str:
    result = (
        f"{profile_for(provider).label} is updated as rcp. An update does not change its "
        "login; Settings, Sign-ins, shows whether the login is alive."
    )
    if before_path is not None and before_path != after_path:
        return (
            f"{result} Its command path changed; existing projects keep their explicit path "
            "until an authenticated member uses Resolve in Project Settings."
        )
    return result


def _running(step: ServerStep, message: str) -> ServerStep:
    return step.model_copy(update={"state": "running", "message": message})


def _failed(step: ServerStep, message: str) -> ServerStep:
    return step.model_copy(update={"state": "failed", "message": message})


__all__ = [
    "UpdateTarget",
    "LocalUpdateTarget",
    "RemoteUpdateTarget",
    "backend_turns_using",
    "prepare_provider_update_command",
    "ProviderUpdateRefused",
    "ProviderVersionChange",
    "run_provider_process_as_self",
    "update_installed_provider",
]
