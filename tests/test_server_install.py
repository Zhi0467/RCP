from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.server_ops import install as server_install
from rcp.server_ops.install import HostFacts, InstallRefused
from rcp.server_ops.layout import ServerLayout


def test_service_account_commands_clear_invoking_credentials_and_use_fixed_home(
    monkeypatch,
) -> None:
    captured: list[tuple[tuple[str, ...], dict[str, object]]] = []
    account = SimpleNamespace(pw_name="rcp", pw_dir="/home/rcp")

    def fake_run(argv, **kwargs):
        captured.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setenv("SSH_AUTH_SOCK", "/operator/agent.sock")
    monkeypatch.setenv("GH_TOKEN", "operator-secret")
    monkeypatch.setattr(server_install, "_run_process", fake_run)

    server_install._run_as_account(account, ("git", "--version"), timeout=1)
    explicit_cwd = Path("/srv/rcp/source")
    server_install._run_as_account(
        account,
        ("git", "status"),
        cwd=explicit_cwd,
        timeout=1,
    )

    argv, default_kwargs = captured[0]
    assert argv[:7] == (
        "runuser",
        "--user",
        "rcp",
        "--",
        "env",
        "-i",
        "HOME=/home/rcp",
    )
    assert "GIT_TERMINAL_PROMPT=0" in argv
    assert (
        "PATH=/home/rcp/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    ) in argv
    assert all("SSH_AUTH_SOCK" not in token and "GH_TOKEN" not in token for token in argv)
    assert default_kwargs["cwd"] == Path("/home/rcp")
    assert captured[1][1]["cwd"] == explicit_cwd


def test_data_state_refuses_unknown_content_without_opening_sqlite(tmp_path: Path) -> None:
    layout = _temporary_layout(tmp_path)
    layout.data_dir.mkdir(parents=True)
    machine = server_install.LinuxInstallMachine(layout)
    machine._service_uid = os.getuid()
    machine._service_gid = os.getgid()

    assert machine._data_state() == "fresh"
    unknown = layout.data_dir / "unknown.bin"
    unknown.write_bytes(b"unknown")
    with pytest.raises(InstallRefused, match="files but no initialized"):
        machine._data_state()
    unknown.unlink()
    database = layout.data_dir / "rcp.sqlite3"
    database.write_bytes(b"not opened by install")
    database.chmod(0o600)
    assert machine._data_state() == "initialized"


def test_supported_host_preflight_checks_exact_versions_without_installing_tools(
    monkeypatch,
) -> None:
    original_is_dir = Path.is_dir

    monkeypatch.setattr(
        server_install,
        "_read_os_release",
        lambda _path: {"ID": "ubuntu", "VERSION_ID": "24.04"},
    )
    monkeypatch.setattr(server_install.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda path: True if path == Path("/run/systemd/system") else original_is_dir(path),
    )
    monkeypatch.setattr(server_install.shutil, "which", lambda command: f"/usr/bin/{command}")

    def fake_require(argv, _error, **_kwargs):
        stdout = ""
        if argv[0] == "node":
            stdout = "v24.4.0\n"
        elif argv[0] == "age":
            stdout = "1.2.1\n"
        elif argv[:3] == ("systemctl", "show", "--property=Version"):
            stdout = "249\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(server_install, "_require_command", fake_require)

    assert server_install.LinuxInstallMachine().validate_host() == HostFacts(ubuntu_release="24.04")


def test_host_preflight_refuses_when_systemd_manager_is_not_reachable(monkeypatch) -> None:
    original_is_dir = Path.is_dir
    monkeypatch.setattr(
        server_install,
        "_read_os_release",
        lambda _path: {"ID": "ubuntu", "VERSION_ID": "22.04"},
    )
    monkeypatch.setattr(server_install.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda path: True if path == Path("/run/systemd/system") else original_is_dir(path),
    )
    monkeypatch.setattr(server_install.shutil, "which", lambda command: f"/usr/bin/{command}")

    def fake_require(argv, error, **_kwargs):
        if argv[:3] == ("systemctl", "show", "--property=Version"):
            raise InstallRefused(error)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(server_install, "_require_command", fake_require)

    with pytest.raises(InstallRefused, match="PID 1"):
        server_install.LinuxInstallMachine().validate_host()


@pytest.mark.parametrize(
    ("release", "architecture", "message"),
    [
        ("20.04", "x86_64", "Ubuntu 22.04 or 24.04"),
        ("24.04", "aarch64", "x86-64"),
    ],
)
def test_host_preflight_refuses_unsupported_release_or_architecture(
    monkeypatch,
    release,
    architecture,
    message,
) -> None:
    monkeypatch.setattr(
        server_install,
        "_read_os_release",
        lambda _path: {"ID": "ubuntu", "VERSION_ID": release},
    )
    monkeypatch.setattr(server_install.platform, "machine", lambda: architecture)

    with pytest.raises(InstallRefused, match=message):
        server_install.LinuxInstallMachine().validate_host()


def test_existing_service_account_must_be_unprivileged_and_have_no_sudo_policy(
    monkeypatch,
    tmp_path: Path,
) -> None:
    layout = _temporary_layout(tmp_path)
    machine = server_install.LinuxInstallMachine(layout)

    def account(*, uid: int = 701, gid: int = 702):
        return SimpleNamespace(
            pw_name="rcp",
            pw_uid=uid,
            pw_gid=gid,
            pw_dir=str(layout.service_home),
            pw_shell="/bin/bash",
        )

    current = account()
    monkeypatch.setattr(server_install.pwd, "getpwnam", lambda _name: current)
    monkeypatch.setattr(
        server_install.grp,
        "getgrgid",
        lambda gid: SimpleNamespace(gr_name="rcp", gr_gid=gid),
    )
    monkeypatch.setattr(server_install.grp, "getgrall", lambda: [])
    monkeypatch.setattr(
        server_install,
        "_require_command",
        lambda argv, _error, **_kwargs: subprocess.CompletedProcess(
            argv,
            0,
            "rcp:*NP*:20000:0:99999:7:::\n",
            "",
        ),
    )

    current = account(uid=0)
    with pytest.raises(InstallRefused, match="root user or group"):
        machine._converge_account()

    current = account(gid=0)
    with pytest.raises(InstallRefused, match="root user or group"):
        machine._converge_account()

    current = account()
    sudo_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def privileged_policy(argv, **kwargs):
        sudo_calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            "User rcp may run /bin/bash on lab.\n",
            "",
        )

    monkeypatch.setattr(server_install, "_run_process", privileged_policy)
    with pytest.raises(InstallRefused, match="has sudo authority"):
        machine._converge_account()
    assert sudo_calls == [
        (
            ("sudo", "-n", "-U", "rcp", "-l"),
            {
                "environment": {"LANG": "C", "LC_ALL": "C"},
                "timeout": server_install.SERVER_INSTALL_PROBE_TIMEOUT_SECONDS,
            },
        )
    ]

    monkeypatch.setattr(
        server_install,
        "_run_process",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, "", "sudo policy error"),
    )
    with pytest.raises(InstallRefused, match="could not prove"):
        machine._converge_account()

    monkeypatch.setattr(
        server_install,
        "_run_process",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            0,
            "",
            "User rcp is not allowed to run sudo on lab.\n",
        ),
    )
    assert machine._converge_account() == current

    monkeypatch.setattr(
        server_install,
        "_run_process",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            1,
            "User rcp is not allowed to run sudo on lab.\n",
            "",
        ),
    )
    assert machine._converge_account() == current


