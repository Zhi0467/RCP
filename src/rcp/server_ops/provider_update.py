"""Provider-native CLI maintenance under the installed service account."""

from __future__ import annotations

import contextlib
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from rcp.providers import ProviderId, profile_for
from rcp.server_ops.cli import CallerIdentity, PreparedServerCommand, ServerEventEmitter
from rcp.server_ops.install import _run_as_account
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT, ServerLayout
from rcp.server_ops.models import (
    MachineTarget,
    NonsecretField,
    ServerCommandRequest,
    ServerPlanEvent,
    ServerStep,
    redact_server_text,
)

ProviderProcessRunner = Callable[
    [pwd.struct_passwd, tuple[str, ...], float], subprocess.CompletedProcess[str]
]

_UPDATE_TIMEOUT_SECONDS = 15 * 60
_PROBE_TIMEOUT_SECONDS = 30
_SAFE_VERSION = re.compile(r"[ -~]{1,120}")


class ProviderUpdateRefused(RuntimeError):
    """The provider update cannot safely run on this installed server."""


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
        before_path: Path | None
        before_version: str | None
        emitter.emit_step(_running(pending[0], "Inspecting the installed provider as rcp."))
        try:
            account = _installed_service_account(layout)
            before_path = _discover_provider(account, provider)
            before_version = (
                _provider_version(account, before_path, process_runner)
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
            _update_provider(account, provider, before_path, process_runner)
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
            after_path = _discover_provider(account, provider)
            if after_path is None:
                raise ProviderUpdateRefused(
                    "The updater completed but no provider executable is discoverable as rcp."
                )
            after_version = _provider_version(account, after_path, process_runner)
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


def _discover_provider(account: pwd.struct_passwd, provider: ProviderId) -> Path | None:
    """Find the account's executable as root, with fixed directories standing in for its PATH."""
    home = Path(account.pw_dir)
    candidates = (
        home / ".local" / "bin" / provider,
        # The same provider-native locations readiness discovers, so the
        # documented installation is the one this command updates.
        *(home / relative for relative in profile_for(provider).install_paths),
        Path("/usr/local/bin") / provider,
        Path("/usr/bin") / provider,
        Path("/bin") / provider,
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _provider_version(
    account: pwd.struct_passwd,
    binary: Path,
    runner: ProviderProcessRunner,
) -> str:
    result = runner(account, (str(binary), "--version"), _PROBE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise ProviderUpdateRefused(
            f"The provider version probe failed: {_bounded_diagnostic(result)}"
        )
    lines = (result.stdout or result.stderr).strip().splitlines()
    version = lines[-1].strip() if lines else ""
    if _SAFE_VERSION.fullmatch(version) is None:
        raise ProviderUpdateRefused("The provider returned no safe bounded version string.")
    return version


def _update_provider(
    account: pwd.struct_passwd,
    provider: ProviderId,
    before_path: Path | None,
    runner: ProviderProcessRunner,
) -> None:
    profile = profile_for(provider)
    update = profile.native_update
    if update.self_update_args is not None:
        assert before_path is not None
        result = runner(
            account, (str(before_path), *update.self_update_args), _UPDATE_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            raise ProviderUpdateRefused(
                f"{profile.label}'s native update failed: {_bounded_diagnostic(result)}"
            )
        return
    assert update.installer_url is not None
    # Agents may write /tmp, so the installer is staged in the account's own
    # `~/.rcp/tmp`, which their write scopes keep read-only.
    temporary = Path(
        tempfile.mkdtemp(prefix=f"rcp-provider-{provider}-", dir=_account_temp_dir(account))
    )
    try:
        os.chown(temporary, account.pw_uid, account.pw_gid)
        os.chmod(temporary, 0o700)
        installer = temporary / "install.sh"
        downloaded = runner(
            account,
            ("/usr/bin/curl", "-fsSL", update.installer_url, "-o", str(installer)),
            _UPDATE_TIMEOUT_SECONDS,
        )
        if downloaded.returncode != 0:
            raise ProviderUpdateRefused(
                f"{profile.label}'s official installer download failed: "
                f"{_bounded_diagnostic(downloaded)}"
            )
        installed = runner(
            account,
            ("/usr/bin/env", *update.installer_env, "/bin/sh", str(installer)),
            _UPDATE_TIMEOUT_SECONDS,
        )
        if installed.returncode != 0:
            raise ProviderUpdateRefused(
                f"{profile.label}'s official installer failed: {_bounded_diagnostic(installed)}"
            )
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


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
    before_path: Path | None,
    after_path: Path,
) -> str:
    result = (
        f"{profile_for(provider).label} is updated as rcp. An update does not change its "
        "login; Settings, Provider logins, shows whether the login is alive."
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
    "prepare_provider_update_command",
    "ProviderUpdateRefused",
]
