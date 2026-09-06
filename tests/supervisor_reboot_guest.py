"""Guest-only fixture and fault wrapper; never shipped in either wheel.

The external controller alone reboots the VM. These commands refuse any host
without its root-owned cloud-init marker and QEMU hardware identity.
"""

from __future__ import annotations

import hashlib
import http.cookies
import io
import json
import os
import pwd
import re
import shlex
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path("/opt/rcp-supervisor-qualification")
STATE = ROOT / "fault-state"
PLAN = ROOT / "case.json"
SCRIPT = ROOT / "tests/supervisor_reboot_guest.py"
SUPERVISOR_PYTHON = "/etc/rcp/supervisor/current/bin/python"
BASE_BUILD = 900001
TARGET_BUILD = 900002


def require_guest(*, root: bool = True) -> None:
    marker = Path("/etc/rcp-supervisor-qualification").lstat()
    if (
        marker.st_uid != 0
        or not stat.S_ISREG(marker.st_mode)
        or stat.S_IMODE(marker.st_mode) != 0o600
        or marker.st_size != 36
        or "QEMU" not in Path("/sys/class/dmi/id/sys_vendor").read_text()
        or (root and os.geteuid() != 0)
    ):
        raise RuntimeError("This command requires the controller's disposable QEMU guest.")
    sys.path.insert(0, str(ROOT))


def run(argv: list[str], *, timeout: int = 120, **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, check=True, capture_output=True, text=True, timeout=timeout, **kwargs
        )
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print("Guest command stderr:\n" + exc.stderr[-8000:], file=sys.stderr)
        raise


