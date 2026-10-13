"""Process-local ask attempts and liveness, shared by command threads and API reads."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from rcp.limits import ASK_TRACKED_CALLS, ASK_WAITING_FRESH_SECONDS

logger = logging.getLogger(__name__)


@dataclass
class _QuestionActivity:
    calls: dict[str, int] = field(default_factory=dict)
    total: int = 0
    last_pending: float = 0


class QuestionActivity:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        changed: Callable[[str, bool], None] | None = None,
    ) -> None:
        self._clock = clock
        self._changed = changed
        self._lock = threading.Lock()
        self._publication_lock = threading.Lock()
        self._questions: dict[str, _QuestionActivity] = {}

    def pending(self, question_id: str, call_id: str | None) -> int | None:
        """Count an invocation once; old clients refresh liveness without a count."""
        # Serialize publications without holding the liveness read lock over SQLite.
        with self._publication_lock:
            with self._lock:
                activity = self._questions.setdefault(question_id, _QuestionActivity())
                activity.last_pending = self._clock()
                attempt = None
                if call_id is not None:
                    if call_id not in activity.calls:
                        activity.total += 1
                        activity.calls[call_id] = activity.total
                        if len(activity.calls) > ASK_TRACKED_CALLS:
                            del activity.calls[next(iter(activity.calls))]
                    attempt = activity.calls[call_id]
            self._publish(question_id, True)
            return attempt

    def forget(self, question_id: str) -> None:
        """Drop a resolved question so the record holds only open ones."""
        with self._publication_lock:
            with self._lock:
                activity = self._questions.pop(question_id, None)
            if activity is not None:
                self._publish(question_id, False)

    def _publish(self, question_id: str, waiting: bool) -> None:
        if self._changed is not None:
            try:
                self._changed(question_id, waiting)
            except Exception:
                logger.exception("Could not publish question activity for %s", question_id)

    def is_waiting(self, question_id: str) -> bool:
        with self._lock:
            activity = self._questions.get(question_id)
            return bool(
                activity is not None
                and self._clock() - activity.last_pending < ASK_WAITING_FRESH_SECONDS
            )
