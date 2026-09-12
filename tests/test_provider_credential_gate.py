"""One provider login is one rotating credential, so startups must not overlap.

Codex and Claude both refresh a single-use refresh token while a process starts
and neither locks the credential file, so two overlapping startups can leave a
spent token on disk and kill the login until a human signs in again.
"""

import asyncio
import json
import threading
from pathlib import Path

import pytest

from rcp.agents import AgentLauncher
from rcp.agents.credential_gate import ProviderCredentialGate
from rcp.provider_skills import ProviderSkillInventoryManager


@pytest.fixture
def prompt_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the stagger so a unit test asserts ordering, not wall time."""

    monkeypatch.setattr(
        "rcp.agents.credential_gate.PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS", 0
    )


@pytest.mark.asyncio
async def test_one_credential_admits_one_startup_at_a_time(prompt_minimum: None) -> None:
    gate = ProviderCredentialGate()
    first = await gate.hold("codex", "")
    second = asyncio.create_task(gate.hold("codex", ""))
    await asyncio.sleep(0)

    assert not second.done(), "a second startup ran while the first held the credential"

    first.release()
    (await second).release()


@pytest.mark.asyncio
async def test_separate_credentials_start_concurrently(prompt_minimum: None) -> None:
    """Staggering is per login, not global; unrelated accounts must not queue."""

    gate = ProviderCredentialGate()
    local = await gate.hold("codex", "")
    remote = await asyncio.wait_for(gate.hold("codex", "agent-host"), timeout=1)
    other_provider = await asyncio.wait_for(gate.hold("claude", ""), timeout=1)

    for hold in (local, remote, other_provider):
        hold.release()


@pytest.mark.asyncio
async def test_release_is_idempotent(prompt_minimum: None) -> None:
    """Every launcher exit path releases, and several of them can run together."""

    gate = ProviderCredentialGate()
    hold = await gate.hold("codex", "")
    hold.release()
    hold.release()

    await asyncio.wait_for(gate.hold("codex", ""), timeout=1)


@pytest.mark.asyncio
async def test_a_silent_provider_does_not_strand_the_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that never speaks must not serialize the whole queue behind it."""

    monkeypatch.setattr(
        "rcp.agents.credential_gate.PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS", 0
    )
    monkeypatch.setattr(
        "rcp.agents.credential_gate.PROVIDER_CREDENTIAL_STARTUP_TIMEOUT_SECONDS", 0.05
    )
    gate = ProviderCredentialGate()
    await gate.hold("codex", "")

    await asyncio.wait_for(gate.hold("codex", ""), timeout=2)


@pytest.mark.asyncio
async def test_launcher_never_overlaps_two_startups_on_one_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: concurrent turns used to start together and race the token."""

    starting = 0
    peak_starting = 0

    class FakeStdin:
        def write(self, value: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        def is_closing(self) -> bool:
            return True

        async def wait_closed(self) -> None:
            return None

    class FakeStdout:
        def __init__(self) -> None:
            self.data = (json.dumps({"type": "turn.completed"}) + "\n").encode()
            self.spoke = False

        async def read(self, size):
            if not self.spoke:
                # Stand in for the provider's auth initialization: without the
                # gate this is exactly the window in which a second process
                # would start and rotate the same refresh token.
                for _ in range(8):
                    await asyncio.sleep(0)
                self.spoke = True
                nonlocal starting
                starting -= 1
                return self.data[:size]
            return b""

    class FakeStderr:
        async def read(self, _size):
            return b""

    class FakeProcess:
        returncode = 0

        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()

        async def wait(self):
            return self.returncode

    async def fake_create_subprocess_exec(*args, **kwargs):
        nonlocal starting, peak_starting
        starting += 1
        peak_starting = max(peak_starting, starting)
        return FakeProcess()

    monkeypatch.setattr(
        "rcp.agents.credential_gate.PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS", 0
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    launcher = AgentLauncher()
    launcher.readiness = lambda provider, host="": type(
        "Readiness", (), {"installed": True, "authenticated": True}
    )()

    async def turn() -> None:
        async for _ in launcher.stream(
            "codex", "prompt", cwd=Path("/tmp"), capability="scratch_patch"
        ):
            pass

    await asyncio.gather(turn(), turn(), turn())

    assert peak_starting == 1, (
        f"{peak_starting} provider startups overlapped on one credential; "
        "concurrent refreshes can spend the same single-use refresh token"
    )


@pytest.mark.asyncio
async def test_minimum_stagger_outlasts_an_immediate_first_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider may rotate its token after it starts talking, so speaking early
    must not immediately release the credential."""

    monkeypatch.setattr(
        "rcp.agents.credential_gate.PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS", 0.2
    )
    gate = ProviderCredentialGate()
    hold = await gate.hold("codex", "")
    hold.release()

    waiting = asyncio.create_task(gate.hold("codex", ""))
    await asyncio.sleep(0.05)
    assert not waiting.done(), "the credential was freed before the minimum stagger"

    (await asyncio.wait_for(waiting, timeout=2)).release()


