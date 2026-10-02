from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rcp import machine_power as power
from rcp.limits import MACHINE_POWER_COMMAND_TIMEOUT_SECONDS
from rcp.machine_power_install import InstallError, InstallStatus
from rcp.storage import AppStore
from tests.helpers import wait_for_entry, wait_until

BATTERY = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=123)\t75%; discharging; 3:20 remaining present: true\n"
AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=123)\t75%; charging; 1:20 remaining present: true\n"
THERMAL = "Note: No thermal warning level has been recorded\nNote: No performance warning level has been recorded\n"
LID = '+-o Root\n    "AppleClamshellState" = No\n'
FLAG = "System-wide power settings:\nCurrently in use:\n SleepDisabled          0\n"


@pytest.mark.parametrize(
    "parser,output,expected",
    [
        (power.parse_battery, BATTERY, (False, 75)),
        (power.parse_battery, AC, (True, 75)),
        (power.parse_battery, "Now drawing from 'AC Power'\n", (True, None)),
        (power.parse_thermal, THERMAL, False),
        (power.parse_thermal, "Thermal Warning = 1\n", True),
        (power.parse_thermal, "CPU_Speed_Limit = 80\n", True),
        (power.parse_lid, LID, False),
        (power.parse_lid, '"AppleClamshellState" = Yes\n', True),
        (power.parse_flag, FLAG, False),
        (power.parse_flag, " SleepDisabled 1\n", True),
    ],
)
def test_power_output_parsers(parser, output, expected):
    assert parser(output) == expected


@pytest.mark.parametrize(
    "parser", [power.parse_battery, power.parse_thermal, power.parse_lid, power.parse_flag]
)
def test_unknown_output_is_never_safe(parser):
    with pytest.raises(ValueError):
        parser("unrecognized output")


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

    def wait(self, timeout):
        assert timeout > 0
        if self.on_wait:
            self.on_wait()
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake process", timeout)
        return self.returncode


class Machine:
    def __init__(self, tmp_path):
        self.root = tmp_path / "machine"
        self.root.mkdir()
        self.store = AppStore(tmp_path / "rcp.sqlite3")
        self.commands = []
        self.children = []
        self.outputs = {"batt": BATTERY, "therm": THERMAL, "lid": LID}
        self.flag = False
        self.reasons = ["episode"]
        self.fail = None
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
            **kwargs,
        )

    def monotonic(self):
        self.ticks += 1
        return self.ticks

    def run(self, argv, timeout):
        assert timeout == MACHINE_POWER_COMMAND_TIMEOUT_SECONDS
        if argv[0] == "/bin/kill":
            self.commands.append(argv)
            return subprocess.CompletedProcess(
                argv, int(argv[1] == "-0" and not self.group_alive), "", ""
            )
        assert argv in (
            [power.PMSET, "-g", "batt"],
            [power.PMSET, "-g", "therm"],
            [power.PMSET, "-g"],
            [power.IOREG, "-r", "-k", "AppleClamshellState"],
        )
        self.commands.append(argv)
        key = argv[-1] if argv[0] == power.PMSET else "lid"
        if self.fail == key:
            raise subprocess.TimeoutExpired(argv, timeout)
        stdout = f"SleepDisabled {int(self.flag)}\n" if key == "-g" else self.outputs[key]
        return subprocess.CompletedProcess(argv, 0, stdout, "")

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
            assert argv == ["/usr/bin/caffeinate", "-i", "-w", "123"]
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
def machine(tmp_path):
    machine = Machine(tmp_path)
    yield machine
    machine.close()


def test_idle_hold_is_worker_bound_and_lid_hold_requires_ack(machine):
    machine.activate()
    assert machine.children[0][0] == ["/usr/bin/caffeinate", "-i", "-w", "123"]
    assert machine.children[1][0][0] == "/bin/sh"
    heartbeat = power.read_record(machine.root / "heartbeat")
    assert heartbeat["pid"] == "123"
    assert heartbeat["start"] == "Thu Oct  1 03:00:00 2026"
    assert heartbeat["desired"] == "on"
    assert (machine.root / "machine_power_watchdog.sh").read_bytes() == Path(
        power.__file__
    ).with_name("machine_power_watchdog.sh").read_bytes()


def test_no_ack_never_requests_on(machine):
    machine.ack = False
    machine.controller.update({"lid_mode": True})
    machine.controller.safety_pass()
    assert power.read_record(machine.root / "heartbeat")["desired"] == "off"
    assert machine.controller.status()["last_release"]["cause"] == "reading_failed"
    assert machine.controller.status()["idle_hold"]["active"] is False


