"""Host bootstrap for promoted release artifacts and an independent supervisor."""

from __future__ import annotations

import grp
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import uuid4

from rcp.limits import (
    SERVER_INSTALL_ACCOUNT_TIMEOUT_SECONDS,
    SERVER_INSTALL_BUILD_TIMEOUT_SECONDS,
    SERVER_INSTALL_PROBE_TIMEOUT_SECONDS,
    SERVER_INSTALL_SERVICE_TIMEOUT_SECONDS,
    SERVER_INSTALL_SOURCE_TIMEOUT_SECONDS,
)
from rcp.server_ops._local_primitives import fsync_directory as _fsync_directory
from rcp.server_ops.cli import CallerIdentity, PreparedServerCommand, ServerEventEmitter
from rcp.server_ops.config import (
    create_installed_server_config,
    load_installed_server_config,
    write_installed_server_config,
)
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT, ServerLayout, server_service_unit_text
from rcp.server_ops.models import ServerCommandRequest, ServerPlanEvent

_VERSION = re.compile(r"(?:^|\s|v)(?P<major>\d+)\.(?P<minor>\d+)(?:\.\d+)?")
_WRAPPER_MODE = 0o755
_UNIT_MODE = 0o644
_SERVICE_DIRECTORY_MODE = 0o700
_CONFIG_DIRECTORY_MODE = 0o750


class InstallRefused(RuntimeError):
    """A safe bootstrap refusal suitable for operator output."""


@dataclass(frozen=True)
class HostFacts:
    ubuntu_release: Literal["22.04", "24.04"]
    architecture: Literal["x86_64"] = "x86_64"


def prepare_install_command(
    request: ServerCommandRequest,
    identity: CallerIdentity,
    *,
    machine: LinuxInstallMachine | None = None,
) -> PreparedServerCommand:
    """Initial bootstrap explicitly carries both wheels from the same stable release."""
    from rcp.server_ops.supervisor_client import execute_supervisor_command

    resolved = machine or LinuxInstallMachine()
    try:
        import rcp_supervisor
        from rcp_supervisor.events import plan as supervisor_plan
        from rcp_supervisor.releases import fetch_release
    except ImportError as exc:
        raise InstallRefused(
            "Bootstrap requires the RCP and supervisor wheels from the same promoted release; use the documented two-wheel uv command."
        ) from exc
    plan = ServerPlanEvent.model_validate_json(json.dumps(supervisor_plan(request.command)))

    def execute(emitter: ServerEventEmitter, _input_stream: BinaryIO) -> None:
        pending = plan.steps[0]
        emitter.emit_step(
            pending.model_copy(
                update={
                    "state": "running",
                    "message": "Preparing the independent supervisor and dedicated service account.",
                }
            )
        )
        try:
            resolved.validate_host()
            resolved.converge_account_and_layout()
            for directory in (
                resolved.layout.supervisor_root,
                resolved.layout.supervisor_bundles_root,
            ):
                _converge_directory(directory, uid=0, gid=0, mode=0o755)
            bundle = fetch_release(
                "stable", resolved.layout.supervisor_bundles_root / f"bootstrap-{uuid4()}"
            )
            from rcp import __version__

            if (
                bundle.version != __version__
                or bundle.supervisor_version != rcp_supervisor.__version__
            ):
                raise InstallRefused(
                    "Bootstrap wheels no longer match the promoted release. Restart using the current paired release wheels."
                )
            resolved.bootstrap_supervisor(bundle)
            initialized_source = (
                resolved._data_state() == "initialized"
                and not resolved.layout.selected_release_receipt.exists()
            )
            resolved.stage_supervisor_integration()
            if not initialized_source:
                if not resolved.layout.config_path.exists():
                    write_installed_server_config(
                        create_installed_server_config(), resolved.layout.config_path
                    )
                resolved.converge_supervisor_integration()
            execute_supervisor_command(
                request, emitter, layout=resolved.layout, already_running=True
            )
        except (OSError, RuntimeError, ValueError) as exc:
            emitter.emit_step(pending.model_copy(update={"state": "failed", "message": str(exc)}))

    return PreparedServerCommand(plan=plan, execute=execute)


