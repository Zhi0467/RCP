from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from pydantic import TypeAdapter

from rcp.agents.continuation_prompt import SECTIONS
from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.api import create_app
from rcp.background import BackgroundAgentTasks
from rcp.core.models import AuthorizedHuman, Patch
from rcp.core.operations import GraphOperation, ProposalOperation
from rcp.history import HistoryManager
from rcp.storage import ACTIVE_AGENT_TASK_STATUSES, AgentTaskRecord, AppStore

# Background tasks run on their own thread, so this bounds a genuine hang rather
# than the expected duration. The poll returns the moment the task is terminal,
# so a generous bound costs nothing on success, while a tight one invents
# failures whenever the full suite is competing for the CPU.
TASK_SETTLE_TIMEOUT = 60.0
_TASK_POLL_INTERVAL = 0.01

# A syntactically valid UUID that is deliberately not version 4, for the cases
# that assert canonical-UUIDv4 rejection. Generating one with `uuid.uuid1()`
# inside a `parametrize` decorator gives every xdist worker a different test id,
# which fails collection, so the non-canonical value is a constant.
NON_UUID4 = "1d2e3f40-a73f-11f1-83be-717957a9b5d0"

_GRAPH_OPERATION_ADAPTER = TypeAdapter(GraphOperation)
_PROPOSAL_OPERATION_ADAPTER = TypeAdapter(ProposalOperation)
_T = TypeVar("_T")

_RCP_OWNED_ITEM_FIELDS = {
    "create_nodes": ("nodes", {"standing", "created_rev", "updated_rev"}),
    "create_edges": ("edges", {"layer", "created_rev"}),
    "create_ambiguities": ("ambiguities", {"raised_rev"}),
    "create_proposals": (
        "proposals",
        {
            "related_node_ids",
            "related_edge_ids",
            "related_config_keys",
            "base_rev",
            "status",
            "raised_rev",
            "resolved_rev",
            "rejection_reason",
        },
    ),
    "upsert_glossary": ("terms", {"updated_rev"}),
}


def launch_contract_path(prompt: str) -> Path:
    """The contract file a launch names: its master pointer or bootstrap, else line two."""

    named = re.search(
        re.escape(SECTIONS["master_pointer"].split("{path}")[0]) + r"([^`]+)`", prompt
    ) or re.search(re.escape(SECTIONS["master_bootstrap"].split("{path}")[0]) + r"(.+)", prompt)
    return Path(named[1] if named else prompt.splitlines()[1].strip())


def changed_values(prompt: str) -> dict[str, str]:
    """The changed values a continuation sends, by dotted key; strings lose their backticks."""

    header = SECTIONS["context_delta"]
    if header not in prompt:
        return {}
    lines = prompt[prompt.index(header) + len(header) :].split("\n\n", 1)[0].splitlines()
    return {
        key: value.strip("`")
        for key, value in (line[2:].split(": ", 1) for line in lines if line.startswith("- "))
    }


def current_command_client(prompt: str) -> str:
    """The command client a launch runs staged commands through.

    A continuation that changed it sends it; otherwise the session's master names it.
    """

    changed = changed_values(prompt).get("patch.command_client")
    if changed is not None:
        return changed
    master = launch_contract_path(prompt).read_text(encoding="utf-8")
    return re.search(r"^- Command client: `([^`]+)`$", master, re.MULTILINE)[1]  # type: ignore[index]


@functools.cache
def _frozen_backend_inventory() -> frozenset[str]:
    from PyInstaller.utils.hooks import collect_data_files

    from rcp.frozen_resources import resource_manifest

    return frozenset(resource_manifest(collect_data_files("rcp", include_py_files=True)))


def assert_frozen_backend_ships(*resources: str) -> None:
    """Each package-relative resource is in the inventory the frozen backend validates.

    The desktop spec collects the whole `rcp` package as data and the startup
    hook checks that build-derived inventory, so a resource ships exactly when
    this collection lists it; there is no hand-maintained allowlist to assert.
    """

    inventory = _frozen_backend_inventory()
    for resource in resources:
        assert f"rcp/{resource}" in inventory, f"{resource} is not packaged"


