"""The member's episode controls, run for their Work chat through the human routes' functions."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from fastapi import HTTPException
from pydantic import ValidationError

from rcp.api.episode_routes import (
    _newest_chain_member,
    _resolved_auto_research_start_request,
    continue_episode_once,
    send_episode_message_as,
    start_auto_research_episode,
    stop_episode_as,
)
from rcp.api.episodes import (
    ContinueEpisodeBody,
    EpisodeMessageBody,
    StartEpisodeBody,
    serialize_episode,
)
from rcp.api.tasks import pause_task_as, resume_task_as
from rcp.background import BackgroundAgentTasks
from rcp.core.models import AuthorizedHuman
from rcp.keyed_locks import ExperimentAdmission, KeyedLocks
from rcp.projects import ProjectCatalog
from rcp.runs.agent_threads import task_owner
from rcp.runs.auto_research_admission import require_auto_research_provider_ready
from rcp.runs.membership_fence import fence_episodes_for_departed_member
from rcp.runs.work_episode_controls import EpisodeRefused, WorkAutoResearchStart, WorkEpisodeAct
from rcp.service import ProjectService
from rcp.storage import AgentTaskRecord, AutoResearchMessageRecord, EpisodeRecord
from rcp.storage.branch_names import BranchNameInvalid, BranchNameTaken


def _refused(exc: HTTPException, code: str) -> EpisodeRefused:
    """The member route's refusal, keeping its own code when it names one."""

    detail = exc.detail
    if isinstance(detail, dict):
        return EpisodeRefused(
            str(detail.get("code") or code), str(detail.get("message") or detail.get("code"))
        )
    return EpisodeRefused(code, str(detail))


