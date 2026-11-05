from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.__main__ import build_parser
from rcp.server_ops.cli import (
    CallerIdentity,
    run_server_command,
)
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT, ServerLayout
from rcp.server_ops.provider_update import (
    _discover_provider,
    prepare_provider_update_command,
)
from rcp.storage import AgentTaskRecord, AppStore
from tests.test_auto_research_children_storage import _identity, _project


def _layout(tmp_path: Path) -> ServerLayout:
    home = tmp_path / "home" / "rcp"
    root = home / "rcp-server"
    layout = replace(
        DEFAULT_SERVER_LAYOUT,
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
    )
    root.mkdir(parents=True)
    layout.config_path.parent.mkdir(parents=True)
    layout.config_path.write_text("installed = true\n", encoding="utf-8")
    layout.current_release.mkdir(parents=True)
    return layout


def _account(layout: ServerLayout):
    return SimpleNamespace(
        pw_name="rcp",
        pw_uid=os.getuid(),
        pw_gid=os.getgid(),
        pw_dir=str(layout.service_home),
    )


def _parse(provider: str):
    return build_parser().parse_args(
        ("server", "provider", "update", provider, "--machine-readable")
    )


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_provider_update_runs_native_maintenance_as_rcp_without_touching_the_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    """Agents may write /tmp, so the installer is staged in protected ~/.rcp/tmp."""
    layout = _layout(tmp_path)
    account = _account(layout)
    binary = layout.service_home / ".local" / "bin" / provider
    state = {"updated": False}
    calls: list[tuple[str, ...]] = []
    if provider == "claude":
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
    monkeypatch.setattr("rcp.server_ops.provider_update.pwd.getpwnam", lambda _name: account)
    monkeypatch.setattr("rcp.server_ops.provider_update.os.chown", lambda *_args: None)

    def runner(_account, argv: tuple[str, ...], _timeout: float):
        """A status command reads the credential file, so an update runs none."""
        calls.append(argv)
        if argv[-1] == "--version":
            version = "2.1.253" if state["updated"] else "2.1.252"
            return subprocess.CompletedProcess(argv, 0, version, "")
        if provider == "claude" and argv[-1] == "update":
            state["updated"] = True
            return subprocess.CompletedProcess(argv, 0, "updated", "")
        if provider == "codex" and argv[0] == "/usr/bin/curl":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if provider == "codex" and argv[:3] == (
            "/usr/bin/env",
            "CODEX_NON_INTERACTIVE=1",
            "/bin/sh",
        ):
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            state["updated"] = True
            return subprocess.CompletedProcess(argv, 0, "installed", "")
        raise AssertionError(f"unexpected provider command: {argv}")

    output = StringIO()
    exit_code = run_server_command(
        _parse(provider),
        handler=lambda request, identity: prepare_provider_update_command(
            request,
            identity,
            runner=runner,
            layout=layout,
        ),
        identity=CallerIdentity(uid=0, username="root", host="lab"),
        stream=output,
    )

    assert exit_code == 0, output.getvalue()
    assert state["updated"] is True
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert events[-1]["step"]["state"] == "succeeded"
    assert events[-1]["step"]["fields"][-1]["name"] == "authentication"
    assert not any(call[-2:] in {("login", "status"), ("auth", "status")} for call in calls)
    if provider == "codex":
        (download,) = [call for call in calls if call[0] == "/usr/bin/curl"]
        assert Path(download[-1]).parent.parent == layout.service_home / ".rcp" / "tmp"
        assert any(
            call[:3] == ("/usr/bin/env", "CODEX_NON_INTERACTIVE=1", "/bin/sh") for call in calls
        )
    else:
        assert (str(binary), "update") in calls


@pytest.mark.parametrize(
    "system_directory",
    ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"],
)
def test_provider_update_prefers_service_path_over_native_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_directory: str
) -> None:
    account = _account(_layout(tmp_path))
    system = Path(system_directory) / "opencode"
    native = Path(account.pw_dir) / ".opencode/bin/opencode"
    available = {system, native}
    monkeypatch.setattr(Path, "is_file", lambda path: path in available)
    monkeypatch.setattr(
        "rcp.server_ops.provider_update.os.access", lambda path, mode: path in available
    )
    assert _discover_provider(account, "opencode") == system


