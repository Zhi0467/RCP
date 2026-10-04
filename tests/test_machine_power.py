from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rcp import machine_power as power
from rcp.machine_power_macos import InstallError, InstallStatus, MacOSProfile
from rcp.storage import AppStore
from tests.helpers import wait_for_entry, wait_until


class FakeHold:
    def __init__(self, argv):
        self.argv, self.returncode = argv, None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self):
        return self.returncode


class Machine:
    def __init__(self, tmp_path, platform="darwin"):
        self.store = AppStore(tmp_path / "rcp.sqlite3")
        self.reasons = ["task"]
        self.holds = []
        self.platform = platform
        self.controller = self.new_controller()

    def new_controller(self):
        def spawn(argv):
            self.holds.append(FakeHold(argv))
            return self.holds[-1]

        return power.MachinePowerController(
            self.store,
            demand_reader=lambda: self.reasons,
            spawn=spawn,
            platform=self.platform,
            profile=(
                SimpleNamespace(idle_hold_command=MacOSProfile().idle_hold_command, lid_mode=None)
                if self.platform == "darwin"
                else None
            ),
            run=Mock(side_effect=OSError("No lid readings in idle-hold tests")),
            directory=self.store.path.parent / "machine",
            installer=SimpleNamespace(status=lambda: InstallStatus(False, "not_installed")),
        )


@pytest.fixture
def machine(tmp_path):
    return Machine(tmp_path)


def test_hold_follows_demand(machine):
    machine.controller.safety_pass()
    assert [hold.argv for hold in machine.holds] == [
        ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())]
    ]
    assert machine.controller.status()["idle_hold"]["active"] is True
    machine.controller.safety_pass()
    assert len(machine.holds) == 1
    machine.reasons = []
    machine.controller.safety_pass()
    assert machine.holds[0].returncode is not None
    assert machine.controller.status()["idle_hold"]["active"] is False


def test_dead_hold_is_replaced(machine):
    machine.controller.safety_pass()
    machine.holds[0].returncode = 1
    machine.controller.safety_pass()
    assert len(machine.holds) == 2


def test_disabling_drops_hold_and_persists(machine):
    machine.controller.safety_pass()
    machine.controller.update({"idle_hold": False})
    # Turning it off drops the hold itself, never waiting on a demand read.
    assert machine.holds[0].returncode is not None
    machine.controller.safety_pass()
    assert machine.controller.status()["idle_hold"] == {"enabled": False, "active": False}
    assert len(machine.holds) == 1
    assert machine.new_controller().status()["idle_hold"]["enabled"] is False


def test_failed_demand_read_keeps_current_hold(machine):
    machine.controller.safety_pass()
    machine.controller.demand_reader = Mock(side_effect=RuntimeError("store busy"))
    machine.controller.safety_pass()
    assert machine.controller.status()["idle_hold"]["active"] is True


def test_stop_drops_hold(machine):
    machine.controller.safety_pass()
    machine.controller.stop()
    assert machine.holds[0].returncode is not None


def test_unsupported_platform_never_spawns(tmp_path):
    machine = Machine(tmp_path, platform="linux")
    machine.controller.start()
    machine.controller.safety_pass()
    assert machine.holds == []
    assert machine.controller.status()["supported"] is False


@pytest.fixture
def demand_inputs(tmp_path, monkeypatch):
    project = SimpleNamespace(project_id="p", home_space_id="personal", locator="unused")
    episode = SimpleNamespace(episode_id="e", status="running")
    tasks = []
    store = SimpleNamespace(
        space_id="personal",
        path=tmp_path / "rcp.sqlite3",
        projects=lambda: [project],
        live_episodes=lambda _: [episode],
        active_project_agent_tasks=lambda _: tasks,
    )
    background = SimpleNamespace(
        runtime_is_idle=lambda: True,
        has_pending_transport_retry=lambda: False,
    )
    health = {}
    monkeypatch.setattr(
        power,
        "load_episode_health",
        lambda store, episodes: {
            e.episode_id: health[e.episode_id] for e in episodes if e.episode_id in health
        },
    )
    monkeypatch.setattr(power.ProviderCredentialStore, "for_data_dir", lambda _: object())
    refusal = Mock(return_value=None)
    monkeypatch.setattr(power, "account_login_refusal", refusal)
    return SimpleNamespace(
        store=store,
        background=background,
        project=project,
        health=health,
        tasks=tasks,
        refusal=refusal,
    )


