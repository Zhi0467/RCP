from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace

import pytest

from rcp.agents.command_mailbox import serve_command_mailbox, stage_command_mailbox
from rcp.agents.command_protocol import (
    CommandResponse,
    FinishCommandRequest,
    MessageArguments,
    MessageCommandRequest,
    PauseCommandRequest,
    ResumeCommandRequest,
    SpawnArguments,
    SpawnCommandRequest,
    StatusCommandRequest,
    StopCommandRequest,
    ValidateCommandRequest,
    WatchGraphCommandRequest,
)
from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope
from rcp.core.models import AuthorizedHuman
from rcp.runs.campaign import (
    CampaignCommandDispatcher,
    CampaignCommandEffectResult,
    CampaignCommandEffects,
    CampaignRunRequest,
)
from rcp.storage import AgentTaskRecord, AppStore, CampaignRecord, ProjectRecord

MAILBOX_ID = "a" * 32
CREDENTIAL = "b" * 64
_RUN_TRUTH_SCOPE = ["repo-a"]


def _campaign_authority(campaign_id: str, role: str) -> AgentDispatchAuthority:
    return AgentDispatchAuthority(
        profile="orchestrator" if role == "orchestrator" else "ordinary",
        task_contract="orchestrate" if role == "orchestrator" else "work_auto",
        scope=AgentDispatchScope(
            run_truth_scope=_RUN_TRUTH_SCOPE,
            campaign_id=campaign_id,
            patch_kind="work",
        ),
    )


def _setup_campaign(tmp_path) -> tuple[AppStore, CampaignRecord, AgentTaskRecord]:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator="/tmp/project/research.yaml",
            name="project",
            state_location="/tmp/project/.research",
            state_remote=False,
            added_at=store.now(),
        )
    )
    authorizer = AuthorizedHuman(
        space_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        display_name="Campaign owner",
    )
    now = store.now()
    root_request = CampaignRunRequest(
        campaign_id="campaign",
        role="orchestrator",
        actor_operation_id="root",
        run_truth_scope=_RUN_TRUTH_SCOPE,
    )
    return (
        store,
        *store.create_campaign_with_root_task(
            CampaignRecord(
                campaign_id="campaign",
                project_id="project",
                status="queued",
                invocation_ceiling=8,
                authorized_by=authorizer,
                created_at=now,
                updated_at=now,
            ),
            AgentTaskRecord(
                operation_id="root",
                project_id="project",
                campaign_id="campaign",
                kind="campaign",
                status="succeeded",
                request=root_request.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
                status_message="done",
                authorized_by=authorizer,
                dispatch_authority=_campaign_authority("campaign", "orchestrator"),
            ),
        ),
    )


def _worker(
    store: AppStore,
    campaign: CampaignRecord,
    root: AgentTaskRecord,
    operation_id: str,
    *,
    seat_node_id: str = "exp/check",
    instruction: str = "Run the check.",
) -> AgentTaskRecord:
    request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role="worker",
        actor_operation_id=operation_id,
        run_truth_scope=_RUN_TRUTH_SCOPE,
        control_node_id=seat_node_id,
        instruction=instruction,
    )
    now = store.now()
    return store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="queued",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="queued",
            parent_operation_id=root.operation_id,
            authorized_by=campaign.authorized_by,
            dispatch_authority=_campaign_authority(campaign.campaign_id, "worker"),
        ),
        role="worker",
    )


def _orchestrator_turn(
    store: AppStore,
    campaign: CampaignRecord,
    root: AgentTaskRecord,
    *,
    operation_id: str,
) -> AgentTaskRecord:
    request = CampaignRunRequest(
        campaign_id=campaign.campaign_id,
        role="orchestrator",
        actor_operation_id=root.operation_id,
        run_truth_scope=_RUN_TRUTH_SCOPE,
    )
    now = store.now()
    return store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="succeeded",
            request=request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="done",
            parent_operation_id=root.operation_id,
            authorized_by=campaign.authorized_by,
            dispatch_authority=_campaign_authority(campaign.campaign_id, "orchestrator"),
        ),
        role="orchestrator",
    )


