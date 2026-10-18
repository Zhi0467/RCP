"""One target-scoped agent roster shared by Bulletin staging and HTTP reads."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from rcp.core.graph_targets import graph_target_json
from rcp.core.transition_models import GraphTargetRef
from rcp.storage import AppStore
from rcp.storage.models import ACTIVE_AGENT_TASK_STATUSES


def bulletin_actor_id(connection: sqlite3.Connection, task: sqlite3.Row) -> str | None:
    """Resolve the roster identity within the caller's storage transaction."""
    request = json.loads(task["request_json"] or "{}")
    work = connection.execute(
        "SELECT work.worker_id FROM auto_research_child_work AS work "
        "JOIN auto_research_child_work_attempts AS attempt ON attempt.worker_id=work.worker_id "
        "WHERE attempt.operation_id=?",
        (task["operation_id"],),
    ).fetchone()
    if work:
        return f"worker:{work['worker_id']}"
    if request.get("role") == "worker":
        return f"worker:{request.get('actor_operation_id') or task['operation_id']}"
    episode = connection.execute(
        "SELECT episode_id,continues_episode_id FROM episodes WHERE episode_id=? AND project_id=?",
        (task["episode_id"], task["project_id"]),
    ).fetchone()
    if episode:
        seen = {episode["episode_id"]}
        while episode["continues_episode_id"]:
            parent = connection.execute(
                "SELECT episode_id,continues_episode_id FROM episodes WHERE episode_id=? AND project_id=?",
                (episode["continues_episode_id"], task["project_id"]),
            ).fetchone()
            if parent is None or parent["episode_id"] in seen:
                break
            episode = parent
            seen.add(episode["episode_id"])
        return f"episode:{episode['episode_id']}"
    if task["kind"] in {"node_chat", "project_chat"}:
        return f"chat:{request.get('chat_id') or task['operation_id']}"
    return None


def bulletin_roster(
    store: AppStore, project_id: str, target: GraphTargetRef
) -> list[dict[str, Any]]:
    tasks = store.graph_target_tasks(project_id, target)
    with store.connection() as connection:
        actor_ids = {
            row["operation_id"]: bulletin_actor_id(connection, row)
            for row in connection.execute(
                "SELECT * FROM graph_runs WHERE project_id=? AND graph_target_json=? AND visible=1",
                (project_id, graph_target_json(target)),
            ).fetchall()
        }
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
        actor_id = actor_ids.get(task.operation_id)
        if actor_id is None:
            continue
        work = store.auto_research_child_work_for_operation(task.operation_id)
        episode = episodes.get(task.episode_id or "")
        instruction = str(task.request.get("message") or task.request.get("prompt") or "")
        if work:
            kind = (
                "worker_project" if task.request.get("chat_scope") == "project" else "worker_node"
            )
            seat = work.control_node_id or None
            instruction = work.instruction
        elif task.request.get("role") == "worker":
            kind = (
                "worker_project" if task.request.get("chat_scope") == "project" else "worker_node"
            )
            seat = task.request.get("node_id")
        elif episode:
            root = episode
            while root.continues_episode_id in episodes:
                root = episodes[root.continues_episode_id]
            kind = "auto_research_root" if episode.mode == "auto_research" else "experiment_loop"
            seat = episode.control_node_id
            auto = store.auto_research_state(root.episode_id)
            if auto:
                instruction = auto.starting_instruction or ""
        elif task.kind in {"node_chat", "project_chat"}:
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
