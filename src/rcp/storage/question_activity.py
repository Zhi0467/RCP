"""Process-local ask attempts and liveness, shared by command threads and API reads."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from rcp.limits import ASK_TRACKED_CALLS, ASK_WAITING_FRESH_SECONDS


@dataclass
class _QuestionActivity:
    calls: dict[str, int] = field(default_factory=dict)
    total: int = 0
    last_pending: float = 0


class QuestionActivity:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._questions: dict[str, _QuestionActivity] = {}

    def pending(self, question_id: str, call_id: str | None) -> int | None:
        """Count an invocation once; old clients refresh liveness without a count."""
        with self._lock:
            activity = self._questions.setdefault(question_id, _QuestionActivity())
            activity.last_pending = self._clock()
            if call_id is None:
                return None
            if call_id not in activity.calls:
                activity.total += 1
                activity.calls[call_id] = activity.total
                if len(activity.calls) > ASK_TRACKED_CALLS:
                    del activity.calls[next(iter(activity.calls))]
            return activity.calls[call_id]

    def forget(self, question_id: str) -> None:
        """Drop a resolved question so the record holds only open ones."""
        with self._lock:
            self._questions.pop(question_id, None)

    def is_waiting(self, question_id: str) -> bool:
        with self._lock:
            activity = self._questions.get(question_id)
            return bool(
                activity is not None
                and self._clock() - activity.last_pending < ASK_WAITING_FRESH_SECONDS
            )
