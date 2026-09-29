from __future__ import annotations

import json
import os
import subprocess
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
        # A status command is a presence check, not proof, and reads the
        # credential file; an update runs none.
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
        # Agents may write /tmp, so the installer is staged in protected ~/.rcp/tmp.
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
    # The documented install writes only ~/.opencode/bin; no symlink is needed.
    assert (str(binary), "upgrade") in calls
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert events[-1]["step"]["state"] == "succeeded"
