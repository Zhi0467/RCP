"""Where one provider turn ends, decided on the execution host.

RCP ships this module, with each provider's `remote.py`, to the execution
machine through `rcp.providers.remote_bundle`. It depends only on the standard
library.

It answers one question: is this turn over. It deliberately does not answer
whether the turn succeeded. RCP cannot ship its decoder to a host, so anything
the host concludes is a second implementation of a protocol RCP already reads,
and the two drift. Boundaries are the part that cannot be deferred -- a
persistent server keeps a turn open until somebody closes its input -- so the
host decides those and nothing else. The verdict waits for the journal to reach
the one decoder that has always owned it.
"""

from __future__ import annotations


class TurnFence:
    """Whether this turn's traffic has reached its own end."""

    def __init__(self, runtime_id: str):
        self.last_error = ""
        self.runtime_id = runtime_id
        self.requests: dict[object, str] = {}
        # Ordered, because which one arrived first is which one was the
        # prompt. A reader rebuilding this turn needs that, and a set loses it.
        self.message_ids: dict[str, None] = {}
        self.outstanding: set[str] = set()
        self.thread_id: str | None = None
        # The thread a resume asked for, known before the reply names it.
        self.requested_thread_id: str | None = None
        self.turn_id: str | None = None
        # Which steer the server was asked to apply, and to which turn. Identity,
        # not judgement: a reader still decides what a delivered steer was worth.
        self.steer_requests: dict[object, object] = {}
        self.terminal = False

    @property
    def prompt_started(self) -> bool:
        return True

    def input(self, value: object) -> None:
        if isinstance(value, dict):
            self._input(value)

    def _input(self, value: dict) -> None:
        pass

    def output(self, value: object) -> None:
        if not isinstance(value, dict) or self.terminal:
            return
        self._output(value)

    def _output(self, value: dict) -> None:
        kind = value.get("type")
        if kind == "error":
            self.last_error = str(value.get("message") or value.get("error") or "")
        if kind in {"turn.completed", "turn.failed"}:
            self.terminal = True


#: Every runtime's fence class by durable runtime id. Each provider's profile
#: declares its own as `turn_fences`; `rcp.providers` registers them here, and
#: the generated registration does the same on a host.
TURN_FENCES: dict[str, type[TurnFence]] = {}


def turn_fence(runtime_id: str) -> TurnFence:
    try:
        fence = TURN_FENCES[runtime_id]
    except KeyError:
        raise ValueError(f"No turn fence is registered for runtime {runtime_id!r}.") from None
    return fence(runtime_id)