@pytest.mark.parametrize(
    "health,blocked,expected",
    [
        ("starting", None, True),
        ("active", None, True),
        ("recovering", None, True),
        ("stopping", None, True),
        ("wrapping_up", None, True),
        ("wrapping_up", "sign_in", False),
        ("needs_action", None, False),
    ],
)
def test_episode_demand_health_rows(demand_inputs, health, blocked, expected):
    demand_inputs.health["e"] = (health, None, None, blocked)
    assert power.demand_snapshot(demand_inputs.store, demand_inputs.background) == (
        ["episode"] if expected else []
    )


def test_episode_demand_is_only_for_home_space(demand_inputs):
    demand_inputs.project.home_space_id = "team"
    demand_inputs.health["e"] = ("active", None, None, None)
    assert power.demand_snapshot(demand_inputs.store, demand_inputs.background) == []


@pytest.mark.parametrize(
    "status,refused,expected",
    [
        ("running", False, True),
        ("pausing", False, True),
        ("queued", False, True),
        ("queued", True, False),
        ("completed", False, False),
    ],
)
def test_task_demand_rows(demand_inputs, status, refused, expected):
    demand_inputs.tasks.append(
        SimpleNamespace(status=status, request={"provider": "codex"}, stage_host="")
    )
    demand_inputs.refusal.return_value = "signed_out" if refused else None
    assert power.demand_snapshot(demand_inputs.store, demand_inputs.background) == (
        ["task"] if expected else []
    )
    assert demand_inputs.refusal.call_count == (1 if status == "queued" else 0)


def test_runtime_demand(demand_inputs):
    demand_inputs.background.runtime_is_idle = lambda: False
    assert power.demand_snapshot(demand_inputs.store, demand_inputs.background) == ["runtime"]


def test_retry_timer_demand(demand_inputs):
    demand_inputs.background.has_pending_transport_retry = lambda: True
    assert power.demand_snapshot(demand_inputs.store, demand_inputs.background) == ["retry"]


def test_armed_watchers_alone_are_not_demand(demand_inputs):
    demand_inputs.store.watchers = Mock(side_effect=AssertionError("watchers are not demand"))
    assert power.demand_snapshot(demand_inputs.store, demand_inputs.background) == []


def test_failed_spawn_is_retried_next_pass(machine):
    spawn = machine.controller.spawn
    machine.controller.spawn = Mock(side_effect=OSError("no processes"))
    machine.controller.safety_pass()
    machine.controller.spawn = spawn
    machine.controller.safety_pass()
    assert machine.controller.status()["idle_hold"]["active"] is True