@dataclass
class _Effects:
    store: AppStore
    campaign: CampaignRecord
    root: AgentTaskRecord
    seat_type: str | None = "experiment"
    spawn_calls: list[tuple[SpawnArguments, str]] = field(default_factory=list)
    message_calls: list[MessageArguments] = field(default_factory=list)
    planned_message_ids: list[str] = field(default_factory=list)
    planned_watcher_ids: list[str] = field(default_factory=list)
    reconcile_calls: list[str] = field(default_factory=list)
    reconcile_planned_effect_ids: list[str | None] = field(default_factory=list)
    reconcile_result: CampaignCommandEffectResult | None = None
    finish_calls: int = 0

    def bundle(self) -> CampaignCommandEffects:
        return CampaignCommandEffects(
            validate=lambda _context, _arguments: CampaignCommandEffectResult(),
            status=lambda _context, _arguments: CampaignCommandEffectResult(),
            spawn=self.spawn,
            pause=lambda _context, _worker_id: CampaignCommandEffectResult(),
            resume=lambda _context, _worker_id: CampaignCommandEffectResult(),
            stop=lambda _context, _worker_id: CampaignCommandEffectResult(),
            message=self.message,
            watch_graph=self.watch_graph,
            finish=self.finish,
            seat_node_type=lambda _project_id, _node_id: self.seat_type,
            reconcile_unknown=self.reconcile_unknown,
        )

    def spawn(
        self,
        _context,
        arguments: SpawnArguments,
        planned_worker_id: str,
    ) -> CampaignCommandEffectResult:
        self.spawn_calls.append((arguments, planned_worker_id))
        worker = _worker(
            self.store,
            self.campaign,
            self.root,
            planned_worker_id,
            seat_node_id=arguments.seat_node_id,
            instruction=arguments.instruction,
        )
        return CampaignCommandEffectResult(
            result={"worker_id": worker.operation_id, "disposition": "created"}
        )

    def message(
        self,
        _context,
        arguments: MessageArguments,
        planned_message_id: str,
    ) -> CampaignCommandEffectResult:
        self.message_calls.append(arguments)
        self.planned_message_ids.append(planned_message_id)
        return CampaignCommandEffectResult(result={"delivered": True})

    def watch_graph(
        self,
        _context,
        _arguments,
        planned_watcher_id: str,
    ) -> CampaignCommandEffectResult:
        self.planned_watcher_ids.append(planned_watcher_id)
        return CampaignCommandEffectResult(result={"armed": True})

    def reconcile_unknown(
        self,
        _context,
        request,
        planned_effect_id: str | None,
    ) -> CampaignCommandEffectResult | None:
        self.reconcile_calls.append(request.verb)
        self.reconcile_planned_effect_ids.append(planned_effect_id)
        return self.reconcile_result

    def finish(self, _context) -> CampaignCommandEffectResult:
        self.finish_calls += 1
        campaign = self.store.begin_campaign_wrapup(self.campaign.campaign_id, "completed")
        return CampaignCommandEffectResult(
            result={"campaign_id": campaign.campaign_id, "ending": campaign.ending}
        )


def _spawn_request(
    request_id: str,
    *,
    key: str | None,
    seat_node_id: str = "exp/check",
) -> SpawnCommandRequest:
    return SpawnCommandRequest(
        mailbox_id=MAILBOX_ID,
        request_id=request_id,
        credential=CREDENTIAL,
        verb="spawn",
        idempotency_key=key,
        arguments={
            "seat_node_id": seat_node_id,
            "instruction": "Inspect everything needed to settle the seat.",
        },
    )


def test_mutating_command_requires_caller_idempotency_key_and_records_the_exit(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())

    response = dispatcher.dispatch(root.operation_id, _spawn_request("1" * 32, key=None))

    assert response.status == "invalid"
    assert response.exit_code == 1
    assert "idempotency key" in (response.message or "")
    assert effects.spawn_calls == []
    invocation = store.agent_command("1" * 32)
    assert invocation is not None
    assert invocation.started_at
    assert invocation.exited_at is not None
    assert invocation.status == "invalid"