def write_json(path: Path, value: dict, *, service_owned: bool = False) -> None:
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}")
    with temporary.open("x") as output:
        os.fchmod(output.fileno(), 0o600)
        if service_owned and os.geteuid() == 0:
            account = pwd.getpwnam("rcp")
            os.fchown(output.fileno(), account.pw_uid, account.pw_gid)
        json.dump(value, output)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def event(name: str, **details) -> dict:
    value = {
        "phase": name,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "monotonic_ns": time.monotonic_ns(),
        **details,
    }
    path = STATE / "events.jsonl"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    if os.geteuid() == 0:
        account = pwd.getpwnam("rcp")
        os.fchown(descriptor, account.pw_uid, account.pw_gid)
    with os.fdopen(descriptor, "a") as stream:
        stream.write(json.dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return value


def boundary(name: str) -> None:
    details = {}
    if name == "rollback_roots_complete":
        details = verify_checkpoint_bytes()
    value = event(name, **details)
    if not PLAN.exists():
        return
    plan = read_json(PLAN)
    state_path = STATE / "state.json"
    state = read_json(state_path)
    index = state["next_pause"]
    if index < len(plan["pauses"]) and plan["pauses"][index] == name:
        state["next_pause"] = index + 1
        write_json(state_path, state, service_owned=True)
        write_json(STATE / "paused.json", {**value, "pause_index": index}, service_owned=True)
        # Parent polls outside the guest and cuts power. A lost controller is a
        # bounded failed qualification, never an indefinitely suspended server.
        time.sleep(600)
        raise RuntimeError("The external controller did not interrupt the armed boundary.")


def verify_checkpoint_bytes() -> dict:
    from rcp_supervisor.driver import store_for
    from rcp_supervisor.runtime import Paths

    operation = store_for(Paths()).active()
    assert operation is not None
    checkpoint = operation["checkpoint"]
    directory = Path(checkpoint["directory"])
    manifest = directory / "checkpoint.json"
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == checkpoint["sha256"]
    roots = read_json(manifest)["roots"]
    account = pwd.getpwnam("rcp")
    for root in roots:
        live = Path(root["live"])
        assert not live.is_symlink()
        assert live.stat().st_uid == account.pw_uid
        assert stat.S_IMODE(live.stat().st_mode) == 0o700
        assert {str(path.relative_to(live)) for path in live.rglob("*")} == {
            item["path"] for item in root["entries"]
        }
        for item in root["entries"]:
            path = live / item["path"]
            assert not path.is_symlink()
            assert path.stat().st_uid == account.pw_uid
            if item["kind"] == "file":
                assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
                assert stat.S_IMODE(path.stat().st_mode) == item["mode"]
            else:
                assert path.is_dir()
                assert stat.S_IMODE(path.stat().st_mode) == 0o700
    return {"checkpoint_bytes_verified": True, "replacement_roots": len(roots)}


def health() -> dict:
    with urllib.request.urlopen("http://127.0.0.1:8421/api/health", timeout=5) as response:
        return json.load(response)


def wait_for(observe, message: str):
    # This module also runs before dev dependencies exist in the guest. The
    # controller uses tests.helpers.wait_until; this bounded guest CLI cannot
    # import the test suite through the isolated supervisor environment.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            value = observe()
            if value:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError(message)


def wait_health() -> dict:
    def healthy():
        value = health()
        return value if value.get("status") == "ok" else None

    return wait_for(healthy, "The guest application never returned healthy HTTP.")


def bootstrap() -> dict:
    run(["apt-get", "update"], timeout=300)
    run(
        [
            "apt-get",
            "install",
            "--yes",
            "age",
            "ca-certificates",
            "curl",
            "git",
            "iproute2",
            "openssh-server",
            "sudo",
            "xz-utils",
        ],
        timeout=600,
    )
    run(["install", "-m", "0755", str(ROOT / "uv"), "/usr/local/bin/uv"])
    python = ROOT / "bootstrap" / "bin/python"
    run(
        [
            "uv",
            "venv",
            "--no-project",
            "--managed-python",
            "--python",
            "3.12",
            str(python.parent.parent),
        ],
        timeout=600,
    )
    bundle = ROOT / "bundles/base"
    (wheel,) = bundle.glob("rcp-*.whl")
    run(
        [
            "uv",
            "pip",
            "sync",
            "--python",
            str(python),
            "--require-hashes",
            "--only-binary",
            ":all:",
            str(bundle / "requirements.lock.txt"),
        ],
        timeout=600,
    )
    run(["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)], timeout=600)
    (supervisor_wheel,) = bundle.glob("rcp_supervisor-*.whl")
    run(
        ["uv", "pip", "install", "--python", str(python), "--no-deps", str(supervisor_wheel)],
        timeout=600,
    )
    result = json.loads(run([str(python), str(SCRIPT), "setup"], timeout=1200).stdout)
    shutil.rmtree(python.parent.parent)
    service(["/usr/local/bin/rcp", "server", "doctor", "--machine-readable"])
    dropin = Path("/etc/systemd/system/rcp.service.d")
    dropin.mkdir()
    (dropin / "qualification.conf").write_text(
        "[Service]\nExecStartPre=\n"
        + f"ExecStartPre=+{SUPERVISOR_PYTHON} {SCRIPT} recover\n"
        + "ExecStart=\n"
        + f"ExecStart={SUPERVISOR_PYTHON} {SCRIPT} application\n"
    )
    run(["systemctl", "daemon-reload"])
    result["temporary_bootstrap_removed"] = True
    return result


def service(argv: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess:
    account = pwd.getpwnam("rcp")
    environment = {**os.environ, "HOME": account.pw_dir, "USER": "rcp", "LOGNAME": "rcp"}
    return run(
        argv,
        timeout=timeout,
        user=account.pw_uid,
        group=account.pw_gid,
        extra_groups=[],
        env=environment,
        cwd=account.pw_dir,
    )


def release_receipt(bundle: Path) -> dict:
    from rcp_supervisor.launch import validate_selected_receipt
    from rcp_supervisor.releases import verify_release

    verified = verify_release(bundle)
    receipt = {
        "version": 1,
        "release_tag": "v" + verified.version.split("+", 1)[0],
        "version_string": verified.version,
        "build": verified.build,
        "commit": verified.commit.ljust(40, verified.commit[0]),
        "manifest_sha256": verified.manifest_sha256,
        "release_directory": f"/home/rcp/rcp-server/releases/{verified.build}",
        "supervisor_version": verified.supervisor_version,
    }
    return validate_selected_receipt(receipt)


def setup() -> dict:
    from rcp_supervisor.releases import verify_release
    from rcp_supervisor.runtime import SystemRuntime

    from rcp.server_ops.config import create_installed_server_config, write_installed_server_config
    from rcp.server_ops.install import LinuxInstallMachine
    from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT

    machine = LinuxInstallMachine()
    machine.validate_host()
    machine.converge_account_and_layout()
    write_installed_server_config(
        create_installed_server_config(), DEFAULT_SERVER_LAYOUT.config_path
    )
    machine.bootstrap_supervisor(verify_release(ROOT / "bundles/base"))
    Path("/etc/rcp/supervisor/operations").mkdir(mode=0o700)
    account = pwd.getpwnam("rcp")
    STATE.mkdir(mode=0o700)
    os.chown(STATE, account.pw_uid, account.pw_gid)
    machine.converge_supervisor_integration()
    first = json.loads(
        run([SUPERVISOR_PYTHON, str(SCRIPT), "qualified-install"], timeout=600).stdout
    )
    assert first["code"] == 3
    assert not Path("/home/rcp/rcp-server/data/rcp.sqlite3").exists()
    assert (
        run(["systemctl", "show", "rcp.service", "--property=MainPID", "--value"]).stdout.strip()
        == "0"
    )
    pause = first["events"][-1]["step"]
    assert pause["state"] == "operator_action_needed"
    (action,) = pause["actions"]
    assert action["kind"] == "command"
    initialized = run(
        ["script", "--quiet", "--return", "--command", shlex.join(action["argv"]), "/dev/null"]
    ).stdout
    (code,) = re.findall(r"rcp_bootstrap_[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{43}", initialized)
    write_json(STATE / "bootstrap.json", {"code": code}, service_owned=True)
    base = release_receipt(ROOT / "bundles/base")
    target = release_receipt(ROOT / "bundles/target")
    write_json(ROOT / "releases.json", {"base": base, "target": target})
    service([SUPERVISOR_PYTHON, str(SCRIPT), "install-release", str(ROOT / "bundles/target")])
    service([f"{base['release_directory']}/.venv/bin/python", str(SCRIPT), "prepare-data"])
    run(
        [
            "/usr/local/bin/rcp",
            "server",
            "install",
            "--team-name",
            "Reboot qualification",
            "--machine-readable",
        ],
        timeout=300,
    )
    wait_health()
    install_receipt = verify_installation()
    assert code not in run(["journalctl", "-u", "rcp.service", "--no-pager", "-o", "cat"]).stdout
    backup = Path("/var/backups/rcp-qualification")
    backup.mkdir(mode=0o700)
    os.chown(backup, account.pw_uid, account.pw_gid)
    run(
        [
            "/usr/local/bin/rcp",
            "server",
            "backup",
            "configure",
            "--destination",
            str(backup),
            "--schedule",
            "02:00",
            "--retention",
            "90",
            "--confirm",
            "--machine-readable",
        ],
        timeout=300,
    )
    runtime = SystemRuntime()
    runtime.protected_backup()
    archive = max(backup.glob("*.tar.age"), key=lambda path: path.stat().st_mtime_ns)
    run([SUPERVISOR_PYTHON, str(SCRIPT), "prepare-restore-request", str(archive)], timeout=600)
    rename_space("Work accepted after protected archive")
    return {
        "status": "ready",
        "base_build": BASE_BUILD,
        "target_build": TARGET_BUILD,
        "fresh_install": install_receipt,
    }


def qualified_install() -> dict:
    from contextlib import redirect_stdout
    from dataclasses import replace

    from rcp_supervisor import cli, driver
    from rcp_supervisor.releases import verify_release

    receipt = release_receipt(ROOT / "bundles/base")
    trusted_fixture = replace(
        verify_release(ROOT / "bundles/base"),
        release_tag=receipt["release_tag"],
        full_commit=receipt["commit"],
    )
    # Only the disposable wrapper substitutes artifact provenance. Installation,
    # service-account preparation, root selection and the CLI remain production.
    driver.followed_release = lambda _runtime: trusted_fixture
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = cli.main(
            ["--machine-readable", "server", "install", "--team-name", "Reboot qualification"]
        )
    return {"code": code, "events": [json.loads(line) for line in stream.getvalue().splitlines()]}


def verify_installation() -> dict:
    from rcp_supervisor.runtime import SystemRuntime

    account = pwd.getpwnam("rcp")
    assert account.pw_dir == "/home/rcp" and account.pw_shell == "/bin/bash"
    assert account.pw_uid > 0 and account.pw_gid > 0
    shadow = run(["getent", "shadow", "rcp"]).stdout.split(":")[1]
    assert shadow == "*NP*"
    assert run(["id", "--groups", "rcp"]).stdout.split() == [str(account.pw_gid)]
    for path in (
        Path("/home/rcp/rcp-server/data"),
        Path("/home/rcp/rcp-server/projects"),
        Path("/home/rcp/rcp-server/credentials"),
        Path("/run/rcp"),
    ):
        info = path.stat()
        assert info.st_uid == account.pw_uid and stat.S_IMODE(info.st_mode) == 0o700
    pid = int(run(["systemctl", "show", "rcp.service", "--property=MainPID", "--value"]).stdout)
    assert pid > 0 and Path(f"/proc/{pid}").stat().st_uid == account.pw_uid
    assert run(["systemctl", "is-enabled", "rcp.service"]).stdout.strip() == "enabled"
    runtime = SystemRuntime()
    metadata = runtime.metadata()
    assert metadata["pid"] == pid
    control = runtime.control("probe")
    assert control["pid"] == pid
    return {
        "account": "rcp",
        "service_pid": pid,
        "space_id": health()["space_id"],
        "control_socket": "healthy",
        "initialization_required": True,
    }


def prepare_restore_request(archive: Path, base: dict) -> None:
    from rcp_supervisor.driver import store_for
    from rcp_supervisor.operations import Coordinator
    from rcp_supervisor.restore import RestoreOperatorAction
    from rcp_supervisor.runtime import SystemRuntime

    request = {
        "archive_path": str(archive),
        "identity_file": "/etc/rcp/backup-recovery.agekey",
        "confirmed_data_dir": "/home/rcp/rcp-server/data",
        "confirmed_by": "disposable qualification",
        "old_authority_disposition": "old-machine-fenced-and-credentials-revoked",
        "confirm_old_authority": None,
        "confirm_member_roster": None,
        "remove_stale_member": None,
    }
    runtime = SystemRuntime(restore_request=request)
    try:
        Coordinator(store_for(runtime.paths), runtime).deploy(base, base, kind="restore")
    except RestoreOperatorAction as exc:
        # The fixture contains synthetic authorities and no real deploy keys.
        # Review the actual archived boundaries before arming any fault case.
        fields = {item["name"]: item["value"] for item in exc.details["fields"]}
        for argument, field in (
            ("confirm_old_authority", "old_authority_boundary"),
            ("confirm_member_roster", "member_roster_boundary"),
        ):
            digest = fields[field]
            assert isinstance(digest, str) and len(digest) == 64
            assert all(character in "0123456789abcdef" for character in digest)
            request[argument] = digest
    else:
        raise AssertionError("An archive restored without its required authority review.")
    write_json(ROOT / "restore.json", request)


def prepare_case(source: Path) -> dict:
    from rcp_supervisor.runtime import Paths

    plan = read_json(source)
    write_json(PLAN, plan)
    PLAN.chmod(0o644)
    write_json(STATE / "state.json", {"next_pause": 0}, service_owned=True)
    for name in ("paused.json", "error.json", "events.jsonl", "accepted.json"):
        (STATE / name).unlink(missing_ok=True)
    if plan["kind"] == "invalid":
        run(["systemctl", "stop", "rcp.service"])
        write_json(Paths().operations / f"{uuid.uuid4()}.json", {"version": 999, "phase": "guess"})
    if plan["kind"] == "fresh_restore":
        run(["systemctl", "stop", "rcp.service"])
        run(["systemctl", "disable", "rcp.service"])
        # Preserve the entire synthetic old data tree. This case qualifies a
        # fresh app-data target with an already provisioned local Git checkout;
        # it does not claim fresh-host GitHub authorization/reconstruction.
        data = Paths().data_dir
        os.replace(data, STATE / "fresh-original-data")
        data.mkdir(mode=0o700)
        account = pwd.getpwnam("rcp")
        os.chown(data, account.pw_uid, account.pw_gid)
    return {"status": "prepared"}


def runtime_type():
    from rcp_supervisor.runtime import SystemRuntime

    class GuestRuntime(SystemRuntime):
        def filesystem(self, action: str, request: dict) -> dict:
            from rcp_supervisor.operations import OperationStore

            active = OperationStore(self.paths.operations, self.paths.releases_root).active()
            prefix = (
                "candidate_"
                if action == "restore" and active is not None and active["phase"] == "activating"
                else ""
            )
            return self.service_json(
                [SUPERVISOR_PYTHON, str(SCRIPT), "filesystem", action, prefix], request
            )

        def probe(self, release: dict, operation: dict, proof: dict | None) -> None:
            super().probe(release, operation, proof)
            event("probe_verified", build=release["build"], proof_checked=proof is not None)
            if release["build"] == operation["target"]["build"] and not self.startup:
                plan = read_json(PLAN)
                if plan["kind"] == "update":
                    with sqlite3.connect(
                        f"file:{self.paths.data_dir / 'rcp.sqlite3'}?mode=ro", uri=True
                    ) as database:
                        assert database.execute(
                            "SELECT name FROM sqlite_master WHERE name='supervisor_qualification'"
                        ).fetchone()
                if plan["force_rollback"] and not getattr(self, "forced_failure", False):
                    self.forced_failure = True
                    raise RuntimeError(
                        "Intentional post-verification failure in the disposable qualification."
                    )

        def start_service(self) -> None:
            super().start_service()
            wait_health()
            boundary("post_admission")

    return GuestRuntime


def runtime(*, startup: bool):
    request = (
        read_json(ROOT / "restore.json")
        if PLAN.exists() and read_json(PLAN)["kind"] in {"restore", "fresh_restore"}
        else None
    )
    return runtime_type()(startup=startup, restore_request=request)


def coordinator(*, startup: bool):
    from rcp_supervisor.driver import store_for
    from rcp_supervisor.operations import Coordinator
    from rcp_supervisor.runtime import Paths

    paths = Paths()
    return Coordinator(
        store_for(paths),
        runtime(startup=startup),
        boundary=boundary,
    )


def qualified_restore() -> dict:
    from contextlib import redirect_stdout
    from functools import partial

    from rcp_supervisor import cli, driver
    from rcp_supervisor.operations import Coordinator

    request = read_json(ROOT / "restore.json")
    argv = ["--machine-readable", "server", "restore", request["archive_path"]]
    for field, flag in (
        ("identity_file", "--identity-file"),
        ("confirmed_data_dir", "--confirm-data-dir"),
        ("old_authority_disposition", "--old-authority-disposition"),
        ("confirm_old_authority", "--confirm-old-authority"),
        ("confirm_member_roster", "--confirm-member-roster"),
    ):
        argv.extend([flag, request[field]])
    driver.SystemRuntime = runtime_type()
    driver.Coordinator = partial(Coordinator, boundary=boundary)
    stream = io.StringIO()
    with redirect_stdout(stream):
        code = cli.main(argv)
    assert code == 0, stream.getvalue()
    return {"code": code, "events": [json.loads(line) for line in stream.getvalue().splitlines()]}


def filesystem(action: str, prefix: str) -> int:
    from rcp_supervisor import checkpoint, fs_worker

    payload = sys.stdin.buffer.read()
    request = json.loads(payload)
    if action == "restore":
        document = read_json(Path(request["directory"]) / "checkpoint.json")
        roots = [Path(item["live"]) for item in document["roots"]]
        original_replace = os.replace
        original_sync = checkpoint._fsync_directory
        pending = None

        def replace(source, destination, *args, **kwargs):
            nonlocal pending
            result = original_replace(source, destination, *args, **kwargs)
            for index, root in enumerate(roots):
                if Path(source) == root:
                    pending = (root.parent, f"{prefix}root_quarantined:{index}")
                if Path(destination) == root:
                    pending = (root.parent, f"{prefix}root_published:{index}")
            return result

        def sync(path):
            nonlocal pending
            original_sync(path)
            if pending is not None and pending[0] == path:
                _parent, name = pending
                pending = None
                boundary(name)

        os.replace = replace
        checkpoint._fsync_directory = sync
    sys.stdin = io.TextIOWrapper(io.BytesIO(payload))
    return fs_worker.main([action])


def session_cookie() -> str:
    fixture = read_json(STATE / "fixture.json")
    protocol = str(health()["team_shell_protocol"]["maximum"])
    exchange = urllib.request.Request(
        "http://127.0.0.1:8421/api/team/session/exchange",
        data=json.dumps({"token": fixture["token"]}).encode(),
        headers={
            "Content-Type": "application/json",
            "RCP-Team-Shell-Protocol": protocol,
        },
    )
    with urllib.request.urlopen(exchange, timeout=10) as response:
        assert response.headers["RCP-Team-Shell-Protocol"] == protocol
        assert json.load(response)["user"]["user_id"] == fixture["member_id"]
        cookies = http.cookies.SimpleCookie()
        cookies.load(response.headers["Set-Cookie"])
    assert len(cookies) == 1
    session = next(iter(cookies.values()))
    return f"{session.key}={session.value}"


def rename_space(name: str) -> dict:
    request = urllib.request.Request(
        "http://127.0.0.1:8421/api/team/space",
        data=json.dumps({"name": name}).encode(),
        method="PATCH",
        headers={"Content-Type": "application/json", "Cookie": session_cookie()},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
        value = json.load(response)
    assert value == {"space_name": name}
    return value


def accept_work() -> dict:
    value = rename_space("Work accepted after activation")
    write_json(STATE / "accepted.json", value, service_owned=True)
    return value


def verify_case() -> dict:
    from rcp_supervisor.launch import read_selected_receipt

    plan = read_json(PLAN)
    if plan["offline"]:
        try:
            with socket.create_connection(("1.1.1.1", 443), timeout=3):
                pass
        except OSError:
            pass
        else:
            raise AssertionError("Offline recovery unexpectedly retained guest Internet egress.")
    if plan["kind"] == "fresh_restore" and plan["pauses"][-1] != "candidate_chosen":
        from rcp_supervisor.driver import store_for
        from rcp_supervisor.runtime import Paths

        wait_for(
            lambda: (
                not (Paths().data_dir / "rcp.sqlite3").exists()
                and store_for(Paths()).active() is None
            ),
            "The interrupted fresh restore did not recover its empty data target.",
        )
        assert (
            run(
                ["systemctl", "show", "rcp.service", "--property=MainPID", "--value"]
            ).stdout.strip()
            == "0"
        )
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        events = [json.loads(line) for line in (STATE / "events.jsonl").read_text().splitlines()]
        this_boot = [item for item in events if item["boot_id"] == boot]
        assert not any(item["phase"] == "application_start" for item in this_boot)
        assert any(item.get("checkpoint_bytes_verified") for item in this_boot)
        try:
            health()
        except OSError:
            return {
                "status": "uninitialized_target_preserved",
                "admission_opened": False,
                "fresh_data_target": True,
                "existing_git_checkout_reused": True,
                "boot_events": this_boot,
                "network_egress_unavailable": plan["offline"],
            }
        raise AssertionError("Rolled-back fresh restore admitted an uninitialized application.")
    if plan["kind"] == "invalid":
        active = run(
            ["systemctl", "show", "rcp.service", "--property=ActiveState", "--value"]
        ).stdout.strip()
        assert active != "active"
        assert (
            run(
                ["systemctl", "show", "rcp.service", "--property=MainPID", "--value"]
            ).stdout.strip()
            == "0"
        )
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        events = [json.loads(line) for line in (STATE / "events.jsonl").read_text().splitlines()]
        assert not any(
            item["boot_id"] == boot and item["phase"] == "application_start" for item in events
        )
        try:
            health()
        except OSError:
            diagnostic = run(
                ["journalctl", "-b", "-u", "rcp.service", "--no-pager", "-o", "cat"]
            ).stdout
            assert "Deployment journal format is unsupported" in diagnostic
            return {
                "status": "refused",
                "service_state": active,
                "admission_opened": False,
                "diagnostic": "Deployment journal format is unsupported.",
            }
        raise AssertionError("An invalid deployment journal admitted application startup.")
    current_health = wait_health()
    selected = read_selected_receipt()
    candidate_committed = plan["pauses"][-1] in ("candidate_chosen", "committed", "post_admission")
    keep_target = candidate_committed and plan["kind"] == "update"
    expected = TARGET_BUILD if keep_target else BASE_BUILD
    assert selected["build"] == current_health["build"] == expected
    assert current_health["version"] == selected["version_string"]
    assert current_health["running_commit"] == selected["commit"]
    fixture = read_json(STATE / "fixture.json")
    assert current_health["space_id"] == fixture["space_id"]
    projects = urllib.request.Request(
        "http://127.0.0.1:8421/api/projects",
        headers={
            "Cookie": session_cookie(),
            "RCP-Team-Shell-Protocol": str(current_health["team_shell_protocol"]["maximum"]),
        },
    )
    with urllib.request.urlopen(projects, timeout=10) as response:
        assert [project["id"] for project in json.load(response)] == [fixture["project_id"]]
    expected_ledger = fixture["ledger_head"] + int(keep_target)
    assert current_health["schema_ledger_head"] == expected_ledger
    checkpoint_required = not (plan["kind"] in {"restore", "fresh_restore"} and candidate_committed)
    if checkpoint_required:
        assert (
            Path(fixture["stage"]) / "retained.txt"
        ).read_bytes() == b"Retained paused Work scratch.\n"
    attachment_files = list(
        Path("/home/rcp/rcp-server/data/chat-attachments").rglob(f"*{fixture['attachment_id']}.txt")
    )
    if checkpoint_required:
        assert len(attachment_files) == 1
        assert attachment_files[0].read_bytes() == b"Retained attachment before deployment.\n"
    patches = Path(fixture["research"]) / "patches"
    assert {
        str(path.relative_to(patches)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in patches.rglob("*")
        if path.is_file()
    } == fixture["canonical_patches"]
    events = [json.loads(line) for line in (STATE / "events.jsonl").read_text().splitlines()]
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    this_boot = [item for item in events if item["boot_id"] == boot]
    phases = [item["phase"] for item in this_boot]
    assert "recovery_ready" in phases and "application_start" in phases
    assert phases.index("recovery_ready") < phases.index("application_start")
    if (STATE / "accepted.json").exists():
        assert current_health["space_name"] == read_json(STATE / "accepted.json")["space_name"]
    else:
        expected_name = (
            "Reboot qualification"
            if plan["kind"] in {"restore", "fresh_restore"} and candidate_committed
            else "Work accepted after protected archive"
        )
        assert current_health["space_name"] == expected_name
    return {
        "status": "verified",
        "build": expected,
        "selected_release": selected,
        "ledger_head": expected_ledger,
        "boot_events": this_boot,
        "checkpoint_stage_attachment_required": checkpoint_required,
        "canonical_history_preserved": True,
        "member_reconnected": True,
        "accepted_work_preserved": (STATE / "accepted.json").exists(),
        "fresh_data_target": plan["kind"] == "fresh_restore",
        "network_egress_unavailable": plan["offline"],
    }


def main() -> int:
    action, *arguments = sys.argv[1:]
    require_guest(
        root=action not in ("install-release", "prepare-data", "application", "filesystem")
    )
    if action == "bootstrap":
        result = bootstrap()
    elif action == "setup":
        result = setup()
    elif action == "install-release":
        from rcp_supervisor.install import install_release

        result = {
            "release": str(
                install_release(Path(arguments[0]), Path("/home/rcp/rcp-server/releases"))
            )
        }
    elif action == "prepare-data":
        from tests.supervisor_reboot_data import prepare_data

        fixture = prepare_data(
            Path("/home/rcp/rcp-server/data"),
            Path("/home/rcp/rcp-server/projects"),
            bootstrap_code=read_json(STATE / "bootstrap.json")["code"],
        )
        (STATE / "bootstrap.json").unlink()
        write_json(STATE / "fixture.json", fixture, service_owned=True)
        result = {"status": "prepared"}
    elif action == "qualified-install":
        result = qualified_install()
    elif action == "prepare-restore-request":
        prepare_restore_request(Path(arguments[0]), read_json(ROOT / "releases.json")["base"])
        result = {"status": "reviewed"}
    elif action == "application":
        from rcp_supervisor.launch import main as launch

        event("application_start")
        return launch(
            ["serve", "--host", "127.0.0.1", "--port", "8421", "--web-assets", "prebuilt"]
        )
    elif action == "filesystem":
        return filesystem(arguments[0], arguments[1])
    elif action == "prepare-case":
        result = prepare_case(Path(arguments[0]))
    elif action == "run-case":
        releases = read_json(ROOT / "releases.json")
        try:
            kind = read_json(PLAN)["kind"]
            if kind == "fresh_restore":
                result = qualified_restore()
            else:
                result = coordinator(startup=False).deploy(
                    releases["base"],
                    releases["base"] if kind == "restore" else releases["target"],
                    kind=kind,
                )
        except Exception as exc:
            write_json(STATE / "error.json", {"error": str(exc)}, service_owned=True)
            raise
    elif action == "recover":
        from rcp_supervisor import driver

        event("recovery_started")
        # The production startup driver owns the chosen-operation lock bypass.
        # Inject only the guest fault observer into that actual control path.
        driver.SystemRuntime = runtime_type()
        result = driver.recover(startup=True, boundary=boundary)
        event("recovery_ready")
    elif action == "status":
        result = (
            read_json(STATE / "error.json")
            if (STATE / "error.json").exists()
            else read_json(STATE / "paused.json")
            if (STATE / "paused.json").exists()
            else {"phase": "waiting"}
        )
    elif action == "accept-work":
        result = accept_work()
    elif action == "verify-case":
        result = verify_case()
    else:
        raise ValueError("Unknown guest qualification action.")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
