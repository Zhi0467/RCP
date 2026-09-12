from __future__ import annotations

import asyncio
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import stat
import subprocess
import threading
from collections.abc import AsyncIterator
from concurrent.futures import Future
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, model_validator

from rcp.agents.credential_gate import ProviderCredentialGate
from rcp.agents.invocation_broker import ProviderInvocationGate
from rcp.agents.steering import LiveProviderSteering
from rcp.agents.write_scope import ProjectWriteScope
from rcp.artifacts import AgentArtifactDescriptor
from rcp.limits import (
    PROVIDER_STDERR_DRAIN_TIMEOUT_SECONDS,
    REMOTE_PROVIDER_KILL_WAIT_SECONDS,
    REMOTE_PROVIDER_PID_WAIT_SECONDS,
    REMOTE_PROVIDER_STOP_POLL_SECONDS,
    REMOTE_PROVIDER_STOP_TIMEOUT_SECONDS,
    REMOTE_PROVIDER_TERM_WAIT_SECONDS,
)
from rcp.providers import (
    AgentCapability,
    ModelChoice,
    ProviderId,
    ProviderRuntimeChoice,
    ProviderSteeringState,
    ProviderSteerReceipt,
    ProviderTurnRequest,
    ProviderUsage,
    profile_for,
)
from rcp.transport.ssh import ssh_arguments
from rcp.transport.state import _remote_script

ProviderPathState = Literal[
    "resolved",
    "missing",
    "denied",
    "unconfigured",
    "unreachable",
]

_EXECUTION_ACCOUNT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,127}")


_REMOTE_PATH_MISSING = 40
_REMOTE_PATH_DENIED = 41
_REMOTE_PATH_NOT_FILE = 42
_REMOTE_PATH_NOT_EXECUTABLE = 43
_REMOTE_PATH_INSPECTION_FAILED = 44
_REMOTE_PATH_PROBE = (
    "import os, stat, sys\n"
    "try:\n"
    "    mode = os.stat(sys.argv[1]).st_mode\n"
    "except FileNotFoundError:\n"
    f"    raise SystemExit({_REMOTE_PATH_MISSING})\n"
    "except PermissionError:\n"
    f"    raise SystemExit({_REMOTE_PATH_DENIED})\n"
    "except OSError:\n"
    f"    raise SystemExit({_REMOTE_PATH_INSPECTION_FAILED})\n"
    "if not stat.S_ISREG(mode):\n"
    f"    raise SystemExit({_REMOTE_PATH_NOT_FILE})\n"
    "if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):\n"
    f"    raise SystemExit({_REMOTE_PATH_NOT_EXECUTABLE})\n"
    "if not os.access(sys.argv[1], os.X_OK):\n"
    f"    raise SystemExit({_REMOTE_PATH_DENIED})\n"
)


class ProviderReadiness(BaseModel):
    provider: ProviderId
    #: The registry's display name, so no surface has to map an id to a label.
    label: str = ""
    installed: bool
    authenticated: bool
    version: str | None = None
    reason: str | None = None
    #: None means the profile has no work-like probe or it could not be checked.
    work_like_available: bool | None = None
    work_like_reason: str | None = None
    #: The exact executable checked. For an unconfigured provider this is the
    #: absolute candidate discovery found and used for a best-effort launch;
    #: a project manifest may save it as a stable pin.
    binary_path: str | None = None
    path_state: ProviderPathState = "resolved"
    #: What this CLI will actually accept, probed where it can enumerate and
    #: declared where it cannot. Empty when the provider is unreachable, which
    #: leaves the UI showing the saved manifest values.
    models: list[ModelChoice] = []
    runtimes: list[ProviderRuntimeChoice] = []
    #: The runtime an omitted manifest value resolves to. Selection surfaces read
    #: this rather than assuming a position inside `runtimes`.
    default_runtime: str = ""

    @model_validator(mode="after")
    def fill_provider_runtimes(self) -> ProviderReadiness:
        profile = profile_for(self.provider)
        if not self.runtimes:
            self.runtimes = list(profile.runtime_choices)
        if not self.default_runtime:
            self.default_runtime = profile.default_runtime
        return self


def work_like_launch_problem(readiness: object) -> str | None:
    """The one reason a Work-like launch is refused on readiness grounds.

    Launch, `rcp server provider check`, and Auto-research Retry admission all
    consult this, so a host that cannot sandbox is refused before a task is
    allocated rather than after its stream begins. `None` means launch may
    proceed; an unchecked probe is not a refusal.
    """

    if getattr(readiness, "work_like_available", None) is False:
        return getattr(readiness, "work_like_reason", None) or (
            "Provider Work readiness is unavailable."
        )
    reason = getattr(readiness, "work_like_reason", None)
    return reason or None


class ProviderExecutionAccount(BaseModel):
    """The nonsecret OS identity reached by the provider launch transport."""

    host: str
    reachable: bool
    os_account: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def identity_matches_reachability(self) -> ProviderExecutionAccount:
        if self.reachable != (self.os_account is not None):
            raise ValueError("reachable execution accounts require one observed account")
        if self.reachable and self.reason is not None:
            raise ValueError("a reachable execution account cannot carry a failure reason")
        if not self.reachable and not self.reason:
            raise ValueError("an unreachable execution account requires a reason")
        return self