def test_finish_is_orchestrator_only_idempotent_and_fences_later_work(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())
    spawned = dispatcher.dispatch(
        root.operation_id,
        _spawn_request("a" * 32, key="spawn-before-finish"),
    )
    assert spawned.status == "ok"
    worker_id = str(spawned.result["worker_id"])
    unknown_key = "unknown-spawn-before-finish"
    unknown_request = _spawn_request("3" * 32, key=unknown_key)
    unknown_worker_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:campaign:{campaign.campaign_id}:spawn:{unknown_key}",
        )
    )
    store.start_agent_command(
        operation_id=root.operation_id,
        command_id=unknown_request.request_id,
        campaign_id=campaign.campaign_id,
        verb=unknown_request.verb,
        idempotency_key=unknown_key,
        payload={
            "request_id": unknown_request.request_id,
            "arguments": unknown_request.arguments.model_dump(mode="json"),
            "planned_worker_id": unknown_worker_id,
        },
    )
    request = FinishCommandRequest(
        mailbox_id=MAILBOX_ID,
        request_id="f" * 32,
        credential=CREDENTIAL,
        verb="finish",
        idempotency_key="finish-once",
    )

    first = dispatcher.dispatch(root.operation_id, request)
    replay = dispatcher.dispatch(
        root.operation_id,
        request.model_copy(update={"request_id": "0" * 32}),
    )
    replayed_spawn = dispatcher.dispatch(
        root.operation_id,
        _spawn_request("b" * 32, key="spawn-before-finish"),
    )
    unknown_spawn = dispatcher.dispatch(
        root.operation_id,
        unknown_request.model_copy(update={"request_id": "4" * 32}),
    )

    denied = [
        dispatcher.dispatch(
            root.operation_id,
            _spawn_request("c" * 32, key="spawn-after-finish"),
        ),
        dispatcher.dispatch(
            root.operation_id,
            MessageCommandRequest(
                mailbox_id=MAILBOX_ID,
                request_id="d" * 32,
                credential=CREDENTIAL,
                verb="message",
                idempotency_key="message-after-finish",
                arguments={
                    "recipient_task_id": worker_id,
                    "body": "This must not cross the ending fence.",
                },
            ),
        ),
        dispatcher.dispatch(
            root.operation_id,
            WatchGraphCommandRequest(
                mailbox_id=MAILBOX_ID,
                request_id="e" * 32,
                credential=CREDENTIAL,
                verb="watch_graph",
                idempotency_key="watch-after-finish",
                arguments={
                    "condition": {"node_id": "hyp/result", "status_in": ["active"]},
                    "reason": "This must not cross the ending fence.",
                },
            ),
        ),
    ]
    status = dispatcher.dispatch(
        root.operation_id,
        StatusCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="1" * 32,
            credential=CREDENTIAL,
            verb="status",
        ),
    )
    validation = dispatcher.dispatch(
        root.operation_id,
        ValidateCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="2" * 32,
            credential=CREDENTIAL,
            verb="validate",
            arguments={"patch": '{"summary":"read only","ops":[]}'},
        ),
    )

    assert first.status == replay.status == "ok"
    assert (
        first.result
        == replay.result
        == {
            "campaign_id": campaign.campaign_id,
            "ending": "completed",
        }
    )
    assert effects.finish_calls == 1
    assert replayed_spawn.status == "ok"
    assert replayed_spawn.result == {
        "worker_id": worker_id,
        "status": "queued",
        "disposition": "existing",
    }
    assert unknown_spawn.status == "unavailable"
    assert unknown_spawn.message == "The campaign is no longer accepting mutating commands."
    assert store.agent_task(unknown_worker_id) is None
    assert all(response.status == "unavailable" for response in denied)
    assert all(
        response.message == "The campaign is no longer accepting mutating commands."
        for response in denied
    )
    assert len(effects.spawn_calls) == 1
    assert effects.message_calls == []
    assert effects.planned_watcher_ids == []
    assert status.status == validation.status == "ok"
    fenced = store.campaign(campaign.campaign_id)
    assert fenced is not None
    assert (fenced.status, fenced.ending) == ("wrapping_up", "completed")
    with pytest.raises(ValueError, match="not admitting new work"):
        _orchestrator_turn(
            store,
            campaign,
            root,
            operation_id="too-late",
        )
    invocation = store.agent_command_by_key(campaign.campaign_id, "finish-once")
    assert invocation is not None
    assert invocation.exited_at is not None
    assert invocation.status == "ok"
    for request_id, expected_status in {
        "b" * 32: "ok",
        "3" * 32: "unavailable",
        "4" * 32: "unavailable",
        "c" * 32: "unavailable",
        "d" * 32: "unavailable",
        "e" * 32: "unavailable",
        "1" * 32: "ok",
        "2" * 32: "ok",
    }.items():
        audited = store.agent_command(request_id)
        assert audited is not None
        assert audited.exited_at is not None
        assert audited.status == expected_status


def test_stop_intent_fences_new_mutating_commands_before_effect_execution(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    _worker(store, campaign, root, "active-worker")
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())

    stopping = store.request_campaign_stop(campaign.campaign_id)
    response = dispatcher.dispatch(
        root.operation_id,
        _spawn_request("5" * 32, key="spawn-after-stop"),
    )

    assert stopping.status == "stopping"
    assert stopping.stop_requested_at is not None
    assert response.status == "unavailable"
    assert response.message == "The campaign is no longer accepting mutating commands."
    assert effects.spawn_calls == []
    invocation = store.agent_command("5" * 32)
    assert invocation is not None
    assert invocation.exited_at is not None
    assert invocation.status == "unavailable"


