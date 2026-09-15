"""One provider login is one rotating credential, so startups must not overlap.

Codex and Claude both refresh a single-use refresh token while a process starts
and neither locks the credential file, so two overlapping startups can leave a
spent token on disk and kill the login until a human signs in again.
"""

import asyncio
import errno
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from rcp.agents import AgentLauncher, AgentProcessControl, credential_gate
from rcp.agents import launcher as launcher_module
from rcp.agents.credential_gate import (
    CredentialStartupHold,
    ProviderCredentialGate,
    remaining_startup_hold,
)
from rcp.agents.launcher import REMOTE_PROVIDER_START_LINE
from rcp.limits import PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS
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


@pytest.mark.asyncio
async def test_a_young_provider_process_is_not_signalled_inside_the_startup_hold() -> None:
    """A kill during the login refresh spends the single-use refresh token.

    Nothing reports when the refresh runs, so every kill of a process younger
    than the startup hold waits the hold out first. A process that exits on
    its own owes nothing.
    """

    assert remaining_startup_hold(time.monotonic() - 3600) == 0
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)", start_new_session=True
    )
    control = AgentProcessControl()
    control.attach(process)
    control.request_pause()
    await asyncio.sleep(PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS / 4)
    assert process.returncode is None, "the pause signalled the provider inside the hold"
    await asyncio.wait_for(process.wait(), timeout=PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS + 5)
    assert process.returncode != 0


def test_a_finished_probe_releases_without_the_turn_stagger() -> None:
    """The minimum exists because a turn's first line can precede its auth.

    A probe that has exited cannot be mid-authentication, so making it wait
    would serialize readiness behind a stagger it does not need.
    """

    gate = ProviderCredentialGate()
    started = time.monotonic()
    with gate.hold_blocking("codex", ""):
        pass
    with gate.hold_blocking("codex", ""):
        pass

    elapsed = time.monotonic() - started
    assert elapsed < PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS, (
        f"two probes took {elapsed:.2f}s; a finished probe waited out the turn stagger"
    )


def test_a_win_during_loop_teardown_does_not_strand_the_credential() -> None:
    """The worker can win after the loop cancelled its wrapper.

    `asyncio.run` cancels pending tasks before shutting its executor down, so a
    win reported only through that future would be lost and the credential
    locked until RCP restarts. The claim has to settle ownership off the loop.
    """

    gate = ProviderCredentialGate()
    blocker = gate._lock_for("codex", "")
    blocker.acquire()

    async def queue_then_tear_down() -> None:
        waiting = asyncio.create_task(gate.hold("codex", ""))
        await asyncio.sleep(0.05)
        waiting.cancel()
        # Hand the lock over exactly while the cancelled wrapper unwinds.
        blocker.release()

    asyncio.run(queue_then_tear_down())

    async def later() -> None:
        hold = await asyncio.wait_for(gate.hold("codex", ""), timeout=5)
        hold.release()

    asyncio.run(later())


@pytest.mark.asyncio
async def test_two_rcp_processes_under_one_account_do_not_both_hold(
    tmp_path: Path, prompt_minimum: None
) -> None:
    """Separate gates stand in for separate RCP processes.

    Two RCP processes with different data directories share one provider login,
    and the single-instance lock only excludes a second process on the same data
    directory. Each has a private in-process map, so only an OS-visible lock can
    keep them apart.
    """

    root = tmp_path / "shared-account"
    first_process = ProviderCredentialGate(account_lock_root=root)
    second_process = ProviderCredentialGate(account_lock_root=root)

    held = await first_process.hold("codex", "")
    queued = asyncio.create_task(second_process.hold("codex", ""))
    await asyncio.sleep(0.3)

    assert not queued.done(), "two RCP processes held one provider login at once"

    held.release()
    (await asyncio.wait_for(queued, timeout=5)).release()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["directory", "open"])