@pytest.mark.parametrize("reading", ["batt", "therm", "lid", "-g"])
def test_reading_timeouts_release_both_holds(machine, reading):
    machine.activate()
    machine.fail = reading
    machine.commands.clear()
    machine.controller.safety_pass()
    assert len(machine.commands) == 4
    assert power.read_record(machine.root / "heartbeat")["cause"] == "reading_failed"
    assert machine.controller.status()["idle_hold"]["active"] is False
    assert machine.controller.status()["lid_mode"]["active"] is False


@pytest.mark.parametrize("reading", ["batt", "therm", "lid"])
def test_unparseable_reading_releases(machine, reading):
    machine.activate()
    machine.outputs[reading] = "unrecognized output"
    machine.controller.safety_pass()
    assert power.read_record(machine.root / "heartbeat")["cause"] == "reading_failed"


def test_demand_gone_releases_both_holds(machine):
    machine.activate()
    machine.reasons = []
    machine.controller.safety_pass()
    assert power.read_record(machine.root / "heartbeat")["cause"] == "demand_gone"
    assert machine.controller.status()["demand"] is False
    assert machine.controller.status()["idle_hold"]["active"] is False


def test_battery_floor_stays_released_until_ac_then_rearms(machine):
    machine.activate()
    machine.outputs["batt"] = BATTERY.replace("75%", "20%")
    machine.controller.safety_pass()
    assert power.read_record(machine.root / "heartbeat")["cause"] == "battery_floor"
    machine.execute()
    machine.outputs["batt"] = BATTERY  # recovering percent on battery is insufficient
    machine.controller.safety_pass()
    assert machine.controller.status()["lid_mode"]["active"] is False
    machine.outputs["batt"] = AC
    machine.controller.safety_pass()
    assert power.read_record(machine.root / "heartbeat")["desired"] == "on"
    assert power.read_record(machine.root / "heartbeat")["generation"] == "2"


def test_thermal_latch_survives_restart_and_human_reenable_clears_it(machine):
    machine.activate()
    machine.outputs["therm"] = "Thermal Warning = 1\n"
    machine.controller.safety_pass()
    machine.execute()
    machine.controller.stop()
    machine.controller = machine.new_controller()
    assert machine.controller.status()["latched"] == "thermal"
    assert machine.controller.status()["lid_mode"]["enabled"] is True
    machine.outputs["therm"] = THERMAL
    machine.controller.safety_pass()
    assert machine.controller._watchdog is None
    machine.controller.update({"lid_mode": True})
    assert machine.controller.status()["latched"] is None
    machine.controller.safety_pass()
    assert power.read_record(machine.root / "heartbeat")["desired"] == "on"


@pytest.mark.parametrize(
    "clear_failed,sleep_failed,kind",
    [(True, False, "clear_failed"), (False, True, "sleep_failed"), (True, True, "clear_failed")],
)
def test_cleanup_failure_latches_and_records_remedy(machine, clear_failed, sleep_failed, kind):
    machine.activate()
    machine.reasons = []
    machine.controller.safety_pass()
    machine.execute(clear_failed=clear_failed, sleep_failed=sleep_failed)
    machine.controller.safety_pass()
    status = machine.controller.status()
    assert status["latched"] == "cleanup_failure"
    assert status["cleanup_failure"] == {"kind": kind, "command": "sudo pmset -a disablesleep 0"}
    machine.controller.stop()
    other = machine.new_controller()
    assert other.status()["cleanup_failure"] == status["cleanup_failure"]
    other.update({"lid_mode": True})
    assert other.status()["latched"] is None
    assert other.status()["cleanup_failure"] is None


def test_persistence_failure_does_not_prevent_release(machine, monkeypatch):
    machine.activate()
    monkeypatch.setattr(machine.controller, "_save", Mock(side_effect=OSError("disk full")))
    machine.reasons = []
    machine.controller.safety_pass()
    assert power.read_record(machine.root / "heartbeat")["cause"] == "demand_gone"
    assert machine.controller.status()["idle_hold"]["active"] is False


def test_dead_watchdog_is_replaced_only_to_release(machine):
    machine.activate()
    machine.controller._watchdog.returncode = -9
    machine.controller.safety_pass()
    heartbeat = power.read_record(machine.root / "heartbeat")
    assert heartbeat["generation"] == "2"
    assert heartbeat["desired"] == "off"
    assert heartbeat["cause"] == "watchdog_lost"
    assert machine.controller.status()["idle_hold"]["active"] is False


