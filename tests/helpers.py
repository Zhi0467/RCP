"""Shared test helpers.

TASK_SETTLE_TIMEOUT bounds a genuine hang, not the expected duration; the poll
returns as soon as a task is terminal, so a tight bound only invents failures under a
busy suite. NON_UUID4 is a constant because a `uuid.uuid1()` call inside
`parametrize` gives each xdist worker a different test id and fails collection.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from fastapi.testclient import TestClient
from httpx import AsyncClient
from pydantic import TypeAdapter

from rcp.core.authority import WriteAuthority, Writer
from rcp.core.models import AuthorizedHuman, BranchMergeReceipt, GraphState, Patch
from rcp.core.operations import GraphOperation, ProposalOperation
from rcp.core.transition_models import GraphTargetRef
from rcp.core.validation.report import ValidationReport
from rcp.storage import ACTIVE_AGENT_TASK_STATUSES, AgentTaskRecord, AppStore

if TYPE_CHECKING:
    import pytest

    from rcp.agents.project_repositories import RepositoryReadScope
    from rcp.background import BackgroundAgentTasks
    from rcp.config import Manifest


TASK_SETTLE_TIMEOUT = 60.0
_TASK_POLL_INTERVAL = 0.01

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


class _UncheckedWriteAuthority(WriteAuthority):
    """Test fixture policy preserving legacy unstamped patch admission."""

    def admit(
        self,
        state: GraphState,
        patch: Patch,
        *,
        project_id: str | None,
        authorized_by: AuthorizedHuman | None,
        apply_target: GraphTargetRef,
    ) -> tuple[Patch, Writer, ValidationReport]:
        return patch, Writer.from_patch(patch), self.validate_permissions(state, patch)

    def require_no_change_merge(
        self, receipt: BranchMergeReceipt, *, project_id: str | None
    ) -> None:
        pass


def read_scope_of(*aliases: str) -> RepositoryReadScope:
    """A launch read scope naming only this project's repositories.

    Imported locally because installed-upgrade seeding imports this module under older
    releases.
    """

    from rcp.agents.project_repositories import RepositoryReadScope

    return RepositoryReadScope(
        project_id="project", paths={alias: f"/{alias}" for alias in aliases}
    )


def unchecked_write_authority() -> WriteAuthority:
    return _UncheckedWriteAuthority()


def launch_contract_path(prompt: str) -> Path:
    """The contract file a launch names: its master pointer or bootstrap, else line two."""

    from rcp.agents.continuation_prompt import SECTIONS

    named = re.search(
        re.escape(SECTIONS["master_pointer"].split("{path}")[0]) + r"([^`]+)`", prompt
    ) or re.search(re.escape(SECTIONS["master_bootstrap"].split("{path}")[0]) + r"(.+)", prompt)
    return Path(named[1] if named else prompt.splitlines()[1].strip())


def changed_values(prompt: str) -> dict[str, str]:
    """The changed values a continuation sends, by dotted key; strings lose their backticks."""

    from rcp.agents.continuation_prompt import SECTIONS

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


def write_test_manifest(tmp_path: Path) -> Manifest:
    """Write the two-repository test project under ``tmp_path`` and load it."""

    from rcp.config import load_manifest

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    research = repo_a / ".research"
    research.mkdir()
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    claude_root.mkdir()
    codex_root.mkdir()
    from rcp.machine_cards import BACKEND_MACHINE_ID

    path = research / "manifest.toml"
    path.write_text(
        f'''name = "test-paper"

machines = ["{BACKEND_MACHINE_ID}"]

[[repositories]]
alias = "repo-a"
machine = "{BACKEND_MACHINE_ID}"
path = "{repo_a}"

[[repositories]]
alias = "repo-b"
machine = "{BACKEND_MACHINE_ID}"
path = "{repo_b}"

[project]
truth_scope = ["repo-a", "repo-b"]

[state]
repository = "repo-a"

[agent]
default_run_truth_scope = ["repo-a"]

[sources]
claude_roots = ["{claude_root}"]
codex_roots = ["{codex_root}"]
''',
        encoding="utf-8",
    )
    return load_manifest(path)


def create_named_app(*args: Any, **kwargs: Any):
    """Create a named test app."""

    from rcp.api import create_app

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
    """A project on the backend card for engine tests that resolve run_on."""

    from rcp.machine_cards import BACKEND_MACHINE_ID

    path = directory / "manifest.toml"
    path.write_text(
        'name = "Test project"\n'
        f'machines = ["{BACKEND_MACHINE_ID}"]\n'
        f'[[repositories]]\nalias = "repo"\nmachine = "{BACKEND_MACHINE_ID}"\n'
        f"path = {json.dumps(str(directory))}\n"
        '[project]\ntruth_scope = ["repo"]\n'
        '[state]\nrepository = "repo"\n'
        '[agent]\ndefault_run_truth_scope = ["repo"]\n',
        encoding="utf-8",
    )
    return path


def store_test_claude_token(store: AppStore, machine_id: str | None = None) -> None:
    """Give fake Claude execution an explicit managed credential.

    Imported locally because release-update bases import this module with an older
    `rcp`.
    """
    from rcp.agents.provider_environment import ProviderCredentialStore
    from rcp.machine_cards import BACKEND_MACHINE_ID
    from rcp.storage.models import MACHINE_LOGIN_ID

    ProviderCredentialStore.for_data_dir(store.path.parent).store_token(
        "claude",
        machine_id or BACKEND_MACHINE_ID,
        "test-setup-token",
        member_id="fixture-member",
        now=store.now(),
        login_id=MACHINE_LOGIN_ID,
    )


def fabricated_authorizer(display_name: str = "Campaign owner") -> AuthorizedHuman:
    """A synthetic authorizer for stores that were never opened as an app."""

    return AuthorizedHuman(
        space_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        display_name=display_name,
    )


def experiment_run_task(
    store: AppStore,
    operation_id: str,
    episode_id: str,
    *,
    project_id: str = "project",
    control_node_id: str = "experiment-node",
    ceiling: int = 1,
    request_fields: dict[str, Any] | None = None,
    **fields: Any,
) -> AgentTaskRecord:
    """A queued first Experiment-loop turn, shaped as the Run route admits it."""

    from rcp.machine_cards import BACKEND_MACHINE_ID
    from rcp.service import RunRequest

    now = store.now()
    request = RunRequest(
        provider="codex",
        run_on=BACKEND_MACHINE_ID,
        chat_id=f"experiment-{episode_id}",
        chat_scope="node",
        node_id=control_node_id,
        message="Run the bounded experiment.",
        mode="work",
        trigger="experiment_run",
        patch_kind="experiment_loop",
        control_node_id=control_node_id,
        control_revision=0,
        control_episode_id=episode_id,
        control_invocation=1,
        control_invocation_ceiling=ceiling,
        control_decision_bundle=[],
        control_completion_criteria=[],
    )
    fields.setdefault("authorized_by", authorized_human(store))
    return AgentTaskRecord(
        operation_id=operation_id,
        project_id=project_id,
        episode_id=episode_id,
        kind="node_chat",
        status="queued",
        request={**request.model_dump(mode="json"), **(request_fields or {})},
        created_at=now,
        updated_at=now,
        status_message="Queued",
        **fields,
    )


def start_experiment_episode(
    store: AppStore, operation_id: str, episode_id: str, **kwargs: Any
) -> AgentTaskRecord:
    """Admit an Experiment episode and its first paid turn through the Run route's store call."""

    return store.create_experiment_episode_with_invocation(
        experiment_run_task(store, operation_id, episode_id, **kwargs)
    )