def test_provider_update_finds_the_native_opencode_install_without_a_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    account = _account(layout)
    binary = layout.service_home / ".opencode" / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr("rcp.server_ops.provider_update.pwd.getpwnam", lambda _name: account)
    calls: list[tuple[str, ...]] = []
    state = {"updated": False}

    def runner(_account, argv: tuple[str, ...], _timeout: float):
        calls.append(argv)
        if argv[-1] == "--version":
            version = "1.18.34" if state["updated"] else "1.18.33"
            return subprocess.CompletedProcess(argv, 0, version, "")
        if argv == (str(binary), "upgrade"):
            state["updated"] = True
            return subprocess.CompletedProcess(argv, 0, "upgraded", "")
        raise AssertionError(f"unexpected provider command: {argv}")

    output = StringIO()
    exit_code = run_server_command(
        _parse("opencode"),
        handler=lambda request, identity: prepare_provider_update_command(
            request,
            identity,
            runner=runner,
            layout=layout,
        ),
        identity=CallerIdentity(uid=0, username="root", host="lab"),
        stream=output,
    )

    assert exit_code == 0, output.getvalue()
    assert state["updated"] is True
    assert (str(binary), "upgrade") in calls
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert events[-1]["step"]["state"] == "succeeded"


def _seed_turn(layout: ServerLayout, provider: str, status: str, machine: str | None) -> None:
    layout.data_dir.mkdir(parents=True, exist_ok=True)
    store = AppStore(layout.data_dir / "rcp.sqlite3")
    _project(store)
    task = store.create_agent_task(
        AgentTaskRecord(
            operation_id=str(uuid.uuid4()),
            project_id="project",
            kind="project_chat",
            status="succeeded",
            request={"provider": provider, "message": "work"},
            created_at=store.now(),
            updated_at=store.now(),
            status_message="",
            authorized_by=_identity(store),
        )
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status=?, stage_machine_id=? WHERE operation_id=?",
            (status, machine, task.operation_id),
        )
    store.close()


def _update_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed) -> tuple[int, list]:
    layout = _layout(tmp_path)
    seed(layout)
    account = _account(layout)
    binary = layout.service_home / ".opencode" / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr("rcp.server_ops.provider_update.pwd.getpwnam", lambda _name: account)
    calls: list[tuple[str, ...]] = []

    def runner(_account, argv: tuple[str, ...], _timeout: float):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "1.18.34", "")

    exit_code = run_server_command(
        _parse("opencode"),
        handler=lambda request, identity: prepare_provider_update_command(
            request, identity, runner=runner, layout=layout
        ),
        identity=CallerIdentity(uid=0, username="root", host="lab"),
        stream=StringIO(),
    )
    return exit_code, calls


@pytest.mark.parametrize("status", ["running", "pausing"])
def test_provider_update_refuses_while_a_backend_turn_runs_that_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    exit_code, calls = _update_opencode(
        tmp_path, monkeypatch, lambda layout: _seed_turn(layout, "opencode", status, None)
    )

    assert exit_code != 0
    assert calls == []


@pytest.mark.parametrize(
    ("provider", "status", "machine"),
    [
        ("codex", "running", None),
        ("opencode", "succeeded", None),
        ("opencode", "running", "gpu-card"),
    ],
)
def test_provider_update_ignores_turns_that_do_not_run_it_on_the_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    status: str,
    machine: str | None,
) -> None:
    exit_code, calls = _update_opencode(
        tmp_path, monkeypatch, lambda layout: _seed_turn(layout, provider, status, machine)
    )

    assert exit_code == 0
    assert any(call[-1] == "upgrade" for call in calls)


def test_provider_update_proceeds_when_the_database_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code, _calls = _update_opencode(tmp_path, monkeypatch, lambda _layout: None)

    assert exit_code == 0
