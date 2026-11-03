"""The nightly consolidation machine.

A world is one real backend over the explorer's tree: the store, the catalog,
the engine and a ``ConsolidationPoller`` run as in production. ``boot`` applies
the instance patches ``tests/test_consolidation_runtime.py`` applies:
``store.now`` and the poller read the explorer clock, and the engine's
``launch_admitted`` keeps the real request validation and then marks the task
running with its launch receipt instead of starting a provider. It also makes
``_schedule_remote_reconciliation`` do nothing. ``seams`` points
``seconds_until_automatic_launch`` at zero and the ``datetime`` that
``rcp.storage.rows`` reads at the explorer clock.

The poller polls every minute, so every action is followed by one
``poll_once``, which reconciles settled runs and then admits due targets.
``poll`` alone is offered only while a target is due or a task waits queued;
``poll launch_unavailable`` is a poll whose launch the engine refuses with a
transient error, leaving the claimed task queued. ``night`` moves the clock to
the next 02:00 and is offered only when nothing is due, since a due target is
claimed by the very next poll. The turn's actions are ``settle`` (success
captures the report and completes; fail; pause), and ``commit``, one keyed
Apply through ``consolidation_apply_handler``, which writes graph history.
Members Keep or Dismiss a settled row and send a turn into the consolidation
chat (``send``, offered while a target is due). A restart runs the startup
reconciliation ``api/app.py`` runs for these tables: ``recover_at_startup``,
then the poller's first pass. ``restore`` runs ``detach_restored_lifecycle``
and the same startup in a fresh poller; it is offered only while a run is
unsettled, the only rows detachment changes. An operator restores, so like a
member action it never counts toward a goal: deleting the schedule must not
hide a stuck one.

Archiving the chat through the route is not an action: the route refuses the
consolidation chat, so it changes no state, and no rule reads it. The legacy
archived row is a seeded start instead.

The schedule runs daily at 02:00 UTC, so local dates read in UTC. Views name
runs by lineage, ``R0`` and ``R1`` in claim order, and member turns in the
consolidation chat ``M0``. Dates and due times are relative to the clock.

Bound: the minimum of horizon 3 and bound 4 does not fit the budget. Without
the archive route, bound 4 still explores 117 states over 183 edges, about
five times the time of bound 3, so the horizon is 2 and the bound 3. No
suspected trace is longer.

``seed`` starts from a stored bad state an older release left, instead of the
fresh schedule: ``request_target`` is a run 0.4.35 queued with ``graph_target``
in its request (fixed in b12897489), and ``archived_chat`` is the consolidation
chat archived by a release before 0.4.38 (fixed in 09f7519f6). A seeded machine
explores only the actions its goals allow, to a bound equal to the horizon, so
both goals are checked from the seeded state.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, NamedTuple, get_args, get_origin
from unittest import mock

import pytest
from pydantic import BaseModel

from rcp.consolidation import ConsolidationPoller
from rcp.storage.artifacts import ArtifactStoreMixin
from rcp.storage.consolidation import (
    ConsolidationRun,
    ConsolidationSchedule,
    ConsolidationStoreMixin,
    migrate_consolidation_targets,
)
from rcp.storage.models import AgentTaskStatus
from rcp.storage.notifications import NotificationStoreMixin
from tests.lifecycles.engine import RESTART, Action, Clock, Goal, Memory, Safety, Step

_NIGHT = time(2)
_OUTCOMES = ("success", "fail", "pause")
_REPORT = b"<!doctype html><html><body>Report</body></html>"


def consolidation_runtime(
    manifest_path: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: Any = None,
) -> SimpleNamespace:
    """One project with a seeded graph and a daily 02:00 schedule due three days ago.

    Launches keep the real request validation and then only mark the task
    running with its launch receipt, recording each launched operation in
    ``dispatched``. ``clock`` replaces ``store.now`` before anything is seeded.
    """

    from tests.helpers import append_fixture_patch, authorized_human, create_named_app, seed_patch

    app = create_named_app(str(manifest_path), data_dir=data_dir)
    store = app.state.background_tasks.store
    if clock is not None:
        store.now = clock
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    now = datetime.fromisoformat(store.now())
    schedule = store.put_consolidation_schedule(
        app.state.default_project_id,
        local_time="02:00",
        timezone="UTC",
        authorized_by=authorized_human(app),
        next_due_at=(now - timedelta(days=3)).isoformat(),
    )
    dispatched: list[str] = []
    fake_launches(monkeypatch, app, dispatched)
    monkeypatch.setattr("rcp.consolidation.seconds_until_automatic_launch", lambda: 0)
    poller = ConsolidationPoller(
        store, app.state.background_tasks, service_for=app.state.catalog.open
    )
    return SimpleNamespace(
        app=app,
        store=store,
        service=service,
        poller=poller,
        schedule=schedule,
        dispatched=dispatched,
        now=now,
    )


def fake_launches(monkeypatch: pytest.MonkeyPatch, app: Any, dispatched: list[str]) -> None:
    """Launch a validated queued task by marking it running with its launch receipt."""

    from tests.helpers import fake_launch_admitted

    store = app.state.background_tasks.store

    def dispatch(operation_id: str):
        if store.mark_agent_task_running(operation_id):
            store.record_agent_task_receipt(operation_id, "agent_launch", {})
            dispatched.append(operation_id)
        return store.agent_task(operation_id)

    fake_launch_admitted(monkeypatch, app.state.background_tasks, dispatch)


def _literal(model: type[BaseModel], field: str) -> Any:
    """The ``Literal`` a model field holds, without its ``None``."""

    annotation = model.model_fields[field].annotation
    if get_origin(annotation) is Literal:
        return annotation
    return next(arg for arg in get_args(annotation) if get_origin(arg) is Literal)


class View(NamedTuple):
    schedule: tuple
    outcome: str | None
    target: tuple
    runs: tuple
    members: tuple
    archived: bool
    request_target: bool


@dataclass
class World:
    """One backend over the tree and the poller that drives it."""

    runtime: SimpleNamespace
    clock: Clock
    last: Action | None = None

    @property
    def store(self):
        return self.runtime.store

    @property
    def poller(self) -> ConsolidationPoller:
        return self.runtime.poller

    @property
    def project_id(self) -> str:
        return self.runtime.app.state.default_project_id

    @property
    def chat_id(self) -> str:
        from rcp.storage.consolidation import consolidation_chat_id

        return consolidation_chat_id(self.project_id)

    def runs(self) -> list[ConsolidationRun]:
        """Runs in claim order."""

        with self.store.connection() as connection:
            order = [
                row[0]
                for row in connection.execute(
                    "SELECT run_id FROM consolidation_runs ORDER BY rowid"
                )
            ]
        runs = {run.run_id: run for run in self.store.consolidation_runs()}
        return [runs[run_id] for run_id in order]

    def members(self) -> list[str]:
        """Member turns in the consolidation chat, in admission order."""

        with self.store.connection() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT operation_id FROM graph_runs WHERE json_extract(request_json,"
                    "'$.chat_id')=? AND json_extract(request_json,'$.trigger')='human' "
                    "ORDER BY rowid",
                    (self.chat_id,),
                )
            ]

    def lineage(self) -> dict[str, str]:
        """Each operation id's lineage name."""

        names = {
            run.operation_id: f"R{index}"
            for index, run in enumerate(self.runs())
            if run.operation_id is not None
        }
        names.update({op: f"M{index}" for index, op in enumerate(self.members())})
        return names

    def schedule(self) -> ConsolidationSchedule | None:
        return self.store.consolidation_schedule(self.project_id)


