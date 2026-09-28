"""Codex's session files and turn boundaries, as an execution host reads them.

Shipped as source text to execution hosts by `rcp.providers.remote_bundle`,
after the shared bases, so it may import only the standard library. The guarded
imports serve the local copy.
"""

from __future__ import annotations

from typing import Any

if "SessionFormat" not in globals():
    from rcp.providers.session_format import SessionFormat, extract_text
if "TurnFence" not in globals():
    from rcp.providers.turn_fence import TurnFence


class CodexSessionFormat(SessionFormat):
    def read_metadata(self, raw: dict[str, Any], metadata: dict[str, Any]) -> None:
        if raw.get("type") != "session_meta":
            super().read_metadata(raw, metadata)
            return
        payload = raw.get("payload", {})
        metadata["cwd"] = payload.get("cwd", metadata["cwd"])
        metadata["session_id"] = (
            payload.get("id") or payload.get("session_id") or metadata["session_id"]
        )
        metadata["thread_source"] = payload.get("thread_source") or metadata["thread_source"]
        metadata["originator"] = payload.get("originator") or metadata["originator"]
        source = payload.get("source")
        if isinstance(source, str):
            metadata["source_kind"] = source
        elif isinstance(source, dict):
            if "subagent" in source:
                metadata["source_kind"] = "subagent"
            subagent = source.get("subagent")
            spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
            if isinstance(spawn, dict):
                metadata["parent_session_id"] = (
                    spawn.get("parent_thread_id") or metadata["parent_session_id"]
                )

    def record_fields(self, raw: dict[str, Any]) -> tuple[Any, str, Any, str, Any]:
        payload = raw.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        record_id = payload.get("id") or raw.get("id")
        raw_type = f"{raw.get('type', '')}:{payload.get('type', '')}".rstrip(":")
        role = payload.get("role", "unknown")
        text = extract_text(payload.get("content"))
        if not text and payload.get("type") in {"user_message", "agent_message"}:
            text = str(payload.get("message", ""))
            role = "user" if payload.get("type") == "user_message" else "assistant"
        if not text and payload.get("type") == "custom_tool_call":
            tool_input = payload.get("input")
            if isinstance(tool_input, str):
                text = tool_input
                role = "assistant"
        return record_id, raw_type, role, text, payload.get("timestamp") or raw.get("timestamp")


class AppServerTurnFence(TurnFence):
    """Codex's persistent app server, which keeps a turn open until told."""

    @property
    def prompt_started(self) -> bool:
        return "turn/start" in self.requests.values()

    def _input(self, value: dict) -> None:
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

    def _output(self, value: dict) -> None:
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