def test_new_service_account_uses_stateful_timeout_and_reports_expiry(
    monkeypatch,
) -> None:
    calls: list[tuple[tuple[str, ...], float]] = []

    def missing_account(_name: str):
        raise KeyError

    def timed_out_useradd(
        argv: tuple[str, ...],
        *,
        timeout: float,
        **_kwargs,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 126, "", "command timed out")

    monkeypatch.setattr(server_install.pwd, "getpwnam", missing_account)
    monkeypatch.setattr(server_install, "_run_process", timed_out_useradd)

    with pytest.raises(InstallRefused, match="did not finish within five minutes"):
        server_install.LinuxInstallMachine()._converge_account()

    assert calls == [
        (
            (
                "useradd",
                "--create-home",
                "--home-dir",
                "/home/rcp",
                "--shell",
                "/bin/bash",
                "--user-group",
                "--password",
                "*NP*",
                "rcp",
            ),
            server_install.SERVER_INSTALL_ACCOUNT_TIMEOUT_SECONDS,
        )
    ]


def test_root_process_drops_inherited_sudo_identity(monkeypatch) -> None:
    captured_environment: dict[str, str] = {}

    def fake_run(argv, **kwargs):
        captured_environment.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv("SUDO_COMMAND", "/usr/bin/env rcp server install")
    monkeypatch.setenv("SUDO_GID", "123")
    monkeypatch.setenv("SUDO_UID", "1001")
    monkeypatch.setenv("SUDO_USER", "runner")
    monkeypatch.setenv("RCP_ENVIRONMENT_SENTINEL", "preserved")
    monkeypatch.setattr(server_install.subprocess, "run", fake_run)

    server_install._run_process(("true",), timeout=1)

    assert captured_environment["RCP_ENVIRONMENT_SENTINEL"] == "preserved"
    assert (
        not {
            "SUDO_COMMAND",
            "SUDO_GID",
            "SUDO_UID",
            "SUDO_USER",
        }
        & captured_environment.keys()
    )