class Consolidation:
    name = "consolidation"
    bound = 3
    horizon = 2
    covers = {
        AgentTaskStatus: ("runs", 6),
        _literal(ConsolidationRun, "kind"): ("runs", 2),
        _literal(ConsolidationRun, "state"): ("runs", 4),
        _literal(ConsolidationSchedule, "last_outcome"): ("outcome", None),
    }
    tables = frozenset(
        {
            "consolidation_runs",
            "consolidation_schedules",
            "consolidation_targets",
            "consolidation_apply_receipts",
        }
    )
    writers = (
        ConsolidationStoreMixin.ensure_consolidation_target,
        ConsolidationStoreMixin.claim_consolidation_occurrence,
        ConsolidationStoreMixin.finish_consolidation_occurrence,
        ConsolidationStoreMixin.settle_consolidation_run,
        ConsolidationStoreMixin.resolve_consolidation_run,
        ArtifactStoreMixin._keep_artifact,
        ConsolidationStoreMixin.reserve_consolidation_apply,
        ConsolidationStoreMixin.finish_consolidation_apply,
        ConsolidationStoreMixin.record_consolidation_apply_failure,
        ConsolidationStoreMixin.detach_consolidation_for_restore,
        ConsolidationStoreMixin.put_consolidation_schedule,
        ConsolidationStoreMixin.delete_consolidation_schedule,
        NotificationStoreMixin.observe_consolidation_notification,
        migrate_consolidation_targets,
    )
    unreached_values = {
        "pausing": "the faked launch starts no worker, so a pause settles paused directly; "
        "the agent task explorer covers pausing",
    }
    unreached_writers = {
        ConsolidationStoreMixin.record_consolidation_apply_failure: (
            "the explorer's keyed Apply always commits; "
            "test_keyed_apply_failure_requires_canonical_commit_before_success covers failures"
        ),
        ConsolidationStoreMixin.put_consolidation_schedule: "start seeds the one schedule",
        ConsolidationStoreMixin.delete_consolidation_schedule: (
            "no action disables the schedule; require_consolidation_launch refuses its runs"
        ),
        NotificationStoreMixin.observe_consolidation_notification: (
            "the notification sender only stamps notification_observed_at, which no rule reads"
        ),
        migrate_consolidation_targets: "the v37 migration runs only when an older store opens",
    }

    def __init__(self, seed: str | None = None) -> None:
        self.seed = seed
        if seed is not None:
            self.bound = self.horizon
        self.manifest_path: Path | None = None
        self.clock = Clock()

    @contextmanager
    def seams(self) -> Iterator[None]:
        machine = self

        class ExplorerDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return machine.clock.now.astimezone(tz)

        with (
            mock.patch("rcp.consolidation.seconds_until_automatic_launch", lambda: 0),
            mock.patch("rcp.storage.rows.datetime", ExplorerDatetime),
        ):
            yield

    def databases(self, root: Path) -> list[Path]:
        return [root / "data" / "rcp.sqlite3"]

    def start(self, root: Path, clock: Clock) -> World:
        from tests.helpers import write_test_manifest

        self.clock = clock
        manifest = write_test_manifest(root)
        self.manifest_path = manifest.path
        patch = pytest.MonkeyPatch()
        try:
            runtime = consolidation_runtime(manifest.path, root / "data", patch, clock=clock.iso)
            if self.seed == "request_target":
                _seed_queued_request_target(runtime)
            elif self.seed == "archived_chat":
                _seed_archived_chat(runtime)
            runtime.store.close()
        finally:
            patch.undo()
        return self.boot(root, clock)

    def boot(self, root: Path, clock: Clock) -> World:
        from tests.helpers import create_named_app

        assert self.manifest_path is not None
        self.clock = clock
        app = create_named_app(str(self.manifest_path), data_dir=root / "data")
        background = app.state.background_tasks
        store = background.store
        store.now = clock.iso
        background._schedule_remote_reconciliation = lambda **kwargs: None
        fake_launches(pytest.MonkeyPatch(), app, [])
        poller = ConsolidationPoller(store, background, service_for=app.state.catalog.open)
        return World(
            SimpleNamespace(app=app, store=store, service=app.state.service, poller=poller), clock
        )

    def close(self, world: World) -> None:
        world.store.close()

    def save_memory(self, world: World) -> Memory:
        names = world.lineage()
        engine = tuple(
            sorted((names[op], message) for op, message in world.poller._launch_errors.items())
        )
        return Memory(engine, None)

    def restore_memory(self, world: World, memory: Memory) -> None:
        if memory.engine is None:
            return
        operation = {name: op for op, name in world.lineage().items()}
        world.poller._launch_errors.update(
            {operation[name]: message for name, message in memory.engine}
        )

    def actions(self, view: View) -> list[Action]:
        scheduled, due_in = view.schedule
        due = scheduled and due_in <= 0
        queued = any(run[6] == "queued" for run in view.runs)
        found = []
        if due or queued:
            found.append(Action("poll"))
        if due:
            found.append(Action("poll", ("launch_unavailable",)))
        elif scheduled:
            found.append(Action("night"))
        live = False
        unsettled = False
        for name, _night, kind, _code, state, settled, status, committed, _verified in view.runs:
            unsettled = unsettled or not settled
            if status == "running":
                live = True
                found.extend(Action("settle", (name, outcome)) for outcome in _OUTCOMES)
                if not committed:
                    found.append(Action("commit", (name,)))
            if settled and state == "open" and self.seed is None:
                found.append(Action("dismiss", (name,), environment=False))
                if kind == "report":
                    found.append(Action("keep", (name,), environment=False))
        for name, status in view.members:
            if status == "running":
                live = True
                found.append(Action("settle", (name, "success")))
        if self.seed is not None:
            return found
        if due and not live:
            found.append(Action("send", environment=False))
        found.append(Action(RESTART))
        if unsettled and scheduled:
            found.append(Action("restore", environment=False))
        return found

    def apply(self, world: World, action: Action) -> None:
        world.last = action
        operation = {name: op for op, name in world.lineage().items()}
        if action.name == "poll":
            if action.args:
                _poll_without_launch(world)
                return
        elif action.name == "night":
            world.clock.advance((_next_night(world.clock.now) - world.clock.now).total_seconds())
        elif action.name == "settle":
            _settle(world, operation[action.args[0]], action.args[1])
        elif action.name == "commit":
            _commit(world, operation[action.args[0]])
        elif action.name in {"keep", "dismiss"}:
            run = world.runs()[int(action.args[0][1:])]
            world.store.resolve_consolidation_run(
                world.project_id,
                run.run_id,
                state="kept" if action.name == "keep" else "dismissed",
                resolved_by=run.authorized_by,
            )
        elif action.name == "send":
            _send(world)
        elif action.name == "restore":
            world.store.detach_restored_lifecycle(
                diagnostic="Restored from an archive.", confirmed_by="explorer"
            )
            background = world.runtime.app.state.background_tasks
            world.runtime.poller = ConsolidationPoller(
                world.store, background, service_for=world.runtime.app.state.catalog.open
            )
            background.recover_at_startup()
        else:
            raise ValueError(f"unknown action {action}")
        world.poller.poll_once()

    def restart(self, world: World) -> None:
        world.last = Action(RESTART)
        world.runtime.app.state.background_tasks.recover_at_startup()
        world.poller.poll_once()

    def view(self, world: World) -> View:
        return _view(world)

    @property
    def rules(self):
        return RULES