def fake_launch_admitted(
    monkeypatch: pytest.MonkeyPatch,
    tasks: BackgroundAgentTasks,
    launch: Callable[[str], object] | None = None,
) -> None:
    """Replace ``launch_admitted`` with ``launch`` behind the real request validation.

    A queued row's request must parse and round-trip as the real launch requires
    before ``launch`` runs, so a producer that persists an unlaunchable request
    fails its test. Without ``launch`` the fake returns the row unchanged.
    ``test_launch_fakes.py`` keeps every test fake of the launch on this helper.
    """

    def launch_admitted(operation_id: str) -> object:
        record = tasks.store.agent_task(operation_id)
        if record is not None and record.status == "queued":
            tasks._launchable_request(record)
        return launch(operation_id) if launch else tasks.store.agent_task(operation_id)

    monkeypatch.setattr(tasks, "launch_admitted", launch_admitted)


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
    """Prepare canonical graph state without impersonating a production agent task.

    Mirrors the cache update the production manager performs on guarded admission.
    """

    from rcp.history import HistoryManager

    fixture_history = HistoryManager(
        service.manifest, service.history.workspace, write_authority=unchecked_write_authority()
    )
    appended, result = fixture_history.append(patch, **kwargs)
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


class _InertSSHAgent:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def stop_after_workers_drained(self, is_idle: object) -> None:
        pass