def test_failed_preference_write_changes_nothing_live(machine, monkeypatch):
    machine.controller.safety_pass()
    monkeypatch.setattr(machine.store, "connection", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        machine.controller.update({"idle_hold": False})
    assert machine.controller.status()["idle_hold"] == {"enabled": True, "active": True}


macos_tools = pytest.mark.skipif(sys.platform != "darwin", reason="macOS BSD tools")


class FakeMacOSProfile(MacOSProfile):
    def __init__(self):
        self.readings = {"battery": (False, 75), "thermal": False, "lid": False, "flag": False}
        self.calls = []
        self.fail = None

    def read(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise OSError("framework read failed")
        return self.readings[name]

    def read_battery(self):
        return self.read("battery")

    def read_thermal(self):
        return self.read("thermal")

    def read_lid(self):
        return self.read("lid")

    def read_flag(self):
        return self.read("flag")


class Process:
    def __init__(self, pid, on_wait=None):
        self.pid = pid
        self.returncode = None
        self.on_wait = on_wait
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        assert timeout is None or timeout > 0
        if self.on_wait:
            self.on_wait()
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake process", timeout)
        return self.returncode


class LidMachine:
    def __init__(self, tmp_path):
        self.root = tmp_path / "machine"
        self.root.mkdir()
        self.store = AppStore(tmp_path / "rcp.sqlite3")
        self.commands = []
        self.children = []
        self.profile = FakeMacOSProfile()
        self.flag = False
        self.reasons = ["episode"]
        self.ack = True
        self.group_alive = False
        self.now = 1000.0
        self.ticks = 0.0
        self.installed = True
        self.installer = SimpleNamespace(
            paths=SimpleNamespace(directory=self.root),
            cancelled=False,
            status=lambda: InstallStatus(
                self.installed, None if self.installed else "not_installed"
            ),
        )
        self.installer.install = Mock()
        self.installer.uninstall = Mock(side_effect=self.uninstall)
        self.controller = self.new_controller()

    def new_controller(self, **kwargs):
        return power.MachinePowerController(
            self.store,
            demand_reader=lambda: self.reasons,
            run=self.run,
            spawn=self.spawn,
            clock=lambda: self.now,
            monotonic=self.monotonic,
            process_identity=lambda: (123, "Thu Oct  1 03:00:00 2026"),
            directory=self.root,
            installer=self.installer,
            platform="darwin",
            profile=self.profile,
            **kwargs,
        )

    def monotonic(self):
        self.ticks += 1
        return self.ticks

    @property
    def flag(self):
        return self.profile.readings["flag"]

    @flag.setter
    def flag(self, value):
        self.profile.readings["flag"] = value

    def run(self, argv, timeout):
        raise AssertionError(f"unexpected command: {argv}")

    def killpg(self, pid, sig):
        assert any(child.pid == pid for _, child in self.children)
        self.commands.append((pid, sig))
        if not self.group_alive:
            raise ProcessLookupError

    def spawn(self, argv):
        child = Process(500 + len(self.children))
        if argv[0] == "/bin/sh":
            assert argv[1] == str(self.root / "machine_power_watchdog.sh")
            if self.ack:
                power.write_record(
                    self.root / "ack", {"generation": argv[3], "watchdog_pid": child.pid}
                )
            child.on_wait = lambda: self.execute(child)
        else:
            assert argv[:3] == ["/usr/bin/caffeinate", "-i", "-w"]
        self.children.append((argv, child))
        return child

    def execute(self, child=None, *, clear_failed=False, sleep_failed=False):
        child = child or self.controller._watchdog
        heartbeat = power.read_record(self.root / "heartbeat")
        if heartbeat.get("desired") == "on":
            self.flag = True
            power.write_record(self.root / "activation", heartbeat | {"set": 1})
        else:
            self.flag = clear_failed
            power.write_record(self.root / "activation", heartbeat | {"set": int(clear_failed)})
            power.write_record(
                self.root / "result",
                heartbeat
                | {
                    "clear_failed": int(clear_failed),
                    "sleep_failed": int(sleep_failed),
                    "complete": 1,
                },
            )
            child.returncode = 0

    def activate(self):
        self.controller.update({"lid_mode": True})
        self.controller.safety_pass()
        self.execute()
        self.controller.safety_pass()
        assert self.controller.status()["lid_mode"]["active"] is True

    def uninstall(self, *, before_remove):
        if not self.installer.cancelled:
            before_remove()
            self.installed = False

    def close(self):
        self.controller.stop()


@pytest.fixture
def lid_machine(tmp_path, monkeypatch):
    lid_machine = LidMachine(tmp_path)
    monkeypatch.setattr(power.os, "killpg", lid_machine.killpg)
    yield lid_machine
    lid_machine.close()


def test_idle_hold_is_worker_bound_and_lid_hold_requires_ack(lid_machine):
    lid_machine.activate()
    assert lid_machine.children[0][0] == ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())]
    assert lid_machine.children[1][0][0] == "/bin/sh"
    heartbeat = power.read_record(lid_machine.root / "heartbeat")
    assert heartbeat["pid"] == "123"
    assert heartbeat["start"] == "Thu Oct  1 03:00:00 2026"
    assert heartbeat["desired"] == "on"
    assert (lid_machine.root / "machine_power_watchdog.sh").read_bytes() == Path(
        power.__file__
    ).with_name("machine_power_watchdog.sh").read_bytes()


