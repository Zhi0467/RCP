"""Execution-host browser owner. Stdlib only; shipped unchanged over SSH."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import plistlib
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager, suppress
from pathlib import Path

from rcp.agents.staged_hidden_read import (
    PROBE_TIMEOUT_SECONDS,
    bwrap_argv,
    probe_hidden_read_wrapper,
)
from rcp.browser.libraries import apt_install_command, missing_library_packages
from rcp.transport.compute_process_owner import (
    owner_alive,
    owner_command,
    require_cancel_success,
)

CLI_VERSION = "0.1.22"

# Use the pinned CLI's own registry resolution, including nearest .playwright.
_SESSION_PROBE = """
const path = require('path');
const base = process.argv[1];
const {Registry, createClientInfo} = require(path.join(base, 'lib/tools/cli-client/registry.js'));
const {Session} = require(path.join(base, 'lib/tools/cli-client/session.js'));
(async () => {
  const entry = (await Registry.load()).entry(createClientInfo(), process.argv[2]);
  console.log(JSON.stringify({alive: !!entry && await new Session(entry).canConnect()}));
})().catch(e => { console.error(e); process.exit(1); });
"""
_EXECUTABLE_PROBE = """
const {chromium} = require(process.argv[1]);
console.log(chromium.executablePath());
"""


# The daemon outlives the turn under an OS owner and its environment is written to
# disk (plist or wrapper), so it gets only what a browser needs, never login secrets.
_DAEMON_ENVIRONMENT = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TZ",
        "PLAYWRIGHT_BROWSERS_PATH",
        "XDG_RUNTIME_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)


class UnavailableError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


# @playwright/cli declares Node 18, but the Playwright it runs refuses anything below 20.
MIN_NODE_MAJOR = 20


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".browser-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


# Written by Install while it holds the host lock. Trusted only while the lock is held,
# so a marker left by a killed install never reports a phantom one.
INSTALL_MARKER = "installing"
SMOKE_OWNER_TOKEN = "install-smoke"


@contextmanager
def host_lock(root: Path, timeout: float, *, report_install: bool = False):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise UnavailableError("storage_unavailable", "Browser root is not an owned directory")
    root.chmod(0o700)
    with (root / "host.lock").open("a") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                # A running install holds the lock for minutes; answer now instead of waiting.
                with suppress(FileNotFoundError):
                    if report_install:
                        started = (root / INSTALL_MARKER).stat().st_mtime
                        minutes = max(0, int((time.time() - started) // 60))
                        raise UnavailableError(
                            "installing", f"Install started {minutes} min ago"
                        ) from None
                if time.monotonic() >= deadline:
                    raise UnavailableError(
                        "busy", "Another browser operation is still running"
                    ) from None
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def pinned_config(executable: str, profile: Path, output: Path, idle: float) -> dict:
    """Explicit nulls replace ambient values in 0.1.22's shallow option merges."""
    return {
        "extension": False,
        "browser": {
            "browserName": "chromium",
            "isolated": False,
            "userDataDir": str(profile),
            "cdpEndpoint": None,
            "cdpHeaders": {},
            "remoteEndpoint": None,
            "remoteHeaders": {},
            "initPage": [],
            "initScript": [],
            "launchOptions": {
                "channel": "chromium",
                "executablePath": executable,
                "headless": True,
                "args": [],
                "ignoreDefaultArgs": False,
                "env": None,
                "downloadsPath": str(output),
                "chromiumSandbox": platform.system() != "Linux",
            },
            "contextOptions": {"storageState": None},
        },
        "outputDir": str(output),
        "saveSession": False,
        "saveVideo": None,
        "secrets": {},
        "timeouts": {"idle": int(idle * 1000)},
    }


