"""One target-scoped agent roster shared by Bulletin staging and HTTP reads."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from rcp.core.graph_targets import graph_target_json
from rcp.core.transition_models import GraphTargetRef
from rcp.storage import AppStore
from rcp.storage.models import ACTIVE_AGENT_TASK_STATUSES, EpisodeRecord


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
    if request.get("chat_id"):
        root = connection.execute(
            "SELECT 1 FROM episodes WHERE episode_id=? AND project_id=? "
            "AND mode='auto_research' AND continues_episode_id IS NULL",
            (request["chat_id"], task["project_id"]),
        ).fetchone()
        prefix = "episode" if root else "chat"
        return f"{prefix}:{request['chat_id']}"
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
    store: AppStore, project_id: str, target: GraphTargetRef | None = None
) -> list[dict[str, Any]]:
    inventory = store.chat_inventory(project_id)
    with store.connection() as connection:
        archived = {
            row["chat_id"]
            for row in connection.execute(
                "SELECT chat_id FROM chat_display WHERE project_id=? AND archived_at IS NOT NULL",
                (project_id,),
            )
        }
    conversations = {
        row["chat_id"]: row
        for row in inventory
        if row["chat_id"] not in archived
        and (target is None or GraphTargetRef.model_validate(row["graph_target"]) == target)
    }
    episodes = {episode.episode_id: episode for episode in store.episodes(project_id, limit=None)}
    questions = {
        question.origin.operation_id
        for question in store.list_questions(project_id=project_id, open_only=True)
    }
    targets = (
        {target.key: target}
        if target is not None
        else {
            GraphTargetRef.model_validate(row["graph_target"]).key: GraphTargetRef.model_validate(
                row["graph_target"]
            )
            for row in conversations.values()
        }
    )
    return [
        row
        for channel in targets.values()
        for row in _target_roster(store, project_id, channel, conversations, episodes, questions)
    ]


def _target_roster(
    store: AppStore,
    project_id: str,
    target: GraphTargetRef,
    conversations: dict[str, dict[str, Any]],
    episodes: dict[str, EpisodeRecord],
    questions: set[str],
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
    waiting = {
        worker_id
        for episode_id in {task.episode_id for task in tasks if task.episode_id}
        for worker_id in store.auto_research_waiting_child_work_ids(episode_id)
    }
    waiting_chats = store.bulletin_waiting_chat_ids(project_id, target)
    last_posts = store.bulletin_last_post_times(project_id, target.key)
    rows: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if task.kind == "auto_research" and task.request.get("role") == "worker":
            continue
        actor_id = actor_ids.get(task.operation_id)
        if actor_id is None:
            continue
        chat_id = actor_id.split(":", 1)[1]
        conversation = conversations.get(chat_id)
        if (
            conversation is None
            or GraphTargetRef.model_validate(conversation["graph_target"]) != target
        ):
            continue
        work = store.auto_research_child_work_for_operation(task.operation_id)
        episode = episodes.get(task.episode_id or "")
        if work:
            kind = (
                "worker_project" if task.request.get("chat_scope") == "project" else "worker_node"
            )
            seat = work.control_node_id or None
        elif episode:
            kind = "auto_research_root" if episode.mode == "auto_research" else "experiment_loop"
            seat = episode.control_node_id
        elif task.kind in {"node_chat", "project_chat"}:
            kind = "human_chat_work" if task.request.get("mode") == "work" else "human_chat_discuss"
            seat = task.request.get("node_id")
        else:
            continue
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
        previous = rows.get(chat_id, {})
        previous_post = previous.get("last_post_at")
        post_at = last_posts.get(task.operation_id)
        rows[chat_id] = {
            "id": actor_id,
            "chat_id": chat_id,
            "target_key": target.key,
            "name": conversation["title"],
            "started_at": previous.get("started_at", task.created_at),
            "kind": kind,
            "seat_node_id": seat,
            "state": state,
            "last_post_at": max(filter(None, (previous_post, post_at)), default=None),
        }
    return list(rows.values())