@pytest.mark.asyncio
async def test_campaign_mailbox_audits_authenticated_mutation_without_key(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())
    workspace = tmp_path / "stage"
    workspace.mkdir()
    staged = stage_command_mailbox(
        local_stage=workspace,
        remote_stage=None,
        campaign_id=campaign.campaign_id,
        task_id=root.operation_id,
        turn_id="turn",
        timeout_seconds=2,
    )
    request_id = "6" * 32
    request = _spawn_request(request_id, key=None).model_copy(
        update={
            "mailbox_id": staged.credential.mailbox_id,
            "credential": staged.credential.token,
        }
    )
    request_name = f"rcp-command-{staged.credential.mailbox_id}-{request_id}.request.json"
    staged.mailbox.write_text(request_name, request.model_dump_json() + "\n")
    handled = asyncio.Event()

    def handler(parsed, identity):
        assert identity.campaign_id == campaign.campaign_id
        assert identity.task_id == root.operation_id
        handled.set()
        return dispatcher.dispatch(identity.task_id, parsed)

    stop = asyncio.Event()
    server = asyncio.create_task(
        serve_command_mailbox(
            staged=staged,
            handler=handler,
            stop=stop,
            poll_seconds=0.01,
        )
    )
    await asyncio.wait_for(handled.wait(), timeout=2)
    stop.set()
    await server

    response_name = request_name.removesuffix(".request.json") + ".response.json"
    response = json.loads(staged.mailbox.read_text(response_name))
    assert response["status"] == "invalid"
    assert "idempotency key" in response["message"]
    assert effects.spawn_calls == []
    invocation = store.agent_command(request_id)
    assert invocation is not None
    assert invocation.idempotency_key is None
    assert invocation.status == "invalid"
    assert invocation.exited_at is not None
    staged.cleanup()


def test_large_validation_records_patch_identity_instead_of_patch_bytes(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    dispatcher = CampaignCommandDispatcher(store, _Effects(store, campaign, root).bundle())
    patch = '{"summary":"large","ops":[],"padding":"' + ("x" * 64_000) + '"}'

    response = dispatcher.dispatch(
        root.operation_id,
        ValidateCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="9" * 32,
            credential=CREDENTIAL,
            verb="validate",
            arguments={"patch": patch},
        ),
    )

    assert response.status == "ok"
    invocation = store.agent_command("9" * 32)
    assert invocation is not None
    assert invocation.start_payload["arguments"] == {
        "patch_byte_length": len(patch.encode("utf-8")),
        "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
    }


def test_command_result_must_fit_the_durable_event_ledger() -> None:
    with pytest.raises(ValueError, match="event ledger limit"):
        CampaignCommandEffectResult(result={"too_large": "x" * 40_000})


def test_status_worker_id_is_normalized_and_bounded_before_durable_start(tmp_path) -> None:
    request = StatusCommandRequest(
        mailbox_id=MAILBOX_ID,
        request_id="8" * 32,
        credential=CREDENTIAL,
        verb="status",
        arguments={"worker_id": "  worker  "},
    )
    assert request.arguments.worker_id == "worker"

    store, campaign, root = _setup_campaign(tmp_path)
    dispatcher = CampaignCommandDispatcher(store, _Effects(store, campaign, root).bundle())
    with pytest.raises(ValueError, match="at most 200 characters"):
        oversized = StatusCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="7" * 32,
            credential=CREDENTIAL,
            verb="status",
            arguments={"worker_id": "x" * 201},
        )
        dispatcher.dispatch(root.operation_id, oversized)
    assert store.agent_command("7" * 32) is None


def test_spawn_seat_is_bounded_but_worker_request_gets_no_mechanical_scope(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    effects = _Effects(store, campaign, root, seat_type="evidence")
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())

    refused = dispatcher.dispatch(
        root.operation_id,
        _spawn_request("2" * 32, key="seat-evidence", seat_node_id="ev/result"),
    )
    assert refused.status == "invalid"
    assert "Experiments and Blockers" in (refused.message or "")
    assert effects.spawn_calls == []

    effects.seat_type = "blocker"
    accepted = dispatcher.dispatch(
        root.operation_id,
        _spawn_request("3" * 32, key="seat-blocker", seat_node_id="blocker/input"),
    )
    assert accepted.status == "ok"
    assert len(effects.spawn_calls) == 1
    worker_id = str(accepted.result["worker_id"])
    worker = store.agent_task(worker_id)
    assert worker is not None
    assert worker.parent_operation_id == root.operation_id
    assert worker.authorized_by == campaign.authorized_by
    assert worker.request["control_node_id"] == "blocker/input"
    assert "scope" not in worker.request