def test_revoked_generation_uses_new_executor(machine):
    machine.activate()
    power.write_record(machine.root / "revoked", {"generation": 1})
    power.write_record(
        machine.root / "result",
        {
            "generation": 1,
            "cause": "heartbeat_stale",
            "clear_failed": 0,
            "sleep_failed": 0,
            "complete": 1,
        },
    )
    power.write_record(machine.root / "activation", {"generation": 1, "set": 0})
    machine.flag = False
    machine.controller._watchdog.returncode = 0
    machine.controller.safety_pass()
    assert power.read_record(machine.root / "heartbeat")["generation"] == "2"
    assert power.read_record(machine.root / "heartbeat")["desired"] == "on"
    assert machine.controller.status()["last_release"]["cause"] == "heartbeat_stale"


def test_restart_recovers_owned_flag_even_when_preferences_off(machine):
    power.write_record(
        machine.root / "activation", {"generation": 8, "pid": 1, "start": "previous", "set": 1}
    )
    machine.flag = True
    machine.controller.safety_pass()
    heartbeat = power.read_record(machine.root / "heartbeat")
    assert heartbeat["generation"] == "9"
    assert heartbeat["desired"] == "off"
    assert heartbeat["cause"] == "watchdog_lost"


def test_external_flag_is_never_adopted_or_cleared(machine):
    machine.flag = True
    machine.controller.update({"lid_mode": True})
    machine.controller.safety_pass()
    assert machine.controller.status()["external_owner"] is True
    assert machine.controller._watchdog is None
    assert not (machine.root / "heartbeat").exists()
    assert machine.flag is True


def test_machine_lock_prevents_two_data_directories_from_owning_lid_mode(machine, tmp_path):
    machine.activate()
    other_store = AppStore(tmp_path / "second.sqlite3")
    other = power.MachinePowerController(
        other_store,
        demand_reader=lambda: ["runtime"],
        run=machine.run,
        spawn=machine.spawn,
        clock=lambda: machine.now,
        process_identity=lambda: (456, "other"),
        directory=machine.root,
        installer=machine.installer,
        platform="darwin",
    )
    other.update({"lid_mode": True, "idle_hold": False})
    other.safety_pass()
    assert other.status()["external_owner"] is True
    assert other._watchdog is None
    assert power.read_record(machine.root / "heartbeat")["pid"] == "123"
    other.stop()


def test_shutdown_off_precedes_worker_bound_hold_exit(machine):
    machine.activate()
    machine.controller.stop()
    assert power.read_record(machine.root / "heartbeat")["desired"] == "off"
    assert power.read_record(machine.root / "heartbeat")["cause"] == "shutdown"
    assert machine.controller._owner is None
    assert machine.controller.status()["idle_hold"]["active"] is False


def test_shutdown_publish_failure_does_not_raise(machine, monkeypatch):
    machine.activate()
    monkeypatch.setattr(machine.controller, "_heartbeat", Mock(side_effect=OSError("disk full")))
    machine.controller.stop()
    assert machine.controller.status()["idle_hold"]["active"] is False


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_cancel_preserves_active_status_preferences_and_heartbeat(machine, action):
    machine.activate()
    before = machine.controller.status()
    heartbeat = (machine.root / "heartbeat").read_bytes()
    machine.installer.cancelled = True
    assert getattr(machine.controller, action)() == before
    assert machine.controller.status() == before
    assert (machine.root / "heartbeat").read_bytes() == heartbeat


def test_uninstall_quiesces_then_disables_preference(machine):
    machine.activate()
    result = machine.controller.uninstall()
    assert result["installed"] is False
    assert result["lid_mode"] == {"enabled": False, "active": False}
    assert machine.controller._owner is None
    assert power.read_record(machine.root / "heartbeat")["desired"] == "off"


def test_safety_thread_runs_independently_and_stops(machine):
    machine.controller.start()
    wait_until(lambda: machine.controller.status()["idle_hold"]["active"], timeout=2)
    thread = machine.controller._thread
    machine.controller.stop()
    assert not thread.is_alive()


def test_preference_changes_before_start_do_not_launch_processes(machine):
    machine.controller.update({"lid_mode": True})
    assert machine.commands == []
    assert machine.children == []


def test_unsupported_platform_has_no_os_commands(machine):
    machine.controller.platform = "linux"
    machine.controller.start()
    machine.controller.safety_pass()
    machine.controller.install()
    machine.controller.uninstall()
    machine.controller.stop()
    assert machine.commands == []
    assert machine.children == []
    assert machine.controller.status()["supported"] is False