def test_one_credential_serializes_across_worker_event_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background tasks each run `asyncio.run` on their own thread.

    A loop-bound lock would wake a waiter on the wrong loop and strand it, so
    the gate has to hold across threads, not just across tasks.
    """

    monkeypatch.setattr(
        "rcp.agents.credential_gate.PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS", 0
    )
    gate = ProviderCredentialGate()
    starting = 0
    peak = 0
    peak_guard = threading.Lock()
    failures: list[BaseException] = []

    def worker() -> None:
        async def run() -> None:
            nonlocal starting, peak
            hold = await gate.hold("codex", "")
            try:
                with peak_guard:
                    starting += 1
                    peak = max(peak, starting)
                await asyncio.sleep(0.05)
                with peak_guard:
                    starting -= 1
            finally:
                hold.release()

        try:
            asyncio.run(run())
        except BaseException as exc:  # pragma: no cover - reported below
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not [thread for thread in threads if thread.is_alive()], (
        "a worker never finished; the credential was released onto another loop"
    )
    assert not failures, failures
    assert peak == 1, f"{peak} startups overlapped across worker threads"


@pytest.mark.asyncio
async def test_one_destination_spelled_two_ways_shares_a_hold(
    prompt_minimum: None,
) -> None:
    """Case and stray space must not split one credential into two keys."""

    gate = ProviderCredentialGate()
    first = await gate.hold("codex", "Agent-Host")
    second = asyncio.create_task(gate.hold("codex", " agent-host "))
    await asyncio.sleep(0)

    assert not second.done(), "one destination was admitted twice under two spellings"

    first.release()
    (await second).release()


@pytest.mark.asyncio
async def test_a_skill_probe_and_a_turn_share_one_credential(
    prompt_minimum: None,
) -> None:
    """Skill inventory runs the provider too, so it cannot start beside a turn.

    Composition hands the inventory manager the launcher's gate; a manager with
    its own gate would leave the credential open to exactly the overlap the
    launcher gate closes.
    """

    launcher = AgentLauncher()
    manager = ProviderSkillInventoryManager(
        store=None,  # type: ignore[arg-type]
        credential_gate=launcher.credential_gate,
    )
    assert manager.credential_gate is launcher.credential_gate

    turn_hold = await launcher.credential_gate.hold("codex", "")
    probing = threading.Event()

    def probe() -> None:
        with manager.credential_gate.hold_blocking("codex", ""):
            probing.set()

    worker = threading.Thread(target=probe, daemon=True)
    worker.start()
    worker.join(timeout=0.2)

    assert not probing.is_set(), "a skill probe started while a turn held the credential"

    turn_hold.release()
    worker.join(timeout=5)
    assert probing.is_set()


@pytest.mark.asyncio
async def test_a_cancelled_wait_does_not_strand_the_credential(
    prompt_minimum: None,
) -> None:
    """`asyncio.to_thread` is not cancellable.

    A cancelled waiter's worker thread still takes the lock, and a win nobody
    holds has no expiry behind it, so the login would stay locked until RCP
    restarts.
    """

    gate = ProviderCredentialGate()
    holder = await gate.hold("codex", "")

    queued = asyncio.create_task(gate.hold("codex", ""))
    await asyncio.sleep(0.1)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    holder.release()

    later = await asyncio.wait_for(gate.hold("codex", ""), timeout=5)
    later.release()
