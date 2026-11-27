from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rcp.agents.launcher import AgentLauncher, ProviderReadiness
from rcp.machine_cards import BACKEND_MACHINE_ID, LoginKey
from rcp.runs.provider_sign_in import ProviderLoginRefused
from rcp.server_ops.provider_update import ProviderUpdateRefused
from tests.test_provider_sign_in import _fake_codex, _runner


def test_update_hold_refuses_duplicate_and_is_scoped_to_machine() -> None:
    launcher = AgentLauncher()
    with launcher.provider_update("codex", BACKEND_MACHINE_ID):
        with (
            pytest.raises(ProviderUpdateRefused),
            launcher.provider_update("codex", BACKEND_MACHINE_ID),
        ):
            pytest.fail("duplicate hold admitted")
        with launcher.provider_update("codex", "remote"):
            assert launcher.provider_updating("codex", "remote")
        assert launcher.provider_updating("codex", BACKEND_MACHINE_ID)
    assert not launcher.provider_updating("codex", BACKEND_MACHINE_ID)


@pytest.mark.parametrize("cached", [False, True])
def test_update_readiness_ignores_refresh_and_preserves_cache(monkeypatch, cached) -> None:
    launcher = AgentLauncher()
    login = LoginKey("codex", BACKEND_MACHINE_ID, "")
    calls = []

    def probe(*args, **kwargs):
        calls.append(True)
        return ProviderReadiness(provider="codex", installed=True, authenticated=True, version="1")

    monkeypatch.setattr(launcher, "_readiness_uncached", probe)
    if cached:
        launcher.readiness(login)
    calls.clear()
    with launcher.provider_update("codex", BACKEND_MACHINE_ID):
        ready = launcher.readiness(login, refresh=True)
        assert ready.updating
        assert ready.version == ("1" if cached else None)
        assert not calls
        assert launcher.member_readiness("codex", machine_id=BACKEND_MACHINE_ID).updating
    assert not launcher.readiness(login).updating


@pytest.mark.parametrize("machine_id,host", [(BACKEND_MACHINE_ID, ""), ("remote", "host")])
def test_launch_waits_before_readiness(monkeypatch, tmp_path: Path, machine_id, host) -> None:
    launcher = AgentLauncher()
    calls = []

    def probe(*args, **kwargs):
        calls.append(True)
        return ProviderReadiness(provider="codex", installed=False, authenticated=False)

    monkeypatch.setattr(launcher, "readiness", probe)

    async def run():
        stream = launcher.stream(
            "codex",
            "hello",
            cwd=tmp_path,
            machine_id=machine_id,
            host=host,
            capability="discuss",
            remote_pid_file="/tmp/provider.pid",
        )
        with launcher.provider_update("codex", machine_id):
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.02)
            assert not pending.done()
            assert not calls
        event = await asyncio.wait_for(pending, timeout=2)
        assert event.event == "error"
        assert calls
        await stream.aclose()

    asyncio.run(run())


def test_update_refuses_sign_in_and_skips_login_verify(tmp_path, monkeypatch) -> None:
    runner = _runner(tmp_path, monkeypatch, _fake_codex(tmp_path))
    login = LoginKey("codex", BACKEND_MACHINE_ID, "")
    with runner.launcher.provider_update("codex", BACKEND_MACHINE_ID):
        with pytest.raises(ProviderLoginRefused):
            runner.start_sign_in(login, member_id="member")
        assert runner.verify(login, member_id="member") == runner.store.provider_login_state(login)
    assert not (tmp_path / "argv.jsonl").exists()


def test_update_invalidates_all_logins_and_paths_only_on_its_machine(monkeypatch) -> None:
    launcher = AgentLauncher()
    monkeypatch.setattr(launcher, "machine_host", lambda machine_id: machine_id)
    version = ["1"]
    monkeypatch.setattr(
        launcher,
        "_readiness_uncached",
        lambda login, **kwargs: ProviderReadiness(
            provider=login.provider, installed=True, authenticated=True, version=version[0]
        ),
    )
    own = LoginKey("codex", BACKEND_MACHINE_ID, "member")
    other = LoginKey("codex", "other", "member")
    for binary in (None, "/home/member/codex"):
        assert launcher.readiness(own, binary=binary).version == "1"
    assert launcher.readiness(other).version == "1"
    version[0] = "2"
    launcher.invalidate_provider_readiness("codex", BACKEND_MACHINE_ID)
    for binary in (None, "/home/member/codex"):
        assert launcher.readiness(own, binary=binary).version == "2"
    assert launcher.readiness(other).version == "1"


def test_update_refresh_does_not_join_an_older_probe(monkeypatch) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    launcher = AgentLauncher()
    login = LoginKey("codex", BACKEND_MACHINE_ID, "member")
    started = threading.Event()
    finish = threading.Event()
    calls = []

    def probe(*args, **kwargs):
        calls.append(True)
        version = str(len(calls))
        if version == "1":
            started.set()
            assert finish.wait(5)
        return ProviderReadiness(
            provider="codex", installed=True, authenticated=True, version=version
        )

    monkeypatch.setattr(launcher, "_readiness_uncached", probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(launcher.readiness, login, refresh=True)
        assert started.wait(5)
        launcher.invalidate_provider_readiness("codex", BACKEND_MACHINE_ID)
        refreshed = pool.submit(launcher.readiness, login, refresh=True)
        try:
            assert refreshed.result(timeout=1).version == "2"
        finally:
            finish.set()
        assert old.result(timeout=2).version == "1"
    assert launcher.readiness(login).version == "2"


def test_sign_in_and_update_admission_are_exclusive() -> None:
    launcher = AgentLauncher()
    with launcher.provider_sign_in("codex", BACKEND_MACHINE_ID):
        with (
            pytest.raises(ProviderUpdateRefused),
            launcher.provider_update("codex", BACKEND_MACHINE_ID),
        ):
            pytest.fail("update admitted during sign-in")
        with launcher.provider_update("codex", "other"):
            assert launcher.provider_updating("codex", "other")
    with (
        launcher.provider_update("codex", BACKEND_MACHINE_ID),
        pytest.raises(ProviderUpdateRefused),
        launcher.provider_sign_in("codex", BACKEND_MACHINE_ID),
    ):
        pytest.fail("sign-in admitted during update")
    with launcher.provider_sign_in("codex", BACKEND_MACHINE_ID):
        pass
