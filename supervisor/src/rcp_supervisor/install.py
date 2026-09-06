"""Prepare an isolated RCP release without changing a running installation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.limits import (
    IDENTITY_TIMEOUT_SECONDS,
    INSTALL_TIMEOUT_SECONDS,
    MAX_CHECKPOINT_BYTES,
    MAX_CHECKPOINT_ENTRIES,
    MAX_SELECTED_RECEIPT_BYTES,
)
from rcp_supervisor.releases import VerifiedRelease, _fsync_directory, verify_release


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
        # An ancestor another account can rename would let it swap the validated
        # root before the path-based writes below; sticky shared roots cannot.
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}:
            raise SupervisorError(
                "Every ancestor of the releases root must be a directory owned by root or this account."
            )
        if info.st_mode & 0o022 and not (parent != path and info.st_mode & stat.S_ISVTX):
            raise SupervisorError(
                "The releases root and its ancestors must not be writable by other accounts."
            )
    if path.stat().st_uid != os.geteuid():
        raise SupervisorError("The releases root must already exist and belong to this account.")


def _run(
    argv: list[str], *, cwd: Path, log, timeout: int, environment: dict[str, str] | None = None
) -> None:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=environment or _environment(),
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


def _verify_installed_identity(
    release: VerifiedRelease, python: Path, *, cwd: Path, log=None
) -> None:
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
    if log is not None and result.stderr:
        # An import failure is the retained diagnostic, not the version-mismatch message.
        log.write(result.stderr)
        log.flush()
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
            _verify_installed_identity(installed_assets, python, cwd=target, log=log)
        resolved_python = python.resolve(strict=True)
        if (
            resolved_python.parent.name != "bin"
            or not resolved_python.parent.parent.name.startswith("cpython-3.12")
        ):
            raise SupervisorError("The application Python is not its managed 3.12 runtime.")
        _protect_venv_lock(target / ".venv")
        _fsync_owned_tree(resolved_python.parent.parent)
        _fsync_owned_tree(target)
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
        try:
            _fsync_directory(target)
            _fsync_directory(releases_root)
        except OSError:
            # A receipt whose durability is unproven must not survive as success.
            (target / "installed.json").unlink(missing_ok=True)
            raise
        return target
    except (OSError, SupervisorError) as exc:
        raise SupervisorError(
            f"Build {release.build} preparation failed; retained {target} for inspection. {exc}"
        ) from exc


def install_supervisor(bundle: Path, supervisor_root: Path) -> Path:
    """Prepare the independent supervisor runtime without selecting it."""
    release = verify_release(bundle)
    return _install_root_environment(
        release,
        supervisor_root,
        storage="versions",
        identity=release.supervisor_version,
        package="rcp_supervisor",
        version=release.supervisor_version,
        wheel=release.supervisor_wheel,
        requirements=release.supervisor_requirements,
    )


def install_operator_console(bundle: Path, supervisor_root: Path) -> Path:
    """Prepare trusted RCP operator code separately from the supervisor runtime."""
    release = verify_release(bundle)
    return _install_root_environment(
        release,
        supervisor_root,
        storage="operator",
        identity=str(release.build),
        package="rcp",
        version=release.version,
        wheel=release.wheel,
        requirements=release.requirements,
        manifest_sha256=release.manifest_sha256,
    )


def _install_root_environment(
    release: VerifiedRelease,
    supervisor_root: Path,
    *,
    storage: str,
    identity: str,
    package: str,
    version: str,
    wheel: Path,
    requirements: Path,
    manifest_sha256: str | None = None,
) -> Path:
    previous_umask = os.umask(0o022)
    try:
        return _prepare_root_environment(
            release,
            supervisor_root,
            storage=storage,
            identity=identity,
            package=package,
            version=version,
            wheel=wheel,
            requirements=requirements,
            manifest_sha256=manifest_sha256,
        )
    finally:
        os.umask(previous_umask)


def _prepare_root_environment(
    release: VerifiedRelease,
    supervisor_root: Path,
    *,
    storage: str,
    identity: str,
    package: str,
    version: str,
    wheel: Path,
    requirements: Path,
    manifest_sha256: str | None,
) -> Path:
    """One root-owned uv environment, immutable identity, and hash-locked inputs."""
    if os.geteuid() != 0:
        raise SupervisorError("Supervisor installation requires root.")
    _require_directory(supervisor_root)
    for parent in supervisor_root.parents:
        info = parent.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise SupervisorError("Supervisor runtime ancestry must exclude non-root writers.")
    uv = shutil.which("uv")
    if uv is None:
        raise SupervisorError("Supervisor installation requires uv on PATH.")
    uv_path = Path(uv).resolve(strict=True)
    for path in (*uv_path.parents, uv_path):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise SupervisorError("The root uv executable must exclude non-root writers.")
    uv = str(uv_path)
    for name in (storage, "python", "cache"):
        path = supervisor_root / name
        path.mkdir(mode=0o755, exist_ok=True)
        _require_directory(path)
    target = supervisor_root / storage / identity
    venv = target / ".venv"
    # Hash the exact files copied below and reverify the full copied bundle before
    # running any wheel; verification never accepts a mixture of source states.
    receipt = {
        "version": 1,
        "package": package,
        "package_version": version,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
    }
    if manifest_sha256 is not None:
        receipt["manifest_sha256"] = manifest_sha256
    if target.exists() or target.is_symlink():
        _require_directory(target)
        receipt_path = target / "installed.json"
        try:
            info = receipt_path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
                or info.st_nlink != 1
                or info.st_size > MAX_SELECTED_RECEIPT_BYTES
            ):
                raise SupervisorError("Supervisor installation receipt is unsafe.")
            if json.loads(receipt_path.read_text()) != receipt:
                raise SupervisorError(
                    "This supervisor version already names different verified assets."
                )
            _require_root_python(venv / "bin/python", supervisor_root)
            _verify_root_identity(venv / "bin/python", package, version, cwd=target)
        except (OSError, ValueError) as exc:
            raise SupervisorError(
                "Existing supervisor preparation is incomplete; inspect it before retrying."
            ) from exc
        return venv
    target.mkdir(mode=0o755)
    assets = target / "assets"
    assets.mkdir(mode=0o755)
    for source in release.directory.iterdir():
        shutil.copyfile(source, assets / source.name, follow_symlinks=False)
    copied = verify_release(assets)
    if copied.manifest_sha256 != release.manifest_sha256:
        raise SupervisorError("The release changed during supervisor preparation.")
    copied_wheel = assets / wheel.name
    copied_requirements = assets / requirements.name
    # Bind the receipt to the bytes that passed copied-bundle verification.
    if (
        receipt["wheel_sha256"] != hashlib.sha256(copied_wheel.read_bytes()).hexdigest()
        or receipt["requirements_sha256"]
        != hashlib.sha256(copied_requirements.read_bytes()).hexdigest()
    ):
        raise SupervisorError("The supervisor assets changed during preparation.")
    environment = {
        **_environment(),
        "UV_PYTHON_INSTALL_DIR": str(supervisor_root / "python"),
        "UV_CACHE_DIR": str(supervisor_root / "cache"),
    }
    python = venv / "bin/python"
    with (target / "install.log").open("xb") as log:
        commands = [
            [
                uv,
                "--no-config",
                "venv",
                "--no-project",
                "--managed-python",
                "--python",
                "3.12",
                str(venv),
            ],
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
                str(copied_requirements),
            ],
            [
                uv,
                "--no-config",
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                str(copied_wheel),
            ],
            [uv, "--no-config", "pip", "check", "--python", str(python)],
        ]
        for argv in commands:
            _run(
                argv, cwd=target, log=log, timeout=INSTALL_TIMEOUT_SECONDS, environment=environment
            )
    _require_root_python(python, supervisor_root)
    _verify_root_identity(python, package, version, cwd=target)
    _protect_venv_lock(venv)
    _fsync_owned_tree(supervisor_root / "python")
    _fsync_owned_tree(target)
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
    for directory in (target.parent, supervisor_root):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return venv


def _verify_root_identity(python: Path, package: str, expected: str, *, cwd: Path) -> None:
    try:
        result = subprocess.run(
            [str(python), "-I", "-c", f"import {package}; print({package}.__version__)"],
            cwd=cwd,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=IDENTITY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupervisorError("The supervisor runtime identity could not be verified.") from exc
    if result.returncode or result.stdout.decode("utf-8", errors="replace").strip() != expected:
        raise SupervisorError("The installed supervisor version does not match its verified wheel.")


def _require_root_python(python: Path, root: Path) -> None:
    resolved = python.resolve(strict=True)
    if not resolved.is_relative_to(root / "python"):
        raise SupervisorError("The root Python must be independently owned.")
    for path in (*resolved.parents, resolved):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise SupervisorError("The root Python runtime has another writer.")


def _fsync_owned_tree(root: Path, *, uid: int | None = None) -> None:
    """Persist the executable tree before any installed receipt can select it."""
    uid = os.geteuid() if uid is None else uid
    count = 0
    size = 0
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        count += len(directories) + len(files) + 1
        if count > MAX_CHECKPOINT_ENTRIES:
            raise SupervisorError("Prepared runtime exceeds its entry bound.")
        for name in files:
            path = Path(current) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
                raise SupervisorError("Prepared runtime contains unsafe file metadata.")
            size += info.st_size
            if size > MAX_CHECKPOINT_BYTES:
                raise SupervisorError("Prepared runtime exceeds its size bound.")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        info = Path(current).lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
            raise SupervisorError("Prepared runtime contains an unsafe directory.")
        descriptor = os.open(current, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _protect_venv_lock(venv: Path) -> None:
    # uv creates this empty coordination file with permissive mode independent
    # of umask. Once its final installer process has exited, retain it privately.
    path = venv / ".lock"
    if not path.exists() and not path.is_symlink():
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise SupervisorError("The prepared environment lock has unsafe metadata.")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