def test_idle_hold_does_not_need_the_watchdog_identity(lid_machine):
    lid_machine.controller.identity_reader = Mock(side_effect=RuntimeError("ps failed"))
    lid_machine.controller.safety_pass()
    assert lid_machine.controller.status()["idle_hold"]["active"] is True


def test_no_ack_never_requests_on(lid_machine):
    lid_machine.ack = False
    lid_machine.controller.update({"lid_mode": True})
    lid_machine.controller.safety_pass()
    assert power.read_record(lid_machine.root / "heartbeat")["desired"] == "off"
    assert lid_machine.controller.status()["last_release"]["cause"] == "reading_failed"
    assert lid_machine.controller.status()["idle_hold"]["active"] is True


@pytest.mark.parametrize("reading", ["battery", "thermal", "lid", "flag"])
def test_reading_failures_release_lid_mode_but_keep_idle_hold(lid_machine, reading):
    lid_machine.activate()
    lid_machine.profile.fail = reading
    lid_machine.profile.calls.clear()
    lid_machine.controller.safety_pass()
    assert lid_machine.profile.calls == ["battery", "thermal", "lid", "flag"]
    assert power.read_record(lid_machine.root / "heartbeat")["cause"] == "reading_failed"
    assert lid_machine.controller.status()["idle_hold"]["active"] is True
    assert lid_machine.controller.status()["lid_mode"]["active"] is False


def test_demand_gone_releases_both_holds(lid_machine):
    lid_machine.activate()
    lid_machine.reasons = []
    lid_machine.controller.safety_pass()
    assert power.read_record(lid_machine.root / "heartbeat")["cause"] == "demand_gone"
    assert lid_machine.controller.status()["demand"] is False
    assert lid_machine.controller.status()["idle_hold"]["active"] is False


def test_broken_installation_releases_immediately(lid_machine):
    lid_machine.activate()
    lid_machine.installed = False
    lid_machine.controller.safety_pass()
    assert power.read_record(lid_machine.root / "heartbeat")["desired"] == "off"


def test_battery_floor_stays_released_until_ac_then_rearms(lid_machine):
    lid_machine.activate()
    lid_machine.profile.readings["battery"] = (False, 20)
    lid_machine.controller.safety_pass()
    assert power.read_record(lid_machine.root / "heartbeat")["cause"] == "battery_floor"
    lid_machine.execute()
    lid_machine.profile.readings["battery"] = (
        False,
        75,
    )  # recovering percent on battery is insufficient
    lid_machine.controller.safety_pass()
    assert lid_machine.controller.status()["lid_mode"]["active"] is False
    lid_machine.profile.readings["battery"] = (True, 75)
    lid_machine.controller.safety_pass()
    assert power.read_record(lid_machine.root / "heartbeat")["desired"] == "on"
    assert power.read_record(lid_machine.root / "heartbeat")["generation"] == "2"


def test_thermal_latch_survives_restart_and_human_reenable_clears_it(lid_machine):
    lid_machine.activate()
    lid_machine.profile.readings["thermal"] = True
    lid_machine.controller.safety_pass()
    lid_machine.execute()
    lid_machine.controller.stop()
    lid_machine.controller = lid_machine.new_controller()
    assert lid_machine.controller.status()["latched"] == "thermal"
    assert lid_machine.controller.status()["lid_mode"]["enabled"] is True
    lid_machine.profile.readings["thermal"] = False
    lid_machine.controller.safety_pass()
    assert lid_machine.controller._watchdog is None
    lid_machine.controller.update({"lid_mode": True})
    assert lid_machine.controller.status()["latched"] is None
    lid_machine.controller.safety_pass()
    assert power.read_record(lid_machine.root / "heartbeat")["desired"] == "on"


