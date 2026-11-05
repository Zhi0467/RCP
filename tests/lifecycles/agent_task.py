"""The agent task machine: one Work turn and the recovery children it grows.

A world is a bare ``BackgroundAgentTasks`` over the explorer's tree, with no
app, as ``tests/test_background.py`` builds it. ``boot`` applies these instance
patches: ``store.now`` reads the explorer clock, ``_spawn_record`` registers a
worker and its process control without starting a thread (as the handoff
explorer does), ``_schedule_transport_retry`` records a timer instead of
sleeping, and ``_schedule_remote_reconciliation`` does nothing. ``seams``
makes ``seconds_until_automatic_launch`` return 0 and points the ``datetime``
that ``rcp.storage.rows`` reads at the explorer clock.

A worker's two halves are separate actions. ``settle`` runs the claimed
worker's body (``_run``) inline with a scripted stream for one outcome, each
taken from an existing test, and holds back its last two calls; ``finish``
then runs ``_task_settled`` and the worker leaves, as in production. A timer
fires only as an action, advancing the clock by its delay; one that fires
after a child exists is refused by ``_transport_retry_superseded``. Restart
runs ``recover_at_startup``, the only startup call ``api/app.py`` makes for
these tables.

The one root turn ``A0`` carries an attachment that expires 30 minutes after
the start instant; ``expire`` moves the clock past it. Expiry changes only
whether the newest turn offers Resume or Retry and whether a timer stands
down, so ``expire`` is offered only while one of those is pending. Children
are named by depth, ``A1``, ``A2``; a second child of one parent would get a
letter.
Member actions act on the newest turn only: Pause, Resume, Retry, and a
sign-in that verifies the machine login and releases parked work through
``resume_provider_account``. A refusal the product raises as ``ValueError``
(its API answers 4xx) leaves the state as it was. Member removal
(``request_member_removal_pause``) and project deletion
(``stop_for_project_deletion``) are left out under the budget rule: no rule
or suspected trace needs them, they write only through Pause's writers, and
with them the bound of 4 took 18 s before any witness ran. A Pause the fixed
code leaves pausing needs a claim first, at depth 2, and a bound of 5 does
not fit, so the horizon is 2 and a bound of 4 checks the goal from there.

Restore, transfer, branch merge recovery and episode reports also rewrite
task status outside the transition table (``paused`` to ``interrupted``,
``interrupted`` to ``queued`` or ``failed``), but they belong to catalog
machines and the episode explorer, so no action here reaches them.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from unittest import mock

from rcp.agents import AgentEvent, AgentProcessControl
from rcp.attachments import ChatAttachmentDescriptor
from rcp.limits import AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS, AGENT_TRANSPORT_RETRY_LIMIT
from rcp.machine_cards import BACKEND_MACHINE_ID, LoginKey
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord, AppStore, ProjectRecord
from rcp.storage.agent_tasks import AgentTaskStoreMixin
from rcp.storage.artifact_imports import ArtifactImportStoreMixin
from rcp.storage.artifacts import ArtifactStoreMixin, migrate_artifacts
from rcp.storage.auto_research import AutoResearchStoreMixin
from rcp.storage.base import AppStoreBase
from rcp.storage.consolidation import ConsolidationStoreMixin
from rcp.storage.episodes import EpisodeStoreMixin, _migrate_campaign_episodes
from rcp.storage.models import (
    ACTIVE_AGENT_TASK_STATUSES,
    AGENT_TASK_TRANSITIONS,
    MACHINE_LOGIN_ID,
    AgentTaskStatus,
)
from rcp.storage.projects import ProjectStoreMixin
from rcp.storage.provider_logins import ProviderLoginStoreMixin, migrate_per_member_provider_logins
from rcp.storage.transfer import ProjectTransferStoreMixin
from tests.helpers import fabricated_authorizer, write_local_test_manifest
from tests.lifecycles.engine import RESTART, START, Action, Clock, Goal, Memory, Safety, Step

_ROOT = "A0"
_EXPIRY = START + timedelta(minutes=30)
_LOGIN = LoginKey("codex", BACKEND_MACHINE_ID, MACHINE_LOGIN_ID)
_OUTCOMES = ("success", "transport_lost", "provider_auth", "session_refused", "paused")
_TERMINAL = frozenset({"succeeded", "failed", "interrupted", "paused"})
_MIGRATION = "a migration runs when a store opens, never inside an action"
_RECEIPTS = ("transport_auto_retry", "provider_auth_auto_retry", "provider_auth_retry_deferred")


def sse(event: AgentEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def seeded_store(tmp_path: Path) -> AppStore:
    """A store at ``tmp_path`` with one local project."""

    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator=str(write_local_test_manifest(tmp_path)),
            name="Project",
            state_machine_id=BACKEND_MACHINE_ID,
            state_path=str(tmp_path / ".research"),
            added_at=store.now(),
        )
    )
    return store


def work_request(chat_id, *, message="Exercise the admitted launch boundary."):
    return RunRequest(
        provider="codex",
        model="",
        reasoning="medium",
        run_on="laptop",
        run_truth_scope=["repo"],
        chat_scope="project",
        chat_id=chat_id,
        message=message,
        mode="work",
        patch_kind="work",
    )


def admitted_launch_task(
    store: AppStore,
    *,
    operation_id: str,
    request: RunRequest | None = None,
    parent_operation_id: str | None = None,
    record_updates: dict[str, object] | None = None,
    continuation_cause: str = "fresh",
) -> AgentTaskRecord:
    """A queued project chat turn whose admission committed and whose launch has not."""

    request = request or work_request(f"launch-{operation_id}")
    authority = resolve_dispatch_authority("project_chat", request)
    assert authority is not None
    now = store.now()
    record = AgentTaskRecord(
        operation_id=operation_id,
        project_id="project",
        kind="project_chat",
        status="queued",
        request=request.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
        status_message="Queued",
        attempt=2 if parent_operation_id is not None else 1,
        parent_operation_id=parent_operation_id,
        phase="queued",
        last_activity_at=now,
        authorized_by=fabricated_authorizer("Researcher"),
        dispatch_authority=authority,
    )
    return store.create_agent_task(
        record.model_copy(update=record_updates or {}), continuation_cause=continuation_cause
    )


async def done_stream(_project_id, _kind, _request, _execution) -> AsyncIterator[str]:
    yield sse(AgentEvent(event="done"))


async def _transport_lost_stream(_project_id, _kind, _request, execution) -> AsyncIterator[str]:
    """``test_a_turn_the_provider_explained_is_never_blamed_on_the_link``: a bare 255 on a host."""

    execution.stage_host = "gpu.example.edu"
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "provider_exit",
        {"return_code": 255, "event_counts": {"raw": 11}},
        tier="diagnostic",
    )
    yield sse(
        AgentEvent(
            event="error", text="The connection to gpu.example.edu was lost before codex finished."
        )
    )


async def _provider_auth_stream(_project_id, _kind, _request, execution) -> AsyncIterator[str]:
    """``test_provider_auth_finalizer_marks_login_before_settlement``: a dead machine login."""

    execution.login_key = _LOGIN
    execution.login_generation = execution.store.provider_login_state(_LOGIN).generation
    yield sse(AgentEvent(event="error", text="refresh_token_reused"))


async def _session_refused_stream(_project_id, _kind, _request, execution) -> AsyncIterator[str]:
    """``test_typed_gate_refusal_does_not_depend_on_error_text``: a refused chat wake."""

    execution.launch_refused = True
    yield sse(AgentEvent(event="error", text="The chat's session moved on."))


async def _paused_stream(_project_id, _kind, _request, execution) -> AsyncIterator[str]:
    """``test_routed_worker_pause_and_stop_target_only_its_current_attempt``, with a session."""

    yield sse(AgentEvent(event="session", session_id=f"session-{execution.operation_id}"))
    yield sse(AgentEvent(event="paused", text="Paused at the exact checkpoint."))


_STREAMS = {
    "success": done_stream,
    "transport_lost": _transport_lost_stream,
    "provider_auth": _provider_auth_stream,
    "session_refused": _session_refused_stream,
    "paused": _paused_stream,
}


class View(NamedTuple):
    tasks: tuple
    timers: tuple
    signed_in: bool
    expired: bool


@dataclass
class World:
    """One engine over the tree, plus the timers it armed."""

    background: Any
    clock: Clock
    timers: list[tuple[str, int, float]] = field(default_factory=list)

    @property
    def store(self) -> AppStore:
        return self.background.store

    def lineage(self) -> dict[str, str]:
        """Each operation id's name: its depth below the root, lettered among siblings."""

        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT operation_id, parent_operation_id FROM graph_runs ORDER BY rowid"
            ).fetchall()
        parents = {row[0]: row[1] for row in rows}
        names: dict[str, str] = {}
        taken: dict[int, int] = {}
        for operation_id in parents:
            depth, cursor = 0, operation_id
            while parents.get(cursor) is not None:
                depth, cursor = depth + 1, parents[cursor]
            sibling = taken.get(depth, 0)
            taken[depth] = sibling + 1
            names[operation_id] = f"A{depth}" + ("" if sibling == 0 else chr(ord("a") + sibling))
        return names

    def children(self) -> dict[str, list[str]]:
        """Each parent's children by name, read from the rows directly."""

        names = self.lineage()
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT operation_id, parent_operation_id FROM graph_runs "
                "WHERE parent_operation_id IS NOT NULL ORDER BY rowid"
            ).fetchall()
        found: dict[str, list[str]] = {}
        for operation_id, parent in rows:
            found.setdefault(names[parent], []).append(names[operation_id])
        return found


