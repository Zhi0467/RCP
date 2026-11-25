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
            lambda login, **_: ProviderReadiness(
                provider=login.provider, installed=True, authenticated=True, version=self.version
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
    assert readiness.update_warning is None


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
        "rcp.runs.provider_auto_update.installed_provider",
        lambda _account, provider: Path("/usr/local/bin") / provider,
    )

    backend.run_pass()

    assert backend.calls == []