@pytest.mark.parametrize(
    "clear_failed,sleep_failed,kind",
    [(True, False, "clear_failed"), (False, True, "sleep_failed"), (True, True, "clear_failed")],
)
def test_cleanup_failure_latches_and_records_remedy(lid_machine, clear_failed, sleep_failed, kind):
    lid_machine.activate()
    lid_machine.reasons = []
    lid_machine.controller.safety_pass()
    lid_machine.execute(clear_failed=clear_failed, sleep_failed=sleep_failed)
    lid_machine.controller.safety_pass()
    status = lid_machine.controller.status()
    assert status["latched"] == "cleanup_failure"
    command = "pmset sleepnow" if kind == "sleep_failed" else "sudo pmset -a disablesleep 0"
    assert status["cleanup_failure"] == {"kind": kind, "command": command}
    lid_machine.controller.stop()
    other = lid_machine.new_controller()
    assert other.status()["cleanup_failure"] == status["cleanup_failure"]
    other.update({"lid_mode": True})
    assert other.status()["latched"] is None
    assert other.status()["cleanup_failure"] is None


def test_persistence_failure_does_not_prevent_release(lid_machine, monkeypatch):
    lid_machine.activate()
    monkeypatch.setattr(lid_machine.controller, "_save", Mock(side_effect=OSError("disk full")))
    lid_machine.reasons = []
    lid_machine.controller.safety_pass()
    assert power.read_record(lid_machine.root / "heartbeat")["cause"] == "demand_gone"
    assert lid_machine.controller.status()["idle_hold"]["active"] is False


def test_dead_watchdog_is_replaced_only_to_release(lid_machine):
    lid_machine.activate()
    lid_machine.controller._watchdog.returncode = -9
    lid_machine.controller.safety_pass()
    heartbeat = power.read_record(lid_machine.root / "heartbeat")
    assert heartbeat["generation"] == "2"
    assert heartbeat["desired"] == "off"
    assert heartbeat["cause"] == "watchdog_lost"
    assert lid_machine.controller.status()["idle_hold"]["active"] is True


def test_revoked_generation_uses_new_executor(lid_machine):
    lid_machine.activate()
    power.write_record(lid_machine.root / "revoked", {"generation": 1})
    power.write_record(
        lid_machine.root / "result",
        {
            "generation": 1,
            "cause": "heartbeat_stale",
            "clear_failed": 0,
            "sleep_failed": 0,
            "complete": 1,
        },
    )
    power.write_record(lid_machine.root / "activation", {"generation": 1, "set": 0})
    lid_machine.flag = False
    lid_machine.controller._watchdog.returncode = 0
    lid_machine.controller.safety_pass()
    assert power.read_record(lid_machine.root / "heartbeat")["generation"] == "2"
    assert power.read_record(lid_machine.root / "heartbeat")["desired"] == "on"
    assert lid_machine.controller.status()["last_release"]["cause"] == "heartbeat_stale"


def test_restart_recovers_owned_flag_even_when_preferences_off(lid_machine):
    power.write_record(
        lid_machine.root / "activation", {"generation": 8, "pid": 1, "start": "previous", "set": 1}
    )
    lid_machine.flag = True
    lid_machine.controller.safety_pass()
    heartbeat = power.read_record(lid_machine.root / "heartbeat")
    assert heartbeat["generation"] == "9"
    assert heartbeat["desired"] == "off"
    assert heartbeat["cause"] == "watchdog_lost"


def test_external_flag_is_never_adopted_or_cleared(lid_machine):
    lid_machine.flag = True
    lid_machine.controller.update({"lid_mode": True})
    lid_machine.controller.safety_pass()
    assert lid_machine.controller.status()["external_owner"] is True
    assert lid_machine.controller._watchdog is None
    assert not (lid_machine.root / "heartbeat").exists()
    assert lid_machine.flag is True


def test_machine_lock_prevents_two_data_directories_from_owning_lid_mode(lid_machine, tmp_path):
    lid_machine.activate()
    other_store = AppStore(tmp_path / "second.sqlite3")
    other = power.MachinePowerController(
        other_store,
        demand_reader=lambda: ["runtime"],
        run=lid_machine.run,
        spawn=lid_machine.spawn,
        clock=lambda: lid_machine.now,
        process_identity=lambda: (456, "other"),
        directory=lid_machine.root,
        installer=lid_machine.installer,
        platform="darwin",
        profile=lid_machine.profile,
    )
    other.update({"lid_mode": True})
    other.safety_pass()
    assert other.status()["external_owner"] is True
    assert other._watchdog is None
    # Lock contention gates lid mode only; this backend's idle hold still runs.
    assert other.status()["idle_hold"]["active"] is True
    assert power.read_record(lid_machine.root / "heartbeat")["pid"] == "123"
    other.stop()


