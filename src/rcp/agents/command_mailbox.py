from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import math
import random
import re
import secrets
import shlex
import subprocess
import threading
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import ValidationError

from rcp.agents.command_protocol import (
    CommandCredential,
    CommandRequest,
    CommandResponse,
    command_authentication_payload,
    command_requires_idempotency_key,
    request_identity_is_well_formed,
    staged_command_broker_source,
    staged_command_client_source,
    validate_command_request,
)
from rcp.agents.invocation_broker import ProviderInvocationGate, isolated_python_argv
from rcp.agents.staged_command_client import COMMAND_MAILBOX_MAX_REQUEST_BYTES
from rcp.limits import (
    COMMAND_BROKER_RESPONSE_GRACE_SECONDS,
    COMMAND_CLIENT_WAIT_SECONDS,
    COMMAND_MAILBOX_HANDLER_MAX_RETRIES,
    COMMAND_MAILBOX_POLL_SECONDS,
    COMMAND_MAILBOX_REMOTE_POLL_SECONDS,
    COMMAND_MAILBOX_RETRY_INITIAL_SECONDS,
    COMMAND_MAILBOX_RETRY_JITTER,
    COMMAND_MAILBOX_RETRY_MAX_SECONDS,
    COMMAND_MAILBOX_STOP_MAX_FAILED_ATTEMPTS,
    COMMAND_MAILBOX_STOP_POLL_SECONDS,
    COMMAND_MAILBOX_TIMEOUT_SECONDS,
    COMMAND_REJECTION_NOTICE_MAX_BYTES,
    COMMAND_REJECTION_NOTICE_MAX_COUNT,
)
from rcp.transport import RemoteRunStage, RunStageMailbox, StateUnavailable, StateUnreachable
from rcp.transport.run_stage import RemoteStageTransportFailure

_MAILBOX_ID = re.compile(r"^[a-f0-9]{32}$")
_CREDENTIAL = re.compile(r"^[a-f0-9]{64}$")
_REQUEST_FILE = re.compile(
    r"^rcp-command-(?P<mailbox_id>[a-f0-9]{32})-"
    r"(?P<request_id>[a-f0-9]{32})\.request\.json$"
)
# A broker refusal that never became a request, signed so an agent cannot forge one.
_REJECTION_FILE = re.compile(
    r"^rcp-command-(?P<mailbox_id>[a-f0-9]{32})-(?P<notice_id>[a-f0-9]{32})\.rejected\.json$"
)
_COMMAND_STATE_PREFIXES = ("rcp-command-", ".rcp-command-", ".rcp-mailbox-")
# The broker signs refusal notices under this prefix, never a request.
REJECTION_NOTICE_DOMAIN = b"rcp-rejection-notice\n"
_REJECTION_NOTICE_FIELDS = frozenset(
    {"version", "mailbox_id", "notice_id", "status", "message", "credential"}
)


@dataclass(frozen=True, slots=True)
class CommandTurnIdentity:
    """The task turn, and optional episode, an in-memory credential represents."""

    episode_id: str | None
    task_id: str
    turn_id: str
    authority: Literal["validate_only", "broker"]

    def __post_init__(self) -> None:
        if self.authority not in {"validate_only", "broker"}:
            raise ValueError("command authority must be validate_only or broker")
        if self.episode_id is not None and self.authority != "broker":
            raise ValueError("episode command authority is broker-only")
        values = (("task id", self.task_id), ("turn id", self.turn_id))
        if self.episode_id is not None:
            values = (("episode id", self.episode_id), *values)
        for label, value in values:
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"command {label} must be a non-blank exact identifier")