class AgentEvent(BaseModel):
    # `answer` is the provider's final assistant message. `message` is any other
    # text the provider emitted on the way there — reasoning, tool output, item
    # summaries. Chat reads only `answer`, so a trace can never be mistaken for
    # the reply.
    event: Literal[
        "session",
        "message",
        "answer",
        "artifact",
        "raw",
        "error",
        "paused",
        "done",
        # Internal durable evidence emitted immediately before the selected
        # runtime can deliver this invocation's provider prompt.
        "runtime",
        # Internal process ownership receipts; these convey no prompt authority.
        "remote_process_start",
        "remote_process_stop",
        # Internal diagnostic emitted when a runtime failed before it could have
        # delivered the prompt and another candidate is about to be tried. API
        # pumps record it instead of forwarding it as UI text.
        "runtime_fallback",
        # Internal orchestration evidence emitted immediately after the provider
        # process exits. API pumps consume it instead of forwarding it as UI text.
        "provider_exit",
    ]
    text: str = ""
    session_id: str | None = None
    artifact: AgentArtifactDescriptor | None = None
    usage: ProviderUsage | None = None


class AgentProcessControl:
    """Thread-safe pause control for one provider subprocess."""

    def __init__(self) -> None:
        self.pause_requested = threading.Event()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._remote_host: str | None = None
        self._remote_pid_file: str | None = None
        self._steering: LiveProviderSteering | None = None

    def steering_state(self) -> ProviderSteeringState:
        with self._lock:
            steering = self._steering
        if steering is None:
            return ProviderSteeringState(False, "No live provider process owns this attempt.")
        return steering.state()

    def steer(
        self, expected_turn_id: str, message_id: str, text: str
    ) -> Future[ProviderSteerReceipt]:
        with self._lock:
            steering = self._steering
            loop = self._loop
            if steering is not None and loop is not None and not loop.is_closed():
                return asyncio.run_coroutine_threadsafe(
                    steering.send(expected_turn_id, message_id, text), loop
                )
        refused: Future[ProviderSteerReceipt] = Future()
        refused.set_result(
            ProviderSteerReceipt("refused", "No live provider process owns this attempt.")
        )
        return refused

    def attach_steering(self, steering: LiveProviderSteering) -> None:
        with self._lock:
            self._steering = steering

    def configure_remote_termination(self, host: str, pid_file: str) -> None:
        with self._lock:
            self._remote_host = host
            self._remote_pid_file = pid_file

    def request_pause(self) -> None:
        self.pause_requested.set()
        with self._lock:
            loop = self._loop
            process = self._process
            remote_host = self._remote_host
            remote_pid_file = self._remote_pid_file
        if remote_host and remote_pid_file:
            threading.Thread(
                target=self._terminate_remote,
                args=(remote_host, remote_pid_file),
                name="rcp-remote-pause",
                daemon=True,
            ).start()
        if loop is not None and process is not None and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._terminate(process)))

    def attach(self, process: asyncio.subprocess.Process) -> None:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._process = process
        if self.pause_requested.is_set():
            asyncio.create_task(self._terminate(process))

    def detach(self, process: asyncio.subprocess.Process) -> None:
        with self._lock:
            if self._process is process:
                self._steering = None
                self._process = None
                self._loop = None

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            return
        except TimeoutError:
            pass
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()

    @staticmethod
    def remote_stopped(host: str, pid_file: str) -> bool | None:
        """Observe the exact remote process group; unavailable is not stopped."""
        command = [
            "python3",
            "-c",
            _remote_script("remote_terminate_provider.py"),
            "--probe",
            pid_file,
        ]
        try:
            result = subprocess.run(
                ssh_arguments(host, shlex.join(command)),
                capture_output=True,
                text=True,
                timeout=REMOTE_PROVIDER_STOP_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return {0: True, 1: False}.get(result.returncode)

    @staticmethod
    def _confirm_remote_stopped(host: str, pid_file: str) -> bool:
        """Settle this newly spawned pass before allowing another runtime or turn."""
        return AgentProcessControl.remote_stopped(host, pid_file) is True or (
            AgentProcessControl._terminate_remote(host, pid_file)
        )

    @staticmethod
    def _terminate_remote(host: str, pid_file: str) -> bool:
        command = [
            "python3",
            "-c",
            _remote_script("remote_terminate_provider.py"),
            pid_file,
            str(REMOTE_PROVIDER_PID_WAIT_SECONDS),
            str(REMOTE_PROVIDER_TERM_WAIT_SECONDS),
            str(REMOTE_PROVIDER_KILL_WAIT_SECONDS),
            str(REMOTE_PROVIDER_STOP_POLL_SECONDS),
        ]
        try:
            result = subprocess.run(
                ssh_arguments(host, shlex.join(command)),
                capture_output=True,
                text=True,
                timeout=REMOTE_PROVIDER_STOP_TIMEOUT_SECONDS,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


@dataclass
class _ReadinessProbe:
    """One process-local probe shared by concurrent callers."""

    completed: threading.Event = field(default_factory=threading.Event)
    result: ProviderReadiness | None = None
    error: BaseException | None = None


class _PrePromptRuntimeFailure(RuntimeError):
    """A provider runtime ended before it could have accepted RCP's prompt."""


def _discover_local_provider(provider: str) -> str | None:
    discovered = shutil.which(provider)
    if discovered:
        # Keep the command path stable across provider-native updates. Native
        # installers commonly replace a symlink target with a versioned binary;
        # persisting the resolved target would pin RCP to the old version.
        return str(Path(discovered))
    try:
        account_home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError:
        return None
    candidate = account_home / ".local" / "bin" / provider
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


class AgentLauncher:
    # Consume pipes in small chunks so asyncio never has to buffer one complete
    # provider event. Final graph patches may be large, but tool/read events
    # beyond the explicit per-event bound are diagnostic noise and are drained.
    _STREAM_LIMIT = 64 * 1024
    _MAX_EVENT_BYTES = 16 * 1024 * 1024
    _MAX_STDERR_BYTES = 1024 * 1024

    def __init__(self) -> None:
        self._readiness_lock = threading.Lock()
        self._readiness_cache: dict[tuple[str, str, str | None], ProviderReadiness] = {}
        self._readiness_probes: dict[tuple[str, str, str | None, bool], _ReadinessProbe] = {}
        self._readiness_generations: dict[tuple[str, str, str | None], int] = {}
        #: Shared so every process that can rotate this login is staggered,
        #: not only task turns.
        self.credential_gate = ProviderCredentialGate()

    def readiness(
        self,
        provider: str,
        *,
        host: str = "",
        binary: str | None = None,
        refresh: bool = False,
    ) -> ProviderReadiness:
        """Return one app-scoped capability, probing only when it is not known.

        Configured capabilities are keyed by their exact executable. Discovery
        without a configured path is a separate entry; an installed and
        authenticated discovered executable is still launchable, while the
        ``unconfigured`` state tells settings that no stable path is saved yet.
        """

        key = (provider, host, binary)
        with self._readiness_lock:
            if not refresh and (cached := self._readiness_cache.get(key)) is not None:
                return cached.model_copy(deep=True)
            forced_key = (*key, True)
            probe_key = forced_key if refresh else (*key, False)
            # An ordinary caller can use a forced probe already in flight. A
            # forced caller must never inherit an ordinary warm-up generation.
            probe = self._readiness_probes.get(forced_key)
            if not refresh and probe is None:
                probe = self._readiness_probes.get(probe_key)
            owner = probe is None
            if probe is None:
                probe = _ReadinessProbe()
                if refresh:
                    self._readiness_cache.pop(key, None)
                    self._readiness_generations[key] = self._readiness_generations.get(key, 0) + 1
                self._readiness_probes[probe_key] = probe
            generation = self._readiness_generations.get(key, 0)

        if not owner:
            probe.completed.wait()
            if probe.error is not None:
                raise probe.error
            assert probe.result is not None
            return probe.result.model_copy(deep=True)

        try:
            result = self._readiness_uncached(provider, host=host, binary=binary)
        except BaseException as exc:
            with self._readiness_lock:
                if self._readiness_generations.get(key, 0) == generation:
                    self._readiness_cache.pop(key, None)
                self._readiness_probes.pop(probe_key, None)
                probe.error = exc
                probe.completed.set()
            raise

        with self._readiness_lock:
            if self._readiness_generations.get(key, 0) == generation:
                self._readiness_cache[key] = result.model_copy(deep=True)
            probe.result = result.model_copy(deep=True)
            self._readiness_probes.pop(probe_key, None)
            probe.completed.set()
        return result

    def cached_readiness(
        self,
        provider: str,
        *,
        host: str = "",
        binary: str | None = None,
    ) -> ProviderReadiness | None:
        """Return one already-probed capability, or None rather than probing."""

        with self._readiness_lock:
            cached = self._readiness_cache.get((provider, host, binary))
        return None if cached is None else cached.model_copy(deep=True)

    def invalidate_readiness(
        self,
        provider: str,
        *,
        host: str = "",
        binary: str | None = None,
    ) -> None:
        """Forget one exact capability without disturbing other projects."""

        with self._readiness_lock:
            key = (provider, host, binary)
            self._readiness_cache.pop(key, None)
            self._readiness_generations[key] = self._readiness_generations.get(key, 0) + 1

    def execution_account(self, *, host: str = "") -> ProviderExecutionAccount:
        """Resolve the exact OS account reached by the same local or SSH route as launches."""

        if not host:
            try:
                account = pwd.getpwuid(os.geteuid()).pw_name
            except (KeyError, OSError):
                return ProviderExecutionAccount(
                    host="",
                    reachable=False,
                    reason="The local provider execution account could not be resolved.",
                )
            return ProviderExecutionAccount(host="", reachable=True, os_account=account)
        result = self._probe(host, ["id", "-un"], login_shell=False)
        if result.returncode != 0:
            return ProviderExecutionAccount(
                host=host,
                reachable=False,
                reason=f"The configured SSH route to {host} is unavailable.",
            )
        lines = result.stdout.strip().splitlines()
        account = lines[-1].strip() if len(lines) == 1 else ""
        if _EXECUTION_ACCOUNT.fullmatch(account) is None:
            return ProviderExecutionAccount(
                host=host,
                reachable=False,
                reason=f"The configured SSH route to {host} did not report one safe OS account.",
            )
        return ProviderExecutionAccount(host=host, reachable=True, os_account=account)

    def _readiness_uncached(
        self,
        provider: str,
        *,
        host: str,
        binary: str | None,
    ) -> ProviderReadiness:
        profile = profile_for(provider)
        configured = binary is not None
        candidate = binary
        if configured:
            path_state, path_problem = self._configured_path_state(binary, host=host)
            if path_state != "resolved":
                return ProviderReadiness(
                    provider=provider,
                    label=profile.label,
                    installed=False,
                    authenticated=False,
                    binary_path=binary,
                    path_state=path_state,
                    reason=path_problem,
                )
            installed = True
        elif host:
            installed_probe = self._probe(host, ["command", "-v", provider])
            if installed_probe.returncode == 255:
                return ProviderReadiness(
                    provider=provider,
                    label=profile.label,
                    installed=False,
                    authenticated=False,
                    path_state="unreachable",
                    reason=f"{host} is unreachable, so {provider} could not be checked.",
                )
            discovered = installed_probe.stdout.strip().splitlines()
            candidate = discovered[-1] if installed_probe.returncode == 0 and discovered else None
            installed = bool(candidate and PurePosixPath(candidate).is_absolute())
        else:
            candidate = _discover_local_provider(provider)
            installed = candidate is not None
        if not installed or candidate is None:
            where = f" on {host}" if host else ""
            reason = (
                f"No {provider} executable is recorded; the CLI is not installed "
                f"or discoverable{where}."
            )
            return ProviderReadiness(
                provider=provider,
                label=profile.label,
                installed=False,
                authenticated=False,
                path_state="unconfigured",
                reason=reason,
            )
        version_result = self._probe(host, [candidate, "--version"])
        if host and version_result.returncode == 255:
            return ProviderReadiness(
                provider=provider,
                label=profile.label,
                installed=False,
                authenticated=False,
                binary_path=candidate,
                path_state="unreachable",
                reason=f"{host} became unreachable while checking {candidate}.",
            )
        version_lines = (version_result.stdout or version_result.stderr).strip().splitlines()
        version = version_lines[-1] if version_result.returncode == 0 and version_lines else None
        auth = self._probe(host, profile.auth_command(candidate))
        authenticated = profile.is_authenticated(auth)
        # Enumerate only once the CLI is known to answer. An unauthenticated
        # catalog probe just costs a subprocess to learn what auth already said.
        catalog_command = profile.catalog_command(candidate) if authenticated else None
        # Both of these run the provider itself and load the same login a turn
        # does, so they are staggered against turns rather than racing them.
        with self.credential_gate.hold_blocking(provider, host):
            catalog = self._probe(host, catalog_command) if catalog_command else None
        work_like_available = None
        work_like_reason = None
        work_command = profile.work_like_probe_command(candidate) if authenticated else None
        if work_command is not None:
            with self.credential_gate.hold_blocking(provider, host):
                work_probe = self._probe(host, work_command)
            if work_probe.returncode == 255:
                where = f" on {host}" if host else ""
                work_like_reason = (
                    f"{profile.label} Work readiness could not be checked{where}: "
                    + (_meaningful_stderr(work_probe.stderr) or "provider probe unavailable.")
                )
            else:
                work_like_available = work_probe.returncode == 0
                if not work_like_available:
                    work_like_reason = (
                        _meaningful_stderr(work_probe.stderr)
                        or work_probe.stdout.strip()
                        or f"{profile.label} Work readiness probe exited {work_probe.returncode}."
                    )
        return ProviderReadiness(
            provider=provider,
            label=profile.label,
            installed=True,
            authenticated=authenticated,
            work_like_available=work_like_available,
            work_like_reason=work_like_reason,
            version=version,
            binary_path=candidate,
            path_state="resolved" if configured else "unconfigured",
            reason=(
                f"{provider} was found at {candidate}; using the discovered path until a saved path is provided."
                if authenticated and not configured
                else (
                    None
                    if authenticated
                    else f"{provider} CLI is not authenticated{f' on {host}' if host else ''}."
                )
            ),
            models=profile.models(catalog) if authenticated else [],
        )

    def _configured_path_state(
        self,
        binary: str,
        *,
        host: str,
    ) -> tuple[ProviderPathState, str | None]:
        where = f" on {host}" if host else ""
        if host:
            probe = self._probe(host, ["python3", "-c", _REMOTE_PATH_PROBE, binary])
            if probe.returncode == 0:
                return "resolved", None
            if probe.returncode == 255:
                return (
                    "unreachable",
                    f"{host} is unreachable, so {binary} could not be checked.",
                )
            if probe.returncode == _REMOTE_PATH_MISSING:
                return "missing", f"The recorded executable {binary}{where} does not exist."
            if probe.returncode == _REMOTE_PATH_NOT_FILE:
                return "denied", f"The recorded executable {binary}{where} is not a regular file."
            if probe.returncode == _REMOTE_PATH_NOT_EXECUTABLE:
                return "denied", f"The recorded executable {binary}{where} is not executable."
            if probe.returncode == _REMOTE_PATH_DENIED:
                return "denied", f"Execute access to {binary}{where} was denied."
            return "denied", f"The recorded executable {binary}{where} could not be inspected."

        try:
            mode = Path(binary).stat().st_mode
        except FileNotFoundError:
            return "missing", f"The recorded executable {binary} does not exist."
        except PermissionError:
            return "denied", f"Access to the recorded executable {binary} was denied."
        except OSError as exc:
            return "denied", f"The recorded executable {binary} could not be inspected: {exc}."
        if not stat.S_ISREG(mode):
            return "denied", f"The recorded executable {binary} is not a regular file."
        if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            return "denied", f"The recorded executable {binary} is not executable."
        if not os.access(binary, os.X_OK):
            return "denied", f"Execute access to {binary} was denied."
        return "resolved", None

    async def stream(
        self,
        provider: str,
        prompt: str,
        *,
        cwd: Path,
        model: str | None = None,
        reasoning: str | None = None,
        session_id: str | None = None,
        read_dirs: list[Path] | None = None,
        write_dirs: list[Path] | None = None,
        write_scope: ProjectWriteScope | None = None,
        host: str = "",
        control: AgentProcessControl | None = None,
        remote_pid_file: str | None = None,
        invocation_gate: ProviderInvocationGate | None = None,
        capability: AgentCapability,
        binary: str | None = None,
        runtime_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run the preferred provider runtime, falling back only before prompt delivery."""

        runtimes = profile_for(provider).runtime_candidates(runtime_id)
        last_failure: _PrePromptRuntimeFailure | None = None
        for index, runtime in enumerate(runtimes):
            try:
                async with aclosing(
                    self._stream_runtime(
                        provider,
                        prompt,
                        cwd=cwd,
                        model=model,
                        reasoning=reasoning,
                        session_id=session_id,
                        read_dirs=read_dirs,
                        write_dirs=write_dirs,
                        write_scope=write_scope,
                        host=host,
                        control=control,
                        remote_pid_file=(
                            f"{remote_pid_file}.{index}"
                            if remote_pid_file and index
                            else remote_pid_file
                        ),
                        invocation_gate=invocation_gate,
                        capability=capability,
                        binary=binary,
                        runtime_id=runtime.id,
                    )
                ) as stream:
                    async for event in stream:
                        yield event
                return
            except _PrePromptRuntimeFailure as exc:
                last_failure = exc
                if index + 1 < len(runtimes):
                    # The next candidate will succeed silently, so this is the
                    # only place the reason the human's chosen runtime was not
                    # used can still be recorded.
                    yield AgentEvent(
                        event="runtime_fallback",
                        text=json.dumps(
                            {"runtime_id": runtime.id, "detail": str(exc)},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
        assert last_failure is not None
        yield AgentEvent(event="error", text=str(last_failure))

    async def _stream_runtime(
        self,
        provider: str,
        prompt: str,
        *,
        cwd: Path,
        model: str | None = None,
        reasoning: str | None = None,
        session_id: str | None = None,
        read_dirs: list[Path] | None = None,
        write_dirs: list[Path] | None = None,
        write_scope: ProjectWriteScope | None = None,
        host: str = "",
        control: AgentProcessControl | None = None,
        remote_pid_file: str | None = None,
        invocation_gate: ProviderInvocationGate | None = None,
        capability: AgentCapability,
        binary: str | None = None,
        runtime_id: str,
    ) -> AsyncIterator[AgentEvent]:
        if control is not None and control.pause_requested.is_set():
            yield AgentEvent(event="paused", text="Paused before the provider started.")
            return
        readiness_kwargs: dict[str, str] = {"host": host}
        if binary is not None:
            readiness_kwargs["binary"] = binary
        readiness = await asyncio.to_thread(
            self.readiness,
            provider,
            **readiness_kwargs,
        )
        discovered_path_is_usable = (
            binary is None
            and getattr(readiness, "path_state", "resolved") == "unconfigured"
            and bool(getattr(readiness, "binary_path", None))
        )
        if (
            not (
                getattr(readiness, "path_state", "resolved") == "resolved"
                or discovered_path_is_usable
            )
            or not readiness.installed
            or not readiness.authenticated
        ):
            yield AgentEvent(event="error", text=readiness.reason or "Provider is unavailable.")
            return

        if write_scope is not None:
            if str(cwd) != write_scope.workspace_root:
                yield AgentEvent(
                    event="error",
                    text="Provider workspace does not match the resolved project write scope.",
                )
                return
            if host != write_scope.execution_host:
                yield AgentEvent(
                    event="error",
                    text="Provider execution host does not match the resolved project write scope.",
                )
                return

        profile = profile_for(provider)
        work_problem = (
            work_like_launch_problem(readiness)
            if capability in {"work_auto", "orchestrate"}
            else None
        )
        if work_problem is not None:
            yield AgentEvent(event="error", text=work_problem)
            return
        runtime = profile.runtime(runtime_id)
        resolved_binary = getattr(readiness, "binary_path", None) or binary or provider
        legacy_command = (
            self._command(
                provider,
                prompt,
                binary=resolved_binary,
                cwd=cwd,
                model=model,
                reasoning=reasoning,
                session_id=session_id,
                read_dirs=read_dirs or [],
                write_dirs=write_dirs or [],
                write_scope=write_scope,
                capability=capability,
                provider_version=getattr(readiness, "version", None),
            )
            if runtime.id == profile.legacy_runtime_id
            else None
        )
        try:
            turn = runtime.turn(
                ProviderTurnRequest(
                    prompt=prompt,
                    binary=resolved_binary,
                    cwd=cwd,
                    model=model,
                    reasoning=reasoning,
                    session_id=session_id,
                    read_dirs=read_dirs or [],
                    write_dirs=write_dirs or [],
                    write_scope=write_scope,
                    capability=capability,
                    provider_version=getattr(readiness, "version", None),
                    legacy_command=legacy_command,
                )
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise _PrePromptRuntimeFailure(str(exc)) from exc
        command = turn.command
        if invocation_gate is not None:
            command = invocation_gate.wrap_command(command)
        local_cwd: str | None = str(cwd)
        if host:
            command = ssh_arguments(
                host,
                self._remote_login_command(command, pid_file=remote_pid_file, cwd=cwd),
            )
            local_cwd = None
            if control is not None and remote_pid_file:
                control.configure_remote_termination(host, remote_pid_file)
        if control is not None and control.pause_requested.is_set():
            yield AgentEvent(event="paused", text="Paused before the provider started.")
            return
        if host and remote_pid_file:
            yield AgentEvent(event="remote_process_start", text=remote_pid_file)
        # Held until this provider speaks, so two turns cannot rotate one
        # credential's refresh token at the same time.
        credential_hold = await self.credential_gate.hold(provider, host)
        if control is not None and control.pause_requested.is_set():
            # The pre-launch check ran before this wait, which can be long.
            credential_hold.release()
            yield AgentEvent(event="paused", text="Paused before the provider started.")
            return
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=local_cwd,
                limit=self._STREAM_LIMIT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except BaseException as exc:
            credential_hold.release()
            if not isinstance(exc, OSError):
                raise
            if host and remote_pid_file:
                yield AgentEvent(event="remote_process_stop", text=remote_pid_file)
            if runtime.id == profile.legacy_runtime_id:
                self.invalidate_readiness(provider, host=host, binary=binary)
            raise _PrePromptRuntimeFailure(str(exc)) from exc
        if control is not None:
            control.attach(process)
        steering = LiveProviderSteering(
            process, turn, control.pause_requested if control is not None else threading.Event()
        )
        if control is not None:
            control.attach_steering(steering)
        stdin_task: asyncio.Task[None] | None = None
        stderr_task: asyncio.Task[str] | None = None
        stdout_task: asyncio.Task[tuple[bytes | None, int]] | None = None
        stdout_lines = None
        remote_stopped: bool | None = None
        prompt_delivered = False
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            prompt_bytes = turn.initial_input()
            if invocation_gate is not None:
                prompt_bytes = invocation_gate.bootstrap(prompt_bytes)
            prompt_delivered = False
            pre_prompt_error = ""
            if turn.initial_input_delivers_prompt:
                yield AgentEvent(event="runtime", text=runtime.id)
                # From this point a write may be partial even if drain raises;
                # retrying through another runtime could duplicate the turn.
                prompt_delivered = True
            stdin_task = asyncio.create_task(
                _feed_stdin(
                    process.stdin,
                    prompt_bytes,
                    close=turn.close_input_after_initial,
                )
            )
            stderr_task = (
                asyncio.create_task(
                    _read_bounded_text(
                        process.stderr,
                        max_bytes=self._MAX_STDERR_BYTES,
                        chunk_bytes=self._STREAM_LIMIT,
                    )
                )
                if process.stderr is not None
                else None
            )
            stdout_lines = _read_bounded_lines(
                process.stdout,
                max_line_bytes=self._MAX_EVENT_BYTES,
                chunk_bytes=self._STREAM_LIMIT,
            )
            stdout_task = asyncio.create_task(anext(stdout_lines))
            stdin_pending = True
            stderr_pending = stderr_task is not None
            stderr = ""
            provider_failed = False
            protocol_complete = False
            event_counts: dict[str, int] = {}
            explicit_terminal_event = False
            stopped_at_result = False
            completion_stop_failed = False
            while stdout_task is not None:
                monitored: set[asyncio.Task[object]] = {stdout_task}
                if stdin_pending:
                    monitored.add(stdin_task)
                if stderr_pending:
                    assert stderr_task is not None
                    monitored.add(stderr_task)
                done, _ = await asyncio.wait(
                    monitored,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stdin_pending and stdin_task in done:
                    await stdin_task
                    stdin_pending = False
                if stderr_pending and stderr_task in done:
                    stderr = await stderr_task
                    stderr_pending = False
                if stdout_task not in done:
                    continue
                try:
                    raw_line, omitted_bytes = stdout_task.result()
                except StopAsyncIteration:
                    stdout_task = None
                    break
                stdout_task = asyncio.create_task(anext(stdout_lines))
                if omitted_bytes:
                    event = AgentEvent(
                        event="raw",
                        text=(
                            "Omitted oversized provider event "
                            f"({omitted_bytes} bytes; limit {self._MAX_EVENT_BYTES} bytes)."
                        ),
                    )
                    event_counts[event.event] = event_counts.get(event.event, 0) + 1
                    credential_hold.release()
                    if prompt_delivered:
                        yield event
                    continue
                assert raw_line is not None
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                if invocation_gate is not None and line == invocation_gate.ready_line:
                    # The broker prints this straight after spawning, before the
                    # provider authenticates, so it does not end the hold.
                    continue
                # A line the provider itself wrote: it is past its own auth
                # initialization and the next startup may begin.
                credential_hold.release()
                if stdin_pending:
                    await stdin_task
                    stdin_pending = False
                step = turn.receive_line(line)
                steering.observe(step)
                if step.stop_process:
                    # Claude can interpret stdin as another turn after result.
                    # Fence delivery and stop before yielding any result event.
                    stopped_at_result = True
                    process.stdin.close()
                    if host and remote_pid_file:
                        stopped_remote = await asyncio.to_thread(
                            AgentProcessControl._terminate_remote, host, remote_pid_file
                        )
                        completion_stop_failed = not stopped_remote
                    elif host:
                        completion_stop_failed = True
                    await AgentProcessControl._terminate(process)
                if step.delivers_prompt:
                    if prompt_delivered:
                        raise RuntimeError(
                            f"{provider} attempted to deliver one prompt more than once."
                        )
                    yield AgentEvent(event="runtime", text=runtime.id)
                    # Mark this before the actual write: a partial JSON-RPC
                    # request is already beyond the safe fallback boundary.
                    prompt_delivered = True
                for outgoing in step.outgoing:
                    await _write_stdin(process.stdin, outgoing)
                for decoded in step.events:
                    event = AgentEvent(
                        event=decoded.event,
                        text=decoded.text,
                        session_id=decoded.session_id,
                        usage=decoded.usage,
                    )
                    event_counts[event.event] = event_counts.get(event.event, 0) + 1
                    if event.event == "error":
                        # Consumers stop at the first error. Capture the diagnostic
                        # before yielding it, including when a result already stopped
                        # the process and bypasses the nonzero-exit branch below.
                        process.stdin.close()
                        if stderr_pending:
                            assert stderr_task is not None
                            try:
                                # Other protocols can finish their error response on
                                # EOF. Give them a bounded graceful exit before TERM.
                                stderr = await asyncio.wait_for(
                                    asyncio.shield(stderr_task),
                                    timeout=PROVIDER_STDERR_DRAIN_TIMEOUT_SECONDS,
                                )
                            except TimeoutError:
                                if not step.stop_process:
                                    if host and remote_pid_file:
                                        await asyncio.to_thread(
                                            AgentProcessControl._terminate_remote,
                                            host,
                                            remote_pid_file,
                                        )
                                    await AgentProcessControl._terminate(process)
                                try:
                                    stderr = await asyncio.wait_for(
                                        stderr_task,
                                        timeout=PROVIDER_STDERR_DRAIN_TIMEOUT_SECONDS,
                                    )
                                except TimeoutError:
                                    stderr = "Provider stderr did not close after termination."
                            stderr_pending = False
                        detail = _meaningful_stderr(stderr)
                        if detail and detail not in event.text:
                            event.text = "\n".join(part for part in (event.text, detail) if part)
                        if host and remote_pid_file:
                            remote_stopped = await asyncio.to_thread(
                                AgentProcessControl._confirm_remote_stopped, host, remote_pid_file
                            )
                            completion_stop_failed = not remote_stopped
                            if prompt_delivered and remote_stopped:
                                # Consumers may close on the first error, before
                                # provider_exit can settle the durable pass.
                                yield AgentEvent(event="remote_process_stop", text=remote_pid_file)
                        provider_failed = True
                        if not prompt_delivered and not pre_prompt_error:
                            pre_prompt_error = event.text
                    if prompt_delivered:
                        yield event
                explicit_terminal_event = explicit_terminal_event or step.explicit_terminal
                protocol_complete = protocol_complete or step.complete
                if step.complete and not process.stdin.is_closing():
                    process.stdin.close()
                if stopped_at_result:
                    break
            if stdin_pending:
                await stdin_task
            return_code = await process.wait()
            if stderr_pending:
                assert stderr_task is not None
                stderr = await stderr_task
            stderr = _meaningful_stderr(stderr)
            if host and remote_pid_file:
                remote_stopped = await asyncio.to_thread(
                    AgentProcessControl._confirm_remote_stopped, host, remote_pid_file
                )
                completion_stop_failed = not remote_stopped
                if not prompt_delivered and remote_stopped:
                    yield AgentEvent(event="remote_process_stop", text=remote_pid_file)
            if not prompt_delivered and not (
                control is not None and control.pause_requested.is_set()
            ):
                if completion_stop_failed:
                    yield AgentEvent(
                        event="error",
                        text="RCP could not confirm that the remote provider process stopped. Runtime fallback is blocked.",
                    )
                    return
                detail = (
                    pre_prompt_error
                    or stderr
                    or (
                        _exit_reason(provider, return_code, host)
                        if return_code
                        else f"{provider} closed its provider runtime before accepting the turn."
                    )
                )
                raise _PrePromptRuntimeFailure(detail)
            paused = control is not None and control.pause_requested.is_set()
            turn_failed = bool(
                completion_stop_failed
                or provider_failed
                or (return_code and not stopped_at_result)
                or (turn.requires_protocol_completion and not protocol_complete)
            )
            # A provider that succeeded can still have dropped part of the
            # launch. Its stderr says so, but stderr itself is the vendor's
            # channel; the profile turns it into one sentence RCP owns. Only a
            # turn that finished gets the note: a failed or paused turn already
            # tells the human why it stopped, and saying how it ran would
            # compete with that rather than add to it.
            degradation = (
                None
                if paused or turn_failed
                else profile.launch_degradation(stderr, requested_reasoning=reasoning)
            )
            yield AgentEvent(
                event="provider_exit",
                text=json.dumps(
                    {
                        "return_code": return_code,
                        "event_counts": event_counts,
                        "explicit_terminal_event": explicit_terminal_event,
                        **(
                            {"remote_process_stopped": remote_stopped}
                            if host and remote_pid_file
                            else {}
                        ),
                        **({"stopped_at_result": True} if stopped_at_result else {}),
                        **({"degradation": degradation} if degradation else {}),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            if paused:
                yield AgentEvent(event="paused", text="Provider process paused.")
            elif completion_stop_failed:
                yield AgentEvent(
                    event="error",
                    text="RCP could not confirm that the remote provider process stopped. Recovery is blocked until its process state can be verified.",
                )
            elif provider_failed:
                # The first error already includes stderr. A forced stop must
                # not invent a second failure from our own termination signal.
                self.invalidate_readiness(provider, host=host, binary=binary)
            elif return_code and not stopped_at_result:
                self.invalidate_readiness(provider, host=host, binary=binary)
                detail = stderr or _exit_reason(provider, return_code, host)
                yield AgentEvent(event="error", text=detail)
            elif turn.requires_protocol_completion and not protocol_complete:
                self.invalidate_readiness(provider, host=host, binary=binary)
                yield AgentEvent(
                    event="error",
                    text=f"{provider} closed its provider protocol before the turn completed.",
                )
            else:
                yield AgentEvent(event="done")
        finally:
            credential_hold.release()
            steering.close()

            async def cleanup() -> None:
                try:
                    if host and remote_pid_file and remote_stopped is not True:
                        await asyncio.to_thread(
                            AgentProcessControl._confirm_remote_stopped, host, remote_pid_file
                        )
                finally:
                    await _cleanup_provider_process(
                        process,
                        stdin_task=stdin_task,
                        stdout_task=stdout_task,
                        stdout_lines=stdout_lines,
                        stderr_task=stderr_task,
                    )

            cleanup_task = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise
            finally:
                if control is not None:
                    control.detach(process)

    @staticmethod
    def _command(
        provider: str,
        prompt: str,
        *,
        binary: str | None = None,
        cwd: Path,
        model: str | None,
        reasoning: str | None,
        session_id: str | None,
        read_dirs: list[Path],
        write_dirs: list[Path] | None = None,
        write_scope: ProjectWriteScope | None = None,
        capability: AgentCapability,
        provider_version: str | None = None,
    ) -> list[str]:
        return profile_for(provider).command(
            prompt,
            binary=binary or provider,
            cwd=cwd,
            model=model,
            reasoning=reasoning,
            session_id=session_id,
            read_dirs=read_dirs,
            write_dirs=write_dirs or [],
            write_scope=write_scope,
            capability=capability,
            provider_version=provider_version,
        )

    @staticmethod
    def _probe(
        host: str,
        command: list[str],
        *,
        login_shell: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        arguments = command
        if host:
            remote = (
                AgentLauncher._remote_login_command(command) if login_shell else shlex.join(command)
            )
            arguments = ssh_arguments(host, remote)
        try:
            return subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                input="",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(arguments, 255, "", str(exc))

    @staticmethod
    def _remote_login_command(
        command: list[str],
        *,
        pid_file: str | None = None,
        cwd: Path | None = None,
    ) -> str:
        # Provider CLIs are commonly installed by nvm or a shell installer and
        # therefore exist only on the user's login-shell PATH. Interactive mode
        # is intentional here; state transport commands do not use this wrapper.
        command_payload = shlex.join(command)
        if pid_file:
            # `exec` must apply to the provider, not to the `cd` shell builtin.
            # `exec cd <cwd> && provider` exits successfully after changing the
            # directory and silently never launches the provider.
            provider = f"exec {command_payload}"
            if cwd is not None:
                provider = f"cd {shlex.quote(str(cwd))} && {provider}"
            child = f"echo $$ > {shlex.quote(pid_file)}; {provider}"
            # Interactive bash can make setsid a process-group leader, forcing
            # setsid to fork. Wait for that child so SSH owns the provider's
            # lifetime and exit status rather than the short-lived wrapper.
            payload = shlex.join(["setsid", "--wait", "sh", "-c", child])
        elif cwd is not None:
            # asyncio's cwd= only reaches the local ssh client, so a remote run
            # would otherwise start in $HOME. Codex is additionally pinned by
            # --cd; Claude has no equivalent flag and depends on this entirely.
            payload = f"cd {shlex.quote(str(cwd))} && {command_payload}"
        else:
            payload = command_payload
        return shlex.join(["bash", "-lic", payload])


# `_remote_login_command` runs the provider under `bash -lic`, and interactive
# bash on a non-tty always writes these two lines to stderr. They are emitted by
# every remote run, including successful ones, so treating them as a failure
# reason replaces the real cause with noise: a connection dropped mid-run was
# reported to the human as "cannot set terminal process group" (found running
# S14 on 2026-07-30).
_SIGNALS = {item.value for item in signal.Signals}

_BASH_TTY_NOISE = (
    "cannot set terminal process group",
    "no job control in this shell",
)


def _meaningful_stderr(stderr: str) -> str:
    kept = [
        line
        for line in stderr.strip().splitlines()
        if not any(noise in line for noise in _BASH_TTY_NOISE)
    ]
    return "\n".join(kept).strip()


def _exit_reason(provider: str, return_code: int, host: str) -> str:
    """What to say when the provider died without explaining itself."""
    where = f" on {host}" if host else ""
    if host and return_code == 255:
        # 255 is ssh's own "the connection failed or was lost" exit code.
        return f"The connection to {host} was lost before {provider} finished."
    if return_code < 0:
        # asyncio reports a signalled child as the negated signal number. For a
        # remote run the signalled process is the ssh client, so the run ended
        # with the link, not with anything the provider decided.
        name = signal.Signals(-return_code).name if -return_code in _SIGNALS else str(-return_code)
        if host:
            return f"The connection to {host} ended ({name}) before {provider} finished."
        return f"{provider} was stopped by {name}."
    return f"{provider} exited {return_code}{where}."


async def _write_stdin(stream, data: bytes) -> None:
    try:
        stream.write(data)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass


async def _feed_stdin(stream, data: bytes, *, close: bool = True) -> None:
    await _write_stdin(stream, data)
    if close:
        stream.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await stream.wait_closed()


async def _cleanup_provider_process(
    process: asyncio.subprocess.Process,
    *,
    stdin_task: asyncio.Task[None] | None,
    stdout_task: asyncio.Task[tuple[bytes | None, int]] | None,
    stdout_lines,
    stderr_task: asyncio.Task[str] | None,
) -> None:
    if process.stdin is not None:
        process.stdin.close()
    if stdin_task is not None and not stdin_task.done():
        stdin_task.cancel()

    stdout_drain_task = (
        asyncio.create_task(_drain_remaining_lines(stdout_task, stdout_lines))
        if stdout_lines is not None
        else (
            asyncio.create_task(_drain_stream(process.stdout))
            if process.stdout is not None
            else None
        )
    )
    stderr_drain_task = (
        asyncio.create_task(_drain_stream(process.stderr))
        if stderr_task is None and process.stderr is not None
        else None
    )

    tasks = [
        task
        for task in (stdin_task, stdout_task, stdout_drain_task, stderr_task, stderr_drain_task)
        if task is not None
    ]
    try:
        await AgentProcessControl._terminate(process)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if process.stdin is not None:
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()


async def _drain_remaining_lines(current_task, lines) -> None:
    if current_task is not None:
        try:
            await current_task
        except (StopAsyncIteration, asyncio.CancelledError):
            return
        except Exception:
            return
    try:
        async for _ in lines:
            pass
    except Exception:
        pass


async def _drain_stream(stream) -> None:
    try:
        while await stream.read(AgentLauncher._STREAM_LIMIT):
            pass
    except Exception:
        pass


async def _read_bounded_lines(
    stream,
    *,
    max_line_bytes: int,
    chunk_bytes: int,
) -> AsyncIterator[tuple[bytes | None, int]]:
    pending = bytearray()
    discarded = 0
    while True:
        chunk = await stream.read(chunk_bytes)
        if not chunk:
            break
        pieces = chunk.split(b"\n")
        for index, piece in enumerate(pieces):
            terminated = index < len(pieces) - 1
            if discarded:
                discarded += len(piece)
            elif len(pending) + len(piece) > max_line_bytes:
                discarded = len(pending) + len(piece)
                pending.clear()
            else:
                pending.extend(piece)
            if terminated:
                if discarded:
                    yield None, discarded
                else:
                    yield bytes(pending).rstrip(b"\r"), 0
                pending.clear()
                discarded = 0
    if discarded:
        yield None, discarded
    elif pending:
        yield bytes(pending).rstrip(b"\r"), 0


async def _read_bounded_text(
    stream,
    *,
    max_bytes: int,
    chunk_bytes: int,
) -> str:
    captured = bytearray()
    total = 0
    while True:
        chunk = await stream.read(chunk_bytes)
        if not chunk:
            break
        total += len(chunk)
        remaining = max(0, max_bytes - len(captured))
        if remaining:
            captured.extend(chunk[:remaining])
    text = captured.decode("utf-8", errors="replace")
    if total > len(captured):
        text += f"\n[stderr truncated; {total - len(captured)} bytes omitted]"
    return text
