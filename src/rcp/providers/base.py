"""What every provider profile shares: the profile base, runtimes, and usage."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from rcp.agents.write_scope import ProjectWriteScope
    from rcp.provider_auth import ProviderAuthentication
    from rcp.providers.session_format import SessionFormat
    from rcp.providers.turn_fence import TurnFence
AgentCapability = Literal[
    "discuss",
    "work_auto",
    "orchestrate",
    "scratch_patch",
    "paper_readonly",
]


class ModelChoice(BaseModel):
    """One model a provider accepts, with the reasoning efforts it supports."""

    id: str
    label: str
    reasoning: list[str] = []
    default_reasoning: str = ""


class ProviderRuntimeChoice(BaseModel):
    """One manifest-selectable way to talk to a provider CLI."""

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)


class ProviderSkill(BaseModel):
    """One user-invocable skill reported by a provider CLI."""

    name: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str
    scope: str | None = None
    path: str | None = None
    enabled: bool = True


class ProviderSkillProbe(BaseModel):
    """The exact provider-owned command and wire protocol used for refresh."""

    command: list[str] = Field(min_length=1)
    protocol: Literal["jsonrpc", "jsonl"]
    messages: tuple[dict[str, object], ...] = ()


class ProviderSkillReference(BaseModel):
    """Immutable per-turn receipt for one provider-native skill invocation."""

    provider: str = Field(min_length=1)
    machine: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    inventory_hash: str = Field(min_length=1)
    name: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str
    stale: bool = False


class ProviderUsage(BaseModel):
    """Provider-normalized usage at one accounting boundary.

    `processed_input_tokens` and `generated_tokens` are the shared accounting
    fields. The reported fields and `provider_fields` preserve what the CLI
    actually emitted, because providers do not use identical cache semantics.
    """

    provider_profile: str
    provider_event_type: str
    dedupe_key: str
    processed_input_tokens: int = Field(ge=0)
    generated_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_write_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    reported_input_tokens: int | None = Field(default=None, ge=0)
    reported_output_tokens: int | None = Field(default=None, ge=0)
    reported_total_tokens: int | None = Field(default=None, ge=0)
    provider_fields: dict[str, object] = Field(default_factory=dict)


@dataclass(frozen=True)
class ProviderStreamEvent:
    event: Literal["session", "message", "answer", "error", "raw"]
    text: str = ""
    session_id: str | None = None
    usage: ProviderUsage | None = None


@dataclass(frozen=True)
class ProviderTurnRequest:
    """One provider-owned runtime invocation after RCP has pinned its policy."""

    prompt: str
    binary: str
    cwd: Path
    model: str | None
    reasoning: str | None
    session_id: str | None
    read_dirs: list[Path]
    write_dirs: list[Path]
    write_scope: ProjectWriteScope | None
    capability: AgentCapability
    provider_version: str | None
    legacy_command: list[str] | None = None


@dataclass(frozen=True)
class ProviderSteerReceipt:
    status: Literal["delivered", "refused", "unknown"]
    reason: str | None = None


@dataclass(frozen=True)
class ProviderSteeringState:
    can_steer: bool
    reason: str | None = None
    turn_id: str | None = None


@dataclass(frozen=True)
class ProviderRuntimeStep:
    """One provider protocol input line normalized for the shared launcher."""

    outgoing: tuple[bytes, ...] = ()
    events: tuple[ProviderStreamEvent, ...] = ()
    complete: bool = False
    explicit_terminal: bool = False
    delivers_prompt: bool = False
    steer_receipts: tuple[tuple[str, ProviderSteerReceipt], ...] = ()
    stop_process: bool = False


class ProviderTurn:
    """Stateful wire conversation for one fresh provider subprocess."""

    command: list[str]
    #: Variables this one process starts with, on top of the provider's own
    #: environment. For a CLI that reads per-launch policy from its environment.
    environment: dict[str, str] = {}
    close_input_after_initial: bool = True
    requires_protocol_completion: bool = False
    initial_input_delivers_prompt: bool = True
    last_error: str = ""

    def initial_input(self) -> bytes:
        raise NotImplementedError

    def receive_line(self, line: str) -> ProviderRuntimeStep:
        raise NotImplementedError

    def steering_state(self) -> ProviderSteeringState:
        return ProviderSteeringState(False, "This runtime does not support live steering.")

    def render_steer(self, expected_turn_id: str, message_id: str, text: str) -> bytes:
        raise ValueError(self.steering_state().reason)

    def adopt_recorded_inputs(
        self, message_ids: list[str], steer_requests: dict[str, object]
    ) -> None:
        """Take on the input identities a recorded pass actually sent.

        Replaying a journal builds a fresh turn, which would otherwise mint its
        own ids and then fail to recognise its own recorded traffic. The host
        wrote these down for exactly this: stdin is the live control channel and
        is never persisted, so the identities travel instead of the bytes.

        A runtime that does not key anything off its inputs has nothing to take
        on, which is why this does nothing by default.
        """


class ProviderRuntime:
    """Provider-owned command and wire protocol hidden behind one RCP boundary."""

    id: str
    #: `inject` puts the message into the turn already running and the provider
    #: confirms it. `queue` names RCP's continuation contract, not a placement
    #: promise: the process may run past its first result while accepted
    #: follow-ups are outstanding. Claude decides placement by timing — a
    #: message that lands during a tool call joins the running turn and is
    #: attributed to its result, otherwise it runs as the next turn.
    steering_behavior: Literal["unsupported", "inject", "queue"] = "unsupported"

    @property
    def supports_steering(self) -> bool:
        return self.steering_behavior != "unsupported"

    @property
    def steer_action_label(self) -> str | None:
        if self.steering_behavior == "queue":
            return "Send to the running turn"
        if self.steering_behavior == "inject":
            return "Steer running turn"
        return None

    def turn(self, request: ProviderTurnRequest) -> ProviderTurn:
        raise NotImplementedError


class _JsonlProviderTurn(ProviderTurn):
    requires_protocol_completion = True

    def __init__(
        self,
        profile: ProviderProfile,
        request: ProviderTurnRequest,
    ) -> None:
        self._profile = profile
        self._prompt = request.prompt
        self.command = request.legacy_command or profile.command(
            request.prompt,
            binary=request.binary,
            cwd=request.cwd,
            model=request.model,
            reasoning=request.reasoning,
            session_id=request.session_id,
            read_dirs=request.read_dirs,
            write_dirs=request.write_dirs,
            write_scope=request.write_scope,
            capability=request.capability,
            provider_version=request.provider_version,
        )

    def initial_input(self) -> bytes:
        return self._prompt.encode("utf-8")

    def steering_state(self) -> ProviderSteeringState:
        return ProviderSteeringState(
            False,
            f"{self._profile.label} {self._profile.default_runtime} does not support live steering.",
        )

    def receive_line(self, line: str) -> ProviderRuntimeStep:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            event = ProviderStreamEvent(event="raw", text=line)
            return ProviderRuntimeStep(events=(event,))
        event = self._profile.decode_event(value, line)
        if isinstance(value, dict) and value.get("type") == "error":
            self.last_error = event.text
        terminal = event.event == "error" or (
            isinstance(value, dict)
            and value.get("type") in {"turn.completed", "turn.failed", "result"}
        )
        return ProviderRuntimeStep(events=(event,), complete=terminal, explicit_terminal=terminal)


class _JsonlProviderRuntime(ProviderRuntime):
    def __init__(self, runtime_id: str, profile: ProviderProfile) -> None:
        self.id = runtime_id
        self._profile = profile

    def turn(self, request: ProviderTurnRequest) -> ProviderTurn:
        return _JsonlProviderTurn(self._profile, request)


@dataclass(frozen=True)
class ProviderNativeUpdate:
    """How the provider's own supported update runs on a team server.

    Exactly one route is set: `self_update_args` runs the installed executable
    with those arguments, and `installer_url` downloads and runs the vendor's
    installer script with `installer_env`.
    """

    self_update_args: tuple[str, ...] | None = None
    installer_url: str | None = None
    installer_env: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.self_update_args is None) == (self.installer_url is None):
            raise ValueError("a native update names exactly one route")

    @property
    def requires_installed(self) -> bool:
        return self.self_update_args is not None


class ProviderProfile:
    """Everything RCP knows about one agent CLI."""

    @property
    def authentication(self) -> ProviderAuthentication:
        from rcp.provider_auth import ProviderAuthentication

        return ProviderAuthentication()

    id: str
    label: str
    #: The CLI version `declared` was last verified against. Empty when the
    #: profile probes the CLI instead of declaring, which cannot go stale.
    declared_against: str = ""
    #: Models known without asking the CLI. Ignored when `catalog_command`
    #: returns a command that answers.
    declared: tuple[ModelChoice, ...] = ()
    #: The manifest fields naming where this CLI keeps its session files; None
    #: when RCP does not index its sessions.
    local_session_roots_field: str | None = None
    remote_session_roots_field: str | None = None
    usage_profile: str = "unknown.v1"
    legacy_runtime_id: str
    default_runtime: str
    runtime_aliases: dict[str, str]
    runtime_choices: tuple[ProviderRuntimeChoice, ...]
    work_like_minimum_version: tuple[int, int, int] | None = None
    native_update: ProviderNativeUpdate
    #: Native executable locations relative to the execution account's home.
    install_paths: tuple[str, ...] = ()
    #: How this provider's native session files look; see `remote.py`. None
    #: when RCP does not index its sessions.
    session_format: SessionFormat | None = None
    #: The execution-host turn fence for each of this provider's runtime ids.
    turn_fences: dict[str, type[TurnFence]]

    def session_roots(self, sources: object, *, remote: bool) -> list[str]:
        """Return this provider's configured native-session roots.

        Keeping the mapping here makes native handoff discovery follow the same
        registry boundary as launch and stream decoding. Adding a provider does
        not require another provider-name branch in the retry assembler.
        """
        field = self.remote_session_roots_field if remote else self.local_session_roots_field
        if field is None:
            return []
        roots = getattr(sources, field, None)
        if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
            raise ValueError(f"Provider {self.id!r} has no configured session roots")
        return roots

    def auth_command(self, binary: str) -> list[str]:
        """The argv that reports whether this CLI is logged in."""
        raise NotImplementedError

    def login_command(self, binary: str) -> list[str]:
        """The provider-native interactive command an operator runs directly."""
        raise NotImplementedError

    def validate_readiness_version(
        self,
        actual: str | None,
        *,
        capability: AgentCapability,
    ) -> None:
        """Apply the same version floor used by a real launch of this capability."""

        if capability in {"work_auto", "orchestrate"}:
            minimum = self.work_like_minimum_version
            if minimum is not None:
                _require_provider_version(
                    provider=self.label,
                    actual=actual,
                    minimum=minimum,
                )

    def login_probe_command(self, binary: str) -> list[str] | None:
        """One real authenticated request; status output is not proof of sign-in."""
        return None

    def work_like_probe_command(self, binary: str) -> list[str] | None:
        """Zero-model-call startup check; the caller must supply empty stdin."""
        return None

    def is_authenticated(self, result: subprocess.CompletedProcess[str]) -> bool:
        """Whether the CLI reports a stored credential.

        This is a presence check, not a liveness check. Every provider status
        command reads its own credential file and names the auth mode without
        contacting the provider, so a spent refresh token still reports success
        and only a real turn discovers the 401. A truthful result here means the
        launch is worth attempting, never that it will authenticate.
        """

        raise NotImplementedError

    def catalog_command(self, binary: str) -> list[str] | None:
        """The argv that enumerates models, or None when the CLI cannot."""
        return None

    def parse_catalog(self, stdout: str) -> list[ModelChoice]:
        return []

    def skill_probe(self, binary: str) -> ProviderSkillProbe:
        """Return the zero-turn command that enumerates this CLI's loaded skills."""

        raise NotImplementedError

    def parse_skills(self, payload: object) -> list[ProviderSkill]:
        """Normalize one successful provider-owned inventory response."""

        raise NotImplementedError

    def native_skill_token(self, name: str) -> str:
        """Provider-native spelling retained in the structured turn marker."""

        return f"/{name}"

    def models(self, catalog: subprocess.CompletedProcess[str] | None) -> list[ModelChoice]:
        """The models to offer, preferring a live catalog over declared ones."""
        if catalog is not None and catalog.returncode == 0:
            try:
                probed = self.parse_catalog(catalog.stdout)
            except (ValueError, KeyError, TypeError):
                probed = []
            if probed:
                return probed
        return list(self.declared)

    def runtime(self, runtime_id: str) -> ProviderRuntime:
        if runtime_id == self.legacy_runtime_id:
            return _JsonlProviderRuntime(runtime_id, self)
        raise ValueError(f"Provider {self.id!r} does not support runtime {runtime_id!r}.")

    def configured_runtime(self, configured: str | None) -> str:
        """Normalize a manifest value to its provider-owned public name."""

        value = (configured or "").strip()
        if not value:
            return self.default_runtime
        runtime_id = self.runtime_aliases.get(value, value)
        self.runtime(runtime_id)
        for choice in self.runtime_choices:
            if self.runtime_aliases[choice.id] == runtime_id:
                return choice.id
        raise ValueError(f"Provider {self.id!r} runtime {value!r} is not manifest-selectable.")

    def configured_runtime_id(self, configured: str | None) -> str:
        public_name = self.configured_runtime(configured)
        return self.runtime_aliases[public_name]

    def runtime_label(self, runtime_id: str) -> str:
        """Name a durable runtime id for a surface reporting what actually ran."""

        for choice in self.runtime_choices:
            if self.runtime_aliases[choice.id] == runtime_id:
                return choice.label
        # A record can name a runtime this build no longer offers. The stored id
        # is then the only honest answer.
        return runtime_id

    def runtime_candidates(self, configured: str | None) -> tuple[ProviderRuntime, ...]:
        """Preferred runtime followed by its safe pre-prompt fallback, if any."""

        preferred = self.runtime(self.configured_runtime_id(configured))
        if preferred.id == self.legacy_runtime_id:
            return (preferred,)
        return (preferred, self.runtime(self.legacy_runtime_id))

    def command(
        self,
        prompt: str,
        *,
        binary: str,
        cwd: Path,
        model: str | None,
        reasoning: str | None,
        session_id: str | None,
        read_dirs: list[Path],
        write_dirs: list[Path],
        write_scope: ProjectWriteScope | None,
        capability: AgentCapability,
        provider_version: str | None,
    ) -> list[str]:
        """The argv that runs one turn. `prompt` arrives on stdin."""
        raise NotImplementedError

    def project_write_enforcement_mode(self) -> str:
        raise ValueError(f"Provider {self.id!r} has no project write enforcement mode")

    def launch_degradation(self, stderr: str, *, requested_reasoning: str | None) -> str | None:
        """One RCP-authored sentence when the CLI ignored part of a launch.

        A provider that rejects an unusable launch outright has nothing to add
        here; that failure already reaches the human on the error path. This is
        for a CLI that accepts the launch, silently drops part of it, and then
        succeeds anyway. The sentence is composed from what RCP asked for, never
        from the provider's own diagnostic text, which is unbounded and unowned.
        """

        del stderr, requested_reasoning
        return None

    def probe_failure_evidence(self, result: subprocess.CompletedProcess[str]) -> str:
        """Diagnostics to classify; successful output is provider data, not failure evidence."""

        return result.stderr + (result.stdout if result.returncode else "")

    def credential_failure(self, stderr: str) -> bool:
        """Whether this CLI's diagnostic says its own login is no longer valid.

        A revoked or expired login fails every attempt the same way, so RCP has
        to tell it apart from a dropped connection: one is worth retrying and
        the other is not. The provider owns these signatures because only it
        knows what its CLI prints. A profile that has not had a real revoked
        login observed returns False rather than guessing, because a wrong
        match here would withdraw Retry from a failure Retry would have fixed.
        """

        del stderr
        return False

    def decode_event(self, value: object, raw: str) -> ProviderStreamEvent:
        return ProviderStreamEvent(event="raw", text=raw)

    def decode_usage(self, value: dict[str, object], raw: str) -> ProviderUsage | None:
        return None