@dataclass(slots=True)
class CommandTurnCredential:
    """A one-shot secret binding that cannot be reactivated after its serve loop."""

    identity: CommandTurnIdentity
    mailbox_id: str
    _token: str = field(repr=False)
    _state: Literal["issued", "active", "expired"] = field(default="issued", repr=False)

    @classmethod
    def issue(cls, identity: CommandTurnIdentity) -> CommandTurnCredential:
        return cls(identity=identity, mailbox_id=uuid.uuid4().hex, _token=secrets.token_hex(32))

    @property
    def token(self) -> str:
        if self._state == "expired":
            raise RuntimeError("command credential has expired")
        return self._token

    @property
    def expired(self) -> bool:
        return self._state == "expired"

    def document(self) -> CommandCredential:
        if self.identity.authority == "broker":
            raise RuntimeError("command authority is broker-only")
        return CommandCredential(mailbox_id=self.mailbox_id, token=self.token)

    def activate(self) -> None:
        if self._state != "issued":
            raise RuntimeError("command credential can serve exactly one turn")
        self._state = "active"

    def accepts(self, request: CommandRequest, document: str) -> bool:
        """Check one request against this turn's binding.

        ``document`` is the request exactly as it was written, because a turn
        broker signs those bytes rather than the model they validate into.
        """

        if self.identity.authority == "broker":
            expected = hmac.new(
                self._token.encode("ascii"),
                command_authentication_payload(document),
                hashlib.sha256,
            ).hexdigest()
        else:
            expected = self._token
        return (
            self._state == "active"
            and _MAILBOX_ID.fullmatch(request.mailbox_id) is not None
            and secrets.compare_digest(self.mailbox_id, request.mailbox_id)
            and secrets.compare_digest(expected, request.credential)
        )

    def accepts_rejection_notice(self, value: dict[str, object]) -> bool:
        """Check a shape-validated refusal notice against this turn's broker token."""

        credential = value.get("credential")
        if (
            self.identity.authority != "broker"
            or self._state != "active"
            or value.get("mailbox_id") != self.mailbox_id
            or not isinstance(credential, str)
            or _CREDENTIAL.fullmatch(credential) is None
        ):
            return False
        unsigned = {name: item for name, item in value.items() if name != "credential"}
        payload = json.dumps(
            unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        expected = hmac.new(
            self._token.encode("ascii"), REJECTION_NOTICE_DOMAIN + payload, hashlib.sha256
        ).hexdigest()
        return secrets.compare_digest(expected, credential)

    def expire(self) -> None:
        self._token = ""
        self._state = "expired"


@dataclass(frozen=True, slots=True)
class StagedCommandMailbox:
    mailbox: RunStageMailbox
    credential: CommandTurnCredential
    client_path: str
    credential_path: str | None
    invocation_gate: ProviderInvocationGate | None = None
    timeout_seconds: float = COMMAND_MAILBOX_TIMEOUT_SECONDS
    ask_wait_seconds: float = COMMAND_CLIENT_WAIT_SECONDS

    @property
    def workspace(self) -> str:
        return str(self.mailbox.workspace)

    def client_argv(self, *arguments: str, timeout_seconds: float | None = None) -> tuple[str, ...]:
        """Build an argv tuple for the separately staged agent-only executable."""

        timeout = (
            min(self.timeout_seconds, COMMAND_CLIENT_WAIT_SECONDS)
            if timeout_seconds is None
            else timeout_seconds
        )
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("command client timeout must be a positive finite number")
        authority = (
            self.invocation_gate.client_arguments()
            if self.invocation_gate is not None
            else ("--credential", self.credential_path or "")
        )
        return (
            *isolated_python_argv(self.client_path),
            *authority,
            "--timeout",
            f"{timeout:g}",
            "--ask-wait",
            f"{self.ask_wait_seconds:g}",
            "--workspace",
            self.workspace,
            *arguments,
        )

    def client_command(self, *arguments: str, timeout_seconds: float | None = None) -> str:
        return shlex.join(self.client_argv(*arguments, timeout_seconds=timeout_seconds))

    def cleanup(self) -> None:
        cleanup_command_mailbox(mailbox=self.mailbox, credential=self.credential)


CommandHandlerResult: TypeAlias = CommandResponse | Awaitable[CommandResponse]
CommandHandler: TypeAlias = Callable[[CommandRequest, CommandTurnIdentity], CommandHandlerResult]
# Called with (status, message) for each refusal the handler never saw.
CommandRejectionRecorder: TypeAlias = Callable[[str, str], None]


def stage_command_mailbox(
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    local_input_stage: Path | None = None,
    episode_id: str | None,
    task_id: str,
    turn_id: str,
    authority: Literal["validate_only", "broker"] | None = None,
    timeout_seconds: float = COMMAND_MAILBOX_TIMEOUT_SECONDS,
    ask_wait_seconds: float = COMMAND_CLIENT_WAIT_SECONDS,
) -> StagedCommandMailbox:
    """Clear a reusable stage and issue either broker or validate-only authority."""

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("command client timeout must be a positive finite number")
    if not math.isfinite(ask_wait_seconds) or ask_wait_seconds <= 0:
        raise ValueError("ask wait must be a positive finite number")
    if remote_stage is not None and local_input_stage is not None:
        raise ValueError("a remote command mailbox cannot use a local input stage")
    mailbox = RunStageMailbox.for_stage(local_stage=local_stage, remote_stage=remote_stage)
    input_mailbox = (
        RunStageMailbox.for_stage(local_stage=local_input_stage, remote_stage=None)
        if local_input_stage is not None
        else mailbox
    )
    prepare_command_mailbox(mailbox=mailbox)
    identity = CommandTurnIdentity(
        episode_id=episode_id,
        task_id=task_id,
        turn_id=turn_id,
        authority=(
            authority
            if authority is not None
            else ("broker" if episode_id is not None else "validate_only")
        ),
    )
    credential = CommandTurnCredential.issue(identity)
    credential_path: str | None = None
    invocation_gate: ProviderInvocationGate | None = None
    try:
        source = staged_command_client_source()
        source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        client_path = input_mailbox.stage_text_input(
            f"rcp-agent-client-{credential.mailbox_id}-{source_digest}.py",
            source,
        )
        if identity.authority == "validate_only":
            credential_name = f"rcp-command-{credential.mailbox_id}.credential.json"
            mailbox.write_text(
                credential_name,
                credential.document().model_dump_json(indent=2) + "\n",
            )
            credential_path = str(mailbox.workspace / credential_name)
        else:
            broker_source = staged_command_broker_source()
            broker_digest = hashlib.sha256(broker_source.encode("utf-8")).hexdigest()[:16]
            broker_path = input_mailbox.stage_text_input(
                f"rcp-command-broker-{credential.mailbox_id}-{broker_digest}.py",
                broker_source,
            )
            invocation_gate = ProviderInvocationGate(
                mailbox_id=credential.mailbox_id,
                broker_path=broker_path,
                client_path=client_path,
                # The broker resolves `~` on the execution host.
                socket_path=f"~/.rcp/sockets/rcp-command-{credential.mailbox_id}.sock",
                workspace=str(mailbox.workspace),
                response_timeout_seconds=timeout_seconds + COMMAND_BROKER_RESPONSE_GRACE_SECONDS,
                _token=credential.token,
            )
    except BaseException:
        with suppress(BaseException):
            cleanup_command_mailbox(mailbox=mailbox, credential=credential)
        raise
    return StagedCommandMailbox(
        mailbox=mailbox,
        credential=credential,
        client_path=client_path,
        credential_path=credential_path,
        invocation_gate=invocation_gate,
        timeout_seconds=timeout_seconds,
        ask_wait_seconds=ask_wait_seconds,
    )


def prepare_command_mailbox(*, mailbox: RunStageMailbox) -> None:
    """Clear every prior command request, response, credential, and interrupted temp file."""

    _clear_command_state(mailbox)


def cleanup_command_mailbox(
    *,
    mailbox: RunStageMailbox,
    credential: CommandTurnCredential | None = None,
) -> None:
    """Expire the credential and fail closed unless all command state is gone."""

    if credential is not None:
        credential.expire()
    _clear_command_state(mailbox)


class _MailboxInterrupted(Exception):
    pass


class _StoppedMailboxUnavailable(Exception):
    """A stopped turn exhausted its bounded response/closure delivery attempts."""


class _CredentialRefused(ValueError):
    pass


async def serve_command_mailbox(
    *,
    staged: StagedCommandMailbox,
    handler: CommandHandler,
    stop: asyncio.Event | threading.Event,
    poll_seconds: float | None = None,
    invocation_gate: ProviderInvocationGate | None = None,
    record_rejection: CommandRejectionRecorder | None = None,
    record_transport: CommandRejectionRecorder | None = None,
    responses: dict[str, CommandResponse] | None = None,
    checkpoint: Callable[[], None] | None = None,
    terminal: dict[str, str] | None = None,
    suspend: threading.Event | None = None,
) -> None:
    """Stop fences admission and drains; suspend preserves this turn for restart."""
    if poll_seconds is None:
        poll_seconds = (
            COMMAND_MAILBOX_REMOTE_POLL_SECONDS
            if staged.mailbox.remote_stage is not None
            else COMMAND_MAILBOX_POLL_SECONDS
        )
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("command mailbox poll interval must be a positive finite number")
    credential = staged.credential
    if credential.identity.authority == "broker":
        if invocation_gate is None or invocation_gate is not staged.invocation_gate:
            raise ValueError("broker command mailbox requires its exact provider invocation gate")
    elif invocation_gate is not None:
        raise ValueError("validate-only mailbox does not accept a provider invocation gate")
    credential.activate()
    seen: set[str] = set()
    answers = responses if responses is not None else {}
    recorded = 0
    outage = False

    def interrupted(*, drain: bool) -> bool:
        return bool(suspend is not None and suspend.is_set()) or (not drain and stop.is_set())

    async def pause(seconds: float, *, drain: bool = False) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while not interrupted(drain=drain):
            if drain and stop.is_set():
                deadline = min(
                    deadline,
                    asyncio.get_running_loop().time() + COMMAND_MAILBOX_RETRY_INITIAL_SECONDS,
                )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, COMMAND_MAILBOX_STOP_POLL_SECONDS))
        raise _MailboxInterrupted

    async def record(status: str, message: str) -> None:
        nonlocal recorded
        if record_rejection is None or recorded >= COMMAND_REJECTION_NOTICE_MAX_COUNT:
            return
        recorded += 1
        await asyncio.to_thread(record_rejection, status, message)

    async def transport(status: str, message: str) -> None:
        if record_transport is not None:
            await asyncio.to_thread(record_transport, status, message)

    async def retry(operation, *, drain: bool = False):
        nonlocal outage
        delay = COMMAND_MAILBOX_RETRY_INITIAL_SECONDS
        stopped_failures = 0
        while True:
            if interrupted(drain=drain):
                raise _MailboxInterrupted
            try:
                outcome = await operation()
            except (
                StateUnreachable,
                RemoteStageTransportFailure,
                TimeoutError,
                subprocess.TimeoutExpired,
            ) as exc:
                if not outage:
                    outage = True
                    await transport("outage", f"Command mailbox transport unavailable: {exc}")
                if stop.is_set():
                    stopped_failures += 1
                    if stopped_failures >= COMMAND_MAILBOX_STOP_MAX_FAILED_ATTEMPTS:
                        raise _StoppedMailboxUnavailable(
                            f"turn stopped; response delivery remains unavailable: {exc}"
                        ) from exc
                await pause(_jittered(delay), drain=drain)
                delay = min(COMMAND_MAILBOX_RETRY_MAX_SECONDS, delay * 2)
                continue
            if outage:
                outage = False
                await transport("recovered", "Command mailbox transport recovered")
            return outcome

    async def handle(request: CommandRequest) -> tuple[CommandResponse, bool]:
        # Only the handler's own no-verdict SSH failure is a blip, and only a turn
        # that checkpoints its answers may re-run a handler; the rest answer now.
        nonlocal outage
        retries = COMMAND_MAILBOX_HANDLER_MAX_RETRIES if checkpoint is not None else 0
        delay = COMMAND_MAILBOX_RETRY_INITIAL_SECONDS
        while True:
            try:
                outcome = await _handle_request(request, staged, handler)
            except (StateUnreachable, RemoteStageTransportFailure) as exc:
                if retries == 0:
                    return _error_response(
                        request.request_id, "unavailable", "Command handler unavailable", exc
                    ), False
                if not outage:
                    outage = True
                    await transport("outage", f"Command handler transport unavailable: {exc}")
            else:
                if outage:
                    outage = False
                    await transport("recovered", "Command mailbox transport recovered")
                return outcome
            retries -= 1
            await pause(_jittered(delay), drain=True)
            delay = min(COMMAND_MAILBOX_RETRY_MAX_SECONDS, delay * 2)

    reason = (terminal or {}).get(
        "reason", "Command mailbox permanently closed: turn stopped or entered settlement."
    )
    # A turn that simply ends closes its mailbox quietly; only a failure warns,
    # and a restored closure already warned when it happened.
    failed = False
    try:
        while not interrupted(drain=False) and not (terminal and terminal.get("reason")):
            names = await retry(lambda: asyncio.to_thread(staged.mailbox.entry_names))
            for name in sorted(
                name
                for name in names
                if name not in seen and _rejection_notice_id(name, credential.mailbox_id)
            ):
                notice = await retry(
                    lambda name=name: asyncio.to_thread(_read_rejection_notice, staged, name)
                )
                if notice is not None:
                    await record(*notice)
                seen.add(name)
            requests = sorted(
                name
                for name in names
                if _request_identity_from_name(name, credential.mailbox_id) is not None
                and name not in seen
            )
            for name in requests:
                if interrupted(drain=False):
                    raise _MailboxInterrupted
                request_id = _request_identity_from_name(name, credential.mailbox_id)
                assert request_id is not None
                refusal = None
                if name not in answers:
                    try:
                        request = await retry(
                            lambda name=name, request_id=request_id: _read_request(
                                name, request_id, staged
                            )
                        )
                    except _CredentialRefused as exc:
                        refusal = exc
                        if terminal is not None:
                            terminal["reason"] = f"Command mailbox permanently closed: {exc}"
                        response = _error_response(
                            request_id, "invalid", "Command request invalid", exc
                        )
                        handled = False
                    except (FileNotFoundError, UnicodeError, ValueError, ValidationError) as exc:
                        response = _error_response(
                            request_id, "invalid", "Command request invalid", exc
                        )
                        handled = False
                    else:
                        response, handled = await handle(request)
                    answers[name] = response
                    if checkpoint is not None:
                        await asyncio.to_thread(checkpoint)
                    if not handled:
                        await record(response.status, response.message or "")
                response_name = name.removesuffix(".request.json") + ".response.json"
                await retry(
                    lambda response_name=response_name, name=name: asyncio.to_thread(
                        staged.mailbox.write_text,
                        response_name,
                        answers[name].model_dump_json(indent=2) + "\n",
                    ),
                    drain=True,
                )
                seen.add(name)
                if refusal is not None:
                    raise refusal
            await pause(poll_seconds)
    except _MailboxInterrupted:
        pass
    except Exception as exc:
        failed = True
        reason = f"Command mailbox permanently closed: {' '.join(str(exc).split())}"[:2_000]
    finally:
        if terminal and terminal.get("reason") and terminal["reason"] != reason:
            failed = True
            reason = terminal["reason"]
        if suspend is None or not suspend.is_set():
            if terminal is not None:
                terminal["reason"] = reason
                if checkpoint is not None:
                    await asyncio.to_thread(checkpoint)
            if failed:
                await transport("closed", reason)
            try:
                await retry(
                    lambda: asyncio.to_thread(
                        staged.mailbox.write_text,
                        f"rcp-command-{credential.mailbox_id}.closed.json",
                        json.dumps(
                            {"version": 1, "mailbox_id": credential.mailbox_id, "message": reason}
                        )
                        + "\n",
                    ),
                    drain=True,
                )
            except (_MailboxInterrupted, _StoppedMailboxUnavailable):
                # Terminal state and completed responses were checkpointed before
                # publication. An unreachable stopped host cannot receive a marker.
                pass
            finally:
                if suspend is None or not suspend.is_set():
                    credential.expire()