def signed_in_client(app: Any, **kwargs: Any) -> TestClient:
    """Get a personal owner cookie through the production redemption endpoint.

    Team fixtures supply their own member admission. Use TestClient directly
    for negative authentication tests and public-route checks.
    """

    client = TestClient(app, **kwargs)
    if app.state.space_kind == "personal":
        code = _store_of(app).create_owner_sign_in_code()
        response = client.post("/api/owner/redeem", json={"code": code})
        assert response.status_code == 200, response.text
    return client


async def sign_in_async_client(client: AsyncClient, app: Any) -> None:
    """Install a production owner cookie on an async disposable-app client."""

    if app.state.space_kind == "personal":
        code = _store_of(app).create_owner_sign_in_code()
        response = await client.post("/api/owner/redeem", json={"code": code})
        assert response.status_code == 200, response.text


def create_named_app(*args: Any, **kwargs: Any):
    """Create a named test app."""

    app = create_app(*args, **kwargs)
    store = app.state.background_tasks.store
    if app.state.space_kind == "personal":
        owner = store.local_owner
        if owner is not None and owner.display_name is None:
            store.rename_space_user(owner.user_id, "Test researcher")
    return app


def _store_of(app_or_store: Any) -> AppStore:
    """Accept either an app or the store itself, so callers keep whichever they hold."""

    if isinstance(app_or_store, AppStore):
        return app_or_store
    return app_or_store.state.background_tasks.store


def authorized_human(
    app_or_store: Any, *, display_name: str = "Test researcher"
) -> AuthorizedHuman:
    """The local owner as a patch author, naming them if the app has not already."""

    store = _store_of(app_or_store)
    owner = store.local_owner
    assert owner is not None, "app has no local owner"
    if owner.display_name is None:
        owner = store.rename_space_user(owner.user_id, display_name)
    return AuthorizedHuman(
        space_id=store.space_id,
        user_id=owner.user_id,
        display_name=owner.display_name,
    )


def seated_on_every_project(_project_id: str, _user_id: str) -> bool:
    """A membership check for histories built without a project catalog.

    `HistoryManager` requires one whenever it can resolve agent authority, so a
    test that fabricates its own resolver supplies this. Membership itself is
    exercised in `test_project_membership.py` against a real store.
    """

    return True


def write_local_test_manifest(directory: Path) -> Path:
    """A real local execution account for engine tests that resolve run_on."""

    path = directory / "manifest.toml"
    path.write_text(
        'name = "Test project"\n'
        '[[machines]]\nalias = "laptop"\nhost = ""\n'
        '[[repositories]]\nalias = "repo"\nmachine = "laptop"\n'
        f"path = {json.dumps(str(directory))}\n"
        '[project]\ntruth_scope = ["repo"]\n'
        '[state]\nrepository = "repo"\n'
        '[agent]\ndefault_run_truth_scope = ["repo"]\n'
        '[execution]\nrun_on = "laptop"\n',
        encoding="utf-8",
    )
    return path


def store_test_claude_token(store: AppStore, host: str = "") -> None:
    """Give fake Claude execution an explicit managed credential."""

    ProviderCredentialStore.for_data_dir(store.path.parent).store_token(
        "claude", host, "test-setup-token", member_id="fixture-member", now=store.now()
    )


def fabricated_authorizer(display_name: str = "Campaign owner") -> AuthorizedHuman:
    """A synthetic authorizer for stores that were never opened as an app."""

    return AuthorizedHuman(
        space_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        display_name=display_name,
    )


def record_launched_experiment_turn(store: AppStore, operation_id: str) -> None:
    """Record what a real Experiment launch holds before its provider starts."""

    for role, content in (
        ("experiment_episode_context_candidate", "{}"),
        ("work", "task contract"),
    ):
        store.record_agent_task_contract(
            operation_id, role, content, hashlib.sha256(content.encode()).hexdigest()
        )


def wait_until(
    probe: Callable[[], _T | None],
    *,
    timeout: float = 5.0,
    interval: float = _TASK_POLL_INTERVAL,
    detail: str | Callable[[], str] = "condition was not met",
    allow_falsy: bool = False,
) -> _T:
    """Return the first settled result; opt in when false or zero means settled."""

    deadline = time.monotonic() + timeout
    while True:
        result = probe()
        if result is not None and (allow_falsy or bool(result)):
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(detail() if callable(detail) else detail)
        time.sleep(min(interval, remaining))