def test_interrupted_successful_spawn_reconciles_existing_worker_without_restart(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())
    key = "dangerous-spawn"
    first_request_id = "4" * 32
    retry_request_id = "5" * 32
    planned_worker_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:campaign:{campaign.campaign_id}:spawn:{key}",
        )
    )
    arguments = SpawnArguments(
        seat_node_id="exp/check",
        instruction="Run the check exactly once.",
    )
    store.start_agent_command(
        operation_id=root.operation_id,
        command_id=first_request_id,
        campaign_id=campaign.campaign_id,
        verb="spawn",
        idempotency_key=key,
        payload={
            "request_id": first_request_id,
            "arguments": arguments.model_dump(mode="json"),
            "planned_worker_id": planned_worker_id,
        },
    )
    existing = _worker(
        store,
        campaign,
        root,
        planned_worker_id,
        instruction=arguments.instruction,
    )
    assert store.agent_command(first_request_id).exited_at is None  # type: ignore[union-attr]

    response = dispatcher.dispatch(
        root.operation_id,
        SpawnCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id=retry_request_id,
            credential=CREDENTIAL,
            verb="spawn",
            idempotency_key=key,
            arguments=arguments,
        ),
    )

    assert response == CommandResponse(
        request_id=retry_request_id,
        status="ok",
        message="Existing campaign worker returned after interrupted spawn.",
        result={
            "worker_id": existing.operation_id,
            "status": existing.status,
            "disposition": "existing",
        },
    )
    assert effects.spawn_calls == []
    assert [task.operation_id for task in store.campaign_tasks(campaign.campaign_id)].count(
        planned_worker_id
    ) == 1
    reconciled = store.agent_command(first_request_id)
    assert reconciled is not None
    assert reconciled.exited_at is not None
    assert reconciled.status == "ok"


def test_interrupted_spawn_rejects_an_existing_worker_with_another_instruction(
    tmp_path,
) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())
    key = "instruction-mismatch"
    request_id = "a" * 32
    retry_id = "b" * 32
    arguments = SpawnArguments(
        seat_node_id="exp/check",
        instruction="Run the intended check exactly once.",
    )
    planned_worker_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:campaign:{campaign.campaign_id}:spawn:{key}",
        )
    )
    store.start_agent_command(
        operation_id=root.operation_id,
        command_id=request_id,
        campaign_id=campaign.campaign_id,
        verb="spawn",
        idempotency_key=key,
        payload={
            "request_id": request_id,
            "arguments": arguments.model_dump(mode="json"),
            "planned_worker_id": planned_worker_id,
        },
    )
    _worker(
        store,
        campaign,
        root,
        planned_worker_id,
        instruction="Run some different work.",
    )

    response = dispatcher.dispatch(
        root.operation_id,
        SpawnCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id=retry_id,
            credential=CREDENTIAL,
            verb="spawn",
            idempotency_key=key,
            arguments=arguments,
        ),
    )

    assert response.status == "unavailable"
    assert "instruction" in (response.message or "").lower()
    assert effects.spawn_calls == []


def test_completed_spawn_key_returns_the_existing_worker_and_never_runs_effect_again(
    tmp_path,
) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())

    created = dispatcher.dispatch(root.operation_id, _spawn_request("6" * 32, key="spawn-once"))
    replayed = dispatcher.dispatch(root.operation_id, _spawn_request("7" * 32, key="spawn-once"))

    assert created.status == "ok"
    assert replayed.status == "ok"
    assert replayed.request_id == "7" * 32
    assert replayed.result["worker_id"] == created.result["worker_id"]
    assert replayed.result["disposition"] == "existing"
    assert len(effects.spawn_calls) == 1
    assert len(store.campaign_tasks(campaign.campaign_id)) == 2


def test_reusing_a_key_with_different_arguments_is_rejected_instead_of_deduplicated(
    tmp_path,
) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())
    created = dispatcher.dispatch(root.operation_id, _spawn_request("e" * 32, key="same-key"))

    mismatched = dispatcher.dispatch(
        root.operation_id,
        _spawn_request("f" * 32, key="same-key", seat_node_id="blocker/other"),
    )

    assert created.status == "ok"
    assert mismatched.status == "invalid"
    assert "idempotency" in (mismatched.message or "").lower()
    assert "arguments" in (mismatched.message or "").lower()
    assert len(effects.spawn_calls) == 1


def test_message_and_watch_graph_persist_and_pass_their_planned_effect_ids(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    worker = _worker(store, campaign, root, "worker")
    effects = _Effects(store, campaign, root)
    bundled = effects.bundle()
    message_key = "planned-message"
    watcher_key = "planned-watcher"
    message_command_id = "0" * 32
    watcher_command_id = "1" * 32

    def message_after_durable_start(context, arguments, planned_message_id):
        invocation = store.agent_command(message_command_id)
        assert invocation is not None
        assert invocation.exited_at is None
        assert invocation.start_payload["planned_message_id"] == planned_message_id
        return effects.message(context, arguments, planned_message_id)

    def watcher_after_durable_start(context, arguments, planned_watcher_id):
        invocation = store.agent_command(watcher_command_id)
        assert invocation is not None
        assert invocation.exited_at is None
        assert invocation.start_payload["planned_watcher_id"] == planned_watcher_id
        return effects.watch_graph(context, arguments, planned_watcher_id)

    dispatcher = CampaignCommandDispatcher(
        store,
        replace(
            bundled,
            message=message_after_durable_start,
            watch_graph=watcher_after_durable_start,
        ),
    )

    message = dispatcher.dispatch(
        root.operation_id,
        MessageCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id=message_command_id,
            credential=CREDENTIAL,
            verb="message",
            idempotency_key=message_key,
            arguments={
                "recipient_task_id": worker.operation_id,
                "body": "Use the durable message id.",
            },
        ),
    )
    watch = dispatcher.dispatch(
        root.operation_id,
        WatchGraphCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id=watcher_command_id,
            credential=CREDENTIAL,
            verb="watch_graph",
            idempotency_key=watcher_key,
            arguments={
                "condition": {"node_id": "hyp/result", "status_in": ["active"]},
                "reason": "Wait for the durable graph condition.",
            },
        ),
    )

    expected_message_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:campaign:{campaign.campaign_id}:message:{message_key}",
        )
    )
    expected_watcher_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:campaign:{campaign.campaign_id}:watch_graph:{watcher_key}",
        )
    )
    assert message.status == "ok"
    assert watch.status == "ok"
    assert effects.planned_message_ids == [expected_message_id]
    assert effects.planned_watcher_ids == [expected_watcher_id]
    message_invocation = store.agent_command(message_command_id)
    watcher_invocation = store.agent_command(watcher_command_id)
    assert message_invocation is not None
    assert watcher_invocation is not None
    assert message_invocation.start_payload["planned_message_id"] == expected_message_id
    assert watcher_invocation.start_payload["planned_watcher_id"] == expected_watcher_id