async def _read_request(name: str, request_id: str, staged: StagedCommandMailbox) -> CommandRequest:
    content = await asyncio.to_thread(
        staged.mailbox.read_text,
        name,
        max_bytes=COMMAND_MAILBOX_MAX_REQUEST_BYTES,
    )
    request = validate_command_request(content)
    if not request_identity_is_well_formed(request):
        raise ValueError("command request identity is malformed")
    if request.request_id != request_id or request.mailbox_id != staged.credential.mailbox_id:
        raise ValueError("command request identity does not match its file name")
    if not staged.credential.accepts(request, content):
        raise _CredentialRefused("command credential is invalid or expired")
    if (
        command_requires_idempotency_key(request.verb)
        and staged.credential.identity.authority != "broker"
    ):
        raise ValueError(f"{request.verb} requires broker authority")
    return request


async def _handle_request(
    request: CommandRequest,
    staged: StagedCommandMailbox,
    handler: CommandHandler,
) -> tuple[CommandResponse, bool]:
    try:
        outcome = handler(request, staged.credential.identity)
        response = await outcome if inspect.isawaitable(outcome) else outcome
        if not isinstance(response, CommandResponse):
            raise TypeError("command handler returned an unsupported response")
        if response.request_id != request.request_id:
            raise ValueError("command handler returned a mismatched request identity")
        return response, True
    except (StateUnreachable, RemoteStageTransportFailure):
        raise
    except Exception as exc:
        return _error_response(
            request.request_id, "unavailable", "Command handler unavailable", exc
        ), False


