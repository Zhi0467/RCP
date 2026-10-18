"""One target-scoped agent roster shared by Bulletin staging and HTTP reads."""

from __future__ import annotations

import re
from typing import Any

from rcp.core.transition_models import GraphTargetRef
from rcp.storage import AppStore
from rcp.storage.models import ACTIVE_AGENT_TASK_STATUSES


def bulletin_roster(
    store: AppStore, project_id: str, target: GraphTargetRef
) -> list[dict[str, Any]]:
    tasks = store.graph_target_tasks(project_id, target)
    episodes = {episode.episode_id: episode for episode in store.episodes(project_id, limit=None)}
    waiting = {
        worker_id
        for episode_id in {task.episode_id for task in tasks if task.episode_id}
        for worker_id in store.auto_research_waiting_child_work_ids(episode_id)
    }
    waiting_chats = store.bulletin_waiting_chat_ids(project_id, target)
    questions = {
        question.origin.operation_id
        for question in store.list_questions(project_id=project_id, open_only=True)
    }
    last_posts = store.bulletin_last_post_times(project_id, target.key)
    rows: dict[str, dict[str, Any]] = {}
    for task in tasks:
        work = store.auto_research_child_work_for_operation(task.operation_id)
        episode = episodes.get(task.episode_id or "")
        instruction = str(task.request.get("message") or task.request.get("prompt") or "")
        if work:
            actor_id = f"worker:{work.worker_id}"
            kind = (
                "worker_project" if task.request.get("chat_scope") == "project" else "worker_node"
            )
            seat = work.control_node_id or None
            instruction = work.instruction
        elif task.request.get("role") == "worker":
            actor_id = f"worker:{task.request.get('actor_operation_id') or task.operation_id}"
            kind = (
                "worker_project" if task.request.get("chat_scope") == "project" else "worker_node"
            )
            seat = task.request.get("node_id")
        elif episode:
            root = episode
            while root.continues_episode_id in episodes:
                root = episodes[root.continues_episode_id]
            actor_id = f"episode:{root.episode_id}"
            kind = "auto_research_root" if episode.mode == "auto_research" else "experiment_loop"
            seat = episode.control_node_id
            auto = store.auto_research_state(root.episode_id)
            if auto:
                instruction = auto.starting_instruction or ""
        elif task.kind in {"node_chat", "project_chat"}:
            actor_id = f"chat:{task.request.get('chat_id') or task.operation_id}"
            kind = "human_chat_work" if task.request.get("mode") == "work" else "human_chat_discuss"
            seat = task.request.get("node_id")
        else:
            continue
        heading = re.search(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", instruction, re.MULTILINE)
        state = (
            "needs_you"
            if task.operation_id in questions
            else "working"
            if task.status in ACTIVE_AGENT_TASK_STATUSES
            else "waiting"
            if (work and work.worker_id in waiting)
            or (
                kind == "human_chat_work"
                and task.status == "succeeded"
                and task.request.get("chat_id") in waiting_chats
            )
            else "needs_you"
            if task.status in {"failed", "paused", "interrupted"}
            or (not work and episode and episode.status == "needs_action")
            else "waiting"
            if not work
            and episode
            and episode.status in {"queued", "running", "stopping", "wrapping_up"}
            else "done"
        )
        previous_post = rows.get(actor_id, {}).get("last_post_at")
        post_at = last_posts.get(task.operation_id)
        rows[actor_id] = {
            "id": actor_id,
            "kind": kind,
            "seat_node_id": seat,
            "instruction_title": heading.group(1)
            if heading
            else next(iter(instruction.strip().splitlines()), ""),
            "state": state,
            "last_post_at": max(filter(None, (previous_post, post_at)), default=None),
        }
    return list(rows.values())
