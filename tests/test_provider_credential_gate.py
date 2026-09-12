"""One provider login is one rotating credential, so startups must not overlap.

Codex and Claude both refresh a single-use refresh token while a process starts
and neither locks the credential file, so two overlapping startups can leave a
spent token on disk and kill the login until a human signs in again.
"""

import asyncio
import json
from pathlib import Path

import pytest

from rcp.agents import AgentLauncher
from rcp.agents.credential_gate import ProviderCredentialGate


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
