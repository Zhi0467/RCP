from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.events import EventEmitter
from rcp_supervisor.launch import validate_selected_receipt

from rcp.server_ops.config import (
    ServerReleaseConfig,
    create_installed_server_config,
    parse_installed_server_config,
    render_installed_server_config,
)
from rcp.server_ops.models import ServerPlanEvent, ServerStepEvent, validate_server_event_prefix


@pytest.mark.parametrize(
    ("arguments", "prefix"),
    [
        (["server", "install", "--machine-readable"], []),
        (["server", "--machine-readable", "update"], []),
        (["--machine-readable", "server", "restore", "archive"], []),
        (["server", "supervisor", "--plan", "update"], []),
        (["server", "--machine-readable", "backup", "configure"], ["operator"]),
        (["server", "provider", "update", "--machine-readable"], ["operator"]),
        (["server", "update-extra"], ["launch"]),
        (["serve", "--port", "8421"], ["launch"]),
    ],
)
def test_installed_wrapper_preserves_argv_and_routes_flag_positions(
    tmp_path: Path, arguments: list[str], prefix: list[str]
) -> None:
    from rcp.server_ops.install import _wrapper_text

    supervisor = tmp_path / "rcp-supervisor"
    supervisor.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    supervisor.chmod(0o755)
    wrapper = tmp_path / "rcp"
    wrapper.write_text(
        _wrapper_text(SimpleNamespace(data_dir=tmp_path / "data", supervisor_wrapper=supervisor))
    )
    result = subprocess.run(["/bin/sh", str(wrapper), *arguments], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == prefix + arguments


def receipt(root: Path) -> dict:
    return {
        "version": 1,
        "release_tag": "v0.3.2",
        "version_string": "0.3.2+build.412.gfe06636",
        "build": 412,
        "commit": "fe06636" + "0" * 33,
        "manifest_sha256": "a" * 64,
        "release_directory": str(root / "412"),
        "supervisor_version": "0.1.1",
    }


def test_installed_systemd_guard_allows_bounded_recovery_to_finish() -> None:
    from rcp_supervisor.limits import (
        APP_COMMAND_TIMEOUT_SECONDS,
        MAINTENANCE_TIMEOUT_SECONDS,
        PROBE_TIMEOUT_SECONDS,
        SERVICE_TIMEOUT_SECONDS,
        STARTUP_RECOVERY_TIMEOUT_SECONDS,
    )

    from rcp.server_ops.layout import server_service_unit_text

    unit = server_service_unit_text()
    assert "ExecStartPre=+/usr/local/bin/rcp-supervisor recover --startup\n" in unit
    assert f"TimeoutStartSec={STARTUP_RECOVERY_TIMEOUT_SECONDS}\n" in unit
    assert STARTUP_RECOVERY_TIMEOUT_SECONDS > (
        MAINTENANCE_TIMEOUT_SECONDS
        + 3 * APP_COMMAND_TIMEOUT_SECONDS
        + PROBE_TIMEOUT_SECONDS
        + 2 * SERVICE_TIMEOUT_SECONDS
    )


@pytest.mark.parametrize(
    "command",
    [
        "server install",
        "server update",
        "server restore",
        "server supervisor update",
        "server doctor",
    ],
)
def test_supervisor_events_preserve_application_wizard_contract(command):
    stream = StringIO()
    emitter = EventEmitter(command, machine_readable=True, stream=stream)
    emitter.emit("running", "Verifying the operation.")
    emitter.emit(
        "operator_action_needed",
        "Initialize the team and save its bootstrap secret.",
        actions=[
            {
                "kind": "command",
                "argv": [
                    "sudo",
                    "-u",
                    "rcp",
                    "-H",
                    "/usr/local/bin/rcp",
                    "space",
                    "init",
                    "--team",
                    "--name",
                    "Research",
                ],
            }
        ],
        resume_argv=["sudo", "rcp", "server", "install", "--team-name", "Research"],
    )
    lines = stream.getvalue().splitlines()
    events = (
        ServerPlanEvent.model_validate_json(lines[0]),
        *(ServerStepEvent.model_validate_json(line) for line in lines[1:]),
    )
    validate_server_event_prefix(events)
    assert events[-1].step.state == "operator_action_needed"
    assert events[0].steps[0].phase == emitter.plan["steps"][0]["phase"]


@pytest.mark.parametrize(
    "change",
    [
        {"build": True},
        {"commit": "a" * 40},
        {"version_string": "0.3.2"},
        {"release_directory": "/tmp/escape"},
        {"release_tag": "main"},
        {"extra": "value"},
        {"supervisor_version": "0.1.1rc1"},
    ],
)
def test_selected_receipt_binds_build_full_commit_version_and_path(tmp_path, change):
    original = receipt(tmp_path)
    assert validate_selected_receipt(original, releases_root=tmp_path) == original
    with pytest.raises(SupervisorError):
        validate_selected_receipt({**original, **change}, releases_root=tmp_path)


def test_fresh_config_follows_stable_without_private_source_metadata():
    config = create_installed_server_config()
    text = render_installed_server_config(config)
    assert "[source]" not in text
    assert config.source is None
    assert parse_installed_server_config(text) == config
    assert config.release.selector == "stable"


@pytest.mark.parametrize("pin", ["main", "build/412", "v0.3.2rc1", "v0.3.2+build.412", "v01.2.3"])
def test_release_pin_refuses_unpromoted_source_or_build_selector(pin):
    with pytest.raises(ValueError):
        ServerReleaseConfig(pin=pin)


def test_legacy_config_retains_only_migration_metadata():
    from rcp.server_ops.config import ServerSourceConfig

    config = create_installed_server_config(
        source=ServerSourceConfig(
            origin="https://github.com/Zhi0467/RCP.git", authentication="public"
        )
    )
    text = (
        render_installed_server_config(config)
        .replace("schema_version = 3", "schema_version = 2")
        .replace('[release]\nfollowed = "stable"\n\n', "")
    )
    recovered = parse_installed_server_config(text)
    assert recovered.source == config.source
    assert recovered.release.selector == "stable"


class Terminal(StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize(
    "answer,executed,resumed",
    [("q\n", False, False), ("", False, False), ("\nq\n", True, False), ("\n\n", True, True)],
)
def test_human_init_inherits_terminal_and_requires_saved_code_confirmation(
    monkeypatch, answer, executed, resumed
):
    import os
    import subprocess
    import sys
    from types import SimpleNamespace

    from rcp_supervisor.events import run_operator_pause

    stream = Terminal()
    emitter = EventEmitter("server install", stream=stream)
    emitter.emit("running", "Preparing.")
    emitter.emit(
        "operator_action_needed",
        "Initialize the team.",
        actions=[
            {
                "kind": "command",
                "argv": [
                    "sudo",
                    "-u",
                    "rcp",
                    "-H",
                    "/usr/local/bin/rcp",
                    "space",
                    "init",
                    "--team",
                    "--name",
                    "Lab",
                ],
            }
        ],
        resume_argv=["sudo", "rcp", "server", "install", "--team-name", "Lab"],
    )
    monkeypatch.setattr(sys, "stdin", Terminal(answer))
    calls = []

    def run(argv, **kwargs):
        assert not {"stdout", "stderr", "capture_output", "stdin"} & kwargs.keys()
        calls.append(("action", argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(os, "execvpe", lambda command, argv, env: calls.append(("resume", argv)))
    run_operator_pause(emitter)
    assert any(kind == "action" for kind, _ in calls) is executed
    assert any(kind == "resume" for kind, _ in calls) is resumed


def test_machine_readable_pause_never_reads_input_or_runs_actions(monkeypatch):
    import sys

    from rcp_supervisor.events import run_operator_pause

    class RefuseRead:
        def isatty(self):
            raise AssertionError("machine input was inspected")

    monkeypatch.setattr(sys, "stdin", RefuseRead())
    emitter = EventEmitter("server update", machine_readable=True, stream=StringIO())
    emitter.emit(
        "operator_action_needed",
        "Review.",
        actions=[{"kind": "external", "instruction": "Review the release."}],
        resume_argv=["sudo", "rcp", "server", "update"],
    )
    assert run_operator_pause(emitter) is None


@pytest.mark.parametrize(
    "answer,selected", [("1\n", 0), ("2\n", 1), ("\n", None), ("q\n", None), ("", None)]
)
def test_restore_authority_choices_execute_exactly_one_explicit_choice(
    monkeypatch, answer, selected
):
    import os
    import subprocess
    import sys

    from rcp_supervisor.events import run_operator_pause

    commands = [
        [
            "sudo",
            "rcp",
            "server",
            "restore",
            "/backup/archive.age",
            "--old-authority-disposition",
            disposition,
            "--confirm-old-authority",
            "a" * 64,
            "--confirm-member-roster",
            "b" * 64,
        ]
        for disposition in ("old-machine-destroyed", "old-machine-fenced-and-credentials-revoked")
    ]
    emitter = EventEmitter("server restore", stream=Terminal())
    emitter.emit(
        "operator_action_needed",
        "Choose the old-authority disposition.",
        actions=[{"kind": "command", "argv": command} for command in commands],
        resume_argv=["sudo", "rcp", "server", "restore", "/backup/archive.age"],
    )
    monkeypatch.setattr(sys, "stdin", Terminal(answer))
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "restore choice must replace the process, never execute both"
        ),
    )
    monkeypatch.setattr(os, "execvpe", lambda command, argv, env: calls.append(argv))
    run_operator_pause(emitter)
    assert calls == ([] if selected is None else [commands[selected]])


def test_ordinary_application_launcher_refuses_root_before_reading_service_files(monkeypatch):
    from rcp_supervisor import launch

    monkeypatch.setattr(launch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        launch,
        "read_selected_receipt",
        lambda: pytest.fail("root ordinary launch reached application metadata"),
    )
    assert launch.main(["serve"]) == 1
    assert launch.launch_operator(["server", "update"]) == 1


def test_operator_environment_preparation_uses_app_wheel_and_its_hash_locked_requirements(
    tmp_path, monkeypatch
):
    from rcp_supervisor import install

    from tests.supervisor_helpers import make_bundle

    bundle = make_bundle(tmp_path / "bundle")
    observed = {}

    def prepare(release, root, **kwargs):
        observed.update(kwargs)
        return root / kwargs["storage"] / kwargs["identity"] / ".venv"

    monkeypatch.setattr(install, "_install_root_environment", prepare)
    result = install.install_operator_console(bundle, tmp_path / "root")
    assert result == tmp_path / "root/operator/412/.venv"
    assert observed["package"] == "rcp"
    assert observed["requirements"] == bundle / "requirements.lock.txt"
    assert observed["wheel"].name.startswith("rcp-")
    assert len(observed["manifest_sha256"]) == 64