def isolate_host(
    monkeypatch: pytest.MonkeyPatch, root: Path, *, provider_discovery: bool = False
) -> list[BackgroundAgentTasks]:
    """Keep one test, or one module template build, off the developer's machine.

    Returns the provider engines constructed under the patch; the caller shuts
    them down. A module-scoped template build applies this with its own
    `MonkeyPatch`, because the function-scoped autouse fixture has not run yet.

    Each test gets its own provider lock root, `~/.rcp` temp dir, SSH control directory
    and Codex home, so no test touches the developer's live state. Provider discovery
    returns nothing, so an unconfigured provider refuses before any real CLI runs or
    bills a turn; the `real_provider_discovery` marker opts out. Every engine registers
    for shutdown at construction, because most test apps never run a lifespan and would
    leave broker and provider children running. The process-wide lock fence is reset so
    one teardown cannot abort a later lock wait. The terminal width is pinned to the
    renderer's 100-column fallback so prose assertions do not depend on the ambient
    terminal. The deploy-key agent socket is per account; `tests/test_ssh_agent.py`
    drives the real one with its own home.
    """

    monkeypatch.setattr(
        "rcp.agents.credential_gate._DEFAULT_ACCOUNT_LOCK_ROOT", root / "credential-locks"
    )
    monkeypatch.setattr("rcp.rcp_home.rcp_home", lambda: root / "rcp-home")
    monkeypatch.setattr("rcp.transport.remote_transfer_git._rcp_temp_dir", lambda: root)
    monkeypatch.setattr("rcp.transport.ssh._control_directory_path", lambda: root / "ssh-control")
    monkeypatch.setenv("CODEX_HOME", str(root / "machine-codex-home"))

    if not provider_discovery:
        monkeypatch.setattr("rcp.agents.launcher._discover_local_provider", lambda provider: None)

    for owner in ("rcp.api.app", "rcp.api.project_state"):
        monkeypatch.setattr(f"{owner}.refresh_compute_probes", lambda *args, **kwargs: None)

    from rcp.background import BackgroundAgentTasks

    engines: list[BackgroundAgentTasks] = []
    construct = BackgroundAgentTasks.__init__

    def register(self: BackgroundAgentTasks, *args: object, **kwargs: object) -> None:
        construct(self, *args, **kwargs)  # type: ignore[arg-type]
        engines.append(self)

    monkeypatch.setattr(BackgroundAgentTasks, "__init__", register)

    monkeypatch.setattr("rcp.transport.state._CANONICAL_LOCK_WAIT_FENCE", threading.Event())

    monkeypatch.setenv("COLUMNS", "100")

    monkeypatch.setenv("RCP_UPDATE_CHECK", "off")

    monkeypatch.setattr("rcp.ssh_agent.rcp_home", lambda: root / "rcp-home")
    monkeypatch.setattr("rcp.api.app.BackendSSHAgent", _InertSSHAgent)

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

    import pytest

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


def resource_watermark_tokens(
    client: TestClient, project_id: str, *resources: dict[str, str], current_ref: str = "main"
) -> list[str]:
    """Read subscribed versions through the same route as an open project tab."""
    response = client.post(
        "/api/resource-watermarks",
        json={
            "client_id": "test-open-tab",
            "open": [
                {"project_id": project_id, "current_ref": current_ref, "resources": resources}
            ],
            "due": [project_id],
        },
    )
    assert response.status_code == 200, response.text
    project = response.json()["projects"][0]
    assert project["requested_id"] == project_id
    return [item["version"] for item in project["resources"]]


def write_turn_outcome(workspace: Path, outcome: str = '{"status":"done"}') -> None:
    """Complete a scripted Work turn with a valid file-backed outcome."""
    (workspace / "outcome.json").write_text(outcome, encoding="utf-8")


class CommandFileMailbox:
    """A stage mailbox double: `read(name, max_bytes)` supplies each command file."""

    def __init__(self, read: Callable[[str, int | None], str]) -> None:
        self.read = read

    def read_text(self, name: str, *, max_bytes: int | None = None) -> str:
        return self.read(name, max_bytes)

    def remove_if_sha256(self, _name: str, _sha256: str) -> bool:
        return True


def traced_connection(
    connection: Callable[[], AbstractContextManager[sqlite3.Connection]],
    record: Callable[[str], None],
) -> Callable[[], AbstractContextManager[sqlite3.Connection]]:
    """Wrap a store's connection factory so every statement it runs reaches `record`."""

    @contextmanager
    def traced() -> Iterator[sqlite3.Connection]:
        with connection() as opened:
            opened.set_trace_callback(record)
            yield opened

    return traced


