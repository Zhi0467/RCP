from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from rcp.limits import CHAT_WAKE_MIN_SECONDS

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks

logger = logging.getLogger(__name__)


def deliver_pending_chat_mail(
    background: BackgroundAgentTasks, project_id: str, chat_id: str
) -> str | None:
    """Admit one bounded mailbox wake through storage's shared chat admission."""
    pending = background.store.pending_episode_mail("chat", chat_id)
    if not pending:
        return None
    task = background.store.admit_chat_wake(
        project_id, chat_id, "episode_mail", [item.message_id for item in pending]
    )
    if task is None:
        return None
    background.launch_admitted(task.operation_id)
    return task.operation_id


def reconcile_pending_chat_mail(background: BackgroundAgentTasks) -> dict[str, str]:
    """Launch committed wakes, then admit each pending recipient once per pass.

    A cooldown refusal arms one later pass, so the mail wakes its chat without
    another settlement.
    """
    statuses: dict[str, str] = {}
    for task in background.store.pending_chat_wakes():
        try:
            statuses[task.operation_id] = background.launch_admitted(task.operation_id).status
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            statuses[task.operation_id] = str(exc)
            logger.warning("Chat mailbox wake %s deferred: %s", task.operation_id, exc)
    for project_id, chat_id in background.store.pending_chat_mail_recipients():
        try:
            operation_id = deliver_pending_chat_mail(background, project_id, chat_id)
            statuses[chat_id] = operation_id or "chat_wake_ineligible"
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            statuses[chat_id] = str(exc)
            logger.info("Chat mailbox %s deferred: %s", chat_id, exc)
    if "chat_wake_cooldown" in statuses.values():
        background.schedule_deferred_pass(
            "chat_mail",
            CHAT_WAKE_MIN_SECONDS,
            lambda: reconcile_pending_chat_mail(background),
        )
    return statuses