async def async_wait_until(
    probe: Callable[[], _T | None],
    *,
    timeout: float = TASK_SETTLE_TIMEOUT,
    interval: float = _TASK_POLL_INTERVAL,
    detail: str | Callable[[], str] = "condition was not met",
    allow_falsy: bool = False,
) -> _T:
    """`wait_until` for a probe that must not block the running event loop.

    A concurrency test drives the work it is waiting on through that same loop,
    so polling it with the synchronous helper would deadlock. Awaiting here
    yields between probes instead. The settle rule is `wait_until`'s, so the two
    agree on what a falsy result means.
    """

    deadline = time.monotonic() + timeout
    while True:
        result = probe()
        if result is not None and (allow_falsy or bool(result)):
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(detail() if callable(detail) else detail)
        await asyncio.sleep(min(interval, remaining))


def wait_for_entry(
    entered: threading.Event,
    *,
    detail: str = "the blocked call was never entered",
) -> None:
    """Block until a patched call reports that it was entered.

    Entry is setup, never the promise: the assertions that follow measure what
    happens *while* that call is parked. A tight bound here measures only how
    loaded the runner is, so this uses the shared settle bound. Give the paired
    release event the same bound, or the hold can expire mid-assertion.

    Call it through ``asyncio.to_thread`` from a coroutine, so the loop stays
    free to run the request that is expected to enter the patched call.
    """

    assert entered.wait(TASK_SETTLE_TIMEOUT), detail


def wait_for_task(
    app_or_store: Any,
    operation_id: str,
    *,
    expect: str | None = None,
    timeout: float = TASK_SETTLE_TIMEOUT,
) -> AgentTaskRecord:
    """Poll the store until the task leaves every active status."""

    store = _store_of(app_or_store)

    def settled_task() -> AgentTaskRecord | None:
        record = store.agent_task(operation_id)
        assert record is not None, f"task {operation_id} was never recorded"
        if record.status not in ACTIVE_AGENT_TASK_STATUSES:
            return record
        return None

    record = wait_until(
        settled_task,
        timeout=timeout,
        detail=f"task {operation_id} did not settle within {timeout}s",
    )
    if expect is not None:
        assert record.status == expect, (
            f"task {operation_id} settled as {record.status!r}, expected {expect!r}: "
            f"{record.error or record.status_message}"
        )
    return record


def wait_for_task_response(
    client: Any,
    project_id: str,
    operation_id: str,
    *,
    expect: str | None = None,
    timeout: float = TASK_SETTLE_TIMEOUT,
) -> dict[str, Any]:
    """Poll the task route until the task leaves every active status."""

    def settled_task() -> dict[str, Any] | None:
        response = client.get(f"/api/projects/{project_id}/tasks/{operation_id}")
        assert response.status_code == 200, response.text
        task = response.json()
        if task["status"] not in ACTIVE_AGENT_TASK_STATUSES:
            return task
        return None

    task = wait_until(
        settled_task,
        timeout=timeout,
        detail=f"task {operation_id} did not settle within {timeout}s",
    )
    if expect is not None:
        assert task["status"] == expect, (
            f"task {operation_id} settled as {task['status']!r}, expected {expect!r}: "
            f"{task.get('error') or task.get('status_message')}"
        )
    return task


def append_fixture_patch(service: Any, patch: Patch, **kwargs: Any):
    """Prepare canonical graph state without impersonating a production agent task."""

    fixture_history = HistoryManager(service.manifest, service.history.workspace)
    appended, result = fixture_history.append(patch, **kwargs)
    # Mirror the cache update that the production manager would have performed
    # if this test-only legacy fixture had gone through its guarded admission.
    service.history._remember_accepted_revision(result)
    return appended, result


