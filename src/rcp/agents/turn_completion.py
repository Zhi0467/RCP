"""Provider-independent completion decisions; callers supply time and own processes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class CompletionVerdict:
    code: Literal["complete", "failed", "injected_notice", "pending_input", "open_work"]
    open_work_since: float | None = None

    @property
    def terminal(self) -> bool:
        return self.code in {"complete", "failed"}


def attributed_ids(value: dict) -> set[str]:
    identifiers = value.get("user_message_uuids")
    result = (
        {item for item in identifiers if isinstance(item, str) and item.strip()}
        if isinstance(identifiers, list)
        else set()
    )
    identifier = value.get("user_message_uuid")
    if isinstance(identifier, str) and identifier.strip():
        result.add(identifier)
    return result


@dataclass
class TurnCompletion:
    open_work: set[str] = field(default_factory=set)
    open_work_since: float | None = None
    _codex_children: set[str] = field(default_factory=set)

    def observe_claude_task(self, value: dict) -> None:
        identifier = value.get("task_id")
        if value.get("type") != "system" or not isinstance(identifier, str) or not identifier:
            return
        if value.get("subtype") == "task_started":
            self.open_work.add(identifier)
        elif value.get("subtype") == "task_notification":
            self.open_work.discard(identifier)

    def observe_codex_task(self, method: object, params: dict, root_thread: str | None) -> None:
        thread_id = params.get("threadId")
        item = params.get("item")
        # A child may delegate too: its activity names the child's thread, not the root's.
        if (
            (thread_id == root_thread or thread_id in self._codex_children)
            and method in {"item/started", "item/completed"}
            and isinstance(item, dict)
            and item.get("type") == "subAgentActivity"
        ):
            child = item.get("agentThreadId")
            if isinstance(child, str) and child:
                self._codex_children.add(child)
                if item.get("kind") == "started":
                    self.open_work.add(child)
                elif item.get("kind") in {"completed", "interrupted"}:
                    self.open_work.discard(child)
        if isinstance(thread_id, str) and thread_id in self._codex_children:
            if method == "turn/started":
                self.open_work.add(thread_id)
            elif method == "turn/completed":
                self.open_work.discard(thread_id)

    def verdict(
        self,
        *,
        failed: bool = False,
        sent_ids: set[str] | None = None,
        finished_ids: set[str] | None = None,
        outstanding_inputs: set[str] | None = None,
        task_notification: bool = False,
        now: float,
    ) -> CompletionVerdict:
        if failed:
            code = "failed"
        elif task_notification and not (finished_ids or set()) & (sent_ids or set()):
            code = "injected_notice"
        elif self.open_work:
            if self.open_work_since is None:
                self.open_work_since = now
            code = "open_work"
        elif finished_ids and outstanding_inputs:
            code = "pending_input"
        else:
            code = "complete"
        return CompletionVerdict(code, self.open_work_since)