@dataclass(frozen=True)
class MemberEpisodeControls:
    """Each control is the human route's function, authorized by the Work turn's member.

    Writes hold the project's write admission, as the routes' dependency does. A
    start probes provider readiness before taking it, since an uncached probe
    runs the provider CLI. ``tasks`` is late-bound: the engine is built with these
    controls.
    """

    catalog: ProjectCatalog
    tasks: Callable[[], BackgroundAgentTasks]
    operation_lock: KeyedLocks
    experiment_admission: ExperimentAdmission

    def _source(self, source_operation_id: str) -> tuple[AgentTaskRecord, AuthorizedHuman]:
        source = self.tasks().store.agent_task(source_operation_id)
        if source is None or source.authorized_by is None:
            raise EpisodeRefused(
                "authorizer_unavailable", "The Work turn that sent this has no human authorizer."
            )
        return source, source.authorized_by

    def _episode(self, act: WorkEpisodeAct) -> tuple[EpisodeRecord, AuthorizedHuman]:
        source, authorizer = self._source(act.source_operation_id)
        episode = self.tasks().store.episode(act.episode_id)
        if episode is None or episode.project_id != source.project_id:
            raise EpisodeRefused("episode_not_found", "No such episode in this project.")
        return episode, authorizer

    @contextmanager
    def _admitted(self, project_id: str, authorizer: AuthorizedHuman) -> Iterator[None]:
        store = self.tasks().store
        with self.operation_lock(project_id):
            store.require_project_accepts_new_work(project_id)
            if not store.is_project_member(project_id, authorizer.user_id):
                raise EpisodeRefused(
                    "member_departed",
                    "The human who sent this Work turn is no longer a project member.",
                )
            yield

    def _control_task(self, episode: EpisodeRecord, offered: str) -> AgentTaskRecord:
        """The turn the episode's Runs card offers ``offered`` on, as its button acts."""

        store = self.tasks().store
        current = _newest_chain_member(store, episode)
        projected = serialize_episode(
            store, current.project_id, current, include_graph_branch=False
        )
        task = (
            store.agent_task(projected.current_control_task_id)
            if projected.current_control_task_id is not None
            else None
        )
        if projected.task_control != offered or task is None:
            raise EpisodeRefused(
                f"episode_not_{offered}able",
                f"This episode's current turn cannot {offered} now.",
            )
        return task

    def start_auto_research(self, start: WorkAutoResearchStart) -> EpisodeRecord:
        tasks = self.tasks()
        store = tasks.store
        source, authorizer = self._source(start.source_operation_id)
        if source.graph_target.kind != "main":
            raise EpisodeRefused(
                "auto_research_starts_from_main",
                "Auto-research branches from main; start it from a chat on main.",
            )
        try:
            service = task_owner(store, source, catalog=self.catalog)
        except HTTPException as exc:
            raise _refused(exc, "auto_research_not_started") from exc
        if not isinstance(service, ProjectService):
            raise EpisodeRefused(
                "auto_research_not_started", "Space threads must hand off Auto-research starts."
            )
        try:
            body = StartEpisodeBody.model_validate(
                {
                    "mode": "auto_research",
                    "invocation_ceiling": start.invocation_ceiling,
                    "starting_instruction": start.goal,
                    "code_worktree": start.code_worktree,
                    "worktree_repository": start.worktree_repository,
                    "branch_name": start.branch_name,
                    "provider": start.provider,
                    "model": start.model,
                    "reasoning": start.reasoning,
                }
            )
            resolved = _resolved_auto_research_start_request(
                service, body, store=store, project_id=source.project_id
            ).model_copy(update={"started_from_operation_id": start.source_operation_id})
        except BranchNameInvalid as exc:
            raise EpisodeRefused("branch_name_invalid", str(exc)) from exc
        except ValueError as exc:
            raise EpisodeRefused("auto_research_not_started", str(exc)) from exc
        try:
            require_auto_research_provider_ready(service, resolved, member_id=authorizer.user_id)
        except ValueError as exc:
            raise EpisodeRefused(str(exc).split(":", 1)[0], str(exc)) from exc
        with self._admitted(source.project_id, authorizer):
            try:
                episode = start_auto_research_episode(
                    source.project_id,
                    resolved,
                    authorized_by=authorizer,
                    catalog=self.catalog,
                    background_tasks=tasks,
                    episode_id=start.episode_id,
                )
            except BranchNameInvalid as exc:
                raise EpisodeRefused("branch_name_invalid", str(exc)) from exc
            except BranchNameTaken as exc:
                raise EpisodeRefused("branch_name_taken", str(exc)) from exc
        if not store.is_project_member(source.project_id, authorizer.user_id):
            fence_episodes_for_departed_member(store, tasks, source.project_id, authorizer.user_id)
        return episode

    def stop(self, act: WorkEpisodeAct) -> None:
        episode, _ = self._episode(act)
        try:
            stop_episode_as(
                _newest_chain_member(self.tasks().store, episode),
                initiated_by=f"agent:{act.source_operation_id}",
                catalog=self.catalog,
                background_tasks=self.tasks(),
                operation_lock=self.operation_lock,
            )
        except HTTPException as exc:
            raise _refused(exc, "episode_not_stoppable") from exc

    def pause(self, act: WorkEpisodeAct) -> AgentTaskRecord:
        episode, _ = self._episode(act)
        try:
            return pause_task_as(self._control_task(episode, "pause"), self.tasks())
        except HTTPException as exc:
            raise _refused(exc, "episode_not_pausable") from exc

    def resume_turn(self, act: WorkEpisodeAct) -> AgentTaskRecord:
        episode, authorizer = self._episode(act)
        with self._admitted(episode.project_id, authorizer):
            try:
                return resume_task_as(
                    self._control_task(episode, "resume"),
                    authorizer,
                    catalog=self.catalog,
                    background_tasks=self.tasks(),
                    experiment_admission=self.experiment_admission,
                )
            except HTTPException as exc:
                raise _refused(exc, "episode_not_resumable") from exc

    def add_turns(
        self, act: WorkEpisodeAct, *, invocation_ceiling: int, request_id: str
    ) -> EpisodeRecord:
        episode, authorizer = self._episode(act)
        store = self.tasks().store
        while (newer := store.episode_continuation(episode.episode_id)) is not None:
            if newer.continuation_request_id == request_id:
                return newer
            episode = newer
        with self._admitted(episode.project_id, authorizer):
            try:
                continuation, _ = continue_episode_once(
                    episode,
                    ContinueEpisodeBody(
                        invocation_ceiling=invocation_ceiling, request_id=request_id
                    ),
                    authorized_by=authorizer,
                    catalog=self.catalog,
                    background_tasks=self.tasks(),
                )
            except HTTPException as exc:
                raise _refused(exc, "episode_not_continuable") from exc
        return continuation

    def message(
        self,
        act: WorkEpisodeAct,
        *,
        body: str,
        invocation_ceiling: int | None,
        request_id: str,
    ) -> AutoResearchMessageRecord:
        episode, authorizer = self._episode(act)
        try:
            message = EpisodeMessageBody(
                body=body, invocation_ceiling=invocation_ceiling, request_id=request_id
            )
        except ValidationError as exc:
            raise EpisodeRefused("episode_message_refused", str(exc)) from exc
        with self._admitted(episode.project_id, authorizer):
            try:
                return send_episode_message_as(
                    episode,
                    message,
                    authorized_by=authorizer,
                    catalog=self.catalog,
                    background_tasks=self.tasks(),
                )
            except HTTPException as exc:
                raise _refused(exc, "episode_message_refused") from exc