def agent_patch_json(patch: Patch) -> str:
    """Render canonical test data as the semantic JSON an agent may write."""

    operations = [operation.model_dump(mode="json", exclude_unset=True) for operation in patch.ops]
    for operation in operations:
        owned = _RCP_OWNED_ITEM_FIELDS.get(operation.get("op"))
        if owned is None:
            continue
        field, excluded = owned
        operation[field] = [
            {key: value for key, value in item.items() if key not in excluded}
            for item in operation.get(field, [])
        ]
    return json.dumps(
        {
            "summary": patch.summary,
            "ops": operations,
            "repositories_read": list(patch.repositories_read),
            "change_summary": list(patch.change_summary),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def refresh_patch(node_id: str = "rq/transfer-after-shift") -> Patch:
    """A minimal refresh patch that applies cleanly on top of ``seed_patch``."""
    return Patch(
        kind="refresh",
        author="agent",
        summary="Recorded a second research question.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": node_id,
                        "type": "research_question",
                        "title": "Transfer after task shift",
                        "question": "Does replanning transfer to an unseen task family?",
                        "motivation": "The seed corpus left transfer unexamined.",
                        "scope": "Matched compute across task families.",
                        "status": "open",
                    }
                ],
            }
        ],
        change_summary=[f"Added {node_id}."],
    )


def shape_invalid_patch() -> Patch:
    """A core-valid Patch using an operation absent from the agent schema."""
    return Patch(
        kind="refresh",
        author="agent",
        summary="Used an operation that is not in the agent schema.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "set_ontology",
                "ontology": {"types": [], "fields": [], "relations": []},
            }
        ],
    )


def graph_operation(document: dict[str, Any]) -> GraphOperation:
    """Parse one exact GraphOperation for direct contract-level test calls."""

    return _GRAPH_OPERATION_ADAPTER.validate_python(document)


def proposal_operation(document: dict[str, Any]) -> ProposalOperation:
    """Parse one exact ProposalOperation for direct contract-level test calls."""

    return _PROPOSAL_OPERATION_ADAPTER.validate_python(document)


def gated_patch() -> Patch:
    """Well formed, but asks for a transition the graph gates behind a Proposal."""
    return Patch(
        kind="refresh",
        author="agent",
        summary="Tried to bypass a gated transition.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "hyp/replanning-restores-plasticity",
                        "changes": {"status": "supported"},
                    }
                ],
            }
        ],
    )


def seed_patch() -> Patch:
    return Patch(
        kind="seed",
        author="agent",
        summary="Seeded the project question and initial hypothesis.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "rq/learning-after-shift",
                        "type": "research_question",
                        "title": "Learning after task shift",
                        "question": "Can the learner retain its ability to adapt after the task changes?",
                        "motivation": "Persistent agents encounter repeated changes.",
                        "scope": "Matched compute and update histories.",
                        "status": "open",
                    },
                    {
                        "id": "hyp/replanning-restores-plasticity",
                        "type": "hypothesis",
                        "title": "Replanning restores plasticity",
                        "statement": "Search-time replanning restores future learning ability.",
                        "rationale": "It may reduce dependence on stale value features.",
                        "predictions": ["The unseen-task learning curve recovers."],
                        "status": "proposed",
                    },
                ],
            },
            {
                "op": "create_edges",
                "edges": [
                    {
                        "source": "rq/learning-after-shift",
                        "target": "hyp/replanning-restores-plasticity",
                        "relation": "has_hypothesis",
                    }
                ],
            },
        ],
    )