class AgentTask:
    name = "agent_task"
    bound = 4
    horizon = 2
    covers = {AgentTaskStatus: ("tasks", 1)}
    tables = frozenset({"graph_runs", "graph_run_receipts"})
    writers = (
        AgentTaskStoreMixin._claim_agent_task_graph_repair,
        AgentTaskStoreMixin._insert_agent_task,
        AgentTaskStoreMixin._insert_agent_task_receipt,
        AgentTaskStoreMixin._transition_agent_task,
        AgentTaskStoreMixin.bind_agent_task_write_scope,
        AgentTaskStoreMixin.checkpoint_agent_task,
        AgentTaskStoreMixin.checkpoint_agent_task_runtime,
        AgentTaskStoreMixin.claim_recorded_finalization,
        AgentTaskStoreMixin.clear_agent_task_stage,
        AgentTaskStoreMixin.complete_agent_task,
        AgentTaskStoreMixin.detach_agent_tasks_for_history,
        AgentTaskStoreMixin.detach_agent_tasks_for_restore,
        AgentTaskStoreMixin.fail_agent_task,
        AgentTaskStoreMixin.interrupt_active_agent_tasks,
        AgentTaskStoreMixin.mark_agent_artifact_kept,
        AgentTaskStoreMixin.mark_agent_task_running,
        AgentTaskStoreMixin.pause_agent_task,
        AgentTaskStoreMixin.prune_operational_storage,
        AgentTaskStoreMixin.reconcile_agentless_merge_task,
        AgentTaskStoreMixin.record_chat_stage_layout,
        AgentTaskStoreMixin.release_recorded_finalization,
        AgentTaskStoreMixin.replace_agent_task_graph_update,
        AgentTaskStoreMixin.request_agent_task_pause,
        AgentTaskStoreMixin.requeue_owned_merge_task,
        AgentTaskStoreMixin.reserve_branch_merge_invocation,
        AgentTaskStoreMixin.update_agent_task_message,
        AgentTaskStoreMixin.update_agent_task_status,
        ArtifactImportStoreMixin.complete_artifact_import,
        ArtifactStoreMixin._remove_artifact,
        migrate_artifacts,
        AutoResearchStoreMixin._abandon_auto_research_recovery_in_connection,
        AutoResearchStoreMixin.request_auto_research_worker_pause,
        AppStoreBase._migrate_episode_isolation,
        AppStoreBase._migrate_episode_lineage,
        AppStoreBase._migrate_legacy_startup_schema,
        AppStoreBase._migrate_paper_live,
        ConsolidationStoreMixin.drop_consolidation_request_target,
        EpisodeStoreMixin._finish_episode_report_attempt_error,
        EpisodeStoreMixin.fail_episode_report_allocation_unlaunchable,
        EpisodeStoreMixin.finish_episode_report_ready,
        EpisodeStoreMixin.mark_episode_report_attempt_running,
        EpisodeStoreMixin.retry_episode_report_allocation,
        _migrate_campaign_episodes,
        ProjectStoreMixin.delete_project_records,
        ProjectStoreMixin.migrate_legacy_project_data,
        ProviderLoginStoreMixin.assign_provider_login,
        ProviderLoginStoreMixin.record_provider_login_refusal,
        migrate_per_member_provider_logins,
        ProjectTransferStoreMixin._insert_transfer_tasks,
        ProjectTransferStoreMixin.settle_source_transfer_tasks,
    )
    unreached_values: dict[str, str] = {
        "running": "settle claims the turn and commits its verdict in one action",
    }
    unreached_writers = {
        AgentTaskStoreMixin._claim_agent_task_graph_repair: (
            "graph repair continues a succeeded Patch, which this model never produces"
        ),
        AgentTaskStoreMixin.bind_agent_task_write_scope: (
            "the Work launcher binds write scope; scripted streams skip the launcher"
        ),
        AgentTaskStoreMixin.checkpoint_agent_task_runtime: (
            "only a runtime stream event checkpoints a runtime; scripted streams send none"
        ),
        AgentTaskStoreMixin.claim_recorded_finalization: (
            "recorded remote results are finalized by remote reconciliation, which is stubbed"
        ),
        AgentTaskStoreMixin.release_recorded_finalization: (
            "recorded remote results are finalized by remote reconciliation, which is stubbed"
        ),
        AgentTaskStoreMixin.clear_agent_task_stage: (
            "graph Patch and branch merge runners clear stages; scripted streams skip them"
        ),
        AgentTaskStoreMixin.record_chat_stage_layout: (
            "the chat launcher records stage layout; scripted streams skip it"
        ),
        AgentTaskStoreMixin.update_agent_task_status: (
            "only a status stream event writes the live snapshot; scripted streams send none"
        ),
        AgentTaskStoreMixin.replace_agent_task_graph_update: "Apply again is a Work graph action",
        AgentTaskStoreMixin.mark_agent_artifact_kept: "keeping an artifact is the artifact catalog machine",
        ArtifactImportStoreMixin.complete_artifact_import: "artifact import is the artifact catalog machine",
        ArtifactStoreMixin._remove_artifact: "artifact removal is the artifact catalog machine",
        AgentTaskStoreMixin.prune_operational_storage: "maintenance pruning runs on a timer in the app",
        AgentTaskStoreMixin.detach_agent_tasks_for_history: "driven by the restore catalog machine",
        AgentTaskStoreMixin.detach_agent_tasks_for_restore: "driven by the restore catalog machine",
        AgentTaskStoreMixin.reconcile_agentless_merge_task: "the branch merge catalog machine",
        AgentTaskStoreMixin.requeue_owned_merge_task: "the branch merge catalog machine",
        AgentTaskStoreMixin.reserve_branch_merge_invocation: "the branch merge catalog machine",
        AutoResearchStoreMixin._abandon_auto_research_recovery_in_connection: (
            "the Auto-research recovery catalog machine"
        ),
        AutoResearchStoreMixin.request_auto_research_worker_pause: (
            "the Auto-research children catalog machine"
        ),
        ConsolidationStoreMixin.drop_consolidation_request_target: (
            "driven by the consolidation explorer"
        ),
        EpisodeStoreMixin._finish_episode_report_attempt_error: "driven by the episode explorer",
        EpisodeStoreMixin.fail_episode_report_allocation_unlaunchable: (
            "driven by the episode explorer"
        ),
        EpisodeStoreMixin.finish_episode_report_ready: "driven by the episode explorer",
        EpisodeStoreMixin.mark_episode_report_attempt_running: "driven by the episode explorer",
        EpisodeStoreMixin.retry_episode_report_allocation: "driven by the episode explorer",
        ProjectStoreMixin.delete_project_records: "the project deletion catalog machine",
        ProviderLoginStoreMixin.assign_provider_login: (
            "the provider launcher assigns logins; scripted streams skip it"
        ),
        ProviderLoginStoreMixin.record_provider_login_refusal: (
            "the provider launcher records login refusals; scripted streams skip it"
        ),
        ProjectTransferStoreMixin._insert_transfer_tasks: "the transfer catalog machine",
        ProjectTransferStoreMixin.settle_source_transfer_tasks: "the transfer catalog machine",
        ProjectStoreMixin.migrate_legacy_project_data: _MIGRATION,
        AppStoreBase._migrate_episode_isolation: _MIGRATION,
        AppStoreBase._migrate_episode_lineage: _MIGRATION,
        AppStoreBase._migrate_legacy_startup_schema: _MIGRATION,
        AppStoreBase._migrate_paper_live: _MIGRATION,
        _migrate_campaign_episodes: _MIGRATION,
        migrate_artifacts: _MIGRATION,
        migrate_per_member_provider_logins: _MIGRATION,
    }

    def __init__(self) -> None:
        self.clock = Clock()

    @contextmanager
    def seams(self) -> Iterator[None]:
        machine = self

        class ExplorerDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return machine.clock.now.astimezone(tz)

        with (
            mock.patch("rcp.background.seconds_until_automatic_launch", lambda: 0),
            mock.patch("rcp.storage.rows.datetime", ExplorerDatetime),
        ):
            yield

    def databases(self, root: Path) -> list[Path]:
        return [root / "rcp.sqlite3"]

    def start(self, root: Path, clock: Clock) -> World:
        self.clock = clock
        store = seeded_store(root)
        store.now = clock.iso
        attachment = ChatAttachmentDescriptor(
            attachment_id="upload",
            name="notes.txt",
            media_type="text/plain",
            size=5,
            expires_at=_EXPIRY.isoformat(),
        )
        request = work_request("launch-A0").model_copy(update={"attachments": [attachment]})
        admitted_launch_task(store, operation_id=_ROOT, request=request)
        store.close()
        return self.boot(root, clock)

    def boot(self, root: Path, clock: Clock) -> World:
        from rcp.background import BackgroundAgentTasks

        self.clock = clock
        store = AppStore(root / "rcp.sqlite3")
        store.now = clock.iso
        background = BackgroundAgentTasks(store, done_stream)
        world = World(background, clock)

        def spawn(record, request, **kwargs):
            current = store.agent_task(record.operation_id) or record
            if current.status == "queued":
                _register(background, current.operation_id, paused=False)
            return current

        def schedule(operation_id: str, *, attempt: int, delay: float | None = None) -> None:
            wait = AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS[attempt] if delay is None else delay
            world.timers.append((operation_id, attempt, wait))

        background._spawn_record = spawn
        background._schedule_transport_retry = schedule
        background._schedule_remote_reconciliation = lambda **kwargs: None
        return world

    def close(self, world: World) -> None:
        """Drop the engine's stand-in workers, so a later engine shutdown pauses nothing."""

        background = world.background
        with background._controls_lock:
            background._controls.clear()
            background._workers.clear()
        world.store.close()

    def save_memory(self, world: World) -> Memory:
        names = world.lineage()
        background = world.background
        engine = (
            tuple(sorted((names[op], attempt, delay) for op, attempt, delay in world.timers)),
            frozenset(names[op] for op in background._workers),
            frozenset(
                names[op]
                for op, control in background._controls.items()
                if control.pause_requested.is_set()
            ),
        )
        return Memory(engine, None)

    def restore_memory(self, world: World, memory: Memory) -> None:
        if memory.engine is None:
            return
        timers, workers, paused = memory.engine
        operation = {name: op for op, name in world.lineage().items()}
        world.timers = [(operation[name], attempt, delay) for name, attempt, delay in timers]
        background = world.background
        for name in workers:
            _register(background, operation[name], paused=name in paused)

    def actions(self, view: View) -> list[Action]:
        found: list[Action] = []
        for name, status, *_rest, (worker, _paused) in view.tasks:
            if not worker and status == "queued":
                found.append(Action("claim", (name,)))
            elif worker and status == "queued":
                found.extend(Action("settle", (name, outcome)) for outcome in _OUTCOMES)
            elif worker and status == "pausing":
                found.append(Action("settle", (name, "paused")))
            elif worker and status in _TERMINAL:
                found.append(Action("finish", (name,)))
        found.extend(
            dict.fromkeys(Action("fire", (name, str(attempt))) for name, attempt, _ in view.timers)
        )
        leaf = _leaf(view)
        name, status, _kind, can_retry, can_resume = leaf[:5]
        if status in AGENT_TASK_TRANSITIONS["pausing"]:
            found.append(Action("pause", (name,), environment=False))
        if can_resume:
            found.append(Action("resume", (name,), environment=False))
        if can_retry:
            found.append(Action("retry", (name,), environment=False))
        if not view.signed_in:
            found.append(Action("sign_in", environment=False))
        if not view.expired and (can_retry or can_resume or view.timers):
            found.append(Action("expire"))
        found.append(Action(RESTART))
        return found

    def apply(self, world: World, action: Action) -> None:
        background = world.background
        names = world.lineage()
        operation = {name: op for op, name in names.items()}
        target = operation[action.args[0]] if action.args else ""
        if action.name == "claim":
            background.launch_admitted(target)
        elif action.name == "settle":
            _settle(world, target, action.args[1])
        elif action.name == "finish":
            _finish(world, target)
        elif action.name == "fire":
            timer = next(
                timer for timer in world.timers if (names[timer[0]], str(timer[1])) == action.args
            )
            world.timers.remove(timer)
            world.clock.advance(timer[2])
            background._run_transport_retry(timer[0], attempt=timer[1])
        elif action.name == "expire":
            world.clock.advance((_EXPIRY - world.clock.now).total_seconds() + 1)
        else:
            try:
                _member_action(world, action.name, target)
            except ValueError:
                return

    def restart(self, world: World) -> None:
        world.background.recover_at_startup()

    def view(self, world: World) -> View:
        names = world.lineage()
        background = world.background
        tasks = []
        for operation_id, name in sorted(names.items(), key=lambda item: item[1]):
            task = world.store.agent_task(operation_id)
            assert task is not None
            receipts = tuple(
                sorted(
                    receipt.category
                    for receipt in world.store.agent_task_receipts(operation_id)
                    if receipt.category.startswith(_RECEIPTS)
                )
            )
            control = background._controls.get(operation_id)
            worker = (
                operation_id in background._workers,
                control is not None and control.pause_requested.is_set(),
            )
            tasks.append(
                (
                    name,
                    task.status,
                    task.failure_kind,
                    task.can_retry,
                    task.can_resume,
                    receipts,
                    worker,
                )
            )
        timers = tuple(sorted((names[op], attempt, delay) for op, attempt, delay in world.timers))
        return View(
            tuple(tasks),
            timers,
            world.store.provider_login_state(_LOGIN).state == "signed_in",
            world.clock.now >= _EXPIRY,
        )

    @property
    def rules(self):
        return RULES


