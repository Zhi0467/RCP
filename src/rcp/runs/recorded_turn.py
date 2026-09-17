"""One provider pass as its execution host recorded it.

The journal is evidence, not authority: it says what crossed the wire, and
nothing about what the turn was worth. That verdict is read here, by the same
runtime object the live pipe drives, so a turn recovered from a record and the
same turn delivered over a link are judged by one decoder rather than two that
must be kept in agreement.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass

from rcp.agents import AgentEvent
from rcp.limits import TURN_JOURNAL_MAX_EVENT_BYTES
from rcp.providers import ProviderTurnRequest, profile_for, require_runtime_id


def _within_event_limit(line: str) -> bool:
    # A UTF-8 character is at most four bytes, so a short line is under the limit
    # without measuring; only a long one is worth encoding to find out.
    return (
        len(line) * 4 <= TURN_JOURNAL_MAX_EVENT_BYTES
        or len(line.encode("utf-8", "surrogateescape")) <= TURN_JOURNAL_MAX_EVENT_BYTES
    )


@dataclass(frozen=True)
class RecordedProviderTurn:
    """What one pass left on its host, verified once on the way in.

    Built by `recorded_provider_turn`, which is the only place that checks the
    record's shape, digest and bounds. Everything downstream reads this value and
    re-verifies nothing, so no two readers can reach different conclusions about
    the same bytes.
    """

    pid_file: str
    provider: str
    runtime_id: str
    provider_version: str | None
    outcome: dict[str, object]
    events: str
    stderr: str
    patch: str | None

    @property
    def accepted(self) -> bool:
        """Whether the host took responsibility for this pass's prompt."""

        return self.outcome.get("accepted") is True

    @property
    def intact(self) -> bool:
        """Whether the record is whole enough to be read as a turn at all.

        Says nothing about whether the turn succeeded. A pass that overflowed its
        journal, hit a supervisor error, or was stopped by a human has lost
        evidence, and a verdict read off what remains would be a guess.
        """

        return bool(
            self.outcome.get("journal_complete") is True
            and not self.outcome.get("error")
            and not self.outcome.get("stopped")
        )

    @property
    def observed_lines(self) -> list[str]:
        """The journal lines a live pipe would have decoded, in order.

        Both ends drop an over-long line rather than parse it, so reading one
        back here would let a recovered turn act on an event the live turn
        refused. Split on the delimiter the writer used: `splitlines` also breaks
        on U+2028, which would cut one answer into two invalid fragments.
        """

        return [line for line in self.events.split("\n") if line and _within_event_limit(line)]

    @property
    def session_id(self) -> str | None:
        root = self.outcome.get("root_thread_id")
        return root if isinstance(root, str) and root else None


@dataclass(frozen=True)
class RecordedVerdict:
    """What RCP's own decoder made of a recorded pass.

    `complete` carries the live meaning exactly: the wire conversation reached
    its own conclusion. A turn the provider failed is still complete, and says so
    through an `error` event among `events`, which is how the live pipe learns it
    too. Resisting a second, recorded-only notion of success is the point -- a
    reader that handles one of these handles both without knowing which it has.
    """

    complete: bool
    events: tuple[AgentEvent, ...]


def recorded_provider_turn(pid_file: str, journal: dict[str, object]) -> RecordedProviderTurn:
    """Verify one host journal and bind it to a value nothing re-checks."""

    outcome = journal.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("version") != 1:
        raise ValueError("The provider journal does not state a version this RCP reads.")
    if outcome.get("pid_file") != pid_file:
        raise ValueError("The provider journal names a different pass than the one asked for.")
    provider = outcome.get("provider")
    runtime_id = outcome.get("runtime_id")
    if not isinstance(provider, str) or not isinstance(runtime_id, str):
        raise ValueError("The provider journal does not name what produced it.")
    require_runtime_id(provider, runtime_id)
    events = str(journal.get("events") or "")
    digest = outcome.get("events_sha256")
    if not isinstance(digest, str) or not digest:
        raise ValueError("The provider journal states no digest for its events.")
    if hashlib.sha256(events.encode("utf-8", "surrogateescape")).hexdigest() != digest:
        raise ValueError("The provider journal's events do not match the digest it recorded.")
    patch = journal.get("patch")
    if patch is not None and not isinstance(patch, str):
        raise ValueError("The provider journal's patch is not text.")
    if isinstance(patch, str):
        patch_digest = outcome.get("patch_sha256")
        if (
            not isinstance(patch_digest, str)
            or hashlib.sha256(patch.encode("utf-8", "surrogateescape")).hexdigest() != patch_digest
        ):
            raise ValueError("The provider journal's patch does not match the digest it recorded.")
    version = outcome.get("provider_version")
    return RecordedProviderTurn(
        pid_file=pid_file,
        provider=provider,
        runtime_id=runtime_id,
        provider_version=version if isinstance(version, str) else None,
        outcome=outcome,
        events=events,
        stderr=str(journal.get("stderr") or ""),
        patch=patch,
    )


def decode_recorded_turn(
    recorded: RecordedProviderTurn, request: ProviderTurnRequest
) -> RecordedVerdict:
    """Replay a recorded pass through the runtime the live pipe would have driven.

    This is the whole reason the host publishes no verdict. One decoder reads the
    wire, whether the bytes arrived down a live link or off a disk hours later,
    so there is no second opinion to keep in agreement with this one.
    """

    request = dataclasses.replace(request, provider_version=recorded.provider_version)
    turn = profile_for(recorded.provider).runtime(recorded.runtime_id).turn(request)
    turn.initial_input()
    events: list[AgentEvent] = [AgentEvent(event="runtime", text=recorded.runtime_id)]
    complete = False
    for line in recorded.observed_lines:
        if line.startswith("RCP_COMMAND_BROKER_READY:"):
            continue
        try:
            json.loads(line)
        except ValueError:
            continue
        # Outgoing bytes are dropped on purpose: no provider is listening, and a
        # recorded turn answers nobody. Steering was already settled by the live
        # pipe that observed it.
        step = turn.receive_line(line)
        events.extend(
            AgentEvent(
                event=item.event, text=item.text, session_id=item.session_id, usage=item.usage
            )
            for item in step.events
        )
        complete = complete or step.complete
        if step.explicit_terminal:
            break
    return RecordedVerdict(complete=complete and recorded.intact, events=tuple(events))