@pytest.fixture
def demand_inputs(tmp_path, monkeypatch):
    project = SimpleNamespace(project_id="p", home_space_id="personal", locator="unused")
    episode = SimpleNamespace(episode_id="e")
    tasks = []
    store = SimpleNamespace(
        space_id="personal",
        path=tmp_path / "rcp.sqlite3",
        projects=lambda: [project],
        episodes=lambda *a, **kw: [episode],
        all_project_agent_tasks=lambda _: tasks,
    )
    background = SimpleNamespace(
        runtime_is_idle=lambda: True,
        _controls_lock=threading.Lock(),
        _transport_retry_timers=[],
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
        ("completed", None, False),
        ("stopped", None, False),
        ("failed", None, False),
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
    demand_inputs.background._transport_retry_timers.append(object())
    assert power.demand_snapshot(demand_inputs.store, demand_inputs.background) == ["retry"]


def test_armed_watchers_alone_are_not_demand(demand_inputs):
    demand_inputs.store.watchers = Mock(side_effect=AssertionError("watchers are not demand"))
    assert power.demand_snapshot(demand_inputs.store, demand_inputs.background) == []


def test_shutdown_replaces_dead_watchdog_to_release(machine):
    machine.activate()
    machine.controller._watchdog.returncode = -9
    machine.controller.stop()
    heartbeat = power.read_record(machine.root / "heartbeat")
    assert heartbeat["generation"] == "2"
    assert heartbeat["cause"] == "shutdown"
    assert heartbeat["desired"] == "off"
    assert machine.flag is False


def test_surviving_watchdog_commands_prevent_replacement(machine):
    machine.activate()
    previous = machine.controller._watchdog
    previous.returncode = -9
    machine.group_alive = True
    machine.controller.safety_pass()
    assert machine.controller._watchdog is previous
    assert machine.controller.status()["latched"] == "cleanup_failure"
    assert machine.controller.status()["cleanup_failure"]["kind"] == "clear_failed"
    assert len([argv for argv, _ in machine.children if argv[0] == "/bin/sh"]) == 1
    machine.group_alive = False


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

    def run(argv, timeout):
        if argv[:3] == [power.PMSET, "-g", "batt"]:
            return subprocess.CompletedProcess(argv, 0, AC, "")
        if argv[:3] == [power.PMSET, "-g", "therm"]:
            return subprocess.CompletedProcess(argv, 0, THERMAL, "")
        if argv[0] == power.PMSET:
            argv = [str(root / "pmset"), *argv[1:]]
        elif argv[0] == power.IOREG:
            argv = [str(root / "ioreg"), *argv[1:]]
        else:
            assert argv[0] == "/bin/kill"
            assert int(argv[-1]) == -children[0].pid
        return subprocess.run(argv, timeout=timeout, capture_output=True, text=True, env=fake.env)

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
        run=run,
        spawn=spawn,
        clock=time.time,
        process_identity=lambda: (123, START),
        directory=root,
        installer=installer,
        platform="darwin",
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


def test_unknown_lid_prefix_is_not_parsed_as_open():
    with pytest.raises(ValueError):
        power.parse_lid('"AppleClamshellState" = Nope')


def test_partial_install_still_releases_previously_owned_flag(machine):
    machine.flag = True
    power.write_record(machine.root / "activation", {"generation": 4, "set": 1})
    machine.installer.status = lambda: InstallStatus(False, "partial")
    machine.controller.safety_pass()
    heartbeat = power.read_record(machine.root / "heartbeat")
    assert heartbeat["desired"] == "off"
    assert heartbeat["generation"] == "5"


def test_external_flag_is_reported_without_installation(machine):
    machine.flag = True
    machine.installed = False
    machine.controller.safety_pass()
    assert machine.controller.status()["external_owner"] is True
    assert machine.controller._watchdog is None


def test_waiting_admin_prompt_does_not_block_safety_passes(machine):
    entered = threading.Event()
    finish = threading.Event()
    passed = threading.Event()
    machine.installed = False
    machine.installer.cancelled = True

    def prompt():
        entered.set()
        assert finish.wait(timeout=5)

    machine.installer.install = prompt
    action = threading.Thread(target=machine.controller.install)
    safety = threading.Thread(target=lambda: (machine.controller.safety_pass(), passed.set()))
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


def test_thermal_warning_latches_even_when_demand_read_fails(machine):
    machine.activate()
    machine.outputs["therm"] = "Thermal Warning = 1\n"
    machine.controller.demand_reader = Mock(side_effect=OSError("store unavailable"))
    machine.controller.safety_pass()
    assert machine.controller.status()["latched"] == "thermal"
    assert power.read_record(machine.root / "heartbeat")["cause"] == "thermal"


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_admin_clear_failure_uses_persisted_cleanup_warning(machine, action):
    machine.installed = False
    setattr(machine.installer, action, Mock(side_effect=InstallError("clear_failed")))
    with pytest.raises(InstallError, match="clear_failed"):
        getattr(machine.controller, action)()
    assert machine.controller.status()["latched"] == "cleanup_failure"
    assert machine.new_controller().status()["cleanup_failure"] == {
        "kind": "clear_failed",
        "command": "sudo pmset -a disablesleep 0",
    }