class LinuxInstallMachine:
    """Converge host prerequisites; release transactions belong to the supervisor."""

    def __init__(self, layout: ServerLayout = DEFAULT_SERVER_LAYOUT) -> None:
        self.layout = layout
        self._service_uid: int | None = None
        self._service_gid: int | None = None

    def validate_host(self) -> HostFacts:
        os_release = _read_os_release(Path("/etc/os-release"))
        release = os_release.get("VERSION_ID")
        if os_release.get("ID") != "ubuntu" or release not in {"22.04", "24.04"}:
            raise InstallRefused(
                "Install supports only Ubuntu 22.04 or 24.04; no machine state was changed."
            )
        if platform.machine() != "x86_64":
            raise InstallRefused(
                "Install supports only x86-64 hosts; no machine state was changed."
            )
        if not Path("/run/systemd/system").is_dir():
            raise InstallRefused(
                "systemd is not the running service manager. Boot this Ubuntu host with systemd "
                "and rerun the same install command."
            )
        for command in (
            "age",
            "age-keygen",
            "curl",
            "git",
            "loginctl",
            "runuser",
            "ssh",
            "ssh-keygen",
            "sudo",
            "systemctl",
            "useradd",
            "uv",
        ):
            if shutil.which(command) is None:
                raise InstallRefused(
                    f"Required tool {command} is missing. Install it for all users, then rerun "
                    "the same server install command."
                )
        _require_command(("systemctl", "--version"), "systemd could not be executed")
        manager = _require_command(
            ("systemctl", "show", "--property=Version", "--value"),
            "The running systemd manager could not be reached. Boot this Ubuntu host with "
            "systemd as PID 1 and rerun the same install command.",
        )
        if not manager.stdout.strip():
            raise InstallRefused(
                "The running systemd manager returned no version. Boot this Ubuntu host with "
                "systemd as PID 1 and rerun the same install command."
            )
        age = _require_command(("age", "--version"), "age could not be executed")
        age_version = _major_minor(f"{age.stdout} {age.stderr}")
        if age_version[0] != 1:
            raise InstallRefused(
                "age >=1.0.0,<2.0.0 is required. Install age 1.x for all users and rerun the "
                "same command."
            )
        return HostFacts(ubuntu_release=release)

    def converge_account_and_layout(self) -> None:
        account = self._converge_account()
        for argv in (
            ("loginctl", "enable-linger", account.pw_name),
            ("loginctl", "show-user", account.pw_name, "--property=Linger"),
        ):
            result = _run_process(argv, timeout=SERVER_INSTALL_SERVICE_TIMEOUT_SECONDS)
            if result.returncode != 0:
                raise InstallRefused(
                    f"{shlex.join(argv)} failed: {' '.join(result.stderr.split())}. "
                    "Repair service-account linger and rerun install."
                )
        if result.stdout.strip() != "Linger=yes":
            raise InstallRefused(
                f"{shlex.join(argv)} did not report Linger=yes: "
                f"{' '.join(result.stdout.split())}; {' '.join(result.stderr.split())}. "
                "Repair service-account linger and rerun install."
            )
        self._service_uid = account.pw_uid
        self._service_gid = account.pw_gid
        _converge_directory(
            self.layout.service_home,
            uid=account.pw_uid,
            gid=account.pw_gid,
            mode=_SERVICE_DIRECTORY_MODE,
        )
        for path in (
            self.layout.server_root,
            self.layout.releases_root,
            self.layout.data_dir,
            self.layout.projects_root,
            self.layout.credentials_root,
            self.layout.update_checkpoints_root,
            self.layout.codex_state_root,
            self.layout.claude_state_root,
            self.layout.ssh_state_root,
        ):
            _converge_directory(
                path,
                uid=account.pw_uid,
                gid=account.pw_gid,
                mode=_SERVICE_DIRECTORY_MODE,
            )
        _converge_directory(
            self.layout.config_path.parent,
            uid=0,
            gid=account.pw_gid,
            mode=_CONFIG_DIRECTORY_MODE,
        )
        self._validate_service_tooling()

    def _converge_account(self) -> pwd.struct_passwd:
        try:
            account = pwd.getpwnam(self.layout.service_account)
        except KeyError:
            account_creation = _run_process(
                (
                    "useradd",
                    "--create-home",
                    "--home-dir",
                    str(self.layout.service_home),
                    "--shell",
                    "/bin/bash",
                    "--user-group",
                    "--password",
                    "*NP*",
                    self.layout.service_account,
                ),
                timeout=SERVER_INSTALL_ACCOUNT_TIMEOUT_SECONDS,
            )
            if account_creation.returncode != 0:
                if account_creation.stderr == "command timed out":
                    raise InstallRefused(
                        "Creating the dedicated rcp account did not finish within five minutes. "
                        "Inspect useradd, NSS, and home-directory policy, then rerun."
                    ) from None
                raise InstallRefused(
                    "Creating the dedicated rcp account failed. Inspect useradd policy and rerun."
                ) from None
            try:
                account = pwd.getpwnam(self.layout.service_account)
            except KeyError as exc:  # pragma: no cover - broken NSS after successful useradd
                raise InstallRefused(
                    "useradd returned success but the rcp account is unavailable."
                ) from exc
        if account.pw_uid == 0 or account.pw_gid == 0:
            raise InstallRefused(
                "The rcp account has root user or group identity. Replace it with a dedicated "
                "unprivileged rcp account, then rerun install."
            )
        if account.pw_dir != str(self.layout.service_home) or account.pw_shell != "/bin/bash":
            raise InstallRefused(
                "The existing rcp account does not use exact home /home/rcp and shell /bin/bash. "
                "Install will not rewrite the account."
            )
        try:
            primary_group = grp.getgrgid(account.pw_gid)
        except KeyError as exc:
            raise InstallRefused("The rcp account has no resolvable primary group.") from exc
        if primary_group.gr_name != self.layout.service_account:
            raise InstallRefused(
                "The existing rcp account does not use its dedicated rcp primary group. Install "
                "will not rewrite the account."
            )
        shadow = _require_command(
            ("getent", "shadow", self.layout.service_account),
            "The rcp shadow entry could not be read. Run install as root and inspect NSS.",
        ).stdout.strip()
        parts = shadow.split(":")
        if len(parts) < 2 or parts[0] != self.layout.service_account or parts[1] != "*NP*":
            raise InstallRefused(
                "The rcp account must have exact unusable non-locking shadow value *NP*. Install "
                "will not change an existing password state."
            )
        supplemental = sorted(
            group.gr_name
            for group in grp.getgrall()
            if group.gr_gid != account.pw_gid and self.layout.service_account in group.gr_mem
        )
        if supplemental:
            raise InstallRefused(
                "The rcp account has supplemental groups. Remove all supplemental memberships "
                "after reviewing them, then rerun install."
            )
        sudo_policy = _run_process(
            ("sudo", "-n", "-U", account.pw_name, "-l"),
            environment={"LANG": "C", "LC_ALL": "C"},
            timeout=SERVER_INSTALL_PROBE_TIMEOUT_SECONDS,
        )
        diagnostic = f"{sudo_policy.stdout}\n{sudo_policy.stderr}".lower()
        if "not allowed to run sudo" in diagnostic:
            return account
        if sudo_policy.returncode == 0:
            raise InstallRefused(
                "The rcp account has sudo authority. Remove every sudo grant for rcp and "
                "rerun install."
            )
        raise InstallRefused(
            "RCP could not prove that the rcp account has no sudo authority. Run "
            "sudo -U rcp -l as root, correct the sudo or NSS error, and rerun install."
        )

    def _validate_service_tooling(self) -> None:
        account = pwd.getpwnam(self.layout.service_account)
        checks = (
            (("git", "--version"), "Git is not executable as rcp."),
            (("ssh", "-V"), "SSH is not executable as rcp."),
            (("uv", "--version"), "uv is not executable as rcp."),
            (("age", "--version"), "age is not executable as rcp."),
        )
        for argv, message in checks:
            result = _run_as_account(
                account,
                argv,
                timeout=SERVER_INSTALL_PROBE_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                raise InstallRefused(
                    f"{message} Install the tool for all users and rerun the same command."
                )
        python = _run_as_account(
            account,
            (
                "uv",
                "python",
                "find",
                "--managed-python",
                "--no-python-downloads",
                "3.12",
            ),
            timeout=SERVER_INSTALL_PROBE_TIMEOUT_SECONDS,
        )
        if python.returncode != 0:
            installed = _run_as_account(
                account,
                ("uv", "python", "install", "--managed-python", "--no-progress", "3.12"),
                timeout=SERVER_INSTALL_SOURCE_TIMEOUT_SECONDS,
                capture_output=True,
            )
            if installed.returncode != 0:
                raise InstallRefused(
                    "uv could not install the managed Python 3.12 runtime for rcp. Run the same "
                    "uv python install command as rcp from /home/rcp, correct the reported host "
                    "or network issue, and rerun install."
                )
            python = _run_as_account(
                account,
                (
                    "uv",
                    "python",
                    "find",
                    "--managed-python",
                    "--no-python-downloads",
                    "3.12",
                ),
                timeout=SERVER_INSTALL_PROBE_TIMEOUT_SECONDS,
            )
        if python.returncode != 0:
            raise InstallRefused(
                "uv installed Python 3.12 but could not find it again as rcp. Inspect "
                "/home/rcp ownership and rerun the same command."
            )
        python_path = Path(python.stdout.strip())
        if not python_path.is_absolute():
            raise InstallRefused("uv returned a non-absolute Python 3.12 runtime path for rcp.")
        version = _run_as_account(
            account,
            (str(python_path), "--version"),
            timeout=SERVER_INSTALL_PROBE_TIMEOUT_SECONDS,
        )
        if version.returncode != 0 or not (version.stdout or version.stderr).startswith(
            "Python 3.12."
        ):
            raise InstallRefused(
                "The uv-selected service runtime is not Python 3.12. Install the correct runtime "
                "for rcp and rerun."
            )

    def _run_as_service(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
        timeout: float,
        error: str | None = None,
        check: bool = True,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        account = pwd.getpwnam(self.layout.service_account)
        result = _run_as_account(
            account,
            argv,
            cwd=cwd,
            environment=environment,
            timeout=timeout,
            capture_output=capture_output,
        )
        if check and result.returncode != 0:
            raise InstallRefused(error or "A managed command failed; inspect the host and rerun.")
        return result

    def _data_state(self) -> Literal["fresh", "initialized"]:
        _require_owned_directory(
            self.layout.data_dir,
            uid=self._service_uid_value,
            gid=self._service_gid_value,
        )
        database = self.layout.data_dir / "rcp.sqlite3"
        if database.exists() or database.is_symlink():
            if database.is_symlink() or not database.is_file():
                raise InstallRefused("The RCP database path is not a regular owned file.")
            info = database.stat()
            if (info.st_uid, info.st_gid) != (self._service_uid_value, self._service_gid_value):
                raise InstallRefused("The RCP database is not owned by the dedicated rcp account.")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise InstallRefused(
                    "The RCP database is readable or writable outside the rcp account."
                )
            return "initialized"
        if any(self.layout.data_dir.iterdir()):
            raise InstallRefused(
                "The data directory has files but no initialized RCP database. Install will not "
                "remove or adopt unknown data."
            )
        return "fresh"

    def _require_service_identity(self) -> None:
        if self._service_uid is None or self._service_gid is None:
            account = pwd.getpwnam(self.layout.service_account)
            self._service_uid = account.pw_uid
            self._service_gid = account.pw_gid

    @property
    def _service_uid_value(self) -> int:
        self._require_service_identity()
        assert self._service_uid is not None
        return self._service_uid

    @property
    def _service_gid_value(self) -> int:
        self._require_service_identity()
        assert self._service_gid is not None
        return self._service_gid

    def bootstrap_supervisor(self, bundle) -> Path:
        from rcp_supervisor.install import install_operator_console, install_supervisor

        _converge_directory(self.layout.supervisor_root, uid=0, gid=0, mode=0o755)
        previous_umask = os.umask(0o022)
        try:
            venv = install_supervisor(bundle.directory, self.layout.supervisor_root)
            install_operator_console(bundle.directory, self.layout.supervisor_root)
        finally:
            os.umask(previous_umask)
        pointer = self.layout.supervisor_current
        if pointer.exists() or pointer.is_symlink():
            if not pointer.is_symlink() or pointer.lstat().st_uid != 0:
                raise InstallRefused("The supervisor current pointer is not root-owned.")
            # Explicit self-update, never bootstrap, changes an existing pointer.
        else:
            temporary = pointer.with_name(".current.bootstrap")
            temporary.symlink_to(venv)
            os.replace(temporary, pointer)
            _fsync_directory(pointer.parent)
        _install_root_file(
            self.layout.supervisor_wrapper,
            "#!/bin/sh\nset -eu\numask 077\nexec "
            + str(self.layout.supervisor_current / "bin/rcp-supervisor")
            + ' "$@"\n',
            mode=0o755,
        )
        lock = _run_as_account(
            pwd.getpwnam(self.layout.service_account),
            (
                str(venv / "bin/python"),
                "-I",
                "-m",
                "rcp_supervisor.fs_worker",
                "prepare-deployment-lock",
            ),
            input_text=json.dumps({"directory": str(self.layout.server_root)}),
            timeout=SERVER_INSTALL_PROBE_TIMEOUT_SECONDS,
        )
        if lock.returncode:
            raise InstallRefused("The shared private backup/deployment lock could not be prepared.")
        return venv

    def stage_supervisor_integration(self) -> None:
        """Seal root-generated integration for the independent adoption coordinator."""
        from rcp.server_ops.config import render_installed_server_config

        config = (
            load_installed_server_config(self.layout.config_path)
            if self.layout.config_path.exists()
            else create_installed_server_config()
        )
        files = {
            "config": {
                "text": render_installed_server_config(config),
                "mode": 0o640,
                "gid": self._service_gid_value,
            },
            "unit": {"text": server_service_unit_text(), "mode": 0o644, "gid": 0},
            "wrapper": {"text": _wrapper_text(self.layout), "mode": 0o755, "gid": 0},
        }
        _install_root_file(
            self.layout.supervisor_root / "integration.json",
            json.dumps({"version": 1, "files": files}, sort_keys=True) + "\n",
            mode=0o644,
            replace_existing=True,
        )

    def converge_supervisor_integration(self) -> None:
        """Converge launch files without selecting a release or starting a service."""
        _install_root_file(
            self.layout.cli_wrapper,
            _wrapper_text(self.layout),
            mode=_WRAPPER_MODE,
            replace_existing=True,
        )
        _install_root_file(
            self.layout.systemd_unit,
            server_service_unit_text(),
            mode=_UNIT_MODE,
            replace_existing=True,
        )
        from rcp.server_ops.backup_config import (
            backup_configuration_lock,
            backup_service_unit_text,
            recover_pending_backup_configuration,
            render_backup_timer_unit,
        )

        with backup_configuration_lock(self.layout):
            recover_pending_backup_configuration(self.layout)
            config = load_installed_server_config(self.layout.config_path)
            schedule = config.backup.schedule if config.backup is not None else None
            fence_backup_timer_before_unit_change()
            install_backup_unit_files(
                service_content=backup_service_unit_text(),
                timer_content=render_backup_timer_unit(schedule),
                layout=self.layout,
            )
            _require_command(
                ("systemctl", "daemon-reload"),
                "systemd could not reload the supervisor integration.",
                timeout=SERVER_INSTALL_SERVICE_TIMEOUT_SECONDS,
            )
            _fence_service_stopped_disabled("rcp-backup.timer")


def _wrapper_text(layout: ServerLayout) -> str:
    return (
        "#!/bin/sh\nset -eu\numask 077\n"
        f"export RCP_DATA_DIR={layout.data_dir}\n"
        'route=""\ncount=0\n'
        'for argument in "$@"; do\n'
        '  case "$argument" in --machine-readable|--plan) continue;; esac\n'
        '  route="${route}${route:+ }${argument}"\n'
        "  count=$((count + 1))\n"
        '  [ "$count" -lt 3 ] || break\n'
        "done\n"
        'case "$route" in\n'
        '  "server backup configure"|"server provider update")\n'
        f'    exec {layout.supervisor_wrapper} operator "$@";;\n'
        '  "server install"|"server install "*|"server update"|"server update "*|"server restore"|"server restore "*|"server supervisor update")\n'
        f'    exec {layout.supervisor_wrapper} "$@";;\n'
        "esac\n"
        f'exec {layout.supervisor_wrapper} launch "$@"\n'
    )


def _read_os_release(path: Path) -> dict[str, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallRefused(
            "Ubuntu release metadata could not be read from /etc/os-release."
        ) from exc
    values: dict[str, str] = {}
    for line in content.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if value.startswith(('"', "'")) and value.endswith(value[:1]):
            value = value[1:-1]
        values[name] = value
    return values


def _major_minor(value: str) -> tuple[int, int]:
    match = _VERSION.search(value)
    if match is None:
        raise InstallRefused("A required tool returned an unrecognized semantic version.")
    return int(match.group("major")), int(match.group("minor"))


def _run_process(
    argv: tuple[str, ...],
    *,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    timeout: float,
    capture_output: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_environment = os.environ.copy()
    for name in ("SUDO_COMMAND", "SUDO_GID", "SUDO_UID", "SUDO_USER"):
        merged_environment.pop(name, None)
    if environment:
        merged_environment.update(environment)
    try:
        output = (
            {"capture_output": True}
            if capture_output
            else {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
        )
        if input_text is not None:
            output["input"] = input_text
        return subprocess.run(
            argv,
            cwd=cwd,
            env=merged_environment,
            text=True,
            timeout=timeout,
            check=False,
            **output,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 126, "", "command timed out")
    except OSError:
        return subprocess.CompletedProcess(argv, 126, "", "command could not be executed")


def _require_command(
    argv: tuple[str, ...],
    error: str,
    *,
    timeout: float = SERVER_INSTALL_PROBE_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    result = _run_process(argv, timeout=timeout)
    if result.returncode != 0:
        raise InstallRefused(error)
    return result


def _read_systemd_property(unit: str, property_name: str) -> str:
    result = _require_command(
        ("systemctl", "show", f"--property={property_name}", "--value", unit),
        f"systemd could not read {property_name} for {unit}. Inspect the unit and rerun install.",
        timeout=SERVER_INSTALL_SERVICE_TIMEOUT_SECONDS,
    )
    value = result.stdout.strip()
    if not value or "\n" in value:
        raise InstallRefused(
            f"systemd returned an invalid {property_name} for {unit}. Inspect the unit and "
            "rerun install."
        )
    return value


def _fence_service_stopped_disabled(unit: str) -> None:
    _require_command(
        ("systemctl", "disable", "--now", unit),
        f"systemd could not stop and disable {unit}. Run systemctl disable --now {unit}, "
        "inspect the failure, and rerun install.",
        timeout=SERVER_INSTALL_SERVICE_TIMEOUT_SECONDS,
    )
    active = _read_systemd_property(unit, "ActiveState")
    enabled = _read_systemd_property(unit, "UnitFileState")
    if active != "inactive" or enabled != "disabled":
        raise InstallRefused(
            f"RCP could not prove {unit} is stopped and disabled: ActiveState={active}, "
            f"UnitFileState={enabled}. Run systemctl disable --now {unit}, verify both states, "
            "and rerun install."
        )


def _run_as_account(
    account: pwd.struct_passwd,
    argv: tuple[str, ...],
    *,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    timeout: float,
    capture_output: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    explicit_environment = {
        "HOME": account.pw_dir,
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        "PATH": (
            f"{account.pw_dir}/.local/bin:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if environment:
        explicit_environment.update(environment)
    env_argv = tuple(f"{name}={value}" for name, value in explicit_environment.items())
    options = {} if input_text is None else {"input_text": input_text}
    return _run_process(
        ("runuser", "--user", account.pw_name, "--", "env", "-i", *env_argv, *argv),
        cwd=cwd or Path(account.pw_dir),
        timeout=timeout,
        capture_output=capture_output,
        **options,
    )


def _converge_directory(path: Path, *, uid: int, gid: int, mode: int) -> None:
    _reject_symlink_ancestry(path.parent)
    if path.is_symlink():
        raise InstallRefused(f"Managed directory {path} is a symlink; install will not follow it.")
    if not path.exists():
        try:
            path.mkdir()
            os.chown(path, uid, gid)
            os.chmod(path, mode)
        except OSError as exc:
            raise InstallRefused(f"Managed directory {path} could not be created safely.") from exc
        return
    _require_owned_directory(path, uid=uid, gid=gid)
    if stat.S_IMODE(path.stat().st_mode) != mode:
        try:
            os.chmod(path, mode)
        except OSError as exc:
            raise InstallRefused(
                f"Managed directory {path} could not be set to its exact mode."
            ) from exc


def _require_owned_directory(path: Path, *, uid: int, gid: int) -> None:
    _reject_symlink_ancestry(path.parent)
    if path.is_symlink() or not path.is_dir():
        raise InstallRefused(f"Managed path {path} is not a regular directory.")
    info = path.stat()
    if (info.st_uid, info.st_gid) != (uid, gid):
        raise InstallRefused(f"Managed directory {path} has unexpected ownership.")


def _require_owned_file(
    path: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
    label: str,
) -> None:
    _reject_symlink_ancestry(path.parent)
    if path.is_symlink() or not path.is_file():
        raise InstallRefused(f"The {label} is not a regular file.")
    info = path.stat()
    if (info.st_uid, info.st_gid) != (uid, gid) or stat.S_IMODE(info.st_mode) != mode:
        raise InstallRefused(f"The {label} has unexpected ownership or mode.")


def _set_owned_file_mode_no_follow(
    path: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
    label: str,
) -> None:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (info.st_uid, info.st_gid) != (uid, gid):
            raise InstallRefused(f"The {label} has unexpected type or ownership.")
        os.fchmod(descriptor, mode)
    except OSError as exc:
        raise InstallRefused(f"The {label} could not be secured without following links.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _reject_symlink_ancestry(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise InstallRefused(f"Managed path ancestry contains a symlink at {candidate}.")


def _install_root_file(
    path: Path,
    content: str,
    *,
    mode: int,
    replace_existing: bool = False,
    owner_identity: tuple[int, int] = (0, 0),
) -> None:
    owner_uid, owner_gid = owner_identity
    _reject_symlink_ancestry(path.parent)
    if path.is_symlink():
        raise InstallRefused(f"Root-managed file {path} is a symlink; install will not replace it.")
    if path.exists():
        if not path.is_file():
            raise InstallRefused(f"Root-managed path {path} is not a regular file.")
        info = path.stat()
        if (info.st_uid, info.st_gid) != owner_identity:
            raise InstallRefused(f"Root-managed file {path} has unexpected ownership.")
        encoded_size = len(content.encode("utf-8"))
        differs = (
            stat.S_IMODE(info.st_mode) != mode
            or info.st_size != encoded_size
            or path.read_text(encoding="utf-8") != content
        )
        if not differs:
            return
        if not replace_existing:
            raise InstallRefused(
                f"Root-managed file {path} differs from this release. Use server update or "
                "restore the known file; install will not overwrite it."
            )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchown(descriptor, owner_uid, owner_gid)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise InstallRefused(
            f"Root-managed file {path} could not be installed atomically."
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def install_backup_unit_files(
    *,
    service_content: str,
    timer_content: str,
    layout: ServerLayout = DEFAULT_SERVER_LAYOUT,
) -> None:
    unit_root = layout.systemd_unit.parent
    _install_root_file(
        unit_root / "rcp-backup.service",
        service_content,
        mode=_UNIT_MODE,
    )
    _install_root_file(
        unit_root / "rcp-backup.timer",
        timer_content,
        mode=_UNIT_MODE,
        replace_existing=True,
    )


def fence_backup_timer_before_unit_change() -> None:
    load_state = _read_systemd_property("rcp-backup.timer", "LoadState")
    if load_state == "not-found":
        return
    _fence_service_stopped_disabled("rcp-backup.timer")


def reload_and_disable_backup_timer() -> None:
    _require_command(
        ("systemctl", "daemon-reload"),
        "systemd could not reload the backup units. Run systemctl daemon-reload, inspect "
        "rcp-backup.service and rcp-backup.timer, then rerun backup configure.",
        timeout=SERVER_INSTALL_SERVICE_TIMEOUT_SECONDS,
    )
    _fence_service_stopped_disabled("rcp-backup.timer")


def run_backup_service_once() -> None:
    """Run one first backup while the timer remains fenced off."""

    _require_command(
        ("systemctl", "start", "rcp-backup.service"),
        "The first protected backup failed. The timer remains disabled; inspect systemctl "
        "status --no-pager rcp-backup.service and backup-status.json, then rerun backup configure.",
        timeout=SERVER_INSTALL_BUILD_TIMEOUT_SECONDS,
    )
    result = _read_systemd_property("rcp-backup.service", "Result")
    if result != "success":
        raise InstallRefused(
            "The first protected backup did not report a successful systemd result. The timer "
            "remains disabled; inspect rcp-backup.service and rerun backup configure."
        )


def enable_backup_timer() -> None:
    """Enable the rendered timer and prove the loaded systemd state."""

    _require_command(
        ("systemctl", "enable", "--now", "rcp-backup.timer"),
        "systemd could not enable the verified backup timer. Inspect rcp-backup.timer and rerun "
        "backup configure.",
        timeout=SERVER_INSTALL_SERVICE_TIMEOUT_SECONDS,
    )
    active, enabled = read_systemd_unit_state("rcp-backup.timer")
    if active != "active" or enabled != "enabled":
        raise InstallRefused(
            "The verified backup timer is not both active and enabled. Disable it, inspect the "
            "unit, and rerun backup configure."
        )


def read_systemd_unit_state(unit: str) -> tuple[str, str]:
    return _read_systemd_property(unit, "ActiveState"), _read_systemd_property(
        unit, "UnitFileState"
    )