def _jittered(delay: float) -> float:
    return min(
        COMMAND_MAILBOX_RETRY_MAX_SECONDS,
        delay * random.uniform(1 - COMMAND_MAILBOX_RETRY_JITTER, 1),
    )


def _error_response(
    request_id: str,
    status: Literal["invalid", "unavailable"],
    prefix: str,
    error: BaseException,
) -> CommandResponse:
    detail = " ".join(str(error).split())
    message = f"{prefix}: {detail}" if detail else f"{prefix}."
    return CommandResponse(request_id=request_id, status=status, message=message[:2_000])


def _rejection_notice_id(name: str, mailbox_id: str) -> str | None:
    match = _REJECTION_FILE.fullmatch(name)
    if match is None or not secrets.compare_digest(match.group("mailbox_id"), mailbox_id):
        return None
    return match.group("notice_id")


def _read_rejection_notice(staged: StagedCommandMailbox, name: str) -> tuple[str, str] | None:
    """Return (status, message) from one authentic broker refusal notice, else None.

    Anything else in the workspace is inert: the agent can write there.
    """

    notice_id = _rejection_notice_id(name, staged.credential.mailbox_id)
    try:
        value = json.loads(
            staged.mailbox.read_text(name, max_bytes=COMMAND_REJECTION_NOTICE_MAX_BYTES)
        )
    except (StateUnreachable, RemoteStageTransportFailure):
        raise
    except (OSError, StateUnavailable, UnicodeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != _REJECTION_NOTICE_FIELDS:
        return None
    status, message = value["status"], value["message"]
    if (
        value["version"] != 1
        or value["notice_id"] != notice_id  # a renamed copy is not a new notice
        or status not in ("invalid", "unavailable")
        or not isinstance(message, str)
        or len(message) > 2_000
        or not staged.credential.accepts_rejection_notice(value)
    ):
        return None
    return str(status), " ".join(message.split())


def _request_identity_from_name(name: str, mailbox_id: str) -> str | None:
    match = _REQUEST_FILE.fullmatch(name)
    if match is None or not secrets.compare_digest(match.group("mailbox_id"), mailbox_id):
        return None
    return match.group("request_id")


def _clear_command_state(mailbox: RunStageMailbox) -> None:
    for name in mailbox.entry_names():
        if name.startswith(_COMMAND_STATE_PREFIXES):
            mailbox.remove(name, missing_ok=False)
