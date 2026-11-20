"""Nonblocking question handling, called only after a concrete owner authorizes ask."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rcp.agents.agent_profile import (
    PRIVATE_PROJECT,
    SHARED_PROJECT,
    AgentProfile,
    AgentTurn,
    Placement,
    resolve_agent_profile,
)
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import (
    ApplyCommandRequest,
    AskCommandRequest,
    AskResult,
    BulletinCommandRequest,
    CommandRequest,
    CommandResponse,
    EpisodeCommandRequest,
    FlagCommandRequest,
    GraphDiffCommandRequest,
    GraphLogCommandRequest,
    NameCommandRequest,
    ProjectsCommandRequest,
    ScheduleCommandRequest,
    WatcherCommandRequest,
)
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.core.models import AuthorizedHuman
from rcp.limits import ASK_MAX_ATTEMPTS
from rcp.runs.evidence_flag_commands import handle_flag
from rcp.runs.graph_history_queries import handle_graph_history_command
from rcp.runs.work_experiment_commands import (
    handle_work_episode_command,
    member_work_chat_turn,
    work_experiment_allowed,
)
from rcp.runs.work_watcher_commands import handle_work_watcher_command
from rcp.storage import AgentTaskRecord, AppStore
from rcp.storage.agent_threads import bound_thread
from rcp.storage.question_models import (
    SPACE_QUESTION_CAPABILITY,
    QuestionArgumentConflict,
    QuestionOrigin,
    QuestionRecord,
)

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.runs.tasks.compute_commands import WorkComputeCommands
    from rcp.service import ProjectService, RunRequest
    from rcp.transport.workspace_mailbox import RunStageMailbox


def handle_ask(
    store: AppStore,
    request: AskCommandRequest,
    origin: QuestionOrigin,
    *,
    parked: bool = False,
    service: ProjectService | None = None,
    approval_card_member: AuthorizedHuman | None = None,
) -> CommandResponse:
    """Persist or read a question without granting authority or admitting follow-ups.

    The caller supplies a server-resolved origin after its own admission check.
    Repeated keys preserve the first origin even across turns. Parking only changes
    the outward pending state; resolution and confirmed client receipt are separate
    store operations. Producing this response does not prove the client received it.
    """
    try:
        if request.arguments.relays is not None:
            _check_relay(store, origin, request.arguments.relays)
        arguments = request.arguments.model_dump(exclude={"action"})
        card = None
        if request.arguments.action is not None:
            from rcp.runs.approval_cards import approval_card_for_ask

            card = approval_card_for_ask(store, request, origin, service, approval_card_member)
            arguments["choices"] = ["Approve", "Decline"]
        question = store.create_or_get_question(
            origin=origin,
            key=request.idempotency_key,
            **arguments,
            card=card,
        )
    except (QuestionArgumentConflict, ValueError) as exc:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message=str(exc),
        )
    return CommandResponse(
        request_id=request.request_id,
        status="ok",
        result=_ask_result(store, request, question, parked).model_dump(exclude_none=True),
    )


def _check_relay(store: AppStore, origin: QuestionOrigin, question_id: str) -> None:
    from rcp.runs.work_episode_controls import chat_started_episode

    relayed = store.get_question(question_id)
    task = store.agent_task(origin.operation_id)
    if (
        origin.owner_kind != "chat"
        or relayed is None
        or relayed.origin.owner_kind != "episode"
        or task is None
        or not chat_started_episode(store, task, relayed.origin.owner_id)
    ):
        raise ValueError("A relay names a question from a run this chat started.")


def _ask_result(
    store: AppStore, request: AskCommandRequest, question: QuestionRecord, parked: bool
) -> AskResult:
    # A parked reply ends the wait at once, so it is neither an attempt nor a live wait.
    withdrawn = question.state == "pending" and question.withdrawn_readonly
    pending = question.state == "pending" and not parked and not withdrawn
    attempt = None
    if pending:
        attempt = store.question_activity.pending(question.question_id, request.call_id)
    elif question.state == "answered":
        # The answer is in flight to the client until its receipt; keep that
        # visible so no steer delivers it a second time.
        store.question_activity.returned(question.question_id)
    else:
        # Resolved, or parked: either way no ask call is waiting on it now.
        store.question_activity.forget(question.question_id)
    answered = question.state == "answered"
    return AskResult(
        attempt=attempt,
        max_attempts=ASK_MAX_ATTEMPTS if pending else None,
        question_id=question.question_id,
        state="withdrawn"
        if withdrawn
        else "parked"
        if parked and question.state == "pending"
        else question.state,
        answer=question.answer if answered else None,
        choices=question.chosen_choices if answered else [],
    )


def work_ask_authorized(execution: AgentTaskExecution | None) -> bool:
    """Concrete human Work owners only; neither capability nor episode id suffices."""
    if execution is None:
        return False
    from rcp.runs.consolidation import is_consolidation

    if is_consolidation(execution):
        return False
    store = execution.store
    task = store.agent_task(execution.operation_id)
    if task is None or task.kind not in {"node_chat", "project_chat"}:
        return False
    authority = task.dispatch_authority
    if (
        authority is None
        or authority.profile != "ordinary"
        or authority.task_contract != "work_auto"
        or task.authorized_by is None
        or store.auto_research_child_work_for_operation(task.operation_id) is not None
    ):
        return False
    if authority.scope.patch_kind == "experiment_loop":
        episode_id = task.episode_id
        if episode_id is None:
            return False
        current = store.episode(episode_id)
        if (
            current is None
            or current.status not in {"queued", "running"}
            or current.stop_requested_at is not None
            or current.ending is not None
        ):
            return False
        seen = set()
        while episode_id is not None and episode_id not in seen:
            seen.add(episode_id)
            episode = store.episode(episode_id)
            if (
                episode is None
                or episode.mode != "experiment_loop"
                or episode.authorized_by is None
                or store.auto_research_child_experiment(episode_id) is not None
            ):
                return False
            episode_id = episode.continues_episode_id
        return episode_id is None
    return authority.is_work_chat


def discuss_ask_authorized(execution: AgentTaskExecution | None) -> bool:
    """Only human conversation Discuss turns may use the question broker."""
    if execution is None:
        return False
    task = execution.store.agent_task(execution.operation_id)
    if task is None or task.kind not in {"node_chat", "project_chat"}:
        return False
    authority = task.dispatch_authority
    return bool(
        authority is not None
        and authority.is_discuss_chat
        and task.request.get("mode") == "discuss"
        and task.authorized_by is not None
        and task.episode_id is None
        and task.write_scope_fingerprint is None
        and execution.store.auto_research_child_work_for_operation(task.operation_id) is None
    )


def discuss_command_handler(
    execution: AgentTaskExecution | None,
    *,
    service: ProjectService | None = None,
    graph_read_scope: GraphReadScope | None = None,
    ask_allowed: bool = True,
) -> WorkCommandHandler:
    """Discuss resolves questions and explicitly granted history reads."""
    from rcp.runs.agent_names import resolve_agent_name_state

    turn = AgentTurn(
        read_verbs=graph_read_scope.verbs if graph_read_scope is not None else frozenset(),
        can_self_name=bool(resolve_agent_name_state(execution).get("can_self_name")),
        ask=ask_allowed and discuss_ask_authorized(execution),
    )
    return WorkCommandHandler(
        execution,
        None,
        resolve_agent_profile(_placement(execution), "discuss", turn),
        service=service,
        graph_read_scope=graph_read_scope,
    )


def _placement(execution: AgentTaskExecution | None) -> Placement:
    if execution is None:
        return SHARED_PROJECT
    task = execution.store.agent_task(execution.operation_id)
    if task is not None and bound_thread(execution.store, task) is not None:
        return PRIVATE_PROJECT
    return SHARED_PROJECT


def question_origin(profile: AgentProfile, task: AgentTaskRecord) -> QuestionOrigin:
    """Resolve after the provider session checkpoint, never from command arguments.

    An unbound origin (a single-entry agent's approval card) resumes no session, so it
    binds no write scope.
    """
    authority = task.dispatch_authority
    if "ask" not in profile.verbs or (profile.binds_questions and authority is None):
        raise ValueError("This turn does not authorize human questions.")
    if authority is not None and profile.binds_questions:
        capability, chat_id = authority.task_contract, authority.scope.chat_id
    else:
        capability, chat_id = SPACE_QUESTION_CAPABILITY, task.request["chat_id"]
    unbound = capability in {"discuss", SPACE_QUESTION_CAPABILITY}
    if (
        not task.native_session_id
        or not task.stage_root
        or (not unbound and not task.write_scope_fingerprint)
    ):
        raise ValueError(
            "The asking turn has no complete native session and authority binding yet."
        )
    episode_id = task.episode_id if profile.episode_role == "experiment" else None
    return QuestionOrigin.model_validate(
        {
            "owner_kind": "episode" if episode_id else "chat",
            "project_id": task.project_id,
            "owner_id": episode_id or chat_id,
            "operation_id": task.operation_id,
            "provider": task.request.get("provider"),
            "native_session_id": task.native_session_id,
            "stage_root": task.stage_root,
            "stage_machine_id": task.stage_machine_id,
            "capability": capability,
            "write_scope_fingerprint": task.write_scope_fingerprint,
            "graph_target": task.graph_target,
        }
    )


def handle_space_ask(
    store: AppStore,
    request: AskCommandRequest,
    task: AgentTaskRecord,
    profile: AgentProfile,
    service: ProjectService | None,
) -> CommandResponse:
    """A single-entry turn asks only approval cards, owned by its agent under the Space scope.

    The same key in a later turn reads the outcome.
    """
    from rcp.runs.approval_cards import approval_card_member

    if request.arguments.action is None:
        return _refuse(request, "A Space ask raises an approval card; add --action.")
    try:
        origin = _reused_origin(
            store, question_origin(profile, task), request, bound=profile.binds_questions
        )
    except ValueError as exc:
        return _refuse(request, str(exc))
    return handle_ask(
        store,
        request,
        origin,
        service=service,
        approval_card_member=approval_card_member(store, task),
    )


def require_question_origin_binding(
    execution: AgentTaskExecution,
    request: RunRequest,
    origin: QuestionOrigin,
    *,
    stage_machine_id: str,
    stage_root: str | None,
    write_scope_fingerprint: str | None,
) -> None:
    """A follow-up may deliver its answer only under the asking turn's binding."""
    task = execution.store.agent_task(execution.operation_id)
    discuss = origin.capability == "discuss"
    if (
        task is None
        or task.dispatch_authority is None
        or task.dispatch_authority.task_contract != origin.capability
        or request.provider != origin.provider
        or task.request.get("provider") != origin.provider
        or request.session_id != origin.native_session_id
        or task.native_session_id not in {None, origin.native_session_id}
        or stage_machine_id != origin.stage_machine_id
        or stage_root != origin.stage_root
        or request.mode != ("discuss" if discuss else "work")
        or task.request.get("mode") != request.mode
        or task.graph_target != origin.graph_target
        or (
            discuss
            and (
                task.write_scope_fingerprint is not None
                or task.dispatch_authority.scope.patch_kind is not None
            )
        )
        or (not discuss and not write_scope_fingerprint)
    ):
        raise ValueError("question_origin_binding_unavailable")


def record_work_question_receipts(execution: AgentTaskExecution) -> None:
    """Client acknowledgement plus successful full settlement confirms receipt.

    The client echoes a token only after consuming the answered response. An offered
    response or successful task alone is insufficient. Missing acknowledgements,
    failed, paused, or disconnected turns retain follow-up eligibility; recovery
    applies the same rule from durable acknowledgement receipts.
    """
    record_settled_question_receipts(execution.store, execution.operation_id)


def record_settled_question_receipts(store: AppStore, operation_id: str) -> None:
    """Replay the same receipt rule after a crash between settlement and callback."""
    task = store.agent_task(operation_id)
    if task is None or task.status != "succeeded":
        return
    for receipt in store.agent_task_receipts_by_category(
        operation_id, "question_answer_acknowledged"
    ):
        payload = receipt.payload
        received = store.record_question_receipt(
            payload["question_id"],
            answer_revision=payload["answer_revision"],
            operation_id=operation_id,
            request_id=payload["request_id"],
        )
        if not received:
            store.record_question_continuation_receipt(
                payload["question_id"],
                answer_revision=payload["answer_revision"],
                operation_id=operation_id,
                request_id=payload["request_id"],
            )


def reconcile_question_receipt(store: AppStore, question_id: str) -> None:
    """Replay successful original or same-binding continuation receipt after restart."""
    for operation_id in store.question_receipt_candidate_operations(question_id):
        record_settled_question_receipts(store, operation_id)


# Every Work launch, an Auto-research child's included, stages and publishes a turn
# artifact directory, so its Bulletin --link can name a file written there.
WORK_TURN_ARTIFACTS = True


@dataclass(frozen=True)
class WorkCommandHandler:
    """Dispatch one owner's resolved verbs; questions share no compute authority."""

    execution: AgentTaskExecution | None
    compute_commands: WorkComputeCommands | None
    profile: AgentProfile
    consolidation_apply: (
        Callable[[ApplyCommandRequest, CommandTurnIdentity], CommandResponse] | None
    ) = None

    service: ProjectService | None = None
    graph_read_scope: GraphReadScope | None = None
    mailbox: RunStageMailbox | None = None
    open_service: Callable[[], ProjectService] | None = None
    #: Whether Bulletin --link may name this turn's artifact files; the usage renders it.
    turn_artifacts: bool = WORK_TURN_ARTIFACTS
    #: The member an `ask` may present an approval card to; the usage renders it.
    approval_card_member: AuthorizedHuman | None = None

    @property
    def allowed_verbs(self) -> frozenset[str]:
        return self.profile.verbs

    def __call__(self, request: CommandRequest, identity: CommandTurnIdentity) -> CommandResponse:
        graph_history = isinstance(request, (GraphDiffCommandRequest, GraphLogCommandRequest))
        if request.verb not in self.allowed_verbs:
            if request.verb in _CARD_ROUTES and self.approval_card_member is not None:
                return _refuse(request, _CARD_ROUTES[request.verb], "approval_card_required")
            return _not_granted(request, graph_history, self.allowed_verbs)
        if request.verb == "projects":
            return self._projects(request, identity)
        if graph_history:
            return self._graph_history(request, identity)
        if isinstance(request, ApplyCommandRequest) and self.consolidation_apply is not None:
            return self.consolidation_apply(request, identity)
        brokered = next((kind for kind in _BROKER_VERBS if isinstance(request, kind)), None)
        if brokered is not None:
            return self._brokered(_BROKER_VERBS[brokered], request, identity)
        if self.compute_commands is not None:
            return self.compute_commands(request, identity)
        return _refuse(request, "This turn does not authorize that command.")

    def _brokered(
        self,
        verb: tuple[Callable[..., CommandResponse], str, str | None],
        request: CommandRequest,
        identity: CommandTurnIdentity,
    ) -> CommandResponse:
        handle, refusal, code = verb
        execution = self._broker_execution(identity)
        if execution is None:
            return _refuse(request, refusal, code)
        return handle(self, execution, request)

    def _broker_execution(self, identity: CommandTurnIdentity) -> AgentTaskExecution | None:
        """This turn's execution, when the caller holds its broker authority."""
        execution = self.execution
        if (
            execution is None
            or identity.authority != "broker"
            or identity.task_id != execution.operation_id
        ):
            return None
        return execution

    def _projects(self, request: CommandRequest, identity: CommandTurnIdentity) -> CommandResponse:
        """A shared agent reads the projects of the member who authorized its turn."""
        from rcp.runs.agent_thread_commands import project_details, projects_projection

        assert isinstance(request, ProjectsCommandRequest)
        execution = self._broker_execution(identity)
        member_id = _turn_member(execution) if execution is not None else None
        service = self.service or (self.open_service() if self.open_service else None)
        if (
            execution is None
            or member_id is None
            or service is None
            or service.project_catalog is None
        ):
            return _refuse(request, "Project listing is unavailable for this turn.")
        store, catalog = execution.store, service.project_catalog
        result: dict[str, object]
        try:
            if request.arguments.action == "list":
                result = {"projects": projects_projection(store, catalog, member_id)}
            else:
                result = project_details(
                    store, catalog, member_id, request.arguments.project_id or ""
                )
        except (ValueError, KeyError, OSError) as exc:
            return _refuse(request, str(exc))
        return CommandResponse(request_id=request.request_id, status="ok", result=result)

    def _graph_history(
        self,
        request: GraphDiffCommandRequest | GraphLogCommandRequest,
        identity: CommandTurnIdentity,
    ) -> CommandResponse:
        if self.graph_read_scope is None or self.service is None:
            return _refuse(
                request, "This turn has no graph history scope.", "graph_history_scope_required"
            )
        if self.execution is not None and identity.task_id != self.execution.operation_id:
            return _refuse(request, "Graph history requires this turn's broker authority.")
        return handle_graph_history_command(
            self.service,
            self.graph_read_scope,
            request,
            resolve_project=self.service.resolve_project_service,
        )

    def _flag(self, execution: AgentTaskExecution, request: FlagCommandRequest) -> CommandResponse:
        if self.service is None:
            return _refuse(
                request, "Evidence flags require this consolidation turn's broker authority."
            )
        return handle_flag(self.service, execution.store, execution.operation_id, request)

    def _schedule(
        self, execution: AgentTaskExecution, request: ScheduleCommandRequest
    ) -> CommandResponse:
        from rcp.runs.agent_schedules import handle_schedule

        return handle_schedule(execution.store, execution.operation_id, request)

    def _name(self, execution: AgentTaskExecution, request: NameCommandRequest) -> CommandResponse:
        from rcp.runs.agent_names import handle_name

        return handle_name(execution.store, execution.operation_id, request)

    def _bulletin(
        self, execution: AgentTaskExecution, request: BulletinCommandRequest
    ) -> CommandResponse:
        from rcp.runs.bulletin import handle_bulletin

        return handle_bulletin(
            execution.store,
            execution.operation_id,
            request,
            post_allowed=True,
            turn_artifacts=self.turn_artifacts,
            mailbox=self.mailbox,
        )

    def _episode(
        self, execution: AgentTaskExecution, request: EpisodeCommandRequest
    ) -> CommandResponse:
        return handle_work_episode_command(execution, self.mailbox, request)

    def _watcher(
        self, execution: AgentTaskExecution, request: WatcherCommandRequest
    ) -> CommandResponse:
        return handle_work_watcher_command(execution, request)

    def _ask(self, execution: AgentTaskExecution, request: AskCommandRequest) -> CommandResponse:
        task = execution.store.agent_task(execution.operation_id)
        try:
            if task is None or not (
                work_ask_authorized(execution) or discuss_ask_authorized(execution)
            ):
                raise ValueError("This turn does not authorize human questions.")
            current = question_origin(self.profile, task)
            origin = _reused_origin(execution.store, current, request)
        except ValueError as exc:
            return _refuse(request, str(exc))
        # Maintenance closes the human's access before it drains turns, so a
        # waiting ask would hold the drain on a human it locked out.
        maintenance = execution.admission is not None and execution.admission.closed
        response = handle_ask(
            execution.store,
            request,
            origin,
            parked=maintenance,
            service=self.service,
            approval_card_member=self.approval_card_member,
        )
        if maintenance and response.result.get("state") == "parked":
            response.message = (
                "Server maintenance is under way, so the human cannot answer now. "
                "End your turn; the question stays on their card and their answer "
                "resumes this conversation."
            )
        if _episode_stopped(execution.store, current, origin):
            return _refuse(request, "question_episode_ended")
        if response.status == "ok" and response.result.get("state") == "answered":
            try:
                response.result["receipt_token"] = _answer_receipt_token(
                    execution, request, str(response.result["question_id"])
                )
            except ValueError as exc:
                return _refuse(request, str(exc))
        return response


_BROKER_VERBS: dict[type, tuple[Callable[..., CommandResponse], str, str | None]] = {
    FlagCommandRequest: (
        WorkCommandHandler._flag,
        "Evidence flags require this consolidation turn's broker authority.",
        None,
    ),
    ScheduleCommandRequest: (
        WorkCommandHandler._schedule,
        "Schedules require this turn's broker authority.",
        None,
    ),
    NameCommandRequest: (
        WorkCommandHandler._name,
        "Naming requires this turn's broker authority.",
        "name_task_unavailable",
    ),
    BulletinCommandRequest: (
        WorkCommandHandler._bulletin,
        "The Bulletin requires this turn's task binding.",
        "bulletin_task_unavailable",
    ),
    EpisodeCommandRequest: (
        WorkCommandHandler._episode,
        "Episode commands require this Work turn's broker authority.",
        None,
    ),
    WatcherCommandRequest: (
        WorkCommandHandler._watcher,
        "Watcher control requires this Work turn's broker authority.",
        None,
    ),
    AskCommandRequest: (
        WorkCommandHandler._ask,
        "Questions require this turn's broker authority.",
        None,
    ),
}

#: Origin fields an answer never crosses: a different session or authority binding.
_QUESTION_BINDING_FIELDS = {
    "provider",
    "native_session_id",
    "stage_root",
    "stage_machine_id",
    "capability",
    "write_scope_fingerprint",
    "graph_target",
}


_CARD_ROUTES: dict[str, str] = {
    "merge": "Only the member merges a branch. Use ask with a merge_branch or merge_episode "
    "action; the member's Approve tap runs the merge.",
}


def _not_granted(
    request: CommandRequest, graph_history: bool, allowed_verbs: frozenset[str]
) -> CommandResponse:
    code = "graph_history_verb_not_granted" if graph_history else "verb_not_granted"
    refused = _refuse(request, "This turn does not authorize that command.", code)
    refused.result["allowed_verbs"] = sorted(allowed_verbs)
    return refused


def _refuse(request: CommandRequest, message: str, code: str | None = None) -> CommandResponse:
    return CommandResponse(
        request_id=request.request_id,
        status="invalid",
        message=message,
        result={"code": code} if code else {},
    )


def _reused_origin(
    store: AppStore, origin: QuestionOrigin, request: AskCommandRequest, *, bound: bool = True
) -> QuestionOrigin:
    """Keys survive turns and episode continuation, keeping their first origin.

    An unbound origin's answer resumes no session, so a later session of the same
    owner may read it.
    """
    owner_ids = (
        store.experiment_question_owner_ids(origin.owner_id)
        if origin.owner_kind == "episode"
        else [origin.owner_id]
    )
    for owner_id in owner_ids:
        previous = next(
            (
                item
                for item in store.list_questions(
                    project_id=origin.project_id, owner_kind=origin.owner_kind, owner_id=owner_id
                )
                if item.key == request.idempotency_key
            ),
            None,
        )
        if previous is None:
            continue
        if bound and previous.origin.model_dump(
            include=_QUESTION_BINDING_FIELDS
        ) != origin.model_dump(include=_QUESTION_BINDING_FIELDS):
            raise ValueError("question_origin_binding_unavailable")
        return previous.origin
    return origin


def _episode_stopped(store: AppStore, current: QuestionOrigin, origin: QuestionOrigin) -> bool:
    """Close the create-vs-Stop race: a fence before insertion did not see this card.

    `current` is this turn's episode; `origin` the question's first one. A later
    fence withdraws it itself. Other endings keep the card; its answer continues
    the episode.
    """
    if current.owner_kind != "episode":
        return False
    episode = store.episode(current.owner_id)
    if episode is not None and episode.stop_requested_at is None and episode.ending != "stopped":
        return False
    store.set_episode_questions_withdrawn(origin.project_id, origin.owner_id, withdrawn=True)
    return True


def _answer_receipt_token(
    execution: AgentTaskExecution, request: AskCommandRequest, question_id: str
) -> str:
    """Offer each answer revision one receipt token; a returned token acknowledges it."""
    store = execution.store
    question = store.get_question(question_id)
    assert question is not None
    offered: dict[str, Any] | None = next(
        (
            item.payload
            for item in store.agent_task_receipts_by_category(
                execution.operation_id, "question_answer_offered"
            )
            if item.payload.get("question_id") == question.question_id
            and item.payload.get("answer_revision") == question.answer_revision
            and item.payload.get("receipt_token")
        ),
        None,
    )
    if request.receipt_token is not None:
        if offered is None or not secrets.compare_digest(
            request.receipt_token, str(offered["receipt_token"])
        ):
            raise ValueError("question_receipt_mismatch")
        _acknowledge(execution, request.receipt_token, offered)
    if offered is None:
        offered = {
            "question_id": question.question_id,
            "answer_revision": question.answer_revision,
            "request_id": request.request_id,
            "receipt_token": secrets.token_hex(32),
        }
        store.record_agent_task_receipt(
            execution.operation_id, "question_answer_offered", offered, tier="diagnostic"
        )
    return str(offered["receipt_token"])


def _acknowledge(execution: AgentTaskExecution, token: str, offered: dict) -> None:
    store = execution.store
    if not any(
        item.payload.get("receipt_token") == token
        for item in store.agent_task_receipts_by_category(
            execution.operation_id, "question_answer_acknowledged"
        )
    ):
        store.record_agent_task_receipt(
            execution.operation_id, "question_answer_acknowledged", offered, tier="diagnostic"
        )


def _turn_member(execution: AgentTaskExecution) -> str | None:
    """The member a turn acts for: its private agent's owner, else its authorizer."""
    store = execution.store
    task = store.agent_task(execution.operation_id)
    thread = bound_thread(store, task) if task is not None else None
    if thread is not None:
        return None if thread.closed_at else thread.user_id
    authorizer = store.agent_task_authorizer(execution.operation_id)
    return authorizer.user_id if authorizer is not None else None


def work_command_handler(
    execution: AgentTaskExecution | None,
    compute_commands: WorkComputeCommands | None,
    *,
    consolidation_apply: Callable[[ApplyCommandRequest, CommandTurnIdentity], CommandResponse]
    | None = None,
    service: ProjectService | None = None,
    graph_read_scope: GraphReadScope | None = None,
    mailbox: RunStageMailbox | None = None,
    ask_allowed: bool = True,
    open_service: Callable[[], ProjectService] | None = None,
) -> WorkCommandHandler:
    """Resolve one Work turn's handler; a resumed turn opens its service only on use."""
    from rcp.runs.agent_names import resolve_agent_name_state
    from rcp.runs.agent_schedules import schedule_thread
    from rcp.runs.consolidation import is_consolidation

    read_verbs = graph_read_scope.verbs if graph_read_scope is not None else frozenset()
    placement = _placement(execution)
    if is_consolidation(execution):
        return WorkCommandHandler(
            execution,
            None,
            resolve_agent_profile(placement, "consolidation", AgentTurn(read_verbs=read_verbs)),
            consolidation_apply,
            service,
            graph_read_scope,
            mailbox,
        )
    task = execution.store.agent_task(execution.operation_id) if execution is not None else None
    asks = work_ask_authorized(execution)
    turn = AgentTurn(
        read_verbs=read_verbs,
        launch=compute_commands is not None and compute_commands.launch_available,
        can_self_name=bool(resolve_agent_name_state(execution).get("can_self_name")),
        bulletin=execution is not None
        and task is not None
        and task.dispatch_authority is not None
        and task.dispatch_authority.task_contract == "work_auto"
        and execution.store.bulletin_enabled_for_task(execution.operation_id),
        ask=ask_allowed and asks,
        member_turn=member_work_chat_turn(execution),
        episode_start=work_experiment_allowed(execution),
        projects=execution is not None
        and (
            (service is not None and service.project_catalog is not None)
            or open_service is not None
        )
        and _turn_member(execution) is not None,
        schedule=execution is not None
        and schedule_thread(execution.store, execution.operation_id) is not None,
    )
    experiment = (
        task is not None
        and task.episode_id is not None
        and task.dispatch_authority is not None
        and task.dispatch_authority.scope.patch_kind == "experiment_loop"
    )
    card_member = None
    if asks and task is not None:
        from rcp.runs.approval_cards import approval_card_member

        assert execution is not None
        card_member = approval_card_member(execution.store, task)
    return WorkCommandHandler(
        execution,
        compute_commands,
        resolve_agent_profile(placement, "experiment" if experiment else None, turn),
        service=service,
        graph_read_scope=graph_read_scope,
        mailbox=mailbox,
        approval_card_member=card_member,
        open_service=open_service,
    )
