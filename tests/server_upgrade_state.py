"""Space state the previous release leaves behind, and the candidate's first reads of it.

0.4.35 passed the installed upgrade and then failed on a team server: an old
display cache (coach on its own machine) made the Runs list return 503, and its
own nightly consolidation queued a request its launch refused every minute. The
seed never held that state and no check read the Runs list or ran the schedule.

Seeds run against the previous release, through its HTTP routes or, with the
service stopped, its own consolidation poller. Checks run on the candidate.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

COACH_MACHINE = "coach-gpu"
BRANCH_NAME = "upgrade-consolidation"


def seed_space_state(request, fixture: dict, cookie: str) -> dict:
    """Through the serving previous release: coach machine, ended Experiment, nightly schedule.

    Each part is seeded only where the release has its routes.
    """
    with request("/openapi.json") as response:
        schema = json.load(response)
    routes = set(schema["paths"])
    settings = set(schema["components"]["schemas"]["ProjectSettingsRequest"]["properties"])
    with request("/api/health") as response:
        version = tuple(
            int(part) for part in json.load(response)["version"].split("+")[0].split(".")
        )
    base = f"/api/projects/{fixture['project_id']}"
    with request(base, cookie=cookie) as response:
        project = json.load(response)
    # 0.4.35 runs every agent on the project's base machine; earlier releases let
    # the coach choose its own, and their display cache records that choice.
    coach = version < (0, 4, 35) and "/api/projects/{project_id}/machines" in routes
    experiment = "/api/projects/{project_id}/experiments/{node_id}/run" in routes
    schedule = "/api/projects/{project_id}/consolidation/schedule" in routes
    seeded: dict[str, object] = {
        "coach_machine": None,
        "experiment_episode_id": None,
        "schedule": schedule,
        "branch_id": None,
    }
    if coach:
        _add_coach_machine(request, base, cookie)
        seeded["coach_machine"] = COACH_MACHINE
    if coach or schedule:
        _put_profiles(request, base, project, settings, cookie, coach=coach, defer_chat=schedule)
    if experiment:
        # The space indexes read a main loop through the main display cache.
        seeded["experiment_episode_id"] = _ended_experiment(request, base, cookie)
    if schedule:
        # A minute ahead keeps the admitted run's timestamps near the real clock.
        due = datetime.now(UTC) + timedelta(minutes=1)
        with request(
            f"{base}/consolidation/schedule",
            method="PUT",
            body={"local_time": due.strftime("%H:%M"), "timezone": "UTC"},
            cookie=cookie,
        ):
            pass
    return seeded


def seed_edited_branch(request, fixture: dict, cookie: str) -> str | None:
    """A branch with one human edit since the previous release's last night.

    The candidate consolidates main and each branch. The previous release's
    queued run holds main, so only this branch makes the candidate admit a run.
    """
    if not fixture["upgrade_state"]["schedule"]:
        return None
    base = f"/api/projects/{fixture['project_id']}"
    with request(f"{base}/graph", cookie=cookie) as response:
        revision = json.load(response)["revision"]
    with request(
        f"{base}/graph-refs",
        method="POST",
        body={"request_id": str(uuid.uuid4()), "at": f"main@{revision}", "name": BRANCH_NAME},
        cookie=cookie,
    ) as response:
        branch_id = json.load(response)["branch_id"]
    _sync(
        request,
        base,
        cookie,
        {"blk/recovery": {"description": "The recovery host needs a second disk."}},
        branch_id=branch_id,
    )
    return branch_id


def _add_coach_machine(request, base: str, cookie: str) -> None:
    with request(
        "/api/space/machines",
        method="POST",
        # Naming the account skips the SSH login probe; nothing connects to it.
        body={"name": "Coach GPU", "host": "coach-gpu.invalid", "os_account": "rcp"},
        cookie=cookie,
    ) as response:
        machine_id = json.load(response)["machine_id"]
    with request(
        f"{base}/machines",
        method="POST",
        body={"machine_id": machine_id, "alias": COACH_MACHINE},
        cookie=cookie,
    ):
        pass


def _put_profiles(
    request,
    base: str,
    project: dict,
    settings: set[str],
    cookie: str,
    *,
    coach: bool,
    defer_chat: bool,
) -> None:
    """Save the project's settings as read, with only the agent profiles changed.

    `settings` names the fields this release's settings request accepts.
    """
    keys = ("provider", "runtime", "model", "reasoning", "run_on")
    profiles = {
        surface: {key: profile[key] for key in keys}
        for surface, profile in project["agent_profiles"].items()
    }
    if coach:
        profiles["paper_coach"]["run_on"] = COACH_MACHINE
    if defer_chat:
        # Nightly runs use the chat profile. Claude is signed out, so every launch
        # defers and the run stays queued through each restart and the update.
        profiles["project_chat"].update(provider="claude", runtime="", model="")
    with request(
        f"{base}/settings",
        method="PUT",
        body={field: project[field] for field in settings if field in project}
        | {"agent_profiles": profiles},
        cookie=cookie,
    ) as response:
        # The response is the display snapshot the release just cached.
        cached = json.load(response)["agent_profiles"]
    assert {surface: cached[surface]["run_on"] for surface in profiles} == {
        surface: profile["run_on"] for surface, profile in profiles.items()
    }, cached


def _ended_experiment(request, base: str, cookie: str) -> str:
    """Make exp/recovery ready on main, start it, and wait until its turn ends.

    No provider can run on the runner, so the first turn fails; the loop's rows remain.
    """
    _sync(
        request,
        base,
        cookie,
        {
            "blk/recovery": {"status": "resolved"},
            "dec/recovery": {
                "options": ["Keep the path", "Replace the path"],
                "selected_option": "Keep the path",
                "status": "decided",
            },
        },
    )
    with request(
        f"{base}/experiments/{quote('exp/recovery', safe='')}/run",
        method="POST",
        body={
            "provider": "codex",
            "reasoning": "medium",
            "run_on": "server",
            "run_truth_scope": ["paper"],
            "chat_id": str(uuid.uuid4()),
        },
        cookie=cookie,
    ) as response:
        task = json.load(response)
    deadline = time.monotonic() + 60
    while task["status"] in {"queued", "running"}:
        assert time.monotonic() < deadline, task
        time.sleep(0.5)
        with request(f"{base}/tasks/{task['operation_id']}", cookie=cookie) as response:
            task = json.load(response)
    return task["episode_id"]


def _sync(request, base: str, cookie: str, changes: dict, *, branch_id: str = "") -> None:
    """One human graph edit through Sync, on main or one branch."""
    query = f"?branch_id={branch_id}" if branch_id else ""
    with request(f"{base}/graph{query}", cookie=cookie) as response:
        graph = json.load(response)
    with request(
        f"{base}/sync{query}",
        method="POST",
        body={
            "base_revision": graph["revision"],
            "nodes": [
                {
                    "node_id": node_id,
                    "base_updated_rev": graph["nodes"][node_id]["updated_rev"],
                    "changes": node_changes,
                }
                for node_id, node_changes in changes.items()
            ],
        },
        cookie=cookie,
    ):
        pass


def admit_due_consolidation(data_dir: Path) -> str:
    """Admit the schedule's next night with the previous release's own poller.

    Runs with the service stopped. The poller writes the run and its queued turn;
    the stand-in launcher only records it. Live services defer its launch for the
    signed-out Claude login, so it stays queued into the candidate.
    """
    from rcp.api import create_app
    from rcp.consolidation import ConsolidationPoller

    app = create_app(data_dir=data_dir)
    tasks = app.state.background_tasks
    store = tasks.store
    (schedule,) = store.consolidation_schedules()
    launched: list[str] = []

    class RecordedLaunches:
        def __init__(self) -> None:
            self.dispatch_authority_resolver = tasks.dispatch_authority_resolver

        def launch_admitted(self, operation_id: str) -> None:
            launched.append(operation_id)

    ConsolidationPoller(
        store,
        RecordedLaunches(),  # type: ignore[arg-type]
        service_for=app.state.catalog.open,
        clock=lambda: schedule.next_due_at,
    ).poll_once()
    (operation_id,) = launched
    task = store.agent_task(operation_id)
    assert task is not None and task.status == "queued", task
    # 0.4.10's store has no close; this helper process exits next.
    return operation_id


def seed_shared_consolidation_workspace(data_dir: Path, project_id: str, target: str) -> None:
    """Write two completed turns as the installed previous release would."""
    from rcp.consolidation import consolidation_chat_id
    from rcp.core.transition_models import GraphTargetRef
    from rcp.storage import AgentTaskRecord, AppStore

    store = AppStore(data_dir / "rcp.sqlite3")
    try:
        now = store.now()
        for _ in range(2):
            store.create_agent_task(
                AgentTaskRecord(
                    operation_id=str(uuid.uuid4()),
                    project_id=project_id,
                    kind="project_chat",
                    status="succeeded",
                    request={"chat_id": consolidation_chat_id(project_id, target)},
                    graph_target=GraphTargetRef()
                    if target == "main"
                    else GraphTargetRef(kind="branch", branch_id=target),
                    stage_root=str(data_dir / "run-stage" / "shared-consolidation"),
                    created_at=now,
                    updated_at=now,
                    finished_at=now,
                    status_message="Completed.",
                )
            )
    finally:
        store.close()


def assert_space_reads(request, fixture: dict, seeded: dict, cookie: str) -> None:
    """The candidate's space-wide reads, before any project opens, serve old caches and rows."""
    project_id = fixture["project_id"]
    with request("/api/space/runs", cookie=cookie) as response:
        listed = {entry["episode_id"] for entry in json.load(response)}
    # An unreadable display cache drops the loop from the list with only a warning.
    if seeded["experiment_episode_id"] is not None:
        assert seeded["experiment_episode_id"] in listed, listed
    loops = "mode=experiment_loop"
    for path in (
        f"/api/episodes?{loops}",
        f"/api/projects/{project_id}/experiment-episodes?{loops}",
        f"/api/projects/{project_id}/consolidation",
    ):
        with request(path, cookie=cookie) as response:
            payload = json.load(response)
        # The Experiment indexes degrade an unreadable loop to a row instead of failing.
        if "episodes" in path:
            assert payload["unavailable"] == [], (path, payload["unavailable"])
    # Every project resource a member page subscribes to, as the rehearsal reads them.
    from rcp.server_ops.application_validation import _WATERMARK_RESOURCES

    with request(
        "/api/resource-watermarks",
        method="POST",
        body={
            "client_id": "installed-upgrade",
            "open": [{"project_id": project_id, "resources": list(_WATERMARK_RESOURCES)}],
            "due": [project_id],
        },
        cookie=cookie,
    ):
        pass


