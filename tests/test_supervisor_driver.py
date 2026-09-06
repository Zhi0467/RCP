from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from rcp_supervisor import cli, driver
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.events import EventEmitter
from rcp_supervisor.operations import OperationBusy

from rcp.server_ops.models import ServerPlanEvent, ServerStepEvent


@pytest.mark.parametrize(
    "command",
    [
        ["install"],
        ["update"],
        ["restore", "/tmp/archive.tar.age"],
        ["supervisor", "update"],
        ["doctor"],
    ],
)
def test_installed_plans_use_the_existing_wizard_contract_without_machine_access(
    command, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        raise AssertionError("A plan must not inspect or mutate the machine or fetch assets")

    monkeypatch.setattr(driver, "SystemRuntime", forbidden)
    monkeypatch.setattr(driver, "followed_release", forbidden)
    assert cli.main(["--plan", "--machine-readable", "server", *command]) == 0
    plan = ServerPlanEvent.model_validate_json(capsys.readouterr().out)
    assert plan.command == "server " + (
        "supervisor update" if command[0] == "supervisor" else command[0]
    )
    assert len(plan.steps) == 1


def test_operator_failure_is_one_terminal_wizard_event(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise SupervisorError("Invalid journal; service remains closed")

    monkeypatch.setattr(driver, "update", fail)
    assert cli.main(["--machine-readable", "server", "update"]) == 1
    lines = capsys.readouterr().out.splitlines()
    ServerPlanEvent.model_validate_json(lines[0])
    assert [ServerStepEvent.model_validate_json(line).step.state for line in lines[1:]] == [
        "running",
        "failed",
    ]


@pytest.mark.parametrize(
    "phase,selected_kind,allowed",
    [
        ("candidate_chosen", "target", True),
        ("previous_chosen", "previous", True),
        ("candidate_chosen", "previous", False),
        ("previous_chosen", "target", False),
        ("candidate_verified", "target", False),
        ("activating", "previous", False),
        ("rollback_started", "previous", False),
        (None, "target", True),
    ],
)
def test_systemd_reentrant_guard_allows_only_durable_selected_choice(
    monkeypatch, phase, selected_kind, allowed
):
    target, previous = {"build": 2}, {"build": 1}
    record = None if phase is None else {"phase": phase, "target": target, "previous": previous}
    store = SimpleNamespace(active=lambda: record)

    class BusyCoordinator:
        def __init__(self, *args, **kwargs):
            pass

        def recover(self, **kwargs):
            raise OperationBusy("outer systemctl start owns the lock")

    monkeypatch.setattr(driver, "SystemRuntime", lambda *args, **kwargs: object())
    monkeypatch.setattr(driver, "store_for", lambda paths: store)
    monkeypatch.setattr(driver, "Coordinator", BusyCoordinator)
    monkeypatch.setattr(driver, "_require_initialized", lambda runtime, selected: None)
    monkeypatch.setattr(
        driver, "selected_pointer", lambda paths: target if selected_kind == "target" else previous
    )
    if allowed:
        assert driver.recover(startup=True) == record
    else:
        with pytest.raises(SupervisorError, match="chosen|selected"):
            driver.recover(startup=True)
    with pytest.raises(OperationBusy):
        driver.recover(startup=False)


def test_invalid_journal_never_takes_live_lock_bypass(monkeypatch):
    class InvalidCoordinator:
        def __init__(self, *args, **kwargs):
            pass

        def recover(self, **kwargs):
            raise SupervisorError("invalid journal")

    monkeypatch.setattr(driver, "SystemRuntime", lambda *args, **kwargs: object())
    monkeypatch.setattr(driver, "store_for", lambda paths: object())
    monkeypatch.setattr(driver, "Coordinator", InvalidCoordinator)
    monkeypatch.setattr(
        driver,
        "selected_pointer",
        lambda paths: pytest.fail("invalid journal cannot admit startup"),
    )
    with pytest.raises(SupervisorError, match="invalid journal"):
        driver.recover(startup=True)


def test_update_displays_bound_target_before_any_install_or_admission(monkeypatch, capsys):
    target = {"release_tag": "v0.3.4", "manifest_sha256": "a" * 64, "build": 9, "commit": "b" * 40}
    monkeypatch.setattr(driver, "recover", lambda **kwargs: None)
    monkeypatch.setattr(driver, "SystemRuntime", lambda *args: object())
    monkeypatch.setattr(driver, "selected_pointer", lambda paths: {"build": 8})
    monkeypatch.setattr(
        driver, "followed_release", lambda runtime: SimpleNamespace(supervisor_version="0.1.1")
    )
    monkeypatch.setattr(driver, "release_receipt", lambda *args: target)
    monkeypatch.setattr(
        driver,
        "prepare_release",
        lambda *args: pytest.fail("unconfirmed target must not be installed"),
    )
    emitter = EventEmitter("server update", machine_readable=True)
    emitter.emit("running", "Verify")
    assert driver.update(SimpleNamespace(confirm_target=None), emitter) == 3
    events = capsys.readouterr().out.splitlines()
    step = ServerStepEvent.model_validate_json(events[-1]).step
    assert step.state == "operator_action_needed"
    assert step.resume_argv[-1] == f"v0.3.4:{'a' * 64}"
    assert json.loads(events[0])["event"] == "plan"


@pytest.mark.parametrize("position", [0, 1, 2, 3])
def test_server_flags_preserve_public_wrapper_positions(position, monkeypatch, capsys):
    argv = ["server", "supervisor", "update"]
    argv[position:position] = ["--plan", "--machine-readable"]
    monkeypatch.setattr(driver, "supervisor_update", lambda *_args: pytest.fail("plan executed"))
    assert cli.main(argv) == 0
    assert (
        ServerPlanEvent.model_validate_json(capsys.readouterr().out).command
        == "server supervisor update"
    )


def test_operator_pause_failure_does_not_emit_a_second_terminal(monkeypatch, capsys):
    from rcp_supervisor import events

    def pause(arguments, emitter):
        emitter.emit(
            "operator_action_needed",
            "Review the exact operation.",
            actions=[{"kind": "external", "instruction": "Review the release."}],
            resume_argv=["sudo", "rcp", "server", "update"],
        )
        return 3

    monkeypatch.setattr(driver, "update", pause)
    monkeypatch.setattr(
        events, "run_operator_pause", lambda _: (_ for _ in ()).throw(OSError("terminal lost"))
    )
    assert cli.main(["server", "update", "--machine-readable"]) == 1
    output = capsys.readouterr()
    states = [
        ServerStepEvent.model_validate_json(line).step.state for line in output.out.splitlines()[1:]
    ]
    assert states == ["running", "operator_action_needed"]
    assert "terminal lost" in output.err


@pytest.mark.parametrize(
    "entrance,values",
    [
        ("launch", ["--version"]),
        ("launch", ["--help"]),
        ("launch", ["serve", "--port", "8421"]),
        ("operator", ["--machine-readable", "server", "backup", "configure"]),
    ],
)
def test_wrapper_passthrough_preserves_application_options(entrance, values):
    arguments = cli._arguments([entrance, *values])
    assert arguments.command == entrance
    assert arguments.arguments == values


@pytest.mark.parametrize("target_version,required", [("0.1.0", "0.1.0"), ("0.1.1", "0.2.0")])
def test_self_update_cannot_downgrade_recovery_below_running_or_selected_app(
    monkeypatch, target_version, required
):
    from contextlib import nullcontext
    from io import StringIO

    monkeypatch.setattr(driver, "recover", lambda **_: None)
    monkeypatch.setattr(driver, "SystemRuntime", lambda *_: object())
    monkeypatch.setattr(
        driver, "store_for", lambda _: SimpleNamespace(locked=nullcontext, active=lambda: None)
    )
    monkeypatch.setattr(
        driver, "followed_release", lambda _: SimpleNamespace(supervisor_version=target_version)
    )
    monkeypatch.setattr(driver, "selected_pointer", lambda _: {"supervisor_version": required})
    monkeypatch.setattr(driver, "install_supervisor", lambda *_: pytest.fail("downgrade installed"))
    with pytest.raises(SupervisorError, match="downgrade"):
        driver.supervisor_update(None, EventEmitter("server supervisor update", stream=StringIO()))


def test_install_recovers_adoption_before_selecting(tmp_path, monkeypatch, capsys):
    from contextlib import contextmanager

    from rcp_supervisor import migration
    from rcp_supervisor.runtime import Paths

    paths = Paths(supervisor=tmp_path, current=tmp_path / "current")
    (tmp_path / "adoption.json").write_text("{}")
    paths.current.symlink_to(tmp_path / "candidate")
    calls = []

    @contextmanager
    def locked():
        calls.append("lock")
        yield

    def runtime(*args, **kwargs):
        calls.append("runtime")
        return SimpleNamespace(config={"schema_version": 3})

    def recover(runtime, *, for_install):
        assert for_install
        assert calls == ["runtime", "lock"]
        assert not paths.selected.exists()
        calls.append("recover")
        return {"phase": "rolled_back", "legacy_backup": str(tmp_path), "protected_backup": None}

    monkeypatch.setattr(driver, "SystemRuntime", runtime)
    monkeypatch.setattr(driver, "_root_directory", lambda *args, **kwargs: None)
    monkeypatch.setattr(driver, "store_for", lambda _: SimpleNamespace(locked=locked))
    monkeypatch.setattr(migration, "recover", recover)
    monkeypatch.setattr(driver, "followed_release", lambda _: pytest.fail("release fetched"))
    emitter = EventEmitter("server install", machine_readable=True)
    assert driver.install(SimpleNamespace(team_name="Team"), emitter, paths=paths) == 1
    assert calls == ["runtime", "lock", "recover", "runtime"]
    assert not paths.selected.exists()
    step = ServerStepEvent.model_validate_json(capsys.readouterr().out.splitlines()[-1]).step
    assert step.state == "failed"


@pytest.mark.parametrize("succeeds", [False, True])
def test_restore_enables_before_deploy_and_guards_uninitialized_rollback(
    tmp_path, monkeypatch, succeeds
):
    from io import StringIO

    from rcp_supervisor.runtime import Paths

    paths = Paths(data_dir=tmp_path, supervisor=tmp_path / "supervisor")
    calls = []
    selected = {"build": 1}
    runtime = SimpleNamespace(
        paths=paths,
        require_capability=lambda _: None,
        application=lambda *args: {"status": "uninitialized"},
        _systemctl=lambda action: calls.append(action),
    )

    class Coordinator:
        def __init__(self, *args, **kwargs):
            pass

        def deploy(self, previous, target, **kwargs):
            assert previous == target == selected
            assert kwargs == {"kind": "restore", "previous_uninitialized": True}
            assert calls == ["enable"]
            if not succeeds:
                raise SupervisorError("activation failed")
            calls.append("committed")
            return {"phase": "committed"}

        def recover(self, **kwargs):
            return {"phase": "rolled_back"}

    startup_recover = driver.recover
    monkeypatch.setattr(driver, "recover", lambda **kwargs: None)
    monkeypatch.setattr(driver, "SystemRuntime", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(driver, "selected_pointer", lambda _: selected)
    monkeypatch.setattr(driver, "Coordinator", Coordinator)
    arguments = SimpleNamespace(
        archive_path=tmp_path / "archive",
        identity_file=None,
        confirm_data_dir=str(tmp_path),
        old_authority_disposition=None,
        confirm_old_authority=None,
        confirm_member_roster=None,
        remove_stale_member=None,
    )
    emitter = EventEmitter("server restore", stream=StringIO())
    emitter.emit("running", "Restore")
    if succeeds:
        assert driver.restore(arguments, emitter, paths=paths) == 0
        assert calls == ["enable", "committed"]
    else:
        with pytest.raises(SupervisorError, match="activation failed"):
            driver.restore(arguments, emitter, paths=paths)
        assert calls == ["enable"]
        with pytest.raises(SupervisorError, match="completed team initialization or restore"):
            startup_recover(paths=paths, startup=True)
