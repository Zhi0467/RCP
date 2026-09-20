"""Shared terminal mount profile, also shipped to the execution machine."""

from __future__ import annotations

import os
from pathlib import Path

_READY_MARKER = b"\x1ercp-terminal-ready\x1f"
_SHELL_PREFLIGHT = r"""
rcp_expansion_probe=intact
case "$rcp_expansion_probe" in
    intact) ;;
    *) printf '%s\n' 'Terminal launch refused: this service manager rewrote the containment preflight.' >&2
       exit 1 ;;
esac
if ! test -w "$PWD"; then
    printf '%s\n' 'Terminal repository is not writable under the required mount profile.' >&2
    exit 1
fi
if ! command -v findmnt >/dev/null; then
    printf '%s\n' 'findmnt is required to verify canonical-state read-only mounts.' >&2
    exit 1
fi
for protected_path do
    if ! test -e "$protected_path" || test -w "$protected_path"; then
        printf '%s\n' 'Terminal canonical-state read-only mounts are unavailable.' >&2
        exit 1
    fi
    case ",$(findmnt --noheadings --output VFS-OPTIONS --target "$protected_path")," in
        *,ro,*) ;;
        *) printf '%s\n' 'Terminal canonical-state mount verification failed.' >&2; exit 1 ;;
    esac
done
printf '\036rcp-terminal-ready\037'
exec /bin/bash --noprofile --norc -i
"""


def launch_command(
    *,
    unit: str,
    repository: Path,
    protected_paths: list[str],
    git_read_paths: tuple[str, ...],
    git_environment: dict[str, str],
    empty_directory: Path,
    stop_timeout: float,
    expand_environment_option: bool = True,
) -> list[str]:
    """Build the required profile; failed properties refuse launch.

    ``--expand-environment=no`` arrived in systemd 254. An older manager
    rejects the option outright, so a launch there omits it and instead
    refuses any path or value carrying ``$``, which that manager would expand.
    Refusing is the fail-closed half: never launch with an expansion we cannot
    turn off. The screen covers values this process chooses; the preflight's
    own first lines catch a manager that expands the script itself, whatever
    version reports it.
    """
    if not expand_environment_option:
        expandable = [
            value
            for value in (
                str(repository),
                str(empty_directory),
                *protected_paths,
                *git_read_paths,
                *git_environment.values(),
            )
            if "$" in value
        ]
        if expandable:
            raise ValueError(
                "This systemd is older than 254 and expands `$` in unit settings, "
                f"which would change {expandable[0]!r}. Upgrade systemd or remove "
                "the dollar sign from the registered path."
            )
    properties = [
        "PrivateUsers=yes",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=tmpfs",
        "NoNewPrivileges=yes",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        f"TimeoutStopSec={stop_timeout}",
        f"BindPaths={_path(str(repository))}",
        "StandardOutput=tty",
        "StandardError=tty",
    ]
    for path in git_read_paths:
        properties.extend([f"BindReadOnlyPaths={_path(path)}", f"ReadOnlyPaths={_path(path)}"])
    for path in protected_paths:
        if Path(path).exists():
            properties.extend([f"BindReadOnlyPaths={_path(path)}", f"ReadOnlyPaths={_path(path)}"])
        else:
            # Mount a read-only empty directory rather than ignoring an absent
            # deny and allowing the shell to create writable canonical state.
            properties.append(f"BindReadOnlyPaths={_path(str(empty_directory))}:{_path(path)}")
    command = [
        "systemd-run",
        "--user",
        "--pty",
        "--wait",
        "--collect",
        "--quiet",
        "--service-type=exec",
        f"--unit={unit}",
        f"--working-directory={_specifier_safe(str(repository))}",
    ]
    if expand_environment_option:
        command.insert(1, "--expand-environment=no")
    for property_value in properties:
        command.extend(["--property", property_value])
    command.extend(["--", *shell_environment(git_environment)])
    command.extend(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            _SHELL_PREFLIGHT,
            "rcp-terminal",
            *protected_paths,
        ]
    )
    return command


def shell_environment(git_environment: dict[str, str]) -> list[str]:
    # Discard ambient provider secrets for either backend. Git receives only
    # its concrete repository access settings.
    environment = {
        "HOME": str(Path.home()),
        "USER": os.environ.get("USER", "rcp"),
        "LOGNAME": os.environ.get("LOGNAME", "rcp"),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "HISTFILE": "/dev/null",
        **git_environment,
    }
    return ["/usr/bin/env", "-i", *(f"{name}={value}" for name, value in environment.items())]


def _specifier_safe(value: str) -> str:
    """systemd expands `%` specifiers in unit settings; `%%` is a literal one.

    A repository path containing `%h` would otherwise resolve to the account's
    home rather than the registered path, or fail admission outright.
    """
    return value.replace("%", "%%")


def _path(value: str) -> str:
    if any(character in value for character in (":", "\n", "\r", "\0")):
        raise ValueError("Terminal mount paths cannot contain colons or control characters.")
    quoted = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + _specifier_safe(quoted) + '"'