async def test_account_lock_io_failure_refuses_startup_and_allows_retry(
    tmp_path: Path, failure: str, prompt_minimum: None
) -> None:
    root = tmp_path / "account-locks"
    lock_path = root / "codex.lock"
    if failure == "directory":
        root.write_text("A file prevents the account directory from being created.")
    else:
        lock_path.mkdir(parents=True)
    gate = ProviderCredentialGate(account_lock_root=root)

    with pytest.raises(OSError) as error:
        hold = await asyncio.wait_for(gate.hold("codex", ""), timeout=5)
        hold.release()
    assert error.value.errno in {errno.EEXIST, errno.ENOTDIR, errno.EISDIR}
    assert not gate._lock_for("codex", "").locked()

    if failure == "directory":
        root.unlink()
    else:
        lock_path.rmdir()
    (await asyncio.wait_for(gate.hold("codex", ""), timeout=5)).release()


@pytest.mark.parametrize("error_number", [errno.EOPNOTSUPP, errno.ENOLCK])
def test_permanent_account_lock_error_refuses_probe_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    gate = ProviderCredentialGate(account_lock_root=tmp_path / "account-locks")
    original_flock = credential_gate.fcntl.flock
    failed_descriptor: int | None = None

    def fail_once(descriptor: int, operation: int) -> None:
        nonlocal failed_descriptor
        if failed_descriptor is None:
            failed_descriptor = descriptor
            raise OSError(error_number, "Account filesystem cannot take the lock")
        original_flock(descriptor, operation)

    monkeypatch.setattr(credential_gate.fcntl, "flock", fail_once)
    with pytest.raises(OSError) as error, gate.hold_blocking("codex", ""):
        pytest.fail("A permanent lock error must refuse the probe instead of retrying")
    assert error.value.errno == error_number
    assert not gate._lock_for("codex", "").locked()
    assert failed_descriptor is not None
    with pytest.raises(OSError) as closed:
        credential_gate.os.fstat(failed_descriptor)
    assert closed.value.errno == errno.EBADF

    with gate.hold_blocking("codex", ""):
        pass


@pytest.mark.parametrize("error_number", sorted({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}))
def test_account_lock_contention_still_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    gate = ProviderCredentialGate(account_lock_root=tmp_path / "account-locks")
    original_flock = credential_gate.fcntl.flock
    busy = True

    def busy_once(descriptor: int, operation: int) -> None:
        nonlocal busy
        if busy:
            busy = False
            raise OSError(error_number, "Another process holds the account lock")
        original_flock(descriptor, operation)

    monkeypatch.setattr(credential_gate.fcntl, "flock", busy_once)
    with gate.hold_blocking("codex", ""):
        assert not busy


@pytest.mark.asyncio
async def test_a_remote_launch_is_not_gated_by_the_local_account_lock(
    tmp_path: Path, prompt_minimum: None
) -> None:
    """The credential for a remote account lives on that machine, not this one."""

    root = tmp_path / "shared-account"
    first_process = ProviderCredentialGate(account_lock_root=root)
    second_process = ProviderCredentialGate(account_lock_root=root)

    held = await first_process.hold("codex", "agent-host")
    other = await asyncio.wait_for(second_process.hold("codex", "agent-host"), timeout=2)

    held.release()
    other.release()
    assert not (root / "codex.lock").exists()