def check_queued_launches(data_dir: Path) -> dict:
    """Launch-validate every queued row, then run one due pass of the schedule poller.

    Runs with the service stopped, behind the startup fence the update rehearsal
    uses, so no provider starts. Consolidation turns launch through the poller,
    as they do live; every other queued turn goes straight to launch validation.
    Any refusal, or any WARNING or worse the candidate logs, fails the check.
    """
    import asyncio

    from rcp.__main__ import instance_lock
    from rcp.api import create_app
    from rcp.background import StartupEffectFence
    from rcp.server_ops.application_validation import (
        _phase_log,
        _RehearsalLaunches,
        _verify_due_consolidation,
    )

    async def check() -> tuple[set[str], list[str], list[str], list[str], list[str]]:
        fence = StartupEffectFence("installed upgrade launch check")
        app = create_app(data_dir=data_dir, startup_effect_fence=fence)
        async with app.router.lifespan_context(app):
            tasks = app.state.background_tasks
            store = tasks.store
            projects = {project.project_id for project in store.projects()}
            pending = store.pending_consolidation_operation_ids()
            queued_tasks = [
                task
                for project_id in projects
                for task in store.all_project_agent_tasks(project_id)
                if task.status == "queued"
            ]
            queued = sorted(t.operation_id for t in queued_tasks if t.operation_id not in pending)
            consolidation = sorted(
                t.operation_id for t in queued_tasks if t.operation_id in pending
            )
            launches = _RehearsalLaunches(tasks, projects)
            for operation_id in queued:
                launches.launch_admitted(operation_id)
            before = {run.run_id for run in store.consolidation_runs()}
            await asyncio.to_thread(_verify_due_consolidation, app, projects)
            admitted = sorted(
                run.operation_id
                for run in store.consolidation_runs()
                if run.run_id not in before and run.operation_id is not None
            )
        return projects, queued, consolidation, admitted, fence.attempted_effects

    with instance_lock(data_dir, timeout=0.0), _phase_log() as log:
        projects, queued, consolidation, admitted, effects = asyncio.run(check())
    # Every project is live here, so no warning is about uncopied state.
    refusal = log.refusal(projects)
    assert refusal is None, refusal
    assert not effects, effects
    return {"validated": queued, "consolidation_queued": consolidation, "admitted": admitted}
