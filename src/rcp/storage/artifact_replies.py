from __future__ import annotations

from typing import Protocol

from rcp.storage.artifact_models import Artifact
from rcp.storage.models import AgentTaskAdmissionConflict, AgentTaskRecord, EpisodeRecord


class ReplyOriginReader(Protocol):
    def agent_task(self, operation_id: str) -> AgentTaskRecord | None: ...

    def episode(self, episode_id: str) -> EpisodeRecord | None: ...

    def episode_tasks(self, episode_id: str) -> list[AgentTaskRecord]: ...


def artifact_reply_origin(
    store: ReplyOriginReader, artifact: Artifact
) -> tuple[AgentTaskRecord, str | None]:
    origin = store.agent_task(artifact.origin_operation_id or "")
    if origin is None or origin.project_id != artifact.project_id:
        raise AgentTaskAdmissionConflict("The artifact's origin task is unavailable.")
    episode = store.episode(artifact.episode_id or origin.episode_id or "")
    if origin.kind == "episode_report":
        parent = store.agent_task(origin.parent_operation_id or "")
        if parent is not None:
            origin = parent
    if origin.request.get("chat_id"):
        return origin, None
    if episode is not None and episode.mode == "auto_research":
        root = store.agent_task(episode.root_operation_id or "")
        if root is None:
            raise AgentTaskAdmissionConflict("The episode's orchestrator thread is unavailable.")
        return origin, episode.episode_id
    if episode is not None and episode.mode == "experiment_loop":
        for task in reversed(store.episode_tasks(episode.episode_id)):
            if task.request.get("chat_id") and task.request.get("patch_kind") == "experiment_loop":
                return task, None
    raise AgentTaskAdmissionConflict("The artifact's reply thread is unavailable.")


def artifact_chat_id(origin: AgentTaskRecord, reply_episode_id: str | None) -> object:
    """The artifact's conversation: an orchestrator thread's is its chain root's branch."""
    return (
        origin.graph_target.branch_id
        if reply_episode_id is not None
        else origin.request.get("chat_id")
    )
