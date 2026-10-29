"""Client-side errors written to the server log.

The web client reports each error notice, uncaught window error, and root render
failure here, best-effort. Each lands as one WARNING line on the ``rcp.client``
logger, so the desktop backend log and a team server's service journal keep a
trace of what the person saw. Nothing is stored. The route sits behind the same
session middleware as every other ``/api`` route.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rcp import __version__, limits
from rcp.api.dependencies import get_identity_access
from rcp.api.identity import IdentityAccess

logger = logging.getLogger("rcp.client")
router = APIRouter()

ClientEventKind = Literal["notice", "window_error", "unhandled_rejection", "render_failure"]
Admission = Literal["log", "limit_reached", "drop"]


def _one_line(text: str) -> str:
    """Collapse to one printable line, so a message cannot forge a second log record."""

    printable = "".join(char if char.isprintable() else " " for char in text)
    return " ".join(printable.split())


class ClientEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ClientEventKind
    # Cut rather than refused, so an oversized error still leaves its head in the log.
    message: str
    # A view name such as "dag" or "runs", never a URL: hashes can carry tokens.
    view: str = Field(
        default="", max_length=limits.CLIENT_EVENT_VIEW_MAX_CHARS, pattern=r"^[a-z0-9_-]*$"
    )

    @field_validator("message")
    @classmethod
    def _bound_message(cls, value: str) -> str:
        cap = limits.CLIENT_EVENT_MESSAGE_MAX_CHARS
        return _one_line(value[: cap * 4])[:cap]


class ClientEventLimiter:
    """A fixed window per member: a toast loop logs a bounded burst and one limit line."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[float, int]] = {}

    def admit(self, user_id: str) -> Admission:
        now = self._clock()
        with self._lock:
            start, count = self._windows.get(user_id, (now, 0))
            if now - start >= limits.CLIENT_EVENT_RATE_WINDOW_SECONDS:
                start, count = now, 0
            count += 1
            self._windows[user_id] = (start, count)
        if count <= limits.CLIENT_EVENT_RATE_MAX_EVENTS:
            return "log"
        return "limit_reached" if count == limits.CLIENT_EVENT_RATE_MAX_EVENTS + 1 else "drop"


@router.post("/api/client-events", status_code=204)
def record_client_event(
    event: ClientEvent,
    request: Request,
    identity_access: Annotated[IdentityAccess, Depends(get_identity_access)],
) -> Response:
    user = identity_access.acting_user(request)
    limiter: ClientEventLimiter = request.app.state.client_event_limiter
    admission = limiter.admit(user.user_id)
    if admission == "limit_reached":
        logger.warning(
            "client events from user=%s exceed %d per %gs; dropping the rest of this window",
            user.user_id,
            limits.CLIENT_EVENT_RATE_MAX_EVENTS,
            limits.CLIENT_EVENT_RATE_WINDOW_SECONDS,
        )
    if admission != "log":
        return Response(status_code=429)
    logger.warning(
        "%s view=%s build=%s user=%s: %s",
        event.kind,
        event.view or "-",
        __version__,
        user.user_id,
        event.message,
    )
    return Response(status_code=204)