def _register(background, operation_id: str, *, paused: bool) -> None:
    control = AgentProcessControl()
    if paused:
        control.request_pause()
    with background._controls_lock:
        background._controls[operation_id] = control
        background._workers[operation_id] = threading.current_thread()


def _settle(world: World, operation_id: str, outcome: str) -> None:
    """Run the worker's body with one scripted stream, holding back its settlement."""

    background = world.background
    record = background._require_operation(operation_id)
    request = background._request_from_record(record)
    cause = world.store.agent_task_continuation_cause(operation_id) or "fresh"
    control = background._controls[operation_id]
    background.stream = _STREAMS[outcome]
    with (
        mock.patch.object(background, "_task_settled", lambda *_args: None),
        mock.patch.object(background, "_forget_control", lambda _op: None),
    ):
        background._run(record, request, control, cause)


def _finish(world: World, operation_id: str) -> None:
    from rcp.background import AgentTaskExecution

    background = world.background
    record = background._require_operation(operation_id)
    execution = AgentTaskExecution(
        operation_id=operation_id,
        store=world.store,
        control=background._controls[operation_id],
    )
    background._task_settled(record, background._request_from_record(record), execution)
    background._forget_control(operation_id)


def _member_action(world: World, name: str, operation_id: str) -> None:
    from rcp.runs.provider_login import resume_provider_account

    background = world.background
    if name == "pause":
        background.pause(operation_id)
    elif name == "resume":
        record = background._require_operation(operation_id)
        background.resume(operation_id, authorized_by=record.authorized_by)
    elif name == "retry":
        record = background._require_operation(operation_id)
        background.retry(operation_id, authorized_by=record.authorized_by)
    elif name == "sign_in":
        world.store.mark_provider_login_verified(_LOGIN, member_id="member", detail="verified")
        resume_provider_account(background, _LOGIN, automatic=True)
    else:
        raise AssertionError(f"unknown action {name}")


