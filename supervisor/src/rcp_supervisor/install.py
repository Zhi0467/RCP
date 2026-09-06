"""Prepare an isolated RCP release without changing a running installation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.limits import IDENTITY_TIMEOUT_SECONDS, INSTALL_TIMEOUT_SECONDS
from rcp_supervisor.releases import VerifiedRelease, verify_release


def _environment() -> dict[str, str]:
    # Do not let the caller's uv/project settings select another environment or
    # disable verification. Network/proxy/CA settings retain their usual meaning.
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("UV_", "PIP_", "PYTHON"))
        and name not in {"VIRTUAL_ENV", "CONDA_PREFIX"}
    }


def _require_directory(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise SupervisorError("The releases root must be an absolute normalized directory.")
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise SupervisorError("The releases root must not traverse a symbolic link.")
    if not path.is_dir() or path.stat().st_uid != os.geteuid():
        raise SupervisorError("The releases root must already exist and belong to this account.")
    if path.stat().st_mode & 0o022:
        raise SupervisorError("The releases root must not be writable by other accounts.")


def _run(argv: list[str], *, cwd: Path, log, timeout: int) -> None:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupervisorError("Release preparation could not finish its subprocess.") from exc
    if result.returncode:
        raise SupervisorError("Release preparation subprocess failed.")


def _verify_installed_identity(release: VerifiedRelease, python: Path, *, cwd: Path) -> None:
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", "import rcp; print(rcp.__version__)"],
            cwd=cwd,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=IDENTITY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupervisorError("The installed RCP identity could not be read.") from exc
    if (
        result.returncode
        or result.stdout.decode("utf-8", errors="replace").strip() != release.version
    ):
        raise SupervisorError("The installed RCP version does not match the verified wheel.")


def install_release(bundle: Path, releases_root: Path) -> Path:
    """Install as an unprivileged account; the later root coordinator selects rcp.

    Preparation never opens app data or changes the current-release pointer.
    An interrupted or failed preparation remains at its exact build directory
    with a log and no installation receipt. It is never reused or overwritten.
    """
    if os.geteuid() == 0:
        raise SupervisorError("Install release artifacts as the service account, not root.")
    _require_directory(releases_root)
    release = verify_release(bundle)
    uv = shutil.which("uv")
    if uv is None:
        raise SupervisorError("Release installation requires uv on PATH.")
    target = releases_root / str(release.build)
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise SupervisorError(
            f"Build directory already exists: {target}. Inspect it; installation never overwrites it."
        ) from exc

    try:
        assets = target / "assets"
        assets.mkdir(mode=0o700)
        for source in release.directory.iterdir():
            shutil.copyfile(source, assets / source.name, follow_symlinks=False)
        installed_assets = verify_release(assets)
        if installed_assets.manifest_sha256 != release.manifest_sha256:
            raise SupervisorError("The release changed while copying its verified assets.")
        python = target / ".venv" / "bin" / "python"
        with (target / "install.log").open("xb") as log:
            _run(
                [
                    uv,
                    "--no-config",
                    "venv",
                    "--no-project",
                    "--managed-python",
                    "--python",
                    "3.12",
                    str(target / ".venv"),
                ],
                cwd=target,
                log=log,
                timeout=INSTALL_TIMEOUT_SECONDS,
            )
            _run(
                [
                    uv,
                    "--no-config",
                    "pip",
                    "sync",
                    "--python",
                    str(python),
                    "--require-hashes",
                    "--only-binary",
                    ":all:",
                    "--allow-empty-requirements",
                    str(installed_assets.requirements),
                ],
                cwd=target,
                log=log,
                timeout=INSTALL_TIMEOUT_SECONDS,
            )
            _run(
                [
                    uv,
                    "--no-config",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--no-deps",
                    str(installed_assets.wheel),
                ],
                cwd=target,
                log=log,
                timeout=INSTALL_TIMEOUT_SECONDS,
            )
            _run(
                [uv, "--no-config", "pip", "check", "--python", str(python)],
                cwd=target,
                log=log,
                timeout=IDENTITY_TIMEOUT_SECONDS,
            )
        _verify_installed_identity(installed_assets, python, cwd=target)
        receipt = {
            "version": release.version,
            "build": release.build,
            "commit": release.commit,
            "bundle_supervisor_version": release.supervisor_version,
            "manifest_sha256": release.manifest_sha256,
        }
        temporary = target / ".installed.json.tmp"
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(receipt, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target / "installed.json")
        descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return target
    except (OSError, SupervisorError) as exc:
        raise SupervisorError(
            f"Build {release.build} preparation failed; retained {target} for inspection. {exc}"
        ) from exc
