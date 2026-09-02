"""Provider-native CLI maintenance under the installed service account."""

from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal

from rcp.providers import profile_for
from rcp.server_ops.cli import CallerIdentity, PreparedServerCommand, ServerEventEmitter
from rcp.server_ops.install import _run_as_account
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT, ServerLayout
from rcp.server_ops.models import (
    CommandAction,
    MachineTarget,
    NonsecretField,
    ServerCommandRequest,
    ServerPlanEvent,
    ServerStep,
    redact_server_text,
)

ProviderUpdateId = Literal["codex", "claude"]
ProviderProcessRunner = Callable[
    [pwd.struct_passwd, tuple[str, ...], float], subprocess.CompletedProcess[str]
]

_UPDATE_TIMEOUT_SECONDS = 15 * 60
_PROBE_TIMEOUT_SECONDS = 30
_SAFE_VERSION = re.compile(r"[ -~]{1,120}")
_CODEX_INSTALLER_URL = "https://chatgpt.com/codex/install.sh"


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
            if provider == "claude" and before_path is None:
                raise ProviderUpdateRefused(
                    "Claude Code is not installed for rcp. Use the documented provider install "
                    "command, then rerun this update."
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

        emitter.emit_step(
            _running(pending[2], "Checking the updated executable, version, and existing login.")
        )
        try:
            after_path = _discover_provider(account, provider)
            if after_path is None:
                raise ProviderUpdateRefused(
                    "The updater completed but no provider executable is discoverable as rcp."
                )
            after_version = _provider_version(account, after_path, process_runner)
            authenticated = _provider_authenticated(
                account,
                provider,
                after_path,
                process_runner,
            )
            if not authenticated:
                login = _server_login_command(provider, after_path)
                emitter.emit_step(
                    pending[2].model_copy(
                        update={
                            "state": "operator_action_needed",
                            "performed_by": "human",
                            "message": (
                                "The provider updated, but its native login is not usable as rcp. "
                                "Complete provider login, then rerun this command."
                            ),
                            "actions": (
                                CommandAction(
                                    argv=(
                                        "sudo",
                                        "-u",
                                        layout.service_account,
                                        "-H",
                                        *login,
                                    )
                                ),
                            ),
                            "resume_argv": (
                                str(layout.cli_wrapper),
                                "server",
                                "provider",
                                "update",
                                provider,
                            ),
                            "fields": (
                                NonsecretField(name="executable_after", value=str(after_path)),
                                NonsecretField(name="version_after", value=after_version),
                            ),
                        }
                    )
                )
                return
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
                        NonsecretField(name="authentication", value="ready"),
                    ),
                }
            )
        )

    return PreparedServerCommand(
        plan=ServerPlanEvent(command=request.command, timestamp=datetime.now(UTC), steps=pending),
        execute=execute,
    )


def _pending_steps(provider: ProviderUpdateId, target: MachineTarget) -> tuple[ServerStep, ...]:
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
            purpose="Prove the updated executable, version, and native authentication.",
            phase="provider_update_verify",
            expected_success=f"{label} is executable and authenticated under the rcp account.",
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


def _discover_provider(account: pwd.struct_passwd, provider: ProviderUpdateId) -> Path | None:
    search = (
        Path(account.pw_dir) / ".local" / "bin",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
    )
    for directory in search:
        candidate = directory / provider
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


def _provider_authenticated(
    account: pwd.struct_passwd,
    provider: ProviderUpdateId,
    binary: Path,
    runner: ProviderProcessRunner,
) -> bool:
    command = tuple(profile_for(provider).auth_command(str(binary)))
    return profile_for(provider).is_authenticated(runner(account, command, _PROBE_TIMEOUT_SECONDS))


def _update_provider(
    account: pwd.struct_passwd,
    provider: ProviderUpdateId,
    before_path: Path | None,
    runner: ProviderProcessRunner,
) -> None:
    if provider == "claude":
        assert before_path is not None
        result = runner(account, (str(before_path), "update"), _UPDATE_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise ProviderUpdateRefused(
                f"Claude's native update failed: {_bounded_diagnostic(result)}"
            )
        return
    temporary = Path(tempfile.mkdtemp(prefix="rcp-provider-codex-"))
    try:
        os.chown(temporary, account.pw_uid, account.pw_gid)
        os.chmod(temporary, 0o700)
        installer = temporary / "install.sh"
        downloaded = runner(
            account,
            ("/usr/bin/curl", "-fsSL", _CODEX_INSTALLER_URL, "-o", str(installer)),
            _UPDATE_TIMEOUT_SECONDS,
        )
        if downloaded.returncode != 0:
            raise ProviderUpdateRefused(
                f"Codex's official installer download failed: {_bounded_diagnostic(downloaded)}"
            )
        installed = runner(
            account,
            ("/usr/bin/env", "CODEX_NON_INTERACTIVE=1", "/bin/sh", str(installer)),
            _UPDATE_TIMEOUT_SECONDS,
        )
        if installed.returncode != 0:
            raise ProviderUpdateRefused(
                f"Codex's official installer failed: {_bounded_diagnostic(installed)}"
            )
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


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


def _server_login_command(provider: ProviderUpdateId, binary: Path) -> tuple[str, ...]:
    if provider == "codex":
        return (str(binary), "login", "--device-auth")
    return tuple(profile_for(provider).login_command(str(binary)))


def _success_message(
    provider: ProviderUpdateId,
    before_path: Path | None,
    after_path: Path,
) -> str:
    result = f"{profile_for(provider).label} is updated and authenticated as rcp."
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