def test_service_tooling_installs_and_rechecks_managed_python_for_fresh_account(
    monkeypatch,
) -> None:
    account = SimpleNamespace(pw_name="rcp", pw_dir="/home/rcp")
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    find_count = 0

    def fake_run(_account, argv, **kwargs):
        nonlocal find_count
        calls.append((argv, kwargs))
        if argv[:3] == ("uv", "python", "find"):
            find_count += 1
            if find_count == 1:
                return subprocess.CompletedProcess(argv, 2, "", "not installed")
            return subprocess.CompletedProcess(
                argv,
                0,
                "/home/rcp/.local/share/uv/python/cpython-3.12/bin/python3.12\n",
                "",
            )
        if argv[0].endswith("python3.12"):
            return subprocess.CompletedProcess(argv, 0, "Python 3.12.10\n", "")
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(server_install.pwd, "getpwnam", lambda _name: account)
    monkeypatch.setattr(server_install, "_run_as_account", fake_run)

    server_install.LinuxInstallMachine()._validate_service_tooling()

    assert (
        "uv",
        "python",
        "find",
        "--managed-python",
        "--no-python-downloads",
        "3.12",
    ) in [argv for argv, _kwargs in calls]
    install_call = next(
        (argv, kwargs) for argv, kwargs in calls if argv[:3] == ("uv", "python", "install")
    )
    assert install_call[0] == (
        "uv",
        "python",
        "install",
        "--managed-python",
        "--no-progress",
        "3.12",
    )
    assert install_call[1]["capture_output"] is True


def test_backup_timer_is_fenced_before_loaded_unit_changes(monkeypatch) -> None:
    fences: list[str] = []
    monkeypatch.setattr(
        server_install,
        "_fence_service_stopped_disabled",
        lambda unit: fences.append(unit),
    )
    monkeypatch.setattr(
        server_install,
        "_read_systemd_property",
        lambda _unit, _property: "not-found",
    )

    server_install.fence_backup_timer_before_unit_change()
    assert fences == []

    monkeypatch.setattr(
        server_install,
        "_read_systemd_property",
        lambda _unit, _property: "loaded",
    )
    server_install.fence_backup_timer_before_unit_change()
    assert fences == ["rcp-backup.timer"]


def test_service_fence_fails_closed_when_stop_or_readback_fails(monkeypatch) -> None:
    def failed_stop(argv, error, **_kwargs):
        if argv[:3] == ("systemctl", "disable", "--now"):
            raise InstallRefused(error)
        return subprocess.CompletedProcess(argv, 0, "inactive\n", "")

    monkeypatch.setattr(server_install, "_require_command", failed_stop)
    with pytest.raises(InstallRefused, match="could not stop and disable"):
        server_install._fence_service_stopped_disabled("rcp.service")

    def wrong_readback(argv, _error, **_kwargs):
        output = "active\n" if "--property=ActiveState" in argv else "disabled\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(server_install, "_require_command", wrong_readback)
    with pytest.raises(InstallRefused, match="could not prove"):
        server_install._fence_service_stopped_disabled("rcp.service")


def _temporary_layout(tmp_path: Path) -> ServerLayout:
    home = tmp_path / "home" / "rcp"
    root = home / "rcp-server"
    return ServerLayout(
        service_account="rcp",
        service_home=home,
        server_root=root,
        source_checkout=root / "source",
        releases_root=root / "releases",
        data_dir=root / "data",
        projects_root=root / "projects",
        credentials_root=root / "credentials",
        update_checkpoints_root=root / "update-checkpoints",
        restore_operations_root=root / "restore-operations",
        codex_state_root=home / ".codex",
        claude_state_root=home / ".claude",
        ssh_state_root=home / ".ssh",
        config_path=tmp_path / "etc" / "rcp" / "server.toml",
        current_release=tmp_path / "etc" / "rcp" / "current",
        runtime_dir=tmp_path / "run" / "rcp",
        control_socket=tmp_path / "run" / "rcp" / "control.sock",
        cli_wrapper=tmp_path / "usr" / "local" / "bin" / "rcp",
        systemd_unit=tmp_path / "etc" / "systemd" / "system" / "rcp.service",
        service_unit_name="rcp.service",
    )
