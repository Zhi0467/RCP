from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.__main__ import build_parser
from rcp.server_ops.cli import (
    SERVER_CLI_EXIT_OPERATOR_ACTION,
    CallerIdentity,
    run_server_command,
)
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT, ServerLayout
from rcp.server_ops.provider_update import (
    _server_login_command,
    _success_message,
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
        pw_uid=501,
        pw_gid=501,
        pw_dir=str(layout.service_home),
    )


def _parse(provider: str):
    return build_parser().parse_args(
        ("server", "provider", "update", provider, "--machine-readable")
    )


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_provider_update_runs_native_maintenance_as_rcp_and_verifies_login(
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
        if argv[-2:] == ("login", "status"):
            return subprocess.CompletedProcess(argv, 0, "Logged in using ChatGPT", "")
        if argv[-2:] == ("auth", "status"):
            return subprocess.CompletedProcess(argv, 0, json.dumps({"loggedIn": True}), "")
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
    assert events[-1]["step"]["fields"][-1] == {
        "name": "authentication",
        "value": "ready",
    }
    if provider == "codex":
        assert any(call[0] == "/usr/bin/curl" for call in calls)
        assert any(
            call[:3] == ("/usr/bin/env", "CODEX_NON_INTERACTIVE=1", "/bin/sh") for call in calls
        )
        assert "existing projects keep their explicit path" not in events[-1]["step"]["message"]
    else:
        assert (str(binary), "update") in calls


def test_provider_update_preserves_success_and_prints_login_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    account = _account(layout)
    binary = layout.service_home / ".local" / "bin" / "claude"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr("rcp.server_ops.provider_update.pwd.getpwnam", lambda _name: account)

    def runner(_account, argv: tuple[str, ...], _timeout: float):
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "2.1.253", "")
        if argv[-1] == "update":
            return subprocess.CompletedProcess(argv, 0, "updated", "")
        return subprocess.CompletedProcess(argv, 1, json.dumps({"loggedIn": False}), "")

    output = StringIO()
    exit_code = run_server_command(
        _parse("claude"),
        handler=lambda request, identity: prepare_provider_update_command(
            request,
            identity,
            runner=runner,
            layout=layout,
        ),
        identity=CallerIdentity(uid=0, username="root", host="lab"),
        stream=output,
    )

    assert exit_code == SERVER_CLI_EXIT_OPERATOR_ACTION
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    final = events[-1]["step"]
    assert final["state"] == "operator_action_needed"
    assert final["actions"][0]["argv"] == [
        "sudo",
        "-u",
        "rcp",
        "-H",
        str(binary),
        "auth",
        "login",
    ]


def test_codex_recovery_uses_headless_device_login() -> None:
    binary = Path("/home/rcp/.local/bin/codex")

    assert _server_login_command("codex", binary) == (
        str(binary),
        "login",
        "--device-auth",
    )


def test_changed_provider_command_path_names_the_member_owned_resolve_step() -> None:
    message = _success_message(
        "codex",
        Path("/usr/local/bin/codex"),
        Path("/home/rcp/.local/bin/codex"),
    )

    assert "updated and authenticated as rcp" in message
    assert "authenticated member uses Resolve in Project Settings" in message
