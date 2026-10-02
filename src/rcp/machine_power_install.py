"""The explicitly requested, administrator-authorized keep-awake installation."""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import pwd
import re
import shlex
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rcp.limits import MACHINE_POWER_ADMIN_TIMEOUT_SECONDS, MACHINE_POWER_COMMAND_TIMEOUT_SECONDS

LABEL = "org.rcp.keep-awake-reset"
MARKER = "RCP keep-awake v1"
LOCK_MARKER = "RCP keep-awake owner lock v1\n"


def run_command(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, timeout=timeout, capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class InstallPaths:
    directory: Path = Path("/Library/Application Support/RCP/keep-awake")
    sudoers: Path = Path("/etc/sudoers.d/rcp-keep-awake")
    daemon: Path = Path(f"/Library/LaunchDaemons/{LABEL}.plist")


@dataclass(frozen=True)
class InstallStatus:
    installed: bool
    install_problem: str | None


class InstallError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sudoers(account: str, uid: int) -> str:
    return (
        f"# {MARKER} {uid} {account}\n"
        f"{account} ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 0, "
        "/usr/bin/pmset -a disablesleep 1\n"
    )


def _daemon(account: str, uid: int) -> str:
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": ["/usr/bin/pmset", "-a", "disablesleep", "0"],
            "RunAtLoad": True,
            "UserName": "root",
            "RCPInstallation": f"{MARKER} {uid} {account}",
        },
        sort_keys=True,
    ).decode()