def _view(world: World) -> View:
    store, now = world.store, world.clock.now
    today = now.date()
    schedule = world.schedule()
    target = store.consolidation_target(world.project_id)
    runs = []
    for index, run in enumerate(world.runs()):
        task = store.agent_task(run.operation_id) if run.operation_id else None
        receipts = (
            store.list_consolidation_apply_receipts(run.operation_id) if run.operation_id else []
        )
        runs.append(
            (
                f"R{index}",
                _days(run.occurrence_date, today),
                run.kind,
                run.error_code,
                run.state,
                run.outcome_settled_at is not None,
                task.status if task is not None else None,
                any(receipt["result"] is not None for receipt in receipts),
                run.revisions_verified,
            )
        )
    members = tuple(
        (f"M{index}", task.status)
        for index, op in enumerate(world.members())
        if (task := store.agent_task(op)) is not None
    )
    with store.connection() as connection:
        request_target = (
            connection.execute(
                "SELECT 1 FROM graph_runs WHERE json_type(request_json,'$.graph_target') IS NOT NULL"
            ).fetchone()
            is not None
        )
    return View(
        (
            schedule is not None,
            _minutes(schedule.next_due_at, now) if schedule else None,
        ),
        schedule.last_outcome if schedule else None,
        (
            _minutes(target.next_due_at, now),
            target.covered_head,
            _days(target.last_occurrence_date, today),
            tuple(_days(day, today) for day in target.skipped_dates),
        )
        if target is not None
        else (),
        tuple(runs),
        members,
        store.chat_archived(world.project_id, world.chat_id),
        request_target,
    )