def isolate_host(
    monkeypatch: pytest.MonkeyPatch, root: Path, *, provider_discovery: bool = False
) -> list[BackgroundAgentTasks]:
    """Keep one test, or one module template build, off the developer's machine.

    Returns the provider engines constructed under the patch; the caller shuts
    them down. A module-scoped template build applies this with its own
    `MonkeyPatch`, because the function-scoped autouse fixture has not run yet.
    """

    # A provider launch takes an advisory lock under the account's RCP
    # directory. Left alone, every test would contend on one real file and
    # leave state in `~/.rcp`, so each test gets its own root.
    monkeypatch.setattr(
        "rcp.agents.credential_gate._DEFAULT_ACCOUNT_LOCK_ROOT", root / "credential-locks"
    )
    # Keep RCP's own `~/.rcp` temporary files out of the human's home.
    monkeypatch.setattr("rcp.rcp_home.rcp_home", lambda: root / "rcp-home")
    # The SSH control directory is keyed by user account, not by data
    # directory, so the developer's own RCP keeps its live masters in the same
    # place a test would sweep. Starting an app runs that sweep, which means the
    # whole suite reaches it, not one test. Each test gets its own root instead.
    monkeypatch.setattr("rcp.transport.ssh._control_directory_path", lambda: root / "ssh-control")

    # Discovery is the seam every unconfigured local provider execution passes
    # through: readiness runs `--version`, `login status` and `debug models`
    # through the path it returns, and a turn's own `codex exec` is built from
    # that same path. No fixture configures a binary, so leaving discovery live
    # meant a test that dispatched a task prompted the developer's own
    # authenticated CLI and billed a real provider turn no assertion ever read.
    # Returning nothing makes readiness `unconfigured`, which refuses the launch
    # before a command is built.
    #
    # Readiness reaches this only for an unconfigured local provider, so an
    # explicit `binary=` is unaffected; that is how the env-gated live
    # qualification in `test_server_provider_readiness_live.py` still probes a
    # real CLI on purpose. A test that needs a configured provider patches
    # `AgentLauncher.readiness` or replaces `BackgroundAgentTasks.stream`; one
    # that needs a real child process stages its own stub through
    # `sys.executable`.
    #
    # The `real_provider_discovery` marker opts a test out. It is for the tests
    # of discovery itself, which replace `PATH` and the OS account home, in this
    # process and in any subprocess they run, so they reach no installed binary
    # either.
    if not provider_discovery:
        monkeypatch.setattr("rcp.agents.launcher._discover_local_provider", lambda provider: None)

    # A lifespan and a compute settings save check compute routes, and a helper
    # probe starts a real launchd or systemd job. A test of the refresh restores
    # the real function and stubs the probe itself.
    for owner in ("rcp.api.app", "rcp.api.project_state"):
        monkeypatch.setattr(f"{owner}.refresh_compute_probes", lambda *args, **kwargs: None)

    # `shutdown` kills each live provider process group, and the app lifespan
    # is the only caller. Most tests build their app with a bare `create_app`
    # or an unentered `TestClient`, so no lifespan ever runs; their workers are
    # daemon threads, so interpreter exit drops them without unwinding and the
    # staged broker plus its provider child are reparented and left running,
    # holding a `~/.rcp/sockets/rcp-command-*.sock` for as long as they live.
    # Registering at construction covers every engine a test creates, including
    # the ones reached through `create_app` and server-operation validation.
    engines: list[BackgroundAgentTasks] = []
    construct = BackgroundAgentTasks.__init__

    def register(self: BackgroundAgentTasks, *args: object, **kwargs: object) -> None:
        construct(self, *args, **kwargs)  # type: ignore[arg-type]
        engines.append(self)

    monkeypatch.setattr(BackgroundAgentTasks, "__init__", register)

    # An app lifespan teardown sets the process-wide fence; without isolation a
    # later lock wait in the same worker would abort for no reason.
    monkeypatch.setattr("rcp.transport.state._CANONICAL_LOCK_WAIT_FENCE", threading.Event())

    # `_print_wrapped` takes its width from `shutil.get_terminal_size`, so every
    # assertion on rendered prose otherwise depends on the ambient terminal. The
    # same install plan passes at 100 columns and fails at 80, where a phrase a
    # test searches for straddles a line break. 100 is the renderer's own
    # fallback, so this pins the value an undetectable terminal already produces.
    monkeypatch.setenv("COLUMNS", "100")

    # Test apps never contact GitHub; transport tests opt in against loopback.
    monkeypatch.setenv("RCP_UPDATE_CHECK", "off")

    # App tests never inspect or alter the host machine's real power state.
    from rcp.machine_power import MachinePowerController

    def refuse_command(*args, **kwargs):
        raise AssertionError("App tests must inject machine power commands")

    monkeypatch.setattr(
        "rcp.api.app.MachinePowerController",
        lambda *args, **kwargs: MachinePowerController(
            *args, platform="linux", run=refuse_command, spawn=refuse_command, **kwargs
        ),
    )
    return engines


@contextmanager
def isolated_template_build(root: Path) -> Iterator[pytest.MonkeyPatch]:
    """Isolate a module template build the way `isolated_host` isolates a test."""

    with pytest.MonkeyPatch.context() as monkeypatch:
        engines = isolate_host(monkeypatch, root)
        try:
            yield monkeypatch
        finally:
            for engine in engines:
                engine.shutdown()


def reset_from_template(template: Path, live: Path) -> None:
    """Give a test the template's exact tree at the fixed path its proofs name."""

    shutil.rmtree(live, ignore_errors=True)
    shutil.copytree(template, live, symlinks=True)
