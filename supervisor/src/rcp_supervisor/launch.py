"""Launch the selected application from a root-owned release identity receipt."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.limits import MAX_SELECTED_RECEIPT_BYTES

SELECTED_RECEIPT = Path("/etc/rcp/supervisor/selected.json")
CURRENT_RELEASE = Path("/etc/rcp/current")
RELEASES_ROOT = Path("/home/rcp/rcp-server/releases")
_FIELDS = {
    "version",
    "release_tag",
    "version_string",
    "build",
    "commit",
    "manifest_sha256",
    "release_directory",
    "supervisor_version",
}


def _root_owned(path: Path, *, regular: bool) -> os.stat_result:
    info = path.lstat()
    if (
        info.st_uid != 0
        or info.st_mode & 0o022
        or not (stat.S_ISREG(info.st_mode) if regular else stat.S_ISDIR(info.st_mode))
        or (regular and info.st_nlink != 1)
    ):
        raise SupervisorError(
            "Selected-release authority must be root-owned and exclude other writers."
        )
    return info


def read_selected_receipt(
    path: Path = SELECTED_RECEIPT, *, releases_root: Path = RELEASES_ROOT
) -> dict:
    """Read one bounded root-owned receipt; never infer identity from a source tree."""
    try:
        if not path.is_absolute() or ".." in path.parts:
            raise SupervisorError("The selected-release receipt path is invalid.")
        for parent in reversed(path.parents):
            _root_owned(parent, regular=False)
        before = _root_owned(path, regular=True)
        if before.st_size > MAX_SELECTED_RECEIPT_BYTES:
            raise SupervisorError("Selected-release receipt exceeds its size limit.")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            if os.fstat(source.fileno()) != before:
                raise SupervisorError("Selected-release receipt changed while opening it.")
            payload = source.read(MAX_SELECTED_RECEIPT_BYTES + 1)
        if len(payload) > MAX_SELECTED_RECEIPT_BYTES:
            raise SupervisorError("Selected-release receipt exceeds its size limit.")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise SupervisorError("Selected-release receipt contains duplicate fields.")
                result[key] = value
            return result

        receipt = json.loads(payload, object_pairs_hook=unique)
        return validate_selected_receipt(receipt, releases_root=releases_root)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise SupervisorError(
            "The root-owned selected-release receipt is unavailable or invalid."
        ) from exc


def validate_selected_receipt(receipt: object, *, releases_root: Path = RELEASES_ROOT) -> dict:
    if (
        not isinstance(receipt, dict)
        or receipt.keys() != _FIELDS
        or type(receipt["version"]) is not int
        or receipt["version"] != 1
    ):
        raise SupervisorError("Selected-release receipt format is unsupported.")
    if type(receipt["build"]) is not int or receipt["build"] < 1:
        raise SupervisorError("Selected-release build identity is invalid.")
    for name, pattern in (
        ("release_tag", r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"),
        ("commit", r"[0-9a-f]{40}"),
        ("manifest_sha256", r"[0-9a-f]{64}"),
        ("supervisor_version", r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"),
    ):
        if not isinstance(receipt[name], str) or re.fullmatch(pattern, receipt[name]) is None:
            raise SupervisorError(f"Selected-release {name} is invalid.")
    expected_version = (
        f"{receipt['release_tag'][1:]}+build.{receipt['build']}.g{receipt['commit'][:7]}"
    )
    if receipt["version_string"] != expected_version or receipt["release_directory"] != str(
        releases_root / str(receipt["build"])
    ):
        raise SupervisorError("Selected-release receipt identities disagree.")
    return receipt


def main(argv: list[str] | None = None) -> int:
    try:
        if os.geteuid() == 0:
            raise SupervisorError(
                "Run application commands as rcp; root deployment commands use the supervisor directly."
            )
        receipt = read_selected_receipt()
        pointer = CURRENT_RELEASE.lstat()
        if (
            pointer.st_uid != 0
            or not stat.S_ISLNK(pointer.st_mode)
            or os.readlink(CURRENT_RELEASE) != receipt["release_directory"]
        ):
            raise SupervisorError("Current-release pointer differs from its selected receipt.")
        python = Path(receipt["release_directory"]) / ".venv" / "bin" / "python"
        environment = dict(os.environ)
        environment["RCP_DATA_DIR"] = "/home/rcp/rcp-server/data"
        environment["RCP_DEPLOYED_COMMIT"] = receipt["commit"]
        environment["RCP_DEPLOYED_VERSION"] = receipt["version_string"]
        os.execve(
            str(python),
            [str(python), "-I", "-m", "rcp", *(sys.argv[1:] if argv is None else argv)],
            environment,
        )
    except (SupervisorError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def launch_operator(argv: list[str]) -> int:
    """Run only retained privileged operators from the pinned root-owned console."""
    from rcp_supervisor.install import _environment

    try:
        if os.geteuid() != 0:
            raise SupervisorError("Privileged operator commands require root.")
        command = [value for value in argv if value != "--machine-readable"][:3]
        if command not in (["server", "backup", "configure"], ["server", "provider", "update"]):
            raise SupervisorError("This command is not a privileged operator-console entry.")
        selected = read_selected_receipt()
        console = SELECTED_RECEIPT.parent / "operator" / str(selected["build"])
        for parent in (
            *reversed(console.parents),
            console,
            console / ".venv",
            console / ".venv/bin",
        ):
            _root_owned(parent, regular=False)
        receipt_path = console / "installed.json"
        info = _root_owned(receipt_path, regular=True)
        if info.st_size > MAX_SELECTED_RECEIPT_BYTES:
            raise SupervisorError("Operator-console receipt exceeds its bound.")
        descriptor = os.open(receipt_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            receipt = json.loads(stream.read(MAX_SELECTED_RECEIPT_BYTES + 1))
        if (
            receipt.get("package") != "rcp"
            or receipt.get("package_version") != selected["version_string"]
            or receipt.get("manifest_sha256") != selected["manifest_sha256"]
        ):
            raise SupervisorError(
                "The root operator console does not match the selected application."
            )
        python = console / ".venv/bin/python"
        resolved = python.resolve(strict=True)
        if not resolved.is_relative_to(SELECTED_RECEIPT.parent / "python"):
            raise SupervisorError("The root operator Python is not independently owned.")
        for parent in reversed(resolved.parents):
            _root_owned(parent, regular=False)
        _root_owned(resolved, regular=True)
        environment = {
            **_environment(),
            "RCP_DEPLOYED_COMMIT": selected["commit"],
            "RCP_DEPLOYED_VERSION": selected["version_string"],
            "RCP_DATA_DIR": "/home/rcp/rcp-server/data",
        }
        os.execve(str(python), [str(python), "-I", "-m", "rcp", *argv], environment)
    except (SupervisorError, OSError, ValueError, TypeError, AttributeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