class MachinePowerInstaller:
    def __init__(
        self,
        *,
        run: Callable[[list[str], float], subprocess.CompletedProcess[str]] = run_command,
        paths: InstallPaths | None = None,
        account: str | None = None,
        uid: int | None = None,
    ) -> None:
        self.paths = paths or InstallPaths()
        self.uid = os.getuid() if uid is None else uid
        self.account = pwd.getpwuid(self.uid).pw_name if account is None else account
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", self.account) or self.uid < 0:
            raise ValueError("Invalid enrollment account")
        self.run = run
        self.cancelled = False

    @property
    def enrollment(self) -> str:
        return f"{MARKER} {self.uid} {self.account}\n"

    def status(self) -> InstallStatus:
        """Inspect provenance without requiring another administrator prompt.

        Sudoers is normally unreadable by the enrolled user. The admin action
        checks its full content again before replacing or removing anything.
        """
        paths = self.paths
        present = [
            p.exists() or p.is_symlink() for p in (paths.directory, paths.sudoers, paths.daemon)
        ]
        if not any(present):
            return InstallStatus(False, "not_installed")
        contents: list[tuple[str, str]] = []
        try:
            if present[0]:
                if paths.directory.is_symlink() or not paths.directory.is_dir():
                    return InstallStatus(False, "foreign_file")
                lock_path = paths.directory / "owner.lock"
                if lock_path.is_symlink() or not lock_path.is_file():
                    return InstallStatus(False, "foreign_file")
                if lock_path.read_text() != LOCK_MARKER:
                    return InstallStatus(False, "foreign_file")
                marker = paths.directory / "enrollment"
                if marker.is_symlink():
                    return InstallStatus(False, "foreign_file")
                if marker.exists():
                    contents.append(("enrollment", marker.read_text()))
                ready = paths.directory / "installed-ready"
                if ready.is_symlink():
                    return InstallStatus(False, "foreign_file")
                if ready.exists():
                    contents.append(("enrollment", ready.read_text()))
            for name, path in (("sudoers", paths.sudoers), ("daemon", paths.daemon)):
                if path.is_symlink():
                    return InstallStatus(False, "foreign_file")
                if not path.exists():
                    continue
                if not path.is_file():
                    return InstallStatus(False, "foreign_file")
                try:
                    contents.append((name, path.read_text()))
                except PermissionError:
                    info = path.stat()
                    if name != "sudoers" or info.st_uid != 0 or info.st_mode & 0o777 != 0o440:
                        return InstallStatus(False, "foreign_file")
                    if not present[0]:
                        return InstallStatus(False, "partial")
        except (OSError, UnicodeError):
            return InstallStatus(False, "foreign_file")
        for name, content in contents:
            match = re.search(r"RCP keep-awake v1 (\d+) ([A-Za-z_][A-Za-z0-9_.-]*)", content)
            if not match:
                return InstallStatus(False, "foreign_file")
            uid, account = int(match[1]), match[2]
            expected = {
                "enrollment": f"{MARKER} {uid} {account}\n",
                "sudoers": _sudoers(account, uid),
                "daemon": _daemon(account, uid),
            }[name]
            if content != expected:
                return InstallStatus(False, "foreign_file")
            if uid != self.uid or account != self.account:
                return InstallStatus(False, "other_account")
        if present == [True, False, False] and not contents:
            return InstallStatus(False, "not_installed")
        installed = (
            all(present)
            and (paths.directory / "enrollment").is_file()
            and (paths.directory / "installed-ready").is_file()
        )
        return InstallStatus(installed, None if installed else "partial")

    def _admin(self, script: str) -> bool:
        # JSON string escaping is also AppleScript string escaping here.
        apple_script = f"do shell script {json.dumps(script)} with administrator privileges"
        try:
            result = self.run(
                # The prompt and the approved script each get one admin window.
                ["/usr/bin/osascript", "-e", apple_script],
                2 * MACHINE_POWER_ADMIN_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InstallError("admin_failed") from exc
        if result.returncode:
            error = result.stderr or ""
            if "-128" in error:
                self.cancelled = True
                return False
            for code in ("foreign_file", "other_account", "clear_failed", "owner_busy"):
                if code in error:
                    raise InstallError(code)
            raise InstallError("admin_failed")
        return True

    def _script(self, uninstall: bool, rendezvous: Path | None = None) -> str:
        q = shlex.quote
        paths = self.paths
        directory, sudoers, daemon = map(
            q, map(str, (paths.directory, paths.sudoers, paths.daemon))
        )
        script = f"""set -eu
umask 077
bounded() {{
    "$@" & child=$!
    ( /bin/sleep {MACHINE_POWER_COMMAND_TIMEOUT_SECONDS}; kill -KILL "$child" 2>/dev/null ) >/dev/null 2>&1 & timer=$!
    result=0
    wait "$child" || result=$?
    kill "$timer" 2>/dev/null || true
    wait "$timer" 2>/dev/null || true
    return "$result"
}}
fail() {{ echo "$1" >&2; exit 1; }}
work=$(bounded /usr/bin/mktemp -d /private/tmp/rcp-power.XXXXXX)
trap 'bounded /bin/rm -rf "$work"' EXIT
printf %s {q(self.enrollment)} > "$work/enrollment"
printf %s {q(LOCK_MARKER)} > "$work/lock"
printf %s {q(_sudoers(self.account, self.uid))} > "$work/sudoers"
printf %s {q(_daemon(self.account, self.uid))} > "$work/daemon"
check_file() {{
    [ ! -L "$1" ] || fail foreign_file
    if [ -e "$1" ]; then
        [ -f "$1" ] || fail foreign_file
        bounded /usr/bin/cmp -s "$1" "$2" || fail foreign_file
    fi
}}
check_file {sudoers} "$work/sudoers"
check_file {daemon} "$work/daemon"
[ ! -L {directory} ] || fail foreign_file
if [ -e {directory} ]; then
    [ -d {directory} ] || fail foreign_file
    check_file {q(str(paths.directory / "enrollment"))} "$work/enrollment"
    check_file {q(str(paths.directory / "owner.lock"))} "$work/lock"
    [ -f {q(str(paths.directory / "owner.lock"))} ] || fail foreign_file
fi
publish() {{
    staged=$(bounded /usr/bin/mktemp "$work/publish.XXXXXX")
    bounded /bin/cp "$1" "$staged"
    bounded /usr/sbin/chown -h root:wheel "$staged"
    bounded /bin/chmod "$3" "$staged"
    [ ! -L "$2" ] && [ ! -d "$2" ] || fail foreign_file
    bounded /bin/mv -fh "$staged" "$2"
}}
"""
        if not uninstall:
            parent = q(str(paths.directory.parent))
            lock_path = q(str(paths.directory / "owner.lock"))
            script += f"""bounded /usr/sbin/visudo -cf "$work/sudoers"
[ ! -L {parent} ] || fail foreign_file
if [ ! -e {parent} ]; then
    bounded /bin/mkdir -m 755 {parent} || [ -d {parent} ] || fail foreign_file
fi
[ ! -L {parent} ] && [ -d {parent} ] || fail foreign_file
[ "$(bounded /usr/bin/stat -f %u {parent})" = 0 ] || fail foreign_file
bounded /bin/chmod -h 755 {parent}
[ "$(bounded /usr/bin/stat -f %Lp {parent})" = 755 ] || fail foreign_file
[ ! -L {directory} ] || fail foreign_file
if [ ! -e {directory} ]; then
    bounded /bin/mkdir -m 755 "$work/{paths.directory.name}"
    bounded /bin/cp "$work/lock" "$work/{paths.directory.name}/owner.lock"
    bounded /bin/chmod 644 "$work/{paths.directory.name}/owner.lock"
    bounded /usr/sbin/chown -h {self.uid} "$work/{paths.directory.name}" "$work/{paths.directory.name}/owner.lock"
    [ ! -L {directory} ] || fail foreign_file
    bounded /bin/mv -nh "$work/{paths.directory.name}" {parent}
fi
check_file {lock_path} "$work/lock"
[ -f {lock_path} ] || fail foreign_file
[ ! -L {directory} ] || fail foreign_file
bounded /usr/sbin/chown -h {self.uid} {directory}
[ ! -L {lock_path} ] || fail foreign_file
bounded /usr/sbin/chown -h {self.uid} {lock_path}
"""
        if rendezvous is not None:
            script += f"""publish "$work/lock" {q(str(rendezvous / "approved"))} 644
attempt=0
while [ ! -f {q(str(rendezvous / "ready"))} ]; do
    [ ! -f {q(str(rendezvous / "abort"))} ] || fail owner_busy
    [ "$attempt" -lt {MACHINE_POWER_ADMIN_TIMEOUT_SECONDS} ] || fail owner_busy
    attempt=$((attempt + 1))
    bounded /bin/sleep 1
done
check_file {q(str(paths.directory / "owner.lock"))} "$work/lock"
check_file {sudoers} "$work/sudoers"
check_file {daemon} "$work/daemon"
check_file {q(str(paths.directory / "enrollment"))} "$work/enrollment"
"""
        if not uninstall:
            script += f"""[ ! -L {q(str(paths.directory / "installed-ready"))} ] || fail foreign_file
bounded /bin/rm -f {q(str(paths.directory / "installed-ready"))}
publish "$work/enrollment" {q(str(paths.directory / "enrollment"))} 644
bounded /bin/mkdir -p {q(str(paths.sudoers.parent))}
publish "$work/sudoers" {sudoers} 440
"""
        if uninstall:
            # Remove the rule before clearing. The backend's lock may lapse
            # here, and no later owner can then set the flag again.
            script += f"""[ ! -L {sudoers} ] || fail foreign_file
bounded /bin/rm -f {sudoers}
"""
        # RunAtLoad is asynchronous; bounded polling proves that it cleared.
        script += f"""publish "$work/daemon" {daemon} 644
bounded /bin/launchctl bootout system/{LABEL} >/dev/null 2>&1 || true
bounded /bin/launchctl bootstrap system {daemon}
clear=false
attempt=0
while [ "$attempt" -lt {MACHINE_POWER_COMMAND_TIMEOUT_SECONDS} ]; do
    bounded /usr/bin/pmset -g > "$work/flag" || fail clear_failed
    # pmset omits the line until the flag has been set once since boot.
    if bounded /usr/bin/grep -Eq '^[[:space:]]*SleepDisabled[[:space:]]+0([[:space:]]|$)' "$work/flag" ||
        ! bounded /usr/bin/grep -Eq '^[[:space:]]*SleepDisabled[[:space:]]' "$work/flag"; then
        clear=true
        break
    fi
    attempt=$((attempt + 1))
    bounded /bin/sleep 1
done
[ "$clear" = true ] || fail clear_failed
"""
        if uninstall:
            script += f"""bounded /bin/launchctl bootout system/{LABEL}
[ ! -L {daemon} ] || fail foreign_file
bounded /bin/rm -f {daemon}
"""
            # Only the directory and its immutable lock inode remain.
            for name in (
                "machine_power_watchdog.sh",
                "activation",
                "heartbeat",
                "ack",
                "result",
                "revoked",
                "enrollment",
                "installed-ready",
            ):
                target = q(str(paths.directory / name))
                script += f"[ ! -L {target} ] || fail foreign_file\nbounded /bin/rm -f {target}\n"
        else:
            script += (
                f'publish "$work/enrollment" {q(str(paths.directory / "installed-ready"))} 644\n'
            )
        return script

    def _change(
        self, *, uninstall: bool, before_remove: Callable[[], None] | None = None
    ) -> InstallStatus:
        self.cancelled = False
        status = self.status()
        if status.install_problem in {"foreign_file", "other_account"}:
            raise InstallError(status.install_problem)
        if uninstall and status.install_problem == "not_installed":
            return status
        lock = None

        def acquire() -> None:
            nonlocal lock
            if not self.paths.directory.exists():
                raise InstallError("owner_busy")
            if lock is None:
                lock_path = self.paths.directory / "owner.lock"
                fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
                lock = os.fdopen(fd, "r")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise InstallError("owner_busy") from exc

        try:
            if not uninstall and self.paths.directory.exists():
                acquire()
            with tempfile.TemporaryDirectory(prefix="rcp-power-authorize-") as temporary:
                rendezvous = Path(temporary)
                done = threading.Event()
                outcome: list[bool | BaseException] = []

                def authorize() -> None:
                    try:
                        outcome.append(self._admin(self._script(uninstall, rendezvous)))
                    except BaseException as exc:
                        outcome.append(exc)
                    finally:
                        done.set()

                threading.Thread(target=authorize, daemon=True).start()
                deadline = time.monotonic() + MACHINE_POWER_ADMIN_TIMEOUT_SECONDS
                try:
                    while not done.is_set() and not (rendezvous / "approved").exists():
                        if time.monotonic() >= deadline:
                            raise InstallError("admin_failed")
                        done.wait(0.02)
                    if (rendezvous / "approved").exists():
                        if uninstall and before_remove is not None:
                            before_remove()
                        if lock is None:
                            acquire()
                        (rendezvous / "ready").touch()
                        # The approved script gets its own window, and the runner's
                        # own bound covers both, so it has settled before we return.
                        deadline = time.monotonic() + 2 * MACHINE_POWER_ADMIN_TIMEOUT_SECONDS
                    if not done.wait(max(0, deadline - time.monotonic())):
                        raise InstallError("admin_failed")
                except BaseException:
                    (rendezvous / "abort").touch()
                    raise
                if isinstance(outcome[0], BaseException):
                    raise outcome[0]
                accepted = outcome[0]
            if not accepted:
                return status
            return self.status()
        finally:
            if lock is not None:
                lock.close()

    def install(self) -> InstallStatus:
        return self._change(uninstall=False)

    def uninstall(self, before_remove: Callable[[], None] | None = None) -> InstallStatus:
        return self._change(uninstall=True, before_remove=before_remove)