def test_shutdown_off_precedes_worker_bound_hold_exit(lid_machine):
    lid_machine.activate()
    lid_machine.controller.stop()
    assert power.read_record(lid_machine.root / "heartbeat")["desired"] == "off"
    assert power.read_record(lid_machine.root / "heartbeat")["cause"] == "shutdown"
    assert lid_machine.controller._owner is None
    assert lid_machine.controller.status()["idle_hold"]["active"] is False


def test_shutdown_publish_failure_does_not_raise(lid_machine, monkeypatch):
    lid_machine.activate()
    monkeypatch.setattr(
        lid_machine.controller, "_heartbeat", Mock(side_effect=OSError("disk full"))
    )
    lid_machine.controller.stop()
    assert lid_machine.controller.status()["idle_hold"]["active"] is False


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_cancel_preserves_active_status_preferences_and_heartbeat(lid_machine, action):
    lid_machine.activate()
    before = lid_machine.controller.status()
    heartbeat = (lid_machine.root / "heartbeat").read_bytes()
    lid_machine.installer.cancelled = True
    assert getattr(lid_machine.controller, action)() == before
    assert lid_machine.controller.status() == before
    assert (lid_machine.root / "heartbeat").read_bytes() == heartbeat


def test_uninstall_quiesces_then_disables_preference(lid_machine):
    lid_machine.activate()
    lid_machine.controller._state["latched"] = "thermal"
    result = lid_machine.controller.uninstall()
    assert result["installed"] is False
    assert result["lid_mode"] == {"enabled": False, "active": False}
    assert result["latched"] is None
    assert lid_machine.controller._owner is None
    assert power.read_record(lid_machine.root / "heartbeat")["desired"] == "off"


def test_safety_thread_runs_independently_and_stops(lid_machine):
    lid_machine.controller.start()
    wait_until(lambda: lid_machine.controller.status()["idle_hold"]["active"], timeout=2)
    thread = lid_machine.controller._thread
    lid_machine.controller.stop()
    assert not thread.is_alive()


def test_preference_changes_before_start_do_not_launch_processes(lid_machine):
    lid_machine.controller.update({"lid_mode": True})
    assert lid_machine.commands == []
    assert lid_machine.children == []


def test_unsupported_platform_has_no_os_commands(lid_machine):
    lid_machine.controller = power.MachinePowerController(
        lid_machine.store,
        demand_reader=lambda: lid_machine.reasons,
        platform="linux",
        run=lid_machine.run,
        spawn=lid_machine.spawn,
        installer=lid_machine.installer,
        directory=lid_machine.root,
    )
    lid_machine.controller.start()
    lid_machine.controller.safety_pass()
    lid_machine.controller.install()
    lid_machine.controller.uninstall()
    lid_machine.controller.stop()
    assert lid_machine.commands == []
    assert lid_machine.children == []
    assert lid_machine.controller.status()["supported"] is False


@pytest.mark.parametrize("off_published", [False, True])
def test_shutdown_replaces_dead_watchdog_to_release(lid_machine, off_published):
    lid_machine.activate()
    if off_published:
        lid_machine.controller._release("disabled")
    lid_machine.controller._watchdog.returncode = -9
    lid_machine.controller.stop()
    heartbeat = power.read_record(lid_machine.root / "heartbeat")
    assert heartbeat["generation"] == "2"
    assert heartbeat["cause"] == "shutdown"
    assert heartbeat["desired"] == "off"
    assert lid_machine.flag is False


@pytest.mark.parametrize("group_alive", [False, True])
def test_watchdog_dies_while_shutdown_waits(lid_machine, group_alive):
    lid_machine.activate()
    previous = lid_machine.controller._watchdog
    previous.on_wait = lambda: setattr(previous, "returncode", -9)
    lid_machine.group_alive = group_alive
    lid_machine.controller.stop()
    assert (previous.pid, signal.SIGTERM) in lid_machine.commands
    if group_alive:
        assert lid_machine.controller._watchdog is previous
        assert lid_machine.flag is True
        assert lid_machine.controller.status()["cleanup_failure"]["kind"] == "clear_failed"
    else:
        assert lid_machine.flag is False
        assert power.read_record(lid_machine.root / "heartbeat")["generation"] == "2"
    lid_machine.group_alive = False


