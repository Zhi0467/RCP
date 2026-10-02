from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rcp import machine_power as power
from rcp.storage import AppStore


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
            self.store, demand_reader=lambda: self.reasons, spawn=spawn, platform=self.platform
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
    machine.controller.safety_pass()
    assert machine.controller.status()["idle_hold"] == {"enabled": False, "active": False}
    assert machine.holds[0].returncode is not None
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