class HostRuntime:
    def __init__(self, request: dict):
        self.request = request
        self.root = Path(request.get("root") or Path.home() / ".rcp" / "browser").resolve()
        self.tools = self.root.parent / "tools"
        self.limits = request["limits"]
        self.deadline = (
            time.monotonic()
            + self.limits[
                {
                    "ensure": "start",
                    "release": "close",
                    "close": "close",
                    "enable_linger": "readiness",
                }.get(request["action"], request["action"])
            ]
        )
        self.env = {
            key: value
            for key, value in request.get("environment", os.environ).items()
            if not key.startswith(("PLAYWRIGHT_", "PWTEST_", "PWDEBUG", "NODE_"))
        }
        self.env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.tools / "browsers")
        self.env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
        self.backend = "launchd" if platform.system() == "Darwin" else "systemd_user"
        if self.backend == "systemd_user":
            # The daemon's socket lives under TMPDIR. A service with PrivateTmp (the team
            # server) sees its own /tmp, the user manager running the daemon another; the
            # account's runtime directory is the same for both, and for the agent's CLI.
            self.env["TMPDIR"] = self.env["XDG_RUNTIME_DIR"]
        self.node = shutil.which("node", path=self.env.get("PATH"))
        self.npm = shutil.which("npm", path=self.env.get("PATH"))
        self.cli = self.tools / "node_modules" / "@playwright" / "cli" / "playwright-cli.js"
        self.core = self.tools / "node_modules" / "playwright-core"
        self.hidden_read_command: list[str] = []
        self.hidden_read_enforcement: dict | None = None
        self.hidden_read_fingerprint: str | None = None

    def browser_policy(self, reason: str | None = None) -> None:
        """Use B's mount wrapper, retaining the browser's existing network environment."""
        scope = self.request.get("hidden_read_scope")
        self.hidden_read_command = []
        if scope is None:
            self.hidden_read_enforcement = None
            self.hidden_read_fingerprint = None
            return
        reasons = set(scope["enforcement"]["reasons"])
        if self.backend == "launchd":
            reason = "browser_unwrapped_macos"
        elif reason is None:
            try:
                readiness = probe_hidden_read_wrapper(
                    timeout=max(
                        0.001, min(PROBE_TIMEOUT_SECONDS, (self.deadline - time.monotonic()) / 2)
                    )
                )
                if readiness["ready"]:
                    executable = shutil.which("bwrap", path=self.env.get("PATH"))
                    if executable is None:
                        reason = "wrapper_unavailable"
                    else:
                        self.hidden_read_command = bwrap_argv(
                            scope, "", executable=executable, persistent=True
                        )[:-1]
                else:
                    reason = readiness["reason"] or "wrapper_unavailable"
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                reason = "wrapper_unavailable"
        if reason:
            reasons.add(reason)
        self.hidden_read_enforcement = {
            "status": "unhidden" if reasons else "enforced",
            "reasons": sorted(reasons),
        }
        self.hidden_read_fingerprint = hashlib.sha256(
            json.dumps(
                [scope["fingerprint"], self.hidden_read_enforcement, self.hidden_read_command],
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def start_with_policy(self, record: dict, executable: str) -> None:
        deadline = self.deadline
        wrapped = bool(self.hidden_read_command)
        record.update(
            hidden_read_fingerprint=self.hidden_read_fingerprint,
            hidden_read_enforcement=self.hidden_read_enforcement,
        )
        self.save(record)
        try:
            # Leave time to clean up a failed wrapped job and admit an unhidden daemon.
            if wrapped:
                self.deadline = time.monotonic() + (deadline - time.monotonic()) / 2
            self.start(record, executable)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            if not wrapped:
                raise
            self.deadline = deadline
            self.close_record(record, preserve_leases=True)
            self.browser_policy("wrapper_unavailable")
            record.update(
                hidden_read_fingerprint=self.hidden_read_fingerprint,
                hidden_read_enforcement=self.hidden_read_enforcement,
            )
            self.save(record)
            self.start(record, executable)
        finally:
            self.deadline = deadline

    def daemon_env(self) -> dict[str, str]:
        return {key: value for key, value in self.env.items() if key in _DAEMON_ENVIRONMENT}

    def run(self, command: list[str], *, cwd: str | None = None, check: bool = True):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        with subprocess.Popen(
            command,
            cwd=cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                # npm and browser downloads spawn children; bound the whole command group.
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check and result.returncode:
            raise UnavailableError("command_failed", (result.stderr or result.stdout).strip())
        return result

    def cli_run(self, record: dict, *arguments: str, check: bool = True):
        return self.run(
            [self.node, str(self.cli), f"-s={record['session_name']}", *arguments],
            cwd=record["workspace_dir"],
            check=check,
        )

    def alive(self, record: dict) -> bool:
        if not self.node:
            raise UnavailableError("node_missing", "Node is missing from the execution account")
        result = self.run(
            [self.node, "-e", _SESSION_PROBE, str(self.core), record["session_name"]],
            cwd=record["workspace_dir"],
        )
        return json.loads(result.stdout)["alive"]

    def owner_status(self, record: dict) -> bool:
        result = self.run(
            owner_command(self.backend, record["handle"], str(os.getuid())), check=False
        )
        status = owner_alive(self.backend, result)
        if status is None:
            raise UnavailableError(
                "owner_unavailable", result.stderr or "Cannot inspect process owner"
            )
        return status

    def stop_owner(self, record: dict) -> None:
        result = self.run(
            owner_command(self.backend, record["handle"], str(os.getuid()), cancel=True),
            check=False,
        )
        require_cancel_success(self.backend, result)

    def owner_ready(self) -> None:
        required = (
            ["launchctl"] if self.backend == "launchd" else ["systemctl", "systemd-run", "loginctl"]
        )
        if any(shutil.which(tool, path=self.env.get("PATH")) is None for tool in required):
            raise UnavailableError("owner_unavailable", "OS process owner tools are missing")
        if self.backend == "launchd":
            result = self.run(["launchctl", "print", f"gui/{os.getuid()}"], check=False)
        else:
            result = self.run(["systemctl", "--user", "show-environment"], check=False)
            if not result.returncode:
                linger = self.run(
                    ["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"],
                    check=False,
                )
                if linger.returncode or linger.stdout.strip() != "yes":
                    raise UnavailableError(
                        "linger_disabled",
                        "Background processes stop when this account's last session ends",
                    )
        if result.returncode:
            raise UnavailableError(
                "owner_unavailable", result.stderr or "OS process owner unavailable"
            )

    def prerequisites(self) -> dict | None:
        if platform.system() not in {"Darwin", "Linux"} or platform.machine() not in {
            "arm64",
            "aarch64",
            "x86_64",
            "AMD64",
        }:
            return self.readiness_result("unsupported_platform")
        if not self.node:
            return self.readiness_result("node_missing")
        version = self.run([self.node, "--version"]).stdout.strip()
        if int(version.lstrip("v").split(".")[0]) < MIN_NODE_MAJOR:
            return self.readiness_result("node_too_old", version)
        if not self.npm:
            return self.readiness_result("npm_missing")
        return None

    @staticmethod
    def readiness_result(
        status: str,
        detail: str | None = None,
        apt_command: str | None = None,
        admin_command: str | None = None,
    ):
        return {
            "status": status,
            "detail": detail,
            "apt_command": apt_command,
            "admin_command": admin_command,
        }

    def executable(self) -> str:
        return self.run([self.node, "-e", _EXECUTABLE_PROBE, str(self.core)]).stdout.strip()

    def readiness(self, *, require_verified: bool = True) -> dict:
        # Ownership is the account's, not the browser's: report it before any
        # browser prerequisite, so a machine card always offers its fix.
        self.owner_ready()
        prerequisite = self.prerequisites()
        if prerequisite:
            return prerequisite
        package = self.tools / "node_modules" / "@playwright" / "cli" / "package.json"
        try:
            if (
                not package.is_file()
                or json.loads(package.read_text()).get("version") != CLI_VERSION
            ):
                return self.readiness_result("not_installed")
            executable = self.executable()
        except (ValueError, UnavailableError) as exc:
            # An interrupted install leaves files Install can repair.
            return self.readiness_result(
                "not_installed", f"RCP browser files are incomplete: {exc}"
            )
        if not Path(executable).is_file():
            return self.readiness_result("not_installed", "RCP Chromium is not installed")
        if platform.system() == "Linux":
            release = platform.freedesktop_os_release()
            if release.get("ID") != "ubuntu" or release.get("VERSION_ID") not in {"22.04", "24.04"}:
                return self.readiness_result(
                    "unsupported_platform", "Supported Linux hosts use Ubuntu 22.04 or 24.04"
                )
            ldd = self.run(["ldd", executable], check=False)
            if "not found" in ldd.stdout:
                packages = missing_library_packages(ldd.stdout, release["VERSION_ID"])
                missing = [
                    line.split()[0] for line in ldd.stdout.splitlines() if "not found" in line
                ]
                return self.readiness_result(
                    "system_libraries_missing",
                    "Missing: " + ", ".join(missing),
                    apt_install_command(packages),
                )
        if not os.access(executable, os.X_OK):
            return self.readiness_result("not_installed", "RCP Chromium is not executable")
        verified = self.tools / "verified.json"
        if require_verified and (
            not verified.is_file() or json.loads(verified.read_text()).get("version") != CLI_VERSION
        ):
            return self.readiness_result(
                "not_installed", "Install must finish its headless smoke check"
            )
        return self.readiness_result("ready")

    def record_path(self, token: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", token):
            raise ValueError("Invalid browser owner token")
        return self.root / "owners" / token / "state.json"

    def save(self, record: dict) -> None:
        atomic_write(self.record_path(record["owner_token"]), json.dumps(record))

    def records(self) -> list[dict]:
        records = []
        for path in (self.root / "owners").glob("*/state.json"):
            record = json.loads(path.read_text())
            if not Path(record["workspace_dir"]).is_dir():
                try:
                    if self.owner_status(record):
                        raise UnavailableError("workspace_missing", "Browser workspace disappeared")
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    if self.request.get("owner_token") in (None, record["owner_token"]):
                        raise
                    # Retain ambiguous owners, but isolate their failure from other owners.
                    continue
                # A new stage gets a new owner, so this profile is unreachable: delete its logins.
                self.close_record(record, delete=True)
                continue
            records.append(record)
        return records

    def close_record(
        self, record: dict, *, delete: bool = False, preserve_leases: bool = False
    ) -> None:
        # Persist intent before contact: retry after a controller interruption.
        record["pending_restart" if preserve_leases else "pending_close"] = True
        record["delete_profile"] = delete or record.get("delete_profile", False)
        self.save(record)
        workspace_exists = Path(record["workspace_dir"]).is_dir()
        if not workspace_exists and self.owner_status(record):
            raise UnavailableError("workspace_missing", "Browser workspace disappeared")
        if workspace_exists and self.alive(record):
            self.cli_run(record, "close")
            while self.alive(record):
                time.sleep(0.05)
        if self.owner_status(record):
            # The browser is already closed gracefully. Remove its residual OS job.
            self.stop_owner(record)
        elif self.backend == "launchd":
            self.stop_owner(record)
        if record["delete_profile"]:
            # Page snapshots and logs can hold signed-in content; delete with the profile.
            shutil.rmtree(self.record_path(record["owner_token"]).parent)
            return
        record.update(pending_close=False, pending_restart=False, delete_profile=False)
        if not preserve_leases:
            record["leases"] = {}
        self.save(record)

    def start(self, record: dict, executable: str) -> None:
        owner_dir = self.record_path(record["owner_token"]).parent
        profile, output = owner_dir / "profile", owner_dir / "output"
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        config = owner_dir / "config.json"
        atomic_write(
            config, json.dumps(pinned_config(executable, profile, output, self.limits["idle"]))
        )
        command = [
            self.node,
            str(self.core / "lib" / "entry" / "cliDaemon.js"),
            record["session_name"],
            f"--config={config}",
            f"--idle-timeout={int(self.limits['idle'] * 1000)}",
        ]
        # The daemon itself is the job's main process, not the CLI's detached launcher.
        if self.backend == "launchd":
            self.stop_owner(record)
            plist = owner_dir / "owner.plist"
            atomic_write(
                plist,
                plistlib.dumps(
                    {
                        "Label": record["handle"],
                        "ProgramArguments": command,
                        "WorkingDirectory": record["workspace_dir"],
                        "EnvironmentVariables": self.daemon_env(),
                        "RunAtLoad": True,
                        "KeepAlive": False,
                        "StandardOutPath": str(owner_dir / "daemon.log"),
                        "StandardErrorPath": str(owner_dir / "daemon.log"),
                    }
                ).decode(),
            )
            result = self.run(
                ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=False
            )
            if result.returncode:
                raise UnavailableError("owner_unavailable", result.stderr.strip())
        else:
            if self.hidden_read_command:
                command = [*self.hidden_read_command, "exec " + shlex.join(command)]
            # Keep environment literals out of systemd's version-dependent $ expansion.
            wrapper = owner_dir / "daemon.sh"
            if "$" in str(wrapper) or "$" in record["workspace_dir"]:
                raise UnavailableError("unsupported_path", "systemd browser paths cannot contain $")
            atomic_write(
                wrapper,
                "#!/bin/sh\nexec "
                + shlex.join(
                    [
                        "/usr/bin/env",
                        "-i",
                        *[f"{k}={v}" for k, v in self.daemon_env().items()],
                        *command,
                    ]
                )
                + "\n",
            )
            result = self.run(
                [
                    "systemd-run",
                    "--user",
                    "--collect",
                    "--quiet",
                    "--unit",
                    record["handle"],
                    "--working-directory",
                    record["workspace_dir"],
                    "--",
                    "/bin/sh",
                    str(wrapper),
                ],
                check=False,
            )
            if result.returncode:
                raise UnavailableError("owner_unavailable", result.stderr.strip())
        while not self.alive(record):
            if time.monotonic() >= self.deadline:
                raise UnavailableError(
                    "start_failed", "The browser daemon is running but its session never answered"
                )
            if not self.owner_status(record):
                log = owner_dir / "daemon.log"
                raise UnavailableError(
                    "start_failed",
                    log.read_text()[-4000:] if log.exists() else "Browser daemon exited",
                )
            time.sleep(0.05)
        self.cli_run(record, "goto", "about:blank")

    def prune_old_leases(self, record: dict) -> None:
        if self.request.get("retained_lease_ids", ()) is None:
            return
        retained = set(self.request.get("retained_lease_ids", ()))
        for lease_id, lease in list(record["leases"].items()):
            if lease["controller_id"] != self.request["controller_id"]:
                continue
            if lease_id in retained:
                lease["controller_epoch"] = self.request["controller_epoch"]
            elif lease["controller_epoch"] != self.request["controller_epoch"]:
                del record["leases"][lease_id]

    def ensure(self) -> dict:
        readiness = self.readiness()
        if readiness["status"] != "ready":
            raise UnavailableError(
                readiness["status"], readiness.get("detail") or readiness["status"]
            )
        self.browser_policy()
        token = self.request["owner_token"]
        self.record_path(token)
        records = self.records()
        controller = self.request["controller_id"]
        epoch = self.request["controller_epoch"]
        for record in records:
            self.prune_old_leases(record)
            self.save(record)
            if record.get("pending_close") and not record["leases"]:
                self.close_record(record)
        records = self.records()
        record = next((r for r in records if r["owner_token"] == token), None)
        if record and record["workspace_dir"] != self.request["workspace_dir"]:
            raise UnavailableError("owner_mismatch", "Browser owner workspace changed")
        if record and record.get("pending_close"):
            raise UnavailableError("closing", "Browser owner is closing")
        if record and (
            record.get("pending_restart")
            or record.get("hidden_read_fingerprint") != self.hidden_read_fingerprint
        ):
            # dispatch holds host_lock through stop/start; profile and leases survive.
            self.close_record(record, preserve_leases=True)
        if not record or not self.alive(record):
            live = [r for r in records if r is not record and self.alive(r)]
            if len(live) >= self.limits["cap"]:
                idle = sorted((r for r in live if not r["leases"]), key=lambda r: r["last_used"])
                if not idle:
                    raise UnavailableError("capacity", "All RCP browser sessions are busy")
                self.close_record(idle[0])
            if record is None:
                name = "rcp-" + hashlib.sha256((str(self.root) + token).encode()).hexdigest()[:24]
                record = {
                    "owner_token": token,
                    "session_name": name,
                    "handle": name,
                    "workspace_dir": self.request["workspace_dir"],
                    "leases": {},
                    "last_used": time.time(),
                    "pending_close": False,
                }
            elif self.owner_status(record):
                # A live OS job with an inaccessible socket is ambiguous, never reopen it.
                raise UnavailableError(
                    "session_unreachable", "Owned daemon has no reachable session"
                )
            self.save(record)
            self.start_with_policy(record, self.executable())
        record["leases"][self.request["lease_id"]] = {
            "controller_id": controller,
            "controller_epoch": epoch,
        }
        record["last_used"] = time.time()
        self.save(record)
        return {
            "hidden_read_enforcement": record.get("hidden_read_enforcement"),
            "session_name": record["session_name"],
            "invocation_dir": record["workspace_dir"],
            "path_prefix": self.cli_launcher(),
            "env": {
                "PLAYWRIGHT_CLI_SESSION": record["session_name"],
                "PLAYWRIGHT_BROWSERS_PATH": str(self.tools / "browsers"),
            },
        }

    def cli_launcher(self) -> str:
        """Agents run the CLI with the Node readiness checked, never whichever is first on PATH."""
        if not self.node:
            raise UnavailableError("node_missing", "Node is missing from the execution account")
        launcher = self.tools / "bin" / "playwright-cli"
        export = (
            f"export TMPDIR={shlex.quote(self.env['TMPDIR'])}\n"
            if self.backend == "systemd_user"
            else ""
        )
        atomic_write(
            launcher,
            f'#!/bin/sh\n{export}exec {shlex.join([self.node, str(self.cli)])} "$@"\n',
        )
        launcher.chmod(0o700)
        return str(launcher.parent)

    def release(self) -> dict:
        path = self.record_path(self.request["owner_token"])
        if not path.exists():
            return {"alive": False, "reason_code": "lost", "detail": "Browser owner disappeared"}
        record = json.loads(path.read_text())
        if self.request["lease_id"] not in record["leases"]:
            return {"alive": False, "reason_code": "lost", "detail": "Browser lease disappeared"}
        record["leases"].pop(self.request["lease_id"])
        record["last_used"] = time.time()
        self.save(record)
        alive = self.alive(record)
        if record.get("pending_close") and not record["leases"]:
            self.close_record(record)
        return {"alive": alive, "reason_code": None if alive else "lost", "detail": None}

    def close(self) -> dict:
        path = self.record_path(self.request["owner_token"])
        if path.exists():
            record = json.loads(path.read_text())
            self.prune_old_leases(record)
            if record["leases"]:
                record.update(
                    pending_close=True,
                    delete_profile=self.request["delete_profile"]
                    or record.get("delete_profile", False),
                )
                self.save(record)
            else:
                self.close_record(record, delete=self.request["delete_profile"])
        return {}

    def enable_linger(self) -> dict:
        """Let this account's user manager outlive its sessions, as the member asked."""
        if self.backend == "systemd_user":
            result = self.run(["loginctl", "enable-linger"], check=False)
            if result.returncode:
                account = pwd.getpwuid(os.getuid()).pw_name
                return self.readiness_result(
                    "linger_disabled",
                    (result.stderr or result.stdout).strip() or None,
                    admin_command=shlex.join(["sudo", "loginctl", "enable-linger", account]),
                )
        return self.readiness()

    def install(self) -> dict:
        # Covers the owner checks too: a stalled probe can hold the lock for minutes.
        atomic_write(self.root / INSTALL_MARKER, "")
        try:
            return self._checked_install()
        finally:
            (self.root / INSTALL_MARKER).unlink(missing_ok=True)

    def _checked_install(self) -> dict:
        self.owner_ready()
        prerequisite = self.prerequisites()
        if prerequisite:
            return prerequisite
        for record in self.records():
            if record["owner_token"] == SMOKE_OWNER_TOKEN:
                # A past install's own smoke daemon is no member's session: stop it
                # rather than refusing, and before its tools are replaced.
                self.stop_owner(record)
                continue
            try:
                alive = self.alive(record)
            except UnavailableError as exc:
                if exc.code != "command_failed":
                    raise
                # A broken CLI cannot inspect its registry. Only a proven-stopped
                # OS owner permits replacing its tools; unknown owners still raise.
                alive = self.owner_status(record)
            if alive:
                return self.readiness_result("busy", "Close RCP browser sessions before installing")
        return self._install()

    def _install(self) -> dict:
        self.tools.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.run(
            [
                self.npm,
                "install",
                "--prefix",
                str(self.tools),
                f"@playwright/cli@{CLI_VERSION}",
                "--no-audit",
                "--no-fund",
            ]
        )
        self.run([self.node, str(self.cli), "install-browser", "chromium", "--no-shell"])
        ready = self.readiness(require_verified=False)
        if ready["status"] != "ready":
            return ready
        token = SMOKE_OWNER_TOKEN
        record = {
            "owner_token": token,
            "session_name": "rcp-smoke-" + hashlib.sha256(str(self.root).encode()).hexdigest()[:16],
            "handle": "rcp-browser-smoke-"
            + hashlib.sha256(str(self.root).encode()).hexdigest()[:16],
            "workspace_dir": str(self.root),
            "leases": {},
            "last_used": time.time(),
        }
        deadline = self.deadline
        try:
            self.save(record)
            self.deadline = min(deadline, time.monotonic() + self.limits["start"])
            self.start(record, self.executable())
        finally:
            self.deadline = deadline
            self.close_record(record, delete=True)
        atomic_write(self.tools / "verified.json", json.dumps({"version": CLI_VERSION}))
        return self.readiness()


def dispatch(request: dict) -> dict:
    action = request["action"]
    try:
        expected = request.get("expected_account")
        if expected and pwd.getpwuid(os.getuid()).pw_name != expected:
            raise UnavailableError(
                "account_mismatch", "SSH answered as a different execution account"
            )
        runtime = HostRuntime(request)
        with host_lock(
            runtime.root,
            max(0.001, runtime.deadline - time.monotonic()),
            report_install=action in {"readiness", "install"},
        ):
            # Holding the lock proves no install runs; drop a marker a killed one left.
            (runtime.root / INSTALL_MARKER).unlink(missing_ok=True)
            return getattr(runtime, action)()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, UnavailableError) else "runtime_error"
        if action in {"readiness", "install", "enable_linger"}:
            return {"status": code, "detail": str(exc), "apt_command": None}
        if action == "release":
            return {"alive": False, "reason_code": code, "detail": str(exc)}
        return {"reason_code": code, "detail": str(exc)}