def test_surviving_watchdog_commands_prevent_replacement(lid_machine):
    lid_machine.activate()
    previous = lid_machine.controller._watchdog
    previous.returncode = -9
    lid_machine.group_alive = True
    lid_machine.controller.safety_pass()
    assert lid_machine.controller._watchdog is previous
    assert lid_machine.controller.status()["latched"] == "cleanup_failure"
    assert lid_machine.controller.status()["cleanup_failure"]["kind"] == "clear_failed"
    assert len([argv for argv, _ in lid_machine.children if argv[0] == "/bin/sh"]) == 1
    lid_machine.group_alive = False


@macos_tools
def test_controller_and_real_watchdog_release_and_retire_crashed_group(tmp_path):
    from tests.test_machine_power_watchdog import START, Watchdog

    root = tmp_path / "machine"
    root.mkdir()
    fake = Watchdog(root)
    store = AppStore(tmp_path / "rcp.sqlite3")
    installer = SimpleNamespace(
        paths=SimpleNamespace(directory=root),
        status=lambda: InstallStatus(True, None),
    )
    controller = None
    children = []

    profile = FakeMacOSProfile()
    profile.readings["battery"] = (True, 75)
    profile.read_flag = lambda: (root / "flag").read_text().strip() == "1"

    def spawn(argv):
        assert argv[0] == "/bin/sh"
        child = subprocess.Popen(
            argv,
            env=fake.env,
            start_new_session=True,
            pass_fds=(controller._owner,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
        return child

    controller = power.MachinePowerController(
        store,
        demand_reader=lambda: ["runtime"],
        spawn=spawn,
        clock=time.time,
        process_identity=lambda: (123, START),
        directory=root,
        installer=installer,
        platform="darwin",
        profile=profile,
    )
    try:
        controller.update({"idle_hold": False, "lid_mode": True})
        controller.safety_pass()
        wait_until(lambda: "on" in fake.calls(), timeout=5)
        controller.safety_pass()
        assert controller.status()["lid_mode"]["active"] is True
        children[0].kill()  # Kill only this disposable fake-power process.
        children[0].wait(timeout=5)
        controller.safety_pass()
        heartbeat = power.read_record(root / "heartbeat")
        assert heartbeat["generation"] == "2"
        assert heartbeat["cause"] == "watchdog_lost"
        assert heartbeat["desired"] == "off"
        children[1].wait(timeout=5)
        assert fake.calls() == ["on", "clear"]
        assert (root / "flag").read_text().strip() == "0"
    finally:
        controller.stop()
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_partial_install_still_releases_previously_owned_flag(lid_machine):
    lid_machine.flag = True
    power.write_record(lid_machine.root / "activation", {"generation": 4, "set": 1})
    lid_machine.installer.status = lambda: InstallStatus(False, "partial")
    lid_machine.controller.safety_pass()
    heartbeat = power.read_record(lid_machine.root / "heartbeat")
    assert heartbeat["desired"] == "off"
    assert heartbeat["generation"] == "5"


def test_other_account_installation_keeps_the_idle_hold(lid_machine):
    activation = lid_machine.root / "activation"
    power.write_record(activation, {"generation": 1, "set": 1})
    activation.chmod(0)
    lid_machine.installer.status = lambda: InstallStatus(False, "other_account")
    lid_machine.controller.safety_pass()
    assert lid_machine.controller.status()["idle_hold"]["active"] is True


def test_external_flag_is_reported_without_installation(lid_machine):
    lid_machine.flag = True
    lid_machine.installed = False
    lid_machine.controller.update({"lid_mode": True})
    lid_machine.controller.safety_pass()
    assert lid_machine.controller.status()["external_owner"] is True
    assert lid_machine.controller._watchdog is None


def test_waiting_admin_prompt_does_not_block_safety_passes(lid_machine):
    entered = threading.Event()
    finish = threading.Event()
    passed = threading.Event()
    lid_machine.installed = False
    lid_machine.installer.cancelled = True

    def prompt():
        entered.set()
        assert finish.wait(timeout=5)

    lid_machine.installer.install = prompt
    action = threading.Thread(target=lid_machine.controller.install)
    safety = threading.Thread(target=lambda: (lid_machine.controller.safety_pass(), passed.set()))
    action.start()
    try:
        wait_for_entry(entered)
        safety.start()
        wait_for_entry(passed)
    finally:
        finish.set()
        action.join(timeout=5)
        if safety.ident is not None:
            safety.join(timeout=5)
    assert not action.is_alive()
    assert not safety.is_alive()


def test_thermal_warning_latches_even_when_demand_read_fails(lid_machine):
    lid_machine.activate()
    lid_machine.profile.readings["thermal"] = True
    lid_machine.controller.demand_reader = Mock(side_effect=OSError("store unavailable"))
    lid_machine.controller.safety_pass()
    assert lid_machine.controller.status()["latched"] == "thermal"
    assert power.read_record(lid_machine.root / "heartbeat")["cause"] == "thermal"


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_admin_clear_failure_uses_persisted_cleanup_warning(lid_machine, action):
    lid_machine.installed = False
    setattr(lid_machine.installer, action, Mock(side_effect=InstallError("clear_failed")))
    with pytest.raises(InstallError, match="clear_failed"):
        getattr(lid_machine.controller, action)()
    assert lid_machine.controller.status()["latched"] == "cleanup_failure"
    assert lid_machine.new_controller().status()["cleanup_failure"] == {
        "kind": "clear_failed",
        "command": "sudo pmset -a disablesleep 0",
    }


@pytest.mark.parametrize("failure", ["sleep_failed", "clear_failed", "thermal"])
def test_restart_consumes_previous_executor_result_before_rearming(lid_machine, failure):
    lid_machine.activate()
    lid_machine.controller._release("thermal" if failure == "thermal" else "shutdown")
    lid_machine.execute(
        clear_failed=failure == "clear_failed", sleep_failed=failure == "sleep_failed"
    )
    # Simulate backend death without consuming the completed executor result.
    lid_machine.controller._close_owner()
    lid_machine.controller = lid_machine.new_controller()
    lid_machine.controller.safety_pass()
    status = lid_machine.controller.status()
    assert status["latched"] == ("thermal" if failure == "thermal" else "cleanup_failure")
    assert power.read_record(lid_machine.root / "heartbeat")["desired"] == "off"
    if failure == "clear_failed":
        assert power.read_record(lid_machine.root / "heartbeat")["generation"] == "1"
    assert (
        status["cleanup_failure"] is None
        if failure == "thermal"
        else status["cleanup_failure"]["kind"] == failure
    )
    lid_machine.controller.update({"lid_mode": True})
    lid_machine.controller.stop()
    lid_machine.controller = lid_machine.new_controller()
    lid_machine.controller.safety_pass()
    assert lid_machine.controller.status()["latched"] is None


def test_shutdown_recovers_previous_owned_activation_without_a_safety_pass(lid_machine):
    power.write_record(lid_machine.root / "activation", {"generation": 8, "set": 1})
    lid_machine.flag = True
    lid_machine.controller.stop()
    assert lid_machine.flag is False
    assert power.read_record(lid_machine.root / "heartbeat")["cause"] == "shutdown"


@pytest.mark.parametrize("reading,value", [("battery", (False, 20)), ("thermal", True)])
def test_lid_safety_release_preserves_main_idle_hold(lid_machine, reading, value):
    lid_machine.activate()
    lid_machine.profile.readings[reading] = value
    lid_machine.controller.safety_pass()
    assert lid_machine.controller.status()["lid_mode"]["active"] is False
    assert lid_machine.controller.status()["idle_hold"]["active"] is True


def test_failed_lid_preference_write_preserves_latches(lid_machine, monkeypatch):
    lid_machine.controller._state["latched"] = "thermal"
    before = lid_machine.controller.status()
    monkeypatch.setattr(lid_machine.store, "connection", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        lid_machine.controller.update({"idle_hold": False, "lid_mode": True})
    assert lid_machine.controller.status() == before