def _provider_error_text(value: dict[str, object]) -> str:
    for candidate in (value.get("result"), value.get("error"), value.get("message")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, dict):
            message = candidate.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    subtype = value.get("subtype")
    return subtype.strip() if isinstance(subtype, str) else ""


def _usage_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _optional_usage_int(value: object) -> int | None:
    if value is None:
        return None
    return _usage_int(value)


def _usage_dedupe_key(value: dict[str, object], raw: str, *fields: str) -> str:
    for field in fields:
        candidate = value.get(field)
        if isinstance(candidate, str) and candidate:
            return candidate
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require_project_write_scope(
    scope: ProjectWriteScope | None,
    *,
    capability: AgentCapability,
    write_dirs: list[Path],
) -> ProjectWriteScope:
    if scope is None:
        raise ValueError(f"{capability} launch requires a resolved project write scope")
    if scope.capability != capability:
        raise ValueError("project write scope capability does not match the provider launch")
    supplied = list(dict.fromkeys(str(item) for item in write_dirs))
    if supplied != scope.repository_roots:
        raise ValueError("provider write directories do not match the resolved project scope")
    return scope


def _require_provider_version(
    *,
    provider: str,
    actual: str | None,
    minimum: tuple[int, int, int],
) -> None:
    parsed = tuple(int(item) for item in re.findall(r"\d+", actual or "")[:3])
    if len(parsed) != 3 or parsed < minimum:
        required = ".".join(str(item) for item in minimum)
        reported = actual or "unknown"
        raise ValueError(
            f"{provider} {reported} cannot enforce the declared project write roots; "
            f"RCP requires {required} or newer"
        )