def _next_night(now: datetime) -> datetime:
    night = datetime.combine(now.date(), _NIGHT, now.tzinfo)
    return night if night > now else night + timedelta(days=1)


def _minutes(instant: str, now: datetime) -> int:
    return round((datetime.fromisoformat(instant) - now).total_seconds() / 60)


def _days(day: str | None, today) -> int | None:
    return None if day is None else (datetime.fromisoformat(day).date() - today).days


def _settle(world: World, operation_id: str, outcome: str) -> None:
    from rcp.storage.artifact_models import Artifact

    store = world.store
    if outcome == "success":
        if store.consolidation_run_for_operation(operation_id) is not None:
            now = store.now()
            store.create_artifact(
                Artifact(
                    artifact_id=f"report-{operation_id}",
                    project_id=world.project_id,
                    supplier="turn",
                    supplier_id=operation_id,
                    origin_operation_id=operation_id,
                    source_name="consolidation-report.html",
                    media_type="text/html",
                    created_at=now,
                    expires_at=now,
                ),
                data=_REPORT,
            )
        store.complete_agent_task(
            operation_id, applied_revision=None, result={"graph_update": {"status": "none"}}
        )
    elif outcome == "fail":
        store.fail_agent_task(operation_id, "Provider failed")
    else:
        store.checkpoint_agent_task(operation_id, native_session_id=f"session-{operation_id}")
        store.pause_agent_task(operation_id)