@pytest.mark.asyncio
async def test_closing_the_stream_at_the_remote_reservation_frees_the_credential(
    monkeypatch: pytest.MonkeyPatch, prompt_minimum: None
) -> None:
    """A consumer can close the generator while it announces a remote pass.

    That yield sits after the credential is held but before the subprocess arm
    and the stream's own finally, so nothing else would give the credential
    back and every later launch would wait out the expiry.
    """

    launcher = AgentLauncher()
    launcher.readiness = lambda provider, host="", binary=None: type(
        "R",
        (),
        {
            "installed": True,
            "authenticated": True,
            "path_state": "resolved",
            "binary_path": "/opt/claude",
            "version": "2.1.267",
        },
    )()

    async def never_spawn(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("the stream was closed before any provider started")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", never_spawn)

    stream = launcher.stream(
        "claude",
        "prompt",
        cwd=Path("/tmp"),
        capability="scratch_patch",
        host="agent-host",
        binary="/opt/claude",
        remote_pid_file="/tmp/rcp-test.pid",
    )
    async for event in stream:
        if event.event == "remote_process_start":
            break
    await stream.aclose()

    hold = await asyncio.wait_for(launcher.credential_gate.hold("claude", "agent-host"), timeout=5)
    hold.release()


def test_the_authentication_probe_runs_under_the_hold(tmp_path: Path) -> None:
    """Readiness asks the provider whether it is logged in by running it.

    The spec has readiness probes share the credential gate, so a startup warm
    or an explicit Refresh cannot touch the login beside a turn that is
    rotating it.
    """

    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    launcher = AgentLauncher()
    holding: list[str] = []

    def probe(_host, command, **_):
        if command[-1:] == ["--version"]:
            # The version read touches no credential and decides whether the
            # stored answer is current; it runs before the hold.
            return subprocess.CompletedProcess(command, 0, "2.1.267", "")
        assert not launcher.credential_gate._lock_for("claude", "").acquire(False), (
            f"{command[-2:]} ran without the credential hold"
        )
        holding.append(command[-1])
        if command[-2:] == ["auth", "status"]:
            return subprocess.CompletedProcess(command, 0, '{"loggedIn":true}', "")
        return subprocess.CompletedProcess(command, 0, "", "")

    launcher._probe = probe
    launcher.readiness("claude", binary=str(binary))

    assert "status" in holding, "the authentication probe never ran"


@pytest.mark.asyncio
async def test_waiting_launch_captures_environment_and_generation_together(
    tmp_path, monkeypatch, prompt_minimum
):
    from types import SimpleNamespace

    from rcp.agents.provider_accounts import ProviderAccounts
    from rcp.agents.provider_environment import ProviderCredentialStore
    from rcp.provider_auth import CLAUDE_TOKEN_VARIABLE
    from rcp.runs.provider_sign_in import ProviderSignInRunner
    from rcp.storage import AppStore

    store = AppStore(tmp_path / "app.sqlite3")
    credentials = ProviderCredentialStore(tmp_path / "providers")
    credentials.store_token("claude", "", "old-token", member_id="member", now=store.now())
    accounts = ProviderAccounts(store, credentials)
    launcher = AgentLauncher(accounts=accounts)
    ProviderSignInRunner(store, launcher, accounts)
    launcher.readiness = lambda *_, **__: SimpleNamespace(
        installed=True,
        authenticated=True,
        path_state="resolved",
        binary_path="/test/claude",
        version="2.1.267",
    )
    held = await launcher.credential_gate.hold("claude", "")
    waiting = asyncio.Event()
    original_hold = launcher.credential_gate.hold

    async def hold(provider, host):
        waiting.set()
        return await original_hold(provider, host)

    monkeypatch.setattr(launcher.credential_gate, "hold", hold)
    observed = {}

    async def capture():
        observed["generation"] = store.provider_login_state("claude", "").generation

    async def spawn(*args, **kwargs):
        observed["token"] = kwargs["env"][CLAUDE_TOKEN_VARIABLE]
        raise OSError("test provider intentionally stopped before spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def consume():
        return [
            event
            async for event in launcher.stream(
                "claude", "prompt", cwd=tmp_path, capability="scratch_patch", before_start=capture
            )
        ]

    pending = asyncio.create_task(consume())
    await asyncio.wait_for(waiting.wait(), timeout=5)
    credentials.store_token("claude", "", "new-token", member_id="member", now=store.now())
    repaired = store.mark_provider_login_verified(
        "claude", "", member_id="member", detail="verified"
    )
    held.release()
    events = await asyncio.wait_for(pending, timeout=5)
    assert any(event.event == "error" for event in events)
    assert observed == {"generation": repaired.generation, "token": "new-token"}


@pytest.mark.asyncio
async def test_a_result_that_stops_the_provider_waits_out_the_startup_hold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Claude's result stops the process; that stop honours the hold like a Pause.

    The stop_process path, error cleanup, and final cleanup all terminate the
    provider without a Pause, so they must all wait out the same hold before
    signalling a process that may still be rotating its refresh token.
    """

    unwanted = tmp_path / "unwanted-next-turn"
    provider_script = "\n".join(
        (
            "import json, sys, time",
            "from pathlib import Path",
            'print(json.dumps({"type": "result", "result": "Finished."}), flush=True)',
            "time.sleep(30)",
            f"Path({str(unwanted)!r}).write_text('kept running')",
        )
    )
    launcher = AgentLauncher()
    launcher.readiness = lambda provider, host="": type(
        "Readiness", (), {"installed": True, "authenticated": True}
    )()
    monkeypatch.setattr(
        launcher, "_command", lambda *args, **kwargs: [sys.executable, "-c", provider_script]
    )

    started = time.monotonic()
    events = [
        event
        async for event in launcher.stream(
            "claude", "prompt", cwd=tmp_path, capability="scratch_patch"
        )
    ]
    elapsed = time.monotonic() - started

    assert events[-1].event == "done"
    assert elapsed >= PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS, (
        f"the result stop signalled the provider after {elapsed:.2f}s, inside the startup hold"
    )
    assert not unwanted.exists(), "the provider outlived its result"


def test_a_restarted_minimum_holds_the_credential_from_the_provider_start() -> None:
    lock = threading.Lock()
    lock.acquire()
    hold = CredentialStartupHold(lock, minimum=0)
    hold.restart_minimum(0.3)
    hold.release()
    assert lock.locked(), "the restarted stagger did not delay the release"
    time.sleep(0.5)
    assert not lock.locked()


@pytest.mark.asyncio
async def test_a_remote_stop_waits_out_the_hold_from_the_provider_start_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A remote hold anchored to the SSH client can expire during the handshake.

    The wrapper announces the provider's start; the stagger and every stop are
    measured from that line, so a result right after it still waits the hold.
    """

    handshake = 1.0
    executable = tmp_path / "provider"
    executable.write_text(
        f"""#!{sys.executable}
import json, sys, time
time.sleep({handshake})
print({REMOTE_PROVIDER_START_LINE!r}, flush=True)
print(json.dumps({{"type": "result", "result": "Finished."}}), flush=True)
time.sleep(30)
"""
    )
    executable.chmod(0o755)
    launcher = AgentLauncher()
    launcher.readiness = lambda *args, **kwargs: type(
        "Ready",
        (),
        {"installed": True, "authenticated": True, "binary_path": str(executable), "version": "1"},
    )()
    monkeypatch.setattr(launcher, "_remote_login_command", lambda command, **kwargs: command)
    monkeypatch.setattr(launcher_module, "ssh_arguments", lambda host, command, **kwargs: command)
    monkeypatch.setattr(
        AgentProcessControl, "_terminate_remote", staticmethod(lambda host, pid_file: True)
    )
    monkeypatch.setattr(AgentProcessControl, "remote_stopped", staticmethod(lambda *args: True))

    started = time.monotonic()
    events = [
        event
        async for event in launcher.stream(
            "claude",
            "prompt",
            cwd=tmp_path,
            capability="discuss",
            host="fixture-only",
            remote_pid_file="fixture.pid",
        )
    ]
    elapsed = time.monotonic() - started

    assert events[-1].event == "done"
    assert elapsed >= handshake + PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS - 0.1, (
        f"the stop came {elapsed:.2f}s after launch; the hold was measured from the SSH client"
    )