def test_unknown_non_spawn_command_is_never_reexecuted_when_reconciliation_is_inconclusive(
    tmp_path,
) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    worker = _worker(store, campaign, root, "worker")
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())
    key = "message-once"
    first_request_id = "0" * 32
    arguments = MessageArguments(
        recipient_task_id=worker.operation_id,
        body="Carry this instruction exactly once.",
    )
    planned_message_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:campaign:{campaign.campaign_id}:message:{key}",
        )
    )
    store.start_agent_command(
        operation_id=root.operation_id,
        command_id=first_request_id,
        campaign_id=campaign.campaign_id,
        verb="message",
        idempotency_key=key,
        payload={
            "request_id": first_request_id,
            "arguments": arguments.model_dump(mode="json"),
            "planned_message_id": planned_message_id,
        },
    )

    response = dispatcher.dispatch(
        root.operation_id,
        MessageCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="1" * 32,
            credential=CREDENTIAL,
            verb="message",
            idempotency_key=key,
            arguments=arguments,
        ),
    )

    assert response.status == "unavailable"
    assert "unknown" in (response.message or "").lower()
    assert effects.reconcile_calls == ["message"]
    assert effects.reconcile_planned_effect_ids == [planned_message_id]
    assert effects.message_calls == []


def test_unknown_watch_retry_uses_and_validates_the_original_planned_watcher_id(
    tmp_path,
) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    effects = _Effects(
        store,
        campaign,
        root,
        reconcile_result=CampaignCommandEffectResult(result={"watcher_id": "existing"}),
    )
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())
    key = "watch-once"
    original_request_id = "4" * 32
    arguments = WatchGraphCommandRequest(
        mailbox_id=MAILBOX_ID,
        request_id=original_request_id,
        credential=CREDENTIAL,
        verb="watch_graph",
        idempotency_key=key,
        arguments={
            "condition": {"node_id": "hyp/result", "status_in": ["active"]},
            "reason": "Wait for the original watcher.",
        },
    ).arguments
    planned_watcher_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:campaign:{campaign.campaign_id}:watch_graph:{key}",
        )
    )
    store.start_agent_command(
        operation_id=root.operation_id,
        command_id=original_request_id,
        campaign_id=campaign.campaign_id,
        verb="watch_graph",
        idempotency_key=key,
        payload={
            "request_id": original_request_id,
            "arguments": arguments.model_dump(mode="json"),
            "planned_watcher_id": planned_watcher_id,
        },
    )

    response = dispatcher.dispatch(
        root.operation_id,
        WatchGraphCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="5" * 32,
            credential=CREDENTIAL,
            verb="watch_graph",
            idempotency_key=key,
            arguments=arguments,
        ),
    )

    assert response.status == "ok"
    assert response.result == {"watcher_id": "existing"}
    assert effects.reconcile_calls == ["watch_graph"]
    assert effects.reconcile_planned_effect_ids == [planned_watcher_id]
    assert effects.planned_watcher_ids == []

    # A malformed durable planned id is unavailable and never reaches reconciliation.
    mismatch_path = tmp_path / "mismatch"
    mismatch_path.mkdir()
    store, campaign, root = _setup_campaign(mismatch_path)
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())
    store.start_agent_command(
        operation_id=root.operation_id,
        command_id="6" * 32,
        campaign_id=campaign.campaign_id,
        verb="watch_graph",
        idempotency_key=key,
        payload={
            "request_id": "6" * 32,
            "arguments": arguments.model_dump(mode="json"),
            "planned_watcher_id": str(uuid.uuid4()),
        },
    )
    refused = dispatcher.dispatch(
        root.operation_id,
        WatchGraphCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="7" * 32,
            credential=CREDENTIAL,
            verb="watch_graph",
            idempotency_key=key,
            arguments=arguments,
        ),
    )
    assert refused.status == "unavailable"
    assert "deterministic effect id" in (refused.message or "")
    assert effects.reconcile_calls == []


