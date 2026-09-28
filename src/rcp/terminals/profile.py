"""Shared terminal mount profile, also shipped to the execution machine."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

SHELL_PATH = "/usr/local/bin:/usr/bin:/bin"

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
while [ "$#" -gt 0 ] && [ "$1" != -- ]; do
    if ! test -w "$1"; then
        printf '%s\n' 'Terminal writable path is not writable under the required mount profile.' >&2
        exit 1
    fi
    shift
done
if [ "$#" -eq 0 ]; then
    printf '%s\n' 'Terminal launch refused: the containment preflight lost its path list.' >&2
    exit 1
fi
shift
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


# A transient unit is built over D-Bus rather than parsed from a unit file, so
# systemd applies no `%` specifier expansion to it. Measured on the execution
# host (systemd 249): `%h` stays `%h` in both properties and command arguments,
# and doubling it instead breaks a real path — `--working-directory` with `%%`
# fails with "Changing to the requested working directory failed". Paths
# therefore reach systemd exactly as registered.
def launch_command(
    *,
    unit: str,
    repository: Path,
    protected_paths: list[str],
    git_read_paths: tuple[str, ...],
    git_environment: dict[str, str],
    empty_directory: Path,
    stop_timeout: float,
    granted_paths: Sequence[str] = (),
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

    ``granted_paths`` are the machine's canonical writable paths; RCP-owned
    paths inside them arrive in ``protected_paths`` and stay read-only.
    """
    # Git configuration is read-only only because nothing else grants it; a
    # grant covering it makes it writable like the rest of the grant. RCP's
    # own files (identities, deploy keys) stay mounted read-only.
    git_read_paths = tuple(
        path
        for path in git_read_paths
        if not any(_within(path, grant) for grant in granted_paths)
        or any(_within(path, protected) for protected in protected_paths)
    )
    if not expand_environment_option:
        expandable = [
            value
            for value in (
                str(repository),
                str(empty_directory),
                *granted_paths,
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
    properties.extend(f"BindPaths={_path(path)}" for path in granted_paths)
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
        f"--working-directory={repository}",
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
            *granted_paths,
            "--",
            *protected_paths,
        ]
    )
    return command


def resolve_grants(
    declared: list[str], owned: list[str], rules: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Canonicalize grants on this machine; return them and the paths to keep read-only.

    ``owned`` is everything a grant may not reopen: RCP's storage and the
    terminal's canonical-state denies. A grant inside one is refused; one a
    grant covers stays read-only, as does every legacy task stage in `/tmp`.
    ``rules`` is the shared grant-path module's namespace: this profile is also
    shipped as source, so it cannot import it.
    """
    grants = set()
    for raw in declared:
        resolved = Path(rules["check_writable_path_text"](raw)).resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"writable path is not a directory: {raw}")
        grants.add(rules["check_writable_path_text"](str(resolved)))
    legacy = legacy_stage_roots()
    canonical_owned = sorted({*owned, *(str(Path(path).resolve()) for path in owned), *legacy})
    ordered = sorted(grants)
    rules["refuse_grants_inside"](ordered, canonical_owned)
    covered = rules["owned_paths_covered"](ordered, canonical_owned)
    return ordered, sorted({*covered, *legacy})


def legacy_stage_roots() -> list[str]:
    """Task stages left in `/tmp` from before `~/.rcp/stages`; symlinks skipped.

    A later release removes this with the legacy stage location.
    """
    return sorted(
        str(path.resolve())
        for path in Path("/tmp").glob("rcp-run.*")
        if path.is_dir() and not path.is_symlink()
    )


def _within(child: str, parent: str) -> bool:
    return Path(child) == Path(parent) or Path(parent) in Path(child).parents


def shell_environment(git_environment: dict[str, str]) -> list[str]:
    # Discard ambient provider secrets for either backend. Git receives only
    # its concrete repository access settings.
    environment = {
        "HOME": str(Path.home()),
        "USER": os.environ.get("USER", "rcp"),
        "LOGNAME": os.environ.get("LOGNAME", "rcp"),
        "PATH": SHELL_PATH,
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "HISTFILE": "/dev/null",
        **git_environment,
    }
    return ["/usr/bin/env", "-i", *(f"{name}={value}" for name, value in environment.items())]


def _path(value: str) -> str:
    if any(character in value for character in (":", "\n", "\r", "\0")):
        raise ValueError("Terminal mount paths cannot contain colons or control characters.")
    quoted = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + quoted + '"'
