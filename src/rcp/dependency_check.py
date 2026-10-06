"""Check a machine against `rcp.dependencies`.

The local machine is read in this process: `platform.system()`, `/etc/os-release`,
and `shutil.which` with the backend's own PATH. Every other check runs the fixed
shipped script `staged_dependency_check.sh` through a caller's command prefix
(the plain SSH route for a remote machine, `runuser` for the server's service
account), so it sees the account and PATH that real work sees.

Only a definite answer refuses a run. Unreachable, timed out, and unreadable
answers are ``not_checked`` and never refuse.
"""

from __future__ import annotations

import importlib.resources
import os
import platform
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Collection, Iterable
from datetime import UTC, datetime
from pathlib import Path

from rcp.core.models import DependencyStatus, MissingProgram
from rcp.dependencies import (
    APT_DISTRIBUTIONS,
    TESTED_LINUX_DISTRIBUTIONS,
    Dependency,
    Platform,
    Role,
    optional,
    required,
)
from rcp.limits import (
    DEPENDENCY_CHECK_ATTEMPT_TIMEOUT_SECONDS,
    DEPENDENCY_CHECK_LOCK_WAIT_SECONDS,
    DEPENDENCY_CHECK_NOT_CHECKED_TTL_SECONDS,
    DEPENDENCY_CHECK_TTL_SECONDS,
    STATE_TRANSFER_STDERR_BYTES,
)
from rcp.transport.ssh import ssh_arguments
from rcp.transport.state_transfer import retrying

# Runs one argv with the script on stdin and a timeout; the subprocess seam tests replace.
Runner = Callable[[list[str], str, float], subprocess.CompletedProcess[str]]
# Wraps the script's own argv (`sh -s -- <nonce> <names...>`) for one execution route.
Route = Callable[[list[str]], list[str]]

_FRAME = "rcp-dependency-check"
_COMMAND_NOT_FOUND = 127
_PLATFORMS: dict[str, Platform] = {"Linux": "linux", "Darwin": "darwin"}


def staged_check_script() -> str:
    """The shipped POSIX script, read from the package."""
    return importlib.resources.files("rcp").joinpath("staged_dependency_check.sh").read_text()