def _commit(world: World, operation_id: str) -> None:
    """The turn applies one keyed Patch through its consolidation Apply command."""

    import uuid

    from rcp.agents import AgentProcessControl
    from rcp.agents.command_mailbox import CommandTurnIdentity
    from rcp.agents.command_protocol import ApplyArguments, ApplyCommandRequest
    from rcp.background import AgentTaskExecution
    from rcp.runs.consolidation import consolidation_apply_handler
    from rcp.transport.workspace_mailbox import RunStageMailbox
    from tests.helpers import agent_patch_json, refresh_patch

    store = world.store
    task = store.agent_task(operation_id)
    assert task is not None
    stage = world.runtime.app.state.background_tasks.store.path.parent.parent / "stage"
    stage.mkdir(exist_ok=True)
    mailbox = RunStageMailbox(stage)
    name = world.lineage()[operation_id]
    mailbox.write_text("patch.json", agent_patch_json(refresh_patch(f"rq/nightly-{name.lower()}")))
    execution = AgentTaskExecution(operation_id, store, AgentProcessControl())
    handler = consolidation_apply_handler(
        lambda: world.runtime.app.state.catalog.open(world.project_id),
        execution,
        mailbox,
        list(task.request["run_truth_scope"]),
    )
    request = ApplyCommandRequest(
        mailbox_id="a" * 32,
        request_id=uuid.uuid4().hex,
        credential="b" * 64,
        verb="apply",
        idempotency_key="nightly",
        arguments=ApplyArguments(patch_file="patch.json"),
    )
    response = handler(request, CommandTurnIdentity(None, operation_id, "turn", "broker"))
    assert response.status == "ok", response.message


def _poll_without_launch(world: World) -> None:
    """A poll whose launch the engine refuses with a transient error, so the task stays queued."""

    def unavailable(_operation_id: str):
        raise OSError("provider admission unavailable")

    background = world.runtime.app.state.background_tasks
    launch = background.launch_admitted
    background.launch_admitted = unavailable
    try:
        world.poller.poll_once()
    finally:
        background.launch_admitted = launch


def _send(world: World) -> None:
    """A member's turn in the consolidation chat, admitted and launched."""

    runtime = world.runtime
    schedule = world.schedule()
    assert schedule is not None
    task = world.poller._task(schedule, runtime.service, "main", "main")
    task = task.model_copy(update={"request": {**task.request, "trigger": "human"}})
    task = world.store.create_agent_task(task)
    runtime.app.state.background_tasks.launch_admitted(task.operation_id)


def _seed_queued_request_target(runtime: SimpleNamespace) -> None:
    """0.4.35 claimed a run and queued its task with ``graph_target`` in the request."""

    with mock.patch.object(runtime.poller, "_launch", lambda _operation_id: None):
        runtime.poller.poll_once()
    (run,) = runtime.store.consolidation_runs()
    with runtime.store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET request_json=json_set(request_json, '$.graph_target', "
            "json(?)) WHERE operation_id=?",
            (json.dumps({"kind": "main", "branch_id": None}), run.operation_id),
        )


def _seed_archived_chat(runtime: SimpleNamespace) -> None:
    """A release before 0.4.38 let a member archive the consolidation chat."""

    from rcp.storage.consolidation import consolidation_chat_id

    project_id = runtime.app.state.default_project_id
    with runtime.store.connection() as connection:
        connection.execute(
            "INSERT INTO chat_display(project_id,chat_id,archived_user_id,archived_at) "
            "VALUES(?,?,?,?)",
            (project_id, consolidation_chat_id(project_id), "member", runtime.store.now()),
        )


def _always(*_args) -> bool:
    return True


def _runs(view: View) -> dict[str, tuple]:
    return {run[0]: run for run in view.runs}


