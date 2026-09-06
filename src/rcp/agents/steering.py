from __future__ import annotations

import asyncio
import threading

from rcp.limits import PROVIDER_STEER_WRITE_TIMEOUT_SECONDS
from rcp.providers import (
    ProviderRuntimeStep,
    ProviderSteeringState,
    ProviderSteerReceipt,
    ProviderTurn,
)


class LiveProviderSteering:
    """One process's inbound pipe; all mutation runs on its owning event loop."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        turn: ProviderTurn,
        pause_requested: threading.Event,
    ) -> None:
        self._process = process
        self._turn = turn
        self._pause_requested = pause_requested
        self._closed = False
        self._state = turn.steering_state()
        self._pending: dict[str, asyncio.Future[ProviderSteerReceipt]] = {}
        self._sent: set[str] = set()

    def state(self) -> ProviderSteeringState:
        # An immutable snapshot is also safe for the API's worker thread.
        if self._pause_requested.is_set():
            return ProviderSteeringState(False, "The provider is stopping.")
        return self._state

    def observe(self, step: ProviderRuntimeStep) -> None:
        self._state = self._turn.steering_state()
        for message_id, receipt in step.steer_receipts:
            pending = self._pending.get(message_id)
            if pending is not None and not pending.done():
                pending.set_result(receipt)
        if step.complete:
            # Completion fences new input, but app-server can still flush a
            # matching steer response. Only stream shutdown settles the rest.
            self._closed = True
            self._state = ProviderSteeringState(False, "The provider turn is no longer running.")

    def close(self) -> None:
        self._closed = True
        self._state = ProviderSteeringState(False, "The provider turn is no longer running.")
        for pending in self._pending.values():
            if not pending.done():
                pending.set_result(
                    ProviderSteerReceipt(
                        "unknown", "The provider ended without acknowledging delivery."
                    )
                )

    async def send(self, expected_turn_id: str, message_id: str, text: str) -> ProviderSteerReceipt:
        stream = self._process.stdin
        if (
            self._closed
            or self._pause_requested.is_set()
            or self._process.returncode is not None
            or stream is None
            or stream.is_closing()
        ):
            return ProviderSteerReceipt("refused", "The provider turn is no longer running.")
        if message_id in self._sent:
            return ProviderSteerReceipt("refused", "This message was already sent to the provider.")
        try:
            outgoing = self._turn.render_steer(expected_turn_id, message_id, text)
        except ValueError as exc:
            return ProviderSteerReceipt("refused", str(exc))
        pending: asyncio.Future[ProviderSteerReceipt] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = pending
        self._sent.add(message_id)
        try:
            try:
                # No await separates the active-turn check from write. A partial
                # write is already uncertain and must never be retried.
                stream.write(outgoing)
                await asyncio.wait_for(stream.drain(), PROVIDER_STEER_WRITE_TIMEOUT_SECONDS)
            except (OSError, RuntimeError, TimeoutError):
                if not pending.done():
                    pending.set_result(
                        ProviderSteerReceipt(
                            "unknown", "The provider connection failed during delivery."
                        )
                    )
            return await asyncio.shield(pending)
        finally:
            self._pending.pop(message_id, None)