def _leaf(view: View) -> tuple:
    return max(view.tasks, key=lambda task: (len(task[0]), task[0]))


def _always(*_args) -> bool:
    return True


def _statuses(view: View) -> dict[str, str]:
    return {task[0]: task[1] for task in view.tasks}


def status_moves_follow_the_table(before: View, world: World) -> list[str]:
    """Every status change is one ``AGENT_TASK_TRANSITIONS`` allows, and a new turn starts queued."""

    old = _statuses(before)
    found = []
    for name, status in _statuses(_view(world)).items():
        previous = old.get(name)
        if previous is None and status != "queued":
            found.append(f"{name} appeared {status}")
        elif previous not in {None, status, *AGENT_TASK_TRANSITIONS.get(status, ())}:
            found.append(f"{name} moved {previous} -> {status}")
    return found


def recovery_keeps_the_parent_status(before: View, world: World) -> list[str]:
    """Resume and Retry add a child and never rewrite the parent's status."""

    old = _statuses(before)
    after = _statuses(_view(world))
    return [
        f"{child} moved its parent {parent} {old[parent]} -> {after[parent]}"
        for parent, children in world.children().items()
        for child in children
        if child not in old and parent in old and after[parent] != old[parent]
    ]


def _new_task(before: View, world: World) -> bool:
    return len(world.lineage()) > len(before.tasks)