def one_run_per_night(world: World) -> list[str]:
    """A target holds at most one run or skip for each local date."""

    target = world.store.consolidation_target(world.project_id)
    nights = [run.occurrence_date for run in world.runs()]
    nights.extend(target.skipped_dates if target is not None else [])
    return [f"{night} is claimed twice" for night in sorted(set(nights)) if nights.count(night) > 1]


def failure_keeps_coverage(before: View, world: World) -> list[str]:
    """A run that settles as a failure consumes its night and never moves the target's coverage."""

    target = world.store.consolidation_target(world.project_id)
    assert target is not None
    found = []
    for run in _newly_failed(before, world):
        if before.target and target.covered_head != before.target[1]:
            found.append(f"failed {run.occurrence_date} moved coverage to {target.covered_head}")
        if (target.last_occurrence_date or "") < run.occurrence_date:
            found.append(f"failed {run.occurrence_date} left its night owed")
    return found


def _newly_failed(before: View, world: World) -> list[ConsolidationRun]:
    settled = {name for name, *rest in before.runs if rest[4]}
    view = _view(world)
    return [
        run
        for run, row in zip(world.runs(), view.runs, strict=True)
        if row[2] == "failure" and row[0] not in settled
    ]


def _fails_now(before: View, world: World) -> bool:
    return bool(_newly_failed(before, world))


def settled_outcomes_are_final(before: View, world: World) -> list[str]:
    """A settled run's kind and error code never change."""

    after = _runs(_view(world))
    return [
        f"{name} changed from {row[2]}/{row[3]} to {after[name][2]}/{after[name][3]}"
        for name, row in _runs(before).items()
        if row[5] and (after[name][2], after[name][3], after[name][5]) != (row[2], row[3], True)
    ]


def _has_settled(before: View, _world: World) -> bool:
    return any(run[5] for run in before.runs)


def only_keep_or_dismiss_closes(before: View, world: World) -> list[str]:
    """Only a member's Keep or Dismiss of that row closes it, and a closed row stays closed."""

    action = world.last
    found = []
    for name, row in _runs(_view(world)).items():
        previous = _runs(before).get(name)
        state = previous[4] if previous is not None else "open"
        if row[4] == state:
            continue
        expected = {"keep": "kept", "dismiss": "dismissed"}.get(action.name if action else "")
        if state != "open" or action is None or action.args[:1] != (name,) or row[4] != expected:
            found.append(f"{name} went from {state} to {row[4]} on {action}")
    return found


def _has_run(_before: View, world: World) -> bool:
    return bool(world.runs())


def task_matches_run_target(world: World) -> list[str]:
    """Each run's task works in the run's graph target and its target's consolidation chat."""

    from rcp.storage.consolidation import consolidation_chat_id

    found = []
    for run in world.runs():
        if run.operation_id is None:
            continue
        task = world.store.agent_task(run.operation_id)
        assert task is not None
        chat = consolidation_chat_id(run.project_id, run.target)
        if task.graph_target != run.graph_target:
            found.append(f"{run.occurrence_date} task targets {task.graph_target}")
        if task.request.get("chat_id") != chat or run.chat_id != chat:
            found.append(f"{run.occurrence_date} task runs outside its consolidation chat")
    return found


def _has_task(world: World) -> bool:
    return any(run.operation_id is not None for run in world.runs())


def queued_task_launches(view: View) -> bool:
    """No unsettled run's task waits queued."""

    return not any(not run[5] and run[6] == "queued" for run in view.runs)


def due_target_advances(view: View) -> bool:
    """The schedule is not due, or its target holds an unsettled run."""

    scheduled, due_in = view.schedule
    return not scheduled or due_in > 0 or any(not run[5] for run in view.runs)


_SCHEDULE = "graph consolidation spec: Authorization and schedule"
_OUTCOME = "graph consolidation spec: Outcomes and Inbox rows"

RULES = (
    Safety(one_run_per_night, _always, _SCHEDULE),
    Step(failure_keeps_coverage, _fails_now, _SCHEDULE),
    Step(settled_outcomes_are_final, _has_settled, _OUTCOME),
    Step(only_keep_or_dismiss_closes, _has_run, _OUTCOME),
    Safety(task_matches_run_target, _has_task, "invariant 10g"),
    Goal(queued_task_launches, _SCHEDULE),
    Goal(due_target_advances, _SCHEDULE),
)
