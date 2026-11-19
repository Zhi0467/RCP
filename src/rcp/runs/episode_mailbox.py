from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from rcp.limits import CHAT_WAKE_MIN_SECONDS
from rcp.storage.models import AgentTaskAdmissionConflict

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks

logger = logging.getLogger(__name__)

_RETRIED_LATER = {"chat_wake_busy", "chat_wake_cooldown", "bulletin_wake_cooldown"}


def _refusal_code(exc: Exception) -> str:
    code = str(exc)
    return code if re.fullmatch(r"[a-z][a-z0-9_]*", code) else "chat_wake_launch_failed"


def deliver_pending_chat_mail(
    background: BackgroundAgentTasks, project_id: str, chat_id: str
) -> str | None:
    """Admit one bounded mailbox wake through storage's shared chat admission."""
    store = background.store
    item_ids, suppressed = store.wake_eligible_chat_mail(project_id, chat_id)
    limited = [item for item, code in suppressed.items() if code == "chat_wake_chain_limit"]
    store.record_episode_mail_diagnostic(limited, "chat_wake_chain_limit")
    if not item_ids:
        return None
    try:
        task = store.admit_chat_wake(project_id, chat_id, "episode_mail", item_ids)
    except (KeyError, ValueError) as exc:
        code = _refusal_code(exc)
        if code not in _RETRIED_LATER:
            store.record_episode_mail_diagnostic(item_ids, code)
        raise
    if task is None:
        return None
    _launch(background, task.operation_id)
    return task.operation_id


def _launch(background: BackgroundAgentTasks, operation_id: str) -> str:
    try:
        return background.launch_admitted(operation_id).status
    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        claimed = [item.message_id for item in background.store.claimed_episode_mail(operation_id)]
        background.store.record_episode_mail_diagnostic(claimed, _refusal_code(exc))
        raise


def reconcile_chat_mail_recipient(
    background: BackgroundAgentTasks, project_id: str, chat_id: str
) -> str:
    """Launch this chat's committed wake, else admit one for its pending mail.

    A cooldown refusal arms one later pass for this chat, so the mail wakes it
    without another trigger. Every other refusal stays on the held-back items.
    """
    try:
        for task in background.store.pending_chat_wakes(project_id, chat_id):
            _launch(background, task.operation_id)
            return task.operation_id
        return deliver_pending_chat_mail(background, project_id, chat_id) or "chat_wake_ineligible"
    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        code = _refusal_code(exc)
        logger.info("Chat mailbox %s deferred: %s", chat_id, exc)
        if code == "chat_wake_cooldown":
            background.schedule_deferred_pass(
                f"chat_mail:{project_id}:{chat_id}",
                CHAT_WAKE_MIN_SECONDS,
                lambda: reconcile_chat_mail_recipient(background, project_id, chat_id),
            )
        return code


def reconcile_pending_chat_mail(background: BackgroundAgentTasks) -> dict[str, str]:
    """Launch committed wakes, then visit every chat with pending mail once.

    Chats are read in keyset pages, longest-waiting first, so a chat that cannot be
    woken never hides a later one.
    """
    statuses: dict[str, str] = {}
    background.store.release_failed_wake_mail()
    for task in background.store.every_pending_chat_wake():
        try:
            statuses[task.operation_id] = _launch(background, task.operation_id)
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            statuses[task.operation_id] = _refusal_code(exc)
            logger.warning("Chat mailbox wake %s deferred: %s", task.operation_id, exc)
    after = ("", "", "")
    seen: set[tuple[str, str]] = set()
    while page := background.store.pending_chat_mail_recipients(after):
        for oldest, project_id, chat_id in page:
            after = (oldest, project_id, chat_id)
            if (project_id, chat_id) in seen:
                continue
            seen.add((project_id, chat_id))
            statuses[chat_id] = reconcile_chat_mail_recipient(background, project_id, chat_id)
    return statuses


def reconcile_settled_chat_mail(background: BackgroundAgentTasks, operation_id: str) -> None:
    """A settled chat turn leaves its chat idle; mail that waited on it may wake it now.

    A wake that failed or was interrupted gives its claimed mail back first.
    """
    task = background.store.agent_task(operation_id)
    if task is None or task.episode_id is not None:
        return
    background.store.release_failed_wake_mail(operation_id)
    chat_id = task.request.get("chat_id")
    if isinstance(chat_id, str) and chat_id:
        reconcile_chat_mail_recipient(background, task.project_id, chat_id)


def wake_bulletin_actor(background: BackgroundAgentTasks, project_id: str, actor_id: str) -> str:
    """Wake an idle Work chat or orchestrator once for its open mentions.

    Chats go through the shared chat admission; orchestrators get one lifecycle
    notice and wake through the Auto-research lifecycle path.
    """
    from rcp.runs.auto_research_delivery import reconcile_pending_auto_research_lifecycle
    from rcp.storage.bulletin import BulletinError

    store = background.store
    try:
        if actor_id.startswith("episode:"):
            episode_id = store.record_bulletin_mention_notice(project_id, actor_id)
            if episode_id is None:
                return "bulletin_wake_ineligible"
            reconcile_pending_auto_research_lifecycle(background, episode_id=episode_id)
            return "bulletin_wake_notice"
        chat_id = actor_id.removeprefix("chat:")
        posts = store.bulletin_wake_candidates(project_id, actor_id)
        if not posts:
            return "bulletin_wake_ineligible"
        task = store.admit_chat_wake(project_id, chat_id, "bulletin_mention", posts)
        if task is None:
            return "bulletin_wake_ineligible"
        return background.launch_admitted(task.operation_id).status
    except (BulletinError, AgentTaskAdmissionConflict, KeyError, ValueError, RuntimeError) as exc:
        code = _refusal_code(exc)
        logger.info("Bulletin mention wake for %s deferred: %s", actor_id, exc)
        if code in _RETRIED_LATER:
            background.schedule_deferred_pass(
                f"bulletin:{project_id}:{actor_id}",
                max(1.0, store.bulletin_wake_cooldown_left(project_id, actor_id))
                if code == "bulletin_wake_cooldown"
                else CHAT_WAKE_MIN_SECONDS,
                lambda: wake_bulletin_actor(background, project_id, actor_id),
            )
        return code


def reconcile_bulletin_mentions(background: BackgroundAgentTasks) -> dict[str, str]:
    """Retry, once per actor, every mention no wake has handled, after a restart.

    A refused wake's later pass lives only in memory, so startup rebuilds it from
    the durable posts, in keyset pages.
    """
    statuses: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    after = (0, -1)
    while page := background.store.unwoken_bulletin_mentions(after):
        for sequence, index, project_id, actor_id in page:
            after = (sequence, index)
            if (project_id, actor_id) in seen or not actor_id.startswith(("chat:", "episode:")):
                continue
            seen.add((project_id, actor_id))
            statuses[actor_id] = wake_bulletin_actor(background, project_id, actor_id)
    return statuses


def wake_bulletin_mentions(
    background: BackgroundAgentTasks, project_id: str, post_id: str
) -> dict[str, str]:
    return {
        actor_id: wake_bulletin_actor(background, project_id, actor_id)
        for actor_id in background.store.bulletin_wake_actors(project_id, post_id)
    }