def one_recovery_child_per_parent(world: World) -> list[str]:
    """A turn is continued by at most one recovery child."""

    return [
        f"{parent} has children {', '.join(children)}"
        for parent, children in world.children().items()
        if len(children) > 1
    ]


def one_active_task_per_chat(world: World) -> list[str]:
    """At most one turn of the chat is queued, running or pausing."""

    active = [task[0] for task in _view(world).tasks if task[1] in ACTIVE_AGENT_TASK_STATUSES]
    return [f"{', '.join(active)} are active together"] if len(active) > 1 else []


def transport_ladder_is_bounded(world: World) -> list[str]:
    """The chat's automatic reattempts for lost links never exceed the limit, restarts included."""

    with world.store.connection() as connection:
        promised = connection.execute(
            "SELECT count(*) FROM graph_run_receipts WHERE category = 'transport_auto_retry'"
        ).fetchone()[0]
    if promised > AGENT_TRANSPORT_RETRY_LIMIT:
        return [f"{promised} reattempts promised"]
    return []


def _armed(before: View, world: World) -> set[str]:
    names = world.lineage()
    return {names[op] for op, _attempt, _ in world.timers} - {
        name for name, _attempt, _ in before.timers
    }


def timers_arm_only_for_untaken_turns(before: View, world: World) -> list[str]:
    """A reattempt timer is armed only for a turn no child has taken over."""

    children = world.children()
    return [
        f"timer armed for {name} after {', '.join(children[name])} took it over"
        for name in sorted(_armed(before, world))
        if name in children
    ]


def _new_timer(before: View, world: World) -> bool:
    return bool(_armed(before, world))


def pausing_settles(view: View) -> bool:
    """No turn is left pausing."""

    return all(task[1] != "pausing" for task in view.tasks)


def _view(world: World) -> View:
    return AgentTask().view(world)


_LIFECYCLE = "operations spec: Durable agent task lifecycle"
_REATTEMPTS = "providers spec: Provider readiness and native skills"

RULES = (
    Step(status_moves_follow_the_table, _always, _LIFECYCLE),
    Step(recovery_keeps_the_parent_status, _new_task, _LIFECYCLE),
    Safety(one_recovery_child_per_parent, _always, _REATTEMPTS),
    Safety(one_active_task_per_chat, _always, "code guard at _transport_retry_superseded"),
    Safety(transport_ladder_is_bounded, _always, _REATTEMPTS),
    Step(timers_arm_only_for_untaken_turns, _new_timer, "code guard at transport_retry_owed"),
    Goal(pausing_settles, _LIFECYCLE),
)