def script_check(
    route: Route,
    roles: Collection[Role],
    *,
    runner: Runner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> DependencyStatus:
    """Run the shipped script through `route` for these Linux roles, with bounded retry."""
    run = runner or _run
    nonce = secrets.token_hex(8)
    names = sorted({_probe_name(d) for d in _dependencies(roles, "linux")})
    try:
        argv = route(["sh", "-s", "--", nonce, *names])
    except (OSError, ValueError) as exc:
        return _not_checked(f"The check could not start: {exc}")
    script = staged_check_script()

    def attempt() -> subprocess.CompletedProcess[str]:
        try:
            return run(argv, script, DEPENDENCY_CHECK_ATTEMPT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # Plain "timed out" is the transfer classifier's transient wording;
            # its "timed out after" (a whole-transfer timeout) is permanent.
            return subprocess.CompletedProcess(argv, 124, "", "dependency check timed out")

    try:
        result = retrying(argv[0], "check", attempt, label="dependency", sleep=sleep)
    except OSError as exc:
        return _not_checked(f"The check could not start: {exc}")
    if result.returncode == _COMMAND_NOT_FOUND:
        # The route started and its last hop could not find `sh` itself
        # (SSH exits 255 for its own failures), so the shell is definitely absent.
        return _status(roles, "linux", None, [], {"sh"})
    if result.returncode:
        stderr = (result.stderr or "").strip()[-STATE_TRANSFER_STDERR_BYTES:]
        return _not_checked(f"The check exited {result.returncode}: {stderr}")
    return parse_output(result.stdout, nonce, roles)


def parse_output(stdout: str, nonce: str, roles: Collection[Role]) -> DependencyStatus:
    """Read one framed script answer; anything outside the frame is ignored."""
    lines = [line.rstrip("\r") for line in stdout.splitlines()]
    begin, end = f"{_FRAME} begin {nonce}", f"{_FRAME} end {nonce}"
    if begin not in lines or end not in lines[lines.index(begin) :]:
        return _not_checked("The machine's answer had no complete frame.")
    start = lines.index(begin)
    fields: dict[str, list[str]] = {}
    for line in lines[start + 1 : lines.index(end, start)]:
        key, _, value = line.partition(" ")
        fields.setdefault(key, []).append(value.strip())
    by_probe = {_probe_name(d): d for d in _dependencies(roles, "linux")}
    absent = fields.get("missing", [])
    if any(name not in by_probe for name in absent):
        return _not_checked("The machine reported a program that was not asked for.")
    system = (fields.get("os") or [""])[0]
    # Without `uname` the script cannot name the system; that absence is itself
    # the definite answer, read as Linux like the missing-shell answer above.
    if not system and "uname" not in absent:
        return _not_checked("The machine did not report its operating system.")
    if system and system != "Linux":
        return _unsupported(system, "RCP runs remote work on Linux only.")
    distribution = (fields.get("id") or [None])[0]
    like = " ".join(fields.get("id_like", [])).split()
    return _status(roles, "linux", distribution, like, {by_probe[name].name for name in absent})


def local_check(roles: Collection[Role]) -> DependencyStatus:
    """Check this process's own machine for these roles, without a shell."""
    system = platform.system()
    family = _PLATFORMS.get(system)
    if family is None:
        return _unsupported(system, "RCP runs on macOS and Linux only.")
    distribution: str | None = None
    like: list[str] = []
    if family == "linux":
        release = _os_release(Path("/etc/os-release"))
        distribution, like = release.get("ID"), release.get("ID_LIKE", "").split()
    absent = {
        d.name
        for d in _dependencies(roles, family)
        if not (os.access(d.path, os.X_OK) if d.path else shutil.which(d.name))
    }
    return _status(roles, family, distribution, like, absent)


class DependencyChecker:
    """One per backend process; admission and the machine card share it.

    Host ``""`` is the local machine (role ``local``); any other host is a remote
    Linux machine (role ``remote``) reached over the plain SSH route the state
    transport uses. Results are cached per host. One lock per host means
    concurrent callers for one machine share a single check and different
    machines never wait on each other.
    """

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        local: Callable[[Collection[Role]], DependencyStatus] = local_check,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._sleep = sleep
        self._local = local
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        # Latest result per host and when it finished. A `not_checked` is kept
        # briefly so runs sent to an unreachable machine do not each wait out the
        # retries; it admits either way, so reusing it changes no outcome.
        self._results: dict[str, tuple[float, DependencyStatus]] = {}

    def status(self, host: str, *, refresh: bool = False) -> DependencyStatus:
        """The cached result when fresh, otherwise a new check."""
        return self._lookup(host, refresh=refresh)[0]

    def launch_refusal(self, host: str) -> str | None:
        """A reason to refuse an agent run on `host`, or None to admit it.

        A remote run also needs the local machine's programs (ssh, rsync, …), so
        the local machine is checked first. A cached ``missing`` or
        ``unsupported`` is rechecked before it refuses; one checked during this
        call is not checked twice. ``not_checked`` admits.
        """
        machines = ("", host) if host else ("",)
        return next(filter(None, map(self._refusal, machines)), None)

    def _refusal(self, host: str) -> str | None:
        result, fresh = self._lookup(host, refresh=False)
        if result.outcome in {"ready", "not_checked"}:
            return None
        if not fresh:
            result, _ = self._lookup(host, refresh=True)
        machine = host or "This machine"
        if result.outcome == "unsupported":
            return f"{machine} is not supported: {result.reason}"
        if result.outcome != "missing":
            return None
        names = ", ".join(p.name for p in result.missing if p.required)
        install = f" Install with: {result.install_command}" if result.install_command else ""
        return f"{machine} is missing required programs: {names}.{install}"

    def _lookup(self, host: str, *, refresh: bool) -> tuple[DependencyStatus, bool]:
        """The result for `host`, and whether it was checked during this call."""
        asked = self._clock()
        with self._lock:
            host_lock = self._host_locks.setdefault(host, threading.Lock())
        if not host_lock.acquire(timeout=DEPENDENCY_CHECK_LOCK_WAIT_SECONDS):
            return _not_checked("Another check of this machine did not finish in time."), True
        try:
            with self._lock:
                latest = self._results.get(host)
            if latest is not None:
                finished, result = latest
                # A check that finished after this call began answers it, even
                # a refresh; otherwise only a result still within its lifetime does.
                if finished > asked:
                    return result, True
                lifetime = (
                    DEPENDENCY_CHECK_NOT_CHECKED_TTL_SECONDS
                    if result.outcome == "not_checked"
                    else DEPENDENCY_CHECK_TTL_SECONDS
                )
                if not refresh and asked - finished < lifetime:
                    return result, False
            result = self._check(host)
            with self._lock:
                self._results[host] = (self._clock(), result)
            return result, True
        finally:
            host_lock.release()

    def _check(self, host: str) -> DependencyStatus:
        if not host:
            return self._local(("local",))
        return script_check(
            lambda argv: ssh_arguments(host, shlex.join(argv)),
            ("remote",),
            runner=self._runner,
            sleep=self._sleep,
        )


def _run(argv: list[str], script: str, timeout: float) -> subprocess.CompletedProcess[str]:
    # A login banner may hold any bytes; only the framed lines must decode.
    return subprocess.run(
        argv,
        input=script,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _dependencies(roles: Iterable[Role], family: Platform) -> list[Dependency]:
    chosen: dict[str, Dependency] = {}
    for role in roles:
        for dependency in (*required(role, family), *optional(role, family)):
            chosen[dependency.name] = dependency
    return list(chosen.values())


def _probe_name(dependency: Dependency) -> str:
    if dependency.login_shell:
        return f"login:{dependency.name}"
    return dependency.path or dependency.name


def _os_release(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in {"ID", "ID_LIKE"}:
            values[key] = value.strip().strip("\"'")
    return values


def _status(
    roles: Collection[Role],
    family: Platform,
    distribution: str | None,
    like: list[str],
    absent: set[str],
) -> DependencyStatus:
    """Describe the absent programs from the registry, with install guidance."""
    needed = {d.name for role in roles for d in required(role, family)}
    lost = sorted(
        (d for d in _dependencies(roles, family) if d.name in absent), key=lambda d: d.name
    )
    missing = tuple(
        MissingProgram(name=d.name, purpose=d.purpose, required=True)
        if d.name in needed
        else MissingProgram(
            name=d.name, purpose=d.purpose, required=False, feature=d.feature, fallback=d.fallback
        )
        for d in lost
    )
    apt = family == "linux" and bool(APT_DISTRIBUTIONS & {distribution, *like})
    install_command = None
    notes: tuple[str, ...] = ()
    if lost and apt and all(d.apt for d in lost):
        packages = " ".join(sorted({d.apt for d in lost if d.apt}))
        install_command = f"sudo apt-get install --yes {packages}"
    elif lost:
        notes = tuple(dict.fromkeys(d.note or f"Install {d.apt or d.name}." for d in lost))
    return DependencyStatus(
        outcome="missing" if any(p.required for p in missing) else "ready",
        platform=family,
        distribution=distribution,
        tested=family != "linux" or distribution in TESTED_LINUX_DISTRIBUTIONS,
        missing=missing,
        install_command=install_command,
        install_notes=notes,
        checked_at=_now(),
    )


def _unsupported(system: str, reason: str) -> DependencyStatus:
    return DependencyStatus(
        outcome="unsupported",
        platform=system.lower() or None,
        reason=f"{reason} This machine reports {system or 'nothing'}.",
        checked_at=_now(),
    )


def _not_checked(reason: str) -> DependencyStatus:
    return DependencyStatus(outcome="not_checked", reason=reason, checked_at=_now())


def _now() -> str:
    return datetime.now(UTC).isoformat()
