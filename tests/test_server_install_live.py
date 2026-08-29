"""Destructive, explicitly gated qualification of the source-server installer.

This test is intentionally absent from ordinary CI execution. It owns an entire
disposable Ubuntu host, installs system state, and temporarily adds one read-only
deploy key to the private source repository. See ``docs/server.md``.
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import pwd
import re
import select
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import BinaryIO

import pytest
from pydantic import TypeAdapter

from rcp.server_ops.models import ServerCommandEvent

_LIVE_GATE = "RCP_RUN_SERVER_INSTALL_LIVE"
_DISPOSABLE_CONFIRMATION = "RCP_SERVER_INSTALL_LIVE_DISPOSABLE"
_EXPECTED_DISPOSABLE_CONFIRMATION = "I_UNDERSTAND_THIS_MUTATES_THE_HOST"
_TOKEN_FILE = "RCP_LIVE_GITHUB_ADMIN_TOKEN_FILE"
_DEPLOY_KEY_RECEIPT_FILE = "RCP_LIVE_DEPLOY_KEY_RECEIPT_FILE"
_REPOSITORY = "Zhi0467/RCP"
_TEAM_NAME = "RCP live install qualification"
_GITHUB_ED25519_FINGERPRINT = "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
_BOOTSTRAP_CODE = re.compile(r"rcp_bootstrap_[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{43}")
_REQUEST_ID = "00000000-0000-4000-8000-000000000000"
_COMMAND_TIMEOUT_SECONDS = 45 * 60
_PTY_TIMEOUT_SECONDS = 60
_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_MAX_COMMAND_OUTPUT_BYTES = 32 * 1024 * 1024
_EVENT_ADAPTER = TypeAdapter(ServerCommandEvent)

_LIVE_TEST_ONLY = pytest.mark.skipif(
    os.environ.get(_LIVE_GATE) != "1",
    reason="destructive disposable-host server-install qualification is disabled",
)


@_LIVE_TEST_ONLY
def test_source_server_install_on_disposable_ubuntu() -> None:
    """Drive the documented install, removal, SSH, and service readback."""

    _require_explicit_disposable_host()
    workspace = _workspace()
    token = _read_admin_token()
    bootstrap_parent = Path(tempfile.mkdtemp(prefix="rcp-server-install-live-"))
    bootstrap = bootstrap_parent / "rcp-bootstrap"
    deploy_key_id: int | None = None

    try:
        _prepare_bootstrap(workspace, bootstrap)
        executable = bootstrap / ".venv" / "bin" / "rcp"

        first_code, first_events = _run_install(executable, cwd=bootstrap)
        assert first_code == 3
        source_pause = _terminal_step(first_events, "source_grant")
        assert source_pause["state"] == "operator_action_needed"
        source_fields = _fields(source_pause)
        assert source_fields["deploy_key_label"].startswith("rcp-source:")
        assert source_fields["public_key_fingerprint"].startswith("SHA256:")

        _write_deploy_key_receipt(str(source_fields["deploy_key_label"]))
        deploy_key_id = _create_read_only_deploy_key(
            token,
            title=str(source_fields["deploy_key_label"]),
            public_key=str(source_fields["deploy_public_key"]),
        )
        trust_argv = _command_actions(source_pause)[0]
        trust_code, trust_output = _run_pty(trust_argv, answer_host_key=True)
        assert trust_code == 1, (
            "GitHub's successful no-shell SSH probe must exit 1; "
            f"output tail={trust_output[-4096:]!r}"
        )
        assert _GITHUB_ED25519_FINGERPRINT in trust_output
        assert "successfully authenticated" in trust_output.lower()

        second_code, second_events = _run_install(executable, cwd=bootstrap)
        assert second_code == 3
        init_pause = _terminal_step(second_events, "team_space_init")
        assert init_pause["state"] == "operator_action_needed"
        init_commands = _command_actions(init_pause)
        assert len(init_commands) == 1

        shutil.rmtree(bootstrap_parent)
        assert not bootstrap_parent.exists()

        init_code, init_output = _run_pty(init_commands[0])
        assert init_code == 0, "interactive team initialization failed"
        bootstrap_codes = _BOOTSTRAP_CODE.findall(init_output)
        if len(bootstrap_codes) != 1:
            pytest.fail("interactive team initialization did not show exactly one bootstrap code")

        final_code, final_events = _run_install(Path("/usr/local/bin/rcp"), cwd=Path("/tmp"))
        assert final_code == 0
        final_step = _terminal_step(final_events, "service_activate")
        assert final_step["state"] == "succeeded"
        assert _fields(final_step) == {
            "status": "ok",
            "space_kind": "team",
            "space_name": _TEAM_NAME,
        }
        health = json.loads(
            _run_checked(("curl", "--fail", "--silent", "http://127.0.0.1:8421/api/health")).stdout
        )
        assert health["status"] == "ok"
        assert health["space_kind"] == "team"
        assert health["space_name"] == _TEAM_NAME

        _assert_installed_ownership_and_modes()
        _assert_service_process_and_listener()
        _assert_password_refused_and_public_key_accepted()
        _assert_narrow_operator_rule()

        journal = _run_checked(
            ("sudo", "-n", "journalctl", "--unit=rcp.service", "--no-pager", "--output=cat")
        ).stdout
        if "rcp_bootstrap_" in journal:
            pytest.fail("the one-time team bootstrap code entered the service journal")

        _delete_deploy_key(token, deploy_key_id)
        deploy_key_id = None
        _clear_deploy_key_receipt()
        _run_checked(("sudo", "-n", "systemctl", "restart", "rcp.service"))
        restarted = json.loads(
            _run_checked(("curl", "--fail", "--silent", "http://127.0.0.1:8421/api/health")).stdout
        )
        assert restarted["status"] == "ok"
        assert restarted["space_kind"] == "team"
    finally:
        if deploy_key_id is not None:
            _delete_deploy_key(token, deploy_key_id)
            _clear_deploy_key_receipt()
        if bootstrap_parent.exists():
            shutil.rmtree(bootstrap_parent)


def _require_explicit_disposable_host() -> None:
    if os.environ.get(_DISPOSABLE_CONFIRMATION) != _EXPECTED_DISPOSABLE_CONFIRMATION:
        pytest.fail(
            f"set {_DISPOSABLE_CONFIRMATION}={_EXPECTED_DISPOSABLE_CONFIRMATION} only on "
            "an entire disposable host"
        )
    if os.geteuid() == 0:
        pytest.fail("run pytest as the ordinary disposable-host operator, not root")
    if os.uname().machine != "x86_64":
        pytest.fail("the live installer qualification requires x86-64")
    release = _os_release().get("VERSION_ID")
    if _os_release().get("ID") != "ubuntu" or release not in {"22.04", "24.04"}:
        pytest.fail("the live installer qualification requires Ubuntu 22.04 or 24.04")
    if os.environ.get("GITHUB_REPOSITORY") != _REPOSITORY:
        pytest.fail(f"the live source grant is fixed to {_REPOSITORY}")
    _run_checked(("sudo", "-n", "true"))
    for path in (
        Path("/etc/rcp"),
        Path("/etc/systemd/system/rcp.service"),
        Path("/etc/systemd/system/multi-user.target.wants/rcp.service"),
        Path("/etc/sudoers.d/rcp-project-provision"),
        Path("/etc/sudoers.d/rcp-project-provision-live-test"),
        Path("/home/rcp"),
        Path("/lib/systemd/system/rcp.service"),
        Path("/run/rcp"),
        Path("/usr/lib/systemd/system/rcp.service"),
        Path("/usr/local/bin/rcp"),
    ):
        if _root_path_exists_or_is_symlink(path):
            pytest.fail(f"disposable host is not clean: {path} already exists")
    for account in ("rcp", "rcp-live-operator"):
        try:
            pwd.getpwnam(account)
        except KeyError:
            pass
        else:
            pytest.fail(f"disposable host is not clean: the {account} account already exists")
    unit_state = _run_checked(
        ("sudo", "-n", "systemctl", "show", "--property=LoadState", "--value", "rcp.service")
    ).stdout.strip()
    if unit_state != "not-found":
        pytest.fail(f"disposable host is not clean: rcp.service load state is {unit_state!r}")
    listeners = _run_checked(("sudo", "-n", "ss", "--tcp", "--listening", "--numeric")).stdout
    if any(re.search(r":8421\s", line) for line in listeners.splitlines()):
        pytest.fail("disposable host is not clean: TCP port 8421 already has a listener")
    processes = _run_checked(("ps", "-eo", "args=")).stdout
    if any(_looks_like_rcp_server(line) for line in processes.splitlines()):
        pytest.fail("disposable host is not clean: an RCP server process is already running")


def _root_path_exists_or_is_symlink(path: Path) -> bool:
    for predicate in ("-e", "-L"):
        result = _run(
            ("sudo", "-n", "test", predicate, str(path)),
            timeout=_PTY_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            return True
        if result.returncode != 1:
            pytest.fail(f"could not inspect root-owned path {path}")
    return False


@pytest.mark.parametrize(
    ("return_codes", "expected"),
    [
        ((0,), True),
        ((1, 0), True),
        ((1, 1), False),
    ],
)
def test_root_path_probe_uses_sudo(
    monkeypatch: pytest.MonkeyPatch,
    return_codes: tuple[int, ...],
    expected: bool,
) -> None:
    calls: list[tuple[str, ...]] = []
    results = iter(return_codes)

    def fake_run(
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment, timeout
        calls.append(argv)
        return subprocess.CompletedProcess(argv, next(results), "", "")

    monkeypatch.setattr(sys.modules[__name__], "_run", fake_run)
    path = Path("/etc/sudoers.d/rcp-project-provision")

    assert _root_path_exists_or_is_symlink(path) is expected
    assert calls == [
        ("sudo", "-n", "test", predicate, str(path))
        for predicate in ("-e", "-L")[: len(return_codes)]
    ]


def test_root_path_probe_fails_on_probe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment, timeout
        return subprocess.CompletedProcess(argv, 2, "", "permission failure")

    monkeypatch.setattr(sys.modules[__name__], "_run", fake_run)

    with pytest.raises(pytest.fail.Exception, match="could not inspect root-owned path"):
        _root_path_exists_or_is_symlink(Path("/etc/sudoers.d/rcp-project-provision"))


def _looks_like_rcp_server(command: str) -> bool:
    return bool(
        re.search(r"(?:^|\s)(?:\S*/)?rcp\s+serve(?:\s|$)", command)
        or re.search(r"(?:^|\s)python\S*\s+-m\s+rcp\s+serve(?:\s|$)", command)
        or "/usr/local/bin/rcp serve" in command
    )


def _workspace() -> Path:
    raw = os.environ.get("GITHUB_WORKSPACE")
    if not raw:
        pytest.fail("GITHUB_WORKSPACE is required for the guarded live drive")
    workspace = Path(raw)
    if not workspace.is_absolute() or not (workspace / ".git").exists():
        pytest.fail("GITHUB_WORKSPACE must be an absolute Git checkout")
    status = _run_checked(("git", "-C", str(workspace), "status", "--porcelain")).stdout
    if status:
        pytest.fail("the live bootstrap source must be a clean checkout")
    head = _run_checked(("git", "-C", str(workspace), "rev-parse", "HEAD")).stdout.strip()
    expected = os.environ.get("GITHUB_SHA")
    if expected and head != expected:
        pytest.fail("GITHUB_WORKSPACE HEAD differs from GITHUB_SHA")
    return workspace


def _read_admin_token() -> str:
    raw = os.environ.get(_TOKEN_FILE)
    if not raw:
        pytest.fail(f"{_TOKEN_FILE} must name a protected fine-grained token file")
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        pytest.fail(f"{_TOKEN_FILE} must be an absolute regular non-symlink file")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        pytest.fail(f"{_TOKEN_FILE} must be owned by the caller and inaccessible to group/other")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 20 or any(ord(character) < 33 or ord(character) == 127 for character in token):
        pytest.fail(f"{_TOKEN_FILE} does not contain one plausible token")
    return token


def _deploy_key_receipt_path() -> Path:
    raw = os.environ.get(_DEPLOY_KEY_RECEIPT_FILE)
    if not raw:
        pytest.fail(f"{_DEPLOY_KEY_RECEIPT_FILE} must name a protected cleanup receipt")
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink() or not path.parent.is_dir():
        pytest.fail(f"{_DEPLOY_KEY_RECEIPT_FILE} must be an absolute new regular-file path")
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022:
        pytest.fail(
            f"{_DEPLOY_KEY_RECEIPT_FILE} parent must be caller-owned and not writable by others"
        )
    return path


def _write_deploy_key_receipt(label: str) -> None:
    if re.fullmatch(r"rcp-source:[0-9a-f-]{36}", label) is None:
        pytest.fail("the generated source-key label is not safe for cleanup")
    path = _deploy_key_receipt_path()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        pytest.fail("the deploy-key cleanup receipt already exists or cannot be created safely")
    try:
        os.write(descriptor, f"{label}\n".encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _clear_deploy_key_receipt() -> None:
    path = _deploy_key_receipt_path()
    if path.exists():
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            pytest.fail("the deploy-key cleanup receipt changed ownership or mode")
        path.unlink()


def _prepare_bootstrap(workspace: Path, bootstrap: Path) -> None:
    _run_checked(("git", "clone", "--no-hardlinks", str(workspace), str(bootstrap)))
    _run_checked(
        (
            "git",
            "-C",
            str(bootstrap),
            "remote",
            "set-url",
            "origin",
            f"https://github.com/{_REPOSITORY}.git",
        )
    )
    _run_checked(("npm", "--prefix", "web", "ci"), cwd=bootstrap)
    _run_checked(("npm", "--prefix", "web", "run", "build"), cwd=bootstrap)
    environment = os.environ.copy()
    environment.update({"UV_MANAGED_PYTHON": "1", "UV_PYTHON": "3.12"})
    _run_checked(("uv", "sync", "--frozen"), cwd=bootstrap, environment=environment)
    if not (bootstrap / ".venv" / "bin" / "rcp").is_file():
        pytest.fail("the documented bootstrap build did not create .venv/bin/rcp")


def _run_install(
    executable: Path,
    *,
    cwd: Path,
) -> tuple[int, list[dict[str, object]]]:
    result = _run(
        (
            "sudo",
            "-n",
            "/usr/bin/env",
            "PYTHONDONTWRITEBYTECODE=1",
            str(executable),
            "server",
            "install",
            "--team-name",
            _TEAM_NAME,
            "--machine-readable",
        ),
        cwd=cwd,
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode not in {0, 3}:
        pytest.fail(
            f"server install returned unexpected exit status {result.returncode}; "
            f"stdout tail={result.stdout[-4096:]!r}; stderr tail={result.stderr[-4096:]!r}"
        )
    lines = result.stdout.splitlines()
    if not lines:
        pytest.fail("server install emitted no machine-readable events")
    events: list[dict[str, object]] = []
    for line in lines:
        try:
            event = _EVENT_ADAPTER.validate_json(line)
        except Exception:
            pytest.fail("server install mixed non-JSON output into its machine-readable stream")
        events.append(event.model_dump(mode="json"))
    assert events[0]["event"] == "plan"
    assert events[0]["command"] == "server install"
    return result.returncode, events


def _terminal_step(events: list[dict[str, object]], phase: str) -> dict[str, object]:
    final = events[-1]
    assert final["event"] == "step"
    step = final["step"]
    assert isinstance(step, dict)
    assert step["phase"] == phase
    return step


def _fields(step: dict[str, object]) -> dict[str, object]:
    fields = step["fields"]
    assert isinstance(fields, list)
    return {str(item["name"]): item["value"] for item in fields if isinstance(item, dict)}


def _command_actions(step: dict[str, object]) -> list[tuple[str, ...]]:
    actions = step["actions"]
    assert isinstance(actions, list)
    commands = []
    for action in actions:
        if isinstance(action, dict) and action.get("kind") == "command":
            argv = action.get("argv")
            assert isinstance(argv, list) and all(isinstance(value, str) for value in argv)
            commands.append(tuple(argv))
    return commands


def _create_read_only_deploy_key(token: str, *, title: str, public_key: str) -> int:
    response = _github_request(
        token,
        method="POST",
        path=f"/repos/{_REPOSITORY}/keys",
        body={"title": title, "key": public_key, "read_only": True},
    )
    key_id = response.get("id")
    if not isinstance(key_id, int) or response.get("read_only") is not True:
        pytest.fail("GitHub did not confirm one read-only deploy key")
    return key_id


def _delete_deploy_key(token: str, key_id: int) -> None:
    _github_request(
        token,
        method="DELETE",
        path=f"/repos/{_REPOSITORY}/keys/{key_id}",
        body=None,
    )


def _github_request(
    token: str,
    *,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> dict[str, object]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        method=method,
        data=payload,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "rcp-server-install-live-test",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read(64 * 1024 + 1)
            status_code = response.status
    except urllib.error.HTTPError as exc:
        pytest.fail(f"GitHub deploy-key API returned HTTP {exc.code}")
    except urllib.error.URLError:
        pytest.fail("GitHub deploy-key API was unreachable")
    if len(content) > 64 * 1024:
        pytest.fail("GitHub deploy-key API response exceeded the live-test bound")
    if method == "DELETE":
        if status_code != 204:
            pytest.fail(f"GitHub deploy-key deletion returned HTTP {status_code}")
        return {}
    if status_code != 201:
        pytest.fail(f"GitHub deploy-key creation returned HTTP {status_code}")
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        pytest.fail("GitHub deploy-key API returned invalid JSON")
    if not isinstance(value, dict):
        pytest.fail("GitHub deploy-key API returned a non-object")
    return value


def _run_pty(
    argv: tuple[str, ...],
    *,
    answer_host_key: bool = False,
) -> tuple[int, str]:
    master, slave = pty.openpty()
    process = subprocess.Popen(
        argv,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    output = bytearray()
    answered = False
    deadline = time.monotonic() + _PTY_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.2)
            if readable:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                if not chunk and process.poll() is not None:
                    break
                output.extend(chunk)
                if len(output) > _MAX_OUTPUT_BYTES:
                    process.kill()
                    pytest.fail("interactive live-test output exceeded its bound")
                text = output.decode("utf-8", errors="replace")
                if answer_host_key and not answered and "Are you sure" in text:
                    if _GITHUB_ED25519_FINGERPRINT not in text:
                        process.kill()
                        pytest.fail("GitHub host prompt did not show the published fingerprint")
                    os.write(master, b"yes\n")
                    answered = True
            if process.poll() is not None and not readable:
                break
        else:
            process.kill()
            pytest.fail("interactive live-test command timed out")
        return process.wait(timeout=5), output.decode("utf-8", errors="replace")
    finally:
        os.close(master)


def _assert_installed_ownership_and_modes() -> None:
    account = pwd.getpwnam("rcp")
    assert account.pw_dir == "/home/rcp"
    assert account.pw_shell == "/bin/bash"
    shadow = _run_checked(("sudo", "-n", "getent", "shadow", "rcp")).stdout.split(":")
    assert shadow[1] == "*NP*"

    for path in (
        Path("/home/rcp"),
        Path("/home/rcp/rcp-server"),
        Path("/home/rcp/rcp-server/data"),
        Path("/home/rcp/rcp-server/credentials"),
        Path("/home/rcp/.ssh"),
    ):
        _assert_path(path, uid=account.pw_uid, gid=account.pw_gid, mode=0o700)
    _assert_path(
        Path("/home/rcp/rcp-server/credentials/source_ed25519"),
        uid=account.pw_uid,
        gid=account.pw_gid,
        mode=0o600,
    )
    _assert_path(
        Path("/home/rcp/rcp-server/credentials/source_ed25519.pub"),
        uid=account.pw_uid,
        gid=account.pw_gid,
        mode=0o644,
    )
    _assert_path(Path("/etc/rcp/server.toml"), uid=0, gid=account.pw_gid, mode=0o640)
    _assert_path(Path("/usr/local/bin/rcp"), uid=0, gid=0, mode=0o755)
    _assert_path(Path("/etc/systemd/system/rcp.service"), uid=0, gid=0, mode=0o644)
    current = Path("/etc/rcp/current")
    info = current.lstat()
    assert stat.S_ISLNK(info.st_mode)
    assert (info.st_uid, info.st_gid) == (0, 0)
    target = Path(os.readlink(current))
    assert target.is_absolute()
    assert target.parent == Path("/home/rcp/rcp-server/releases")
    assert re.fullmatch(r"[0-9a-f]{40}", target.name)
    assert target.is_dir()
    assert target.stat().st_uid == account.pw_uid


def _assert_path(path: Path, *, uid: int, gid: int, mode: int) -> None:
    info = path.stat()
    assert (info.st_uid, info.st_gid) == (uid, gid)
    assert stat.S_IMODE(info.st_mode) == mode


def _assert_service_process_and_listener() -> None:
    account = pwd.getpwnam("rcp")
    main_pid = int(
        _run_checked(
            (
                "sudo",
                "-n",
                "systemctl",
                "show",
                "--property=MainPID",
                "--value",
                "rcp.service",
            )
        ).stdout
    )
    assert main_pid > 1
    assert Path(f"/proc/{main_pid}").stat().st_uid == account.pw_uid
    listeners = _run_checked(("sudo", "-n", "ss", "--tcp", "--listening", "--numeric")).stdout
    matching = [line for line in listeners.splitlines() if re.search(r":8421\s", line)]
    assert matching
    assert all("127.0.0.1:8421" in line for line in matching)


def _assert_password_refused_and_public_key_accepted() -> None:
    _run_checked(("sudo", "-n", "systemctl", "start", "ssh.service"))
    common = (
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
    )
    password = _run(
        (
            "ssh",
            *common,
            "-o",
            "BatchMode=yes",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "PreferredAuthentications=password",
            "rcp@127.0.0.1",
            "true",
        ),
        timeout=30,
    )
    assert password.returncode == 255

    key_root = Path(tempfile.mkdtemp(prefix="rcp-live-client-key-"))
    key = key_root / "id_ed25519"
    try:
        _run_checked(("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)))
        _run_checked(
            (
                "sudo",
                "-n",
                "install",
                "--owner=rcp",
                "--group=rcp",
                "--mode=0600",
                str(key.with_suffix(".pub")),
                "/home/rcp/.ssh/authorized_keys",
            )
        )
        public_key = _run_checked(
            (
                "ssh",
                *common,
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-i",
                str(key),
                "rcp@127.0.0.1",
                "id",
                "-un",
            ),
            timeout=30,
        )
        assert public_key.stdout.strip() == "rcp"
        assert (
            _run_checked(("sudo", "-n", "systemctl", "is-active", "rcp.service")).stdout.strip()
            == "active"
        )
    finally:
        _run_checked(("sudo", "-n", "rm", "-f", "--", "/home/rcp/.ssh/authorized_keys"))
        shutil.rmtree(key_root)
    assert not Path("/home/rcp/.ssh/authorized_keys").exists()


def _assert_narrow_operator_rule() -> None:
    operator = "rcp-live-operator"
    target = Path("/etc/sudoers.d/rcp-project-provision-live-test")
    operator_created = False
    descriptor, rule_name = tempfile.mkstemp(prefix="rcp-live-sudoers-")
    os.close(descriptor)
    rule_source = Path(rule_name)
    try:
        _run_checked(
            (
                "sudo",
                "-n",
                "useradd",
                "--create-home",
                "--shell",
                "/bin/bash",
                "--user-group",
                operator,
            )
        )
        operator_created = True
        rule_source.write_text(
            f"{operator} ALL=(rcp) NOPASSWD: /usr/local/bin/rcp server project provision * "
            "--machine-readable\n",
            encoding="utf-8",
        )
        os.chmod(rule_source, 0o600)
        _run_checked(
            (
                "sudo",
                "-n",
                "install",
                "--owner=root",
                "--group=root",
                "--mode=0440",
                str(rule_source),
                str(target),
            )
        )
        _run_checked(("sudo", "-n", "visudo", "--check", "--file", str(target)))
        allowed = _run(
            (
                "sudo",
                "-n",
                "-u",
                operator,
                "-H",
                "sudo",
                "-n",
                "-u",
                "rcp",
                "-H",
                "/usr/local/bin/rcp",
                "server",
                "project",
                "provision",
                _REQUEST_ID,
                "--machine-readable",
            ),
            timeout=30,
        )
        assert allowed.returncode == 69
        assert json.loads(allowed.stdout.splitlines()[-1])["step"]["state"] == "unavailable"
        denied = _run(
            (
                "sudo",
                "-n",
                "-u",
                operator,
                "-H",
                "sudo",
                "-n",
                "-u",
                "rcp",
                "-H",
                "/usr/bin/id",
            ),
            timeout=30,
        )
        assert denied.returncode != 0
    finally:
        _run_checked(("sudo", "-n", "rm", "-f", "--", str(target)))
        if operator_created:
            _run_checked(("sudo", "-n", "userdel", "--remove", operator))
        rule_source.unlink(missing_ok=True)
    assert not target.exists()
    with pytest.raises(KeyError):
        pwd.getpwnam(operator)


def _run_checked(
    argv: tuple[str, ...],
    *,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    timeout: float = _COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    result = _run(argv, cwd=cwd, environment=environment, timeout=timeout)
    if result.returncode != 0:
        pytest.fail(f"live-test command {argv[0]!r} returned {result.returncode}")
    return result


def _run(
    argv: tuple[str, ...],
    *,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    streams: dict[int, tuple[BinaryIO, bytearray]] = {
        process.stdout.fileno(): (process.stdout, stdout_buffer),
        process.stderr.fileno(): (process.stderr, stderr_buffer),
    }
    selector = selectors.DefaultSelector()
    for stream, _ in streams.values():
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(process)
                pytest.fail(f"live-test command {argv[0]!r} timed out")
            for key, _ in selector.select(timeout=min(remaining, 0.5)):
                stream = key.fileobj
                chunk = os.read(stream.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                streams[stream.fileno()][1].extend(chunk)
                if sum(len(output) for _, output in streams.values()) > _MAX_COMMAND_OUTPUT_BYTES:
                    _kill_process_group(process)
                    pytest.fail(f"live-test command {argv[0]!r} exceeded its output bound")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process_group(process)
            pytest.fail(f"live-test command {argv[0]!r} timed out")
        return_code = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        pytest.fail(f"live-test command {argv[0]!r} timed out")
    finally:
        selector.close()
        for stream, _ in streams.values():
            stream.close()
    stdout = stdout_buffer.decode("utf-8", errors="replace")
    stderr = stderr_buffer.decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(argv, return_code, stdout, stderr)


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


def test_bounded_command_runner_keeps_separate_output() -> None:
    result = _run(
        (
            sys.executable,
            "-c",
            "import sys; print('ordinary output'); print('diagnostic', file=sys.stderr)",
        ),
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == "ordinary output\n"
    assert result.stderr == "diagnostic\n"


def test_bounded_command_runner_stops_excess_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "_MAX_COMMAND_OUTPUT_BYTES", 128)

    with pytest.raises(pytest.fail.Exception, match="exceeded its output bound"):
        _run((sys.executable, "-c", "print('x' * 4096)"), timeout=5)


def _os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values