def test_orchestrator_message_requires_the_stable_worker_actor_id_before_effect_or_spend(
    tmp_path,
) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    worker = _worker(store, campaign, root, "worker")
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())

    accepted = dispatcher.dispatch(
        root.operation_id,
        MessageCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="2" * 32,
            credential=CREDENTIAL,
            verb="message",
            idempotency_key="message-stable-worker",
            arguments={
                "recipient_task_id": worker.operation_id,
                "body": "Continue the bounded check.",
            },
        ),
    )

    assert accepted.status == "ok"
    assert effects.message_calls == [
        MessageArguments(
            recipient_task_id=worker.operation_id,
            body="Continue the bounded check.",
        )
    ]

    store.complete_agent_task(worker.operation_id, applied_revision=None, result={})
    worker = store.agent_task(worker.operation_id)
    assert worker is not None
    worker_request = CampaignRunRequest.model_validate(worker.request)
    now = store.now()
    continuation = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id="worker-continuation",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="queued",
            request=worker_request.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="queued",
            parent_operation_id=worker.operation_id,
            authorized_by=campaign.authorized_by,
            dispatch_authority=worker.dispatch_authority,
        ),
        role="worker",
    )
    effects.message_calls.clear()
    budget_before = store.campaign_budget_meter(campaign.campaign_id)

    refused = dispatcher.dispatch(
        root.operation_id,
        MessageCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="3" * 32,
            credential=CREDENTIAL,
            verb="message",
            idempotency_key="message-worker-continuation",
            arguments={
                "recipient_task_id": continuation.operation_id,
                "body": "Do not create another paid delivery.",
            },
        ),
    )

    assert refused.status == "invalid"
    assert "stable worker id" in (refused.message or "")
    assert effects.message_calls == []
    assert store.campaign_budget_meter(campaign.campaign_id) == budget_before


@pytest.mark.parametrize(
    ("worker_role", "seat_node_id", "parent_matches_context"),
    [
        ("orchestrator", None, True),
        ("worker", "blocker/not-the-requested-seat", True),
        ("worker", "exp/check", False),
    ],
)
def test_spawn_verifies_worker_role_exact_seat_and_parent_before_reporting_success(
    tmp_path,
    worker_role,
    seat_node_id,
    parent_matches_context,
) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    effects = _Effects(store, campaign, root)
    command_task = root
    if not parent_matches_context:
        command_task = _orchestrator_turn(
            store,
            campaign,
            root,
            operation_id="later-orchestrator-turn",
        )

    def malformed_spawn(_context, arguments, planned_worker_id):
        request = CampaignRunRequest(
            campaign_id=campaign.campaign_id,
            role=worker_role,
            actor_operation_id=(
                root.operation_id if worker_role == "orchestrator" else planned_worker_id
            ),
            run_truth_scope=_RUN_TRUTH_SCOPE,
            control_node_id=seat_node_id,
            instruction=arguments.instruction,
        )
        now = store.now()
        store.create_campaign_agent_task(
            AgentTaskRecord(
                operation_id=planned_worker_id,
                project_id=campaign.project_id,
                campaign_id=campaign.campaign_id,
                kind="campaign",
                status="queued",
                request=request.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
                status_message="queued",
                parent_operation_id=root.operation_id,
                authorized_by=campaign.authorized_by,
                dispatch_authority=_campaign_authority(
                    campaign.campaign_id,
                    worker_role,
                ),
            ),
            role=worker_role,
        )
        return CampaignCommandEffectResult(result={"worker_id": planned_worker_id})

    dispatcher = CampaignCommandDispatcher(
        store,
        replace(effects.bundle(), spawn=malformed_spawn),
    )
    response = dispatcher.dispatch(
        command_task.operation_id,
        _spawn_request("2" * 32, key="verify-postconditions"),
    )

    assert response.status == "unavailable"
    assert any(word in (response.message or "").lower() for word in ("role", "seat", "parent"))