class _TracingAppStore(AppStore):
    """Count projection reads without changing the production connection path."""

    def __init__(self, path) -> None:
        self.select_count = 0
        super().__init__(path)

    def connection(self) -> AbstractContextManager[sqlite3.Connection]:
        return traced_connection(super().connection, self._count_select)()

    def _count_select(self, statement: str) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            self.select_count += 1


_WRITE_KEYWORDS = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})


def is_write_statement(statement: str) -> bool:
    """`BEGIN IMMEDIATE` or DML, the statements a read-only route never runs."""

    words = statement.split(None, 2)
    if not words:
        return False
    head = words[0].upper()
    if head == "BEGIN":
        return len(words) > 1 and words[1].upper() == "IMMEDIATE"
    return head in _WRITE_KEYWORDS


@dataclass
class RequestTrace:
    """Effects observed while `trace_requests` is active.

    `transactions` holds the statements of each store connection use, in order.
    """

    transactions: list[list[str]] = field(default_factory=list)
    subprocesses: list[list[str]] = field(default_factory=list)
    http: list[str] = field(default_factory=list)
    remote_reads: list[str] = field(default_factory=list)

    @property
    def statements(self) -> list[str]:
        return [statement for transaction in self.transactions for statement in transaction]

    def writes(self) -> list[str]:
        """Write statements across every connection use."""

        return [
            statement
            for transaction in self.transactions
            for statement in transaction
            if is_write_statement(statement)
        ]

    def effects(self) -> dict[str, list[Any]]:
        """Every non-empty effect a read-only route must not have."""

        found = {
            "writes": self.writes(),
            "subprocesses": self.subprocesses,
            "http": self.http,
            "remote_reads": self.remote_reads,
        }
        return {kind: items for kind, items in found.items() if items}


@contextmanager
def trace_requests(app: Any) -> Iterator[RequestTrace]:
    """Record what requests to `app` do beyond reading the local store.

    Statements come from the app store's connection factory. Subprocesses and
    remote reads run as usual and are recorded. Outbound HTTP is recorded and
    refused, so no test reaches a real network.
    """

    import urllib.request

    import pytest

    from rcp.projects import ProjectCatalog
    from rcp.transport.state import SSHStateWorkspace, StateWorkspace

    trace = RequestTrace()
    store = _store_of(app)
    popen_init = subprocess.Popen.__init__

    def record_popen(self: subprocess.Popen[Any], args: Any, *rest: Any, **kwargs: Any) -> None:
        trace.subprocesses.append(
            [args] if isinstance(args, str | bytes) else [str(part) for part in args]
        )
        popen_init(self, args, *rest, **kwargs)

    def refuse_urlopen(url: Any, *args: Any, **kwargs: Any) -> Any:
        trace.http.append(str(getattr(url, "full_url", url)))
        raise OSError("Outbound HTTP is refused while tracing requests.")

    def refuse_transport(_transport: Any, request: httpx.Request) -> Any:
        trace.http.append(str(request.url))
        raise httpx.ConnectError("Outbound HTTP is refused while tracing requests.")

    async def refuse_async_transport(transport: Any, request: httpx.Request) -> Any:
        return refuse_transport(transport, request)

    def remote_read(owner: type, name: str) -> Callable[..., Any]:
        original = getattr(owner, name)

        @functools.wraps(original)
        def recorded(*args: Any, **kwargs: Any) -> Any:
            trace.remote_reads.append(f"{owner.__name__}.{name}")
            return original(*args, **kwargs)

        return recorded

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subprocess.Popen, "__init__", record_popen)
        patch.setattr(urllib.request, "urlopen", refuse_urlopen)
        patch.setattr(httpx.HTTPTransport, "handle_request", refuse_transport)
        patch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse_async_transport)
        for owner, name in (
            (StateWorkspace, "refresh_if_stale"),
            (SSHStateWorkspace, "refresh_if_stale"),
            (ProjectCatalog, "open_display"),
        ):
            patch.setattr(owner, name, remote_read(owner, name))
        connection = store.connection

        def traced() -> AbstractContextManager[sqlite3.Connection]:
            transaction: list[str] = []
            trace.transactions.append(transaction)
            return traced_connection(connection, transaction.append)()

        store.connection = traced
        try:
            yield trace
        finally:
            del store.connection
