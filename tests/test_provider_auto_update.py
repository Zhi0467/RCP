from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from rcp.agents.launcher import AgentLauncher, ProviderReadiness
from rcp.machine_cards import BACKEND_MACHINE_ID, LoginKey
from rcp.runs.provider_auto_update import auto_update_providers
from rcp.storage import AppStore
from tests.test_server_provider_update import _account, _layout, _seed_turn

_LOGIN = LoginKey("opencode", BACKEND_MACHINE_ID, "")


class _Backend:
    """An idle backend whose service account has OpenCode 1.18.33 in its home."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.layout = _layout(tmp_path)
        self.account = _account(self.layout)
        self.binary = self.layout.service_home / ".opencode" / "bin" / "opencode"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text("#!/bin/sh\n", encoding="utf-8")
        self.binary.chmod(0o755)
        self.version = "1.18.33"
        self.upgrade_exit = 0
        self.calls: list[tuple[str, ...]] = []
        self.launcher = AgentLauncher()
        monkeypatch.setattr(
            self.launcher,
            "_readiness_uncached",
            lambda login, binary=None, **_: ProviderReadiness(
                provider=login.provider,
                installed=True,
                authenticated=True,
                version=self.version,
                binary_path=binary or str(self.binary),
            ),
        )

    def runner(self, _account, argv: tuple[str, ...], _timeout: float):
        self.calls.append(argv)
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, self.version, "")
        if argv[-1] == "upgrade" and self.upgrade_exit == 0:
            self.version = "1.18.34"
        return subprocess.CompletedProcess(argv, self.upgrade_exit, "", "no network")

    def run_pass(self) -> ProviderReadiness:
        self.layout.data_dir.mkdir(parents=True, exist_ok=True)
        store = AppStore(self.layout.data_dir / "rcp.sqlite3")
        try:
            self.launcher.readiness(_LOGIN)
            auto_update_providers(
                store,
                self.launcher,
                account=self.account,
                runner=self.runner,
                stop=threading.Event(),
            )
            return self.launcher.readiness(_LOGIN)
        finally:
            store.close()

    @property
    def upgrades(self) -> int:
        return sum(call[-1] == "upgrade" for call in self.calls)


def test_an_idle_backend_updates_its_cli_and_reads_the_new_version(tmp_path, monkeypatch) -> None:
    backend = _Backend(tmp_path, monkeypatch)

    readiness = backend.run_pass()

    assert backend.upgrades == 1
    assert readiness.version == "1.18.34"
    assert readiness.updated_from == "1.18.33"
    assert readiness.update_warning is None
    other = backend.launcher.readiness(_LOGIN, binary="/opt/other/opencode")
    assert other.updated_from is None


def test_the_card_reads_updating_until_the_new_readiness_is_cached(tmp_path, monkeypatch) -> None:
    backend = _Backend(tmp_path, monkeypatch)
    probe = backend.launcher._readiness_uncached
    seen: list[ProviderReadiness | None] = []

    def watched(login, **kwargs):
        if backend.upgrades:
            seen.append(backend.launcher.cached_readiness(login))
        return probe(login, **kwargs)

    monkeypatch.setattr(backend.launcher, "_readiness_uncached", watched)

    backend.run_pass()

    assert seen and all(read is not None and read.updating for read in seen)
    assert backend.launcher.cached_readiness(_LOGIN).version == "1.18.34"


@pytest.mark.parametrize("status", ["queued", "running", "pausing"])
def test_the_update_waits_while_a_backend_turn_uses_the_provider(
    tmp_path, monkeypatch, status: str
) -> None:
    backend = _Backend(tmp_path, monkeypatch)
    _seed_turn(backend.layout, "opencode", status, None)

    readiness = backend.run_pass()

    assert backend.upgrades == 0
    assert readiness.version == "1.18.33"
    assert readiness.update_warning is None


def test_a_failed_update_warns_on_the_card_and_keeps_the_old_version(tmp_path, monkeypatch) -> None:
    backend = _Backend(tmp_path, monkeypatch)
    backend.upgrade_exit = 1

    readiness = backend.run_pass()

    assert backend.upgrades == 1
    assert readiness.version == "1.18.33"
    assert readiness.installed and readiness.authenticated
    assert readiness.update_warning

    backend.upgrade_exit = 0
    assert backend.run_pass().update_warning is None


def test_a_provider_the_account_has_not_installed_is_skipped(tmp_path, monkeypatch) -> None:
    backend = _Backend(tmp_path, monkeypatch)
    backend.binary.unlink()

    readiness = backend.run_pass()

    assert backend.calls == []
    assert readiness.update_warning is None


def test_a_cli_installed_outside_the_account_home_is_left_alone(tmp_path, monkeypatch) -> None:
    backend = _Backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "rcp.server_ops.provider_update.LocalUpdateTarget.discover",
        lambda _target, provider: str(Path("/usr/local/bin") / provider),
    )

    backend.run_pass()

    assert backend.calls == []


def test_the_backends_configured_executable_is_the_one_updated(tmp_path, monkeypatch) -> None:
    backend = _Backend(tmp_path, monkeypatch)
    configured = backend.layout.service_home / ".local" / "bin" / "opencode"
    configured.parent.mkdir(parents=True)
    configured.write_text("#!/bin/sh\n", encoding="utf-8")
    configured.chmod(0o755)
    monkeypatch.setattr(
        backend.launcher,
        "provider_binary",
        lambda provider, _machine: str(configured) if provider == "opencode" else None,
    )

    backend.run_pass()

    assert backend.upgrades == 1
    assert all(str(configured) in " ".join(call) for call in backend.calls)


@pytest.mark.parametrize("machine_id", [BACKEND_MACHINE_ID, "remote"])
def test_on_demand_refuses_busy_machine_inside_hold(tmp_path, monkeypatch, machine_id) -> None:
    from rcp.runs.provider_auto_update import update_provider
    from rcp.server_ops.provider_update import (
        LocalUpdateTarget,
        ProviderUpdateRefused,
        RemoteUpdateTarget,
    )
    from tests.test_provider_update_targets import Transport

    backend = _Backend(tmp_path, monkeypatch)
    _seed_turn(backend.layout, "opencode", "running", machine_id)
    store = AppStore(backend.layout.data_dir / "rcp.sqlite3")
    transport = Transport("/home/worker")
    target = (
        LocalUpdateTarget(backend.account, backend.runner)
        if machine_id == BACKEND_MACHINE_ID
        else RemoteUpdateTarget("machine", "worker", runner=transport.ssh)
    )
    admitted = []
    from contextlib import contextmanager

    @contextmanager
    def guard():
        admitted.append(True)
        yield

    try:
        with pytest.raises(ProviderUpdateRefused):
            update_provider(
                store,
                backend.launcher,
                "opencode",
                machine_id,
                target=target,
                stop=threading.Event(),
                guard=guard,
            )
        assert admitted == []
        assert backend.calls == []
        assert transport.calls == []
    finally:
        store.close()


def test_daily_pass_defers_to_an_on_demand_hold(tmp_path, monkeypatch) -> None:
    backend = _Backend(tmp_path, monkeypatch)
    with backend.launcher.provider_update("opencode", BACKEND_MACHINE_ID):
        backend.run_pass()
    assert backend.upgrades == 0


def test_on_demand_refuses_outside_home_after_admission(tmp_path, monkeypatch) -> None:
    from rcp.runs.provider_auto_update import update_provider
    from rcp.server_ops.provider_update import LocalUpdateTarget, ProviderUpdateRefused

    backend = _Backend(tmp_path, monkeypatch)
    monkeypatch.setattr(backend.launcher, "provider_binary", lambda *_: "/usr/bin/opencode")
    store = AppStore(tmp_path / "store.sqlite3")
    try:
        with pytest.raises(ProviderUpdateRefused):
            update_provider(
                store,
                backend.launcher,
                "opencode",
                BACKEND_MACHINE_ID,
                target=LocalUpdateTarget(backend.account, backend.runner),
                stop=threading.Event(),
            )
        assert backend.calls == []
        assert backend.launcher.readiness(_LOGIN).update_warning
    finally:
        store.close()


@pytest.mark.parametrize(
    "stage,run_on,expected", [(None, "remote", "remote"), ("other", "remote", "other")]
)
def test_busy_count_uses_queued_target_until_stage_is_captured(tmp_path, stage, run_on, expected):
    from rcp.runs.provider_auto_update import turns_using

    layout = _layout(tmp_path)
    _seed_turn(layout, "opencode", "queued", stage)
    store = AppStore(layout.data_dir / "rcp.sqlite3")
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json, '$.run_on', ?)",
            (run_on,),
        )
    assert turns_using(store, "opencode", expected) == 1
    assert turns_using(store, "opencode", BACKEND_MACHINE_ID) == 0
    assert turns_using(store, "claude", expected) == 0


def test_busy_count_resolves_unstaged_default_to_project_base_machine(tmp_path):
    from rcp.runs.provider_auto_update import turns_using

    layout = _layout(tmp_path)
    _seed_turn(layout, "opencode", "queued", None)
    store = AppStore(layout.data_dir / "rcp.sqlite3")
    project = store.project("project")
    assert project is not None
    manifest = Path(project.locator)
    manifest.write_text(manifest.read_text().replace('"backend"', '"remote"'))
    assert turns_using(store, "opencode", "remote") == 1
    assert turns_using(store, "opencode", BACKEND_MACHINE_ID) == 0
