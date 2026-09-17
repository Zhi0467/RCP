"""Where one provider turn ends, decided on the execution host.

The launcher ships this module's source to the execution machine, so it holds
to the standard library and imports nothing from RCP.

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
        if self.runtime_id == "codex.app-server-stdio.v1":
            return "turn/start" in self.requests.values()
        if self.runtime_id == "claude.stream-json.v1":
            return bool(self.message_ids)
        return True

    def input(self, value: object) -> None:
        if not isinstance(value, dict):
            return
        if value.get("type") == "user" and isinstance(value.get("uuid"), str):
            self.message_ids.setdefault(value["uuid"], None)
        method = value.get("method")
        identifier = value.get("id")
        if isinstance(method, str) and identifier is not None:
            # Every request this side sends, not only the ones whose replies are
            # interesting. A server that rejects `initialize` or `config/read`
            # answers with an error and then sits there; the canonical decoder
            # ends the turn on that, and a fence that had not written the request
            # down would leave a persistent server running with no turn to end.
            self.requests[identifier] = method
            params = value.get("params")
            if method == "thread/resume" and isinstance(params, dict):
                requested = params.get("threadId")
                if isinstance(requested, str) and requested:
                    self.requested_thread_id = requested
        elif method == "turn/steer":
            params = value.get("params")
            self.steer_requests[identifier] = (
                params.get("expectedTurnId") if isinstance(params, dict) else None
            )

    def output(self, value: object) -> None:
        if not isinstance(value, dict) or self.terminal:
            return
        if self.runtime_id == "codex.app-server-stdio.v1":
            self._app_server_output(value)
            return
        kind = value.get("type")
        if self.runtime_id == "claude.stream-json.v1":
            self._claude_output(value, kind)
            return
        if kind in {"turn.completed", "turn.failed", "error"}:
            self.terminal = True

    def _app_server_output(self, value: dict) -> None:
        result = value.get("result")
        method = self.requests.get(value.get("id"))
        if isinstance(result, dict):
            if method in {"thread/start", "thread/resume"}:
                self.thread_id = result.get("thread", {}).get("id")
            elif method == "turn/start":
                self.turn_id = result.get("turn", {}).get("id")
        if value.get("error") is not None and method is not None:
            self.terminal = True
            return
        params = value.get("params")
        thread = params.get("threadId") if isinstance(params, dict) else None
        # A resumed server can speak for another of its threads before it replies
        # to this one. The canonical decoder answers such a request knowing the
        # thread it asked to resume; this fence has to know it too, or the check
        # below would end a turn over a request never addressed to it.
        expected_thread = self.thread_id or self.requested_thread_id
        if isinstance(thread, str) and expected_thread is not None and thread != expected_thread:
            return
        # The canonical decoder fences any server-to-client request before it
        # inspects params or the thread, because an unattended turn cannot answer
        # one. Checking it later let a request carrying no params, or arriving
        # before thread/start replied, hold input open on a link this exists to
        # survive.
        if "id" in value and "method" in value:
            self.terminal = True
            return
        if not isinstance(params, dict) or self.thread_id is None:
            return
        # A multiplexed server speaks for its other turns on this thread, and the
        # canonical decoder drops any notification naming one. Dropping it here
        # too is what stops another turn's failure ending a live root turn.
        turn_id = params.get("turnId")
        if self.turn_id is not None and isinstance(turn_id, str) and turn_id != self.turn_id:
            return
        if value.get("method") == "error" and params.get("willRetry") is not True:
            self.terminal = True
            return
        turn = params.get("turn")
        # A fast server can complete the turn before its turn/start reply is
        # read. The canonical decoder accepts that completion, having no id to
        # disagree with yet, so requiring one here left the turn unfenced with
        # input open -- and nothing for a later reader to settle.
        if (
            value.get("method") == "turn/completed"
            and isinstance(turn, dict)
            and (self.turn_id is None or turn.get("id") == self.turn_id)
        ):
            self.terminal = True

    def _claude_output(self, value: dict, kind: object) -> None:
        identifier = value.get("command_uuid")
        if kind == "command_lifecycle" and identifier in self.message_ids:
            if value.get("state") in {"queued", "started"}:
                self.outstanding.add(identifier)
            elif value.get("state") == "completed":
                self.outstanding.discard(identifier)
        if kind == "error":
            self.terminal = True
            return
        if kind != "result":
            return
        identifiers = value.get("user_message_uuids")
        finished = (
            {item for item in identifiers if isinstance(item, str) and item.strip()}
            if isinstance(identifiers, list)
            else set()
        )
        identifier = value.get("user_message_uuid")
        if not finished and isinstance(identifier, str) and identifier.strip():
            finished.add(identifier)
        self.outstanding.difference_update(finished)
        # A failure ends the turn even with inputs still out: nothing is coming
        # back for them. This is the one place a boundary needs to know that a
        # result went badly, and it still says nothing about what to report.
        failed = (
            value.get("is_error") is True or "error" in str(value.get("subtype") or "").casefold()
        )
        if finished and self.outstanding and not failed:
            return
        self.terminal = True