def test_deduplicated_client_attempt_is_audited_on_the_current_task(tmp_path) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())
    first = dispatcher.dispatch(root.operation_id, _spawn_request("3" * 32, key="audit-replay"))
    current = store.create_campaign_agent_task(
        AgentTaskRecord(
            operation_id="later-orchestrator-turn",
            project_id=campaign.project_id,
            campaign_id=campaign.campaign_id,
            kind="campaign",
            status="succeeded",
            request=CampaignRunRequest(
                campaign_id=campaign.campaign_id,
                role="orchestrator",
                actor_operation_id=root.operation_id,
                run_truth_scope=_RUN_TRUTH_SCOPE,
            ).model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message="done",
            parent_operation_id=root.operation_id,
            authorized_by=campaign.authorized_by,
            dispatch_authority=_campaign_authority(
                campaign.campaign_id,
                "orchestrator",
            ),
        ),
        role="orchestrator",
    )

    replayed = dispatcher.dispatch(
        current.operation_id,
        _spawn_request("4" * 32, key="audit-replay"),
    )

    assert replayed.status == "ok"
    assert replayed.result["worker_id"] == first.result["worker_id"]
    assert len(effects.spawn_calls) == 1
    current_events = store.agent_task_events(current.operation_id)
    assert any(
        "spawn" in event.message.lower()
        and any(
            marker in event.message.lower()
            for marker in ("idempotency", "existing", "reused", "duplicate")
        )
        for event in current_events
    )


@pytest.mark.parametrize("original_completed", [False, True])
def test_worker_cannot_replay_an_orchestrator_idempotency_key(
    tmp_path,
    original_completed: bool,
) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())
    key = "orchestrator-only-key"
    first_request_id = "c" * 32
    request = _spawn_request(first_request_id, key=key)

    if original_completed:
        original = dispatcher.dispatch(root.operation_id, request)
        assert original.status == "ok"
    else:
        planned_worker_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"rcp:campaign:{campaign.campaign_id}:spawn:{key}",
            )
        )
        store.start_agent_command(
            operation_id=root.operation_id,
            command_id=first_request_id,
            campaign_id=campaign.campaign_id,
            verb="spawn",
            idempotency_key=key,
            payload={
                "request_id": first_request_id,
                "arguments": request.arguments.model_dump(mode="json"),
                "planned_worker_id": planned_worker_id,
            },
        )
    caller = _worker(store, campaign, root, "caller-worker")

    refused = dispatcher.dispatch(
        caller.operation_id,
        request.model_copy(update={"request_id": "d" * 32}),
    )

    assert refused.status == "invalid"
    assert "same canonical campaign actor and role" in (refused.message or "")
    original_invocation = store.agent_command(first_request_id)
    assert original_invocation is not None
    assert (original_invocation.exited_at is not None) is original_completed
    assert len(effects.spawn_calls) == int(original_completed)
    retry_attempt = store.agent_command("d" * 32)
    assert retry_attempt is not None and retry_attempt.status == "invalid"


def test_worker_may_reply_only_by_message_while_other_mutations_remain_orchestrator_only(
    tmp_path,
) -> None:
    store, campaign, root = _setup_campaign(tmp_path)
    worker = _worker(store, campaign, root, "worker")
    effects = _Effects(store, campaign, root)
    dispatcher = CampaignCommandDispatcher(store, effects.bundle())

    reply = dispatcher.dispatch(
        worker.operation_id,
        MessageCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="8" * 32,
            credential=CREDENTIAL,
            verb="message",
            idempotency_key="worker-reply",
            arguments={
                "recipient_task_id": root.operation_id,
                "body": "The bounded check finished.",
            },
        ),
    )
    assert reply.status == "ok"
    assert effects.message_calls == [
        MessageArguments(
            recipient_task_id=root.operation_id,
            body="The bounded check finished.",
        )
    ]

    forbidden = [
        SpawnCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="9" * 32,
            credential=CREDENTIAL,
            verb="spawn",
            idempotency_key="worker-spawn",
            arguments={
                "seat_node_id": "exp/other",
                "instruction": "Start another worker.",
            },
        ),
        PauseCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="a" * 32,
            credential=CREDENTIAL,
            verb="pause",
            idempotency_key="worker-pause",
            arguments={"worker_id": worker.operation_id},
        ),
        ResumeCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="b" * 32,
            credential=CREDENTIAL,
            verb="resume",
            idempotency_key="worker-resume",
            arguments={"worker_id": worker.operation_id},
        ),
        StopCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="c" * 32,
            credential=CREDENTIAL,
            verb="stop",
            idempotency_key="worker-stop",
            arguments={"worker_id": worker.operation_id},
        ),
        WatchGraphCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="d" * 32,
            credential=CREDENTIAL,
            verb="watch_graph",
            idempotency_key="worker-watch",
            arguments={
                "condition": {"node_id": "hyp/result", "status_in": ["active"]},
                "reason": "Wait for the belief transition.",
            },
        ),
        FinishCommandRequest(
            mailbox_id=MAILBOX_ID,
            request_id="e" * 32,
            credential=CREDENTIAL,
            verb="finish",
            idempotency_key="worker-finish",
        ),
    ]
    for request in forbidden:
        response = dispatcher.dispatch(worker.operation_id, request)
        assert response.status == "invalid"
        assert "Only the campaign orchestrator" in (response.message or "")
    assert effects.spawn_calls == []
