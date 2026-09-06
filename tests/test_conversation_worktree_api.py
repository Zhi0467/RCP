from __future__ import annotations

import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from rcp.agents import AgentEvent
from rcp.runs.tasks.discuss import stream_discuss_run
from rcp.runs.tasks.work import stream_work_run
from rcp.storage import AgentTaskRecord

from .helpers import (
    TASK_SETTLE_TIMEOUT,
    append_fixture_patch,
    create_named_app,
    seed_patch,
    wait_for_task_response,
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


class _ChatLauncher:
    def __init__(self, harness):
        self.harness = harness
        self.request = None
        self.launches: list[dict] = []
        self.sessions: dict[str, str] = {}

    async def stream(self, _provider, prompt, **kwargs):
        request = self.request
        session = kwargs.get("session_id") or str(uuid.uuid4())
        self.sessions[request.chat_id] = session
        scope = kwargs.get("write_scope")
        contract = Path(
            prompt.splitlines()[0].removeprefix("RCP master context: ")
            if prompt.startswith("RCP master context: ")
            else prompt.splitlines()[1]
        )
        inputs = "\n".join(path.read_text() for path in contract.parent.glob("*.md"))
        root = self.harness.repository
        binding = self.harness.store.conversation_worktree(self.harness.project_id, request.chat_id)
        if binding:
            root = Path(binding.worktree_path)
            assert str(root) in inputs
        if request.message == "edit notes":
            assert scope is not None
            assert scope.repository_roots == [str(root)]
            (root / "notes.txt").write_text(f"edit in {request.chat_id}\n")
        self.launches.append(
            {
                "request": request,
                "scope": scope,
                "root": str(root),
                "notes": (root / "notes.txt").read_text(),
                "session": kwargs.get("session_id"),
            }
        )
        yield AgentEvent(event="session", session_id=session)
        yield AgentEvent(event="answer", text=(root / "notes.txt").read_text())
        yield AgentEvent(event="done")


class _Harness:
    def __init__(self, manifest, data_dir: Path):
        self.manifest = manifest
        self.data_dir = data_dir
        self.repository = Path(manifest.repository_map["repo-a"].path)
        self.app = create_named_app(str(manifest.path), data_dir=data_dir)
        self.project_id = self.app.state.default_project_id
        self.store = self.app.state.background_tasks.store
        self.launcher = _ChatLauncher(self)
        self.client = TestClient(self.app)

        async def stream(_project_id, kind, request, execution):
            assert kind == "project_chat"
            self.launcher.request = request
            function = stream_work_run if request.mode == "work" else stream_discuss_run
            async for frame in function(
                self.app.state.service, self.launcher, request, data_dir, execution=execution
            ):
                yield frame

        self.app.state.background_tasks.stream = stream

    def controls(self, chat_id: str, **params):
        return self.client.get(
            f"/api/projects/{self.project_id}/chats/{chat_id}/worktree", params=params
        )

    def send(self, chat_id: str, **body):
        return self.client.post(
            f"/api/projects/{self.project_id}/tasks/project_chat",
            json={
                "chat_id": chat_id,
                "message": "answer",
                "mode": "work",
                "run_truth_scope": ["repo-a"],
                **body,
            },
        )

    def turn(self, chat_id: str, **body):
        response = self.send(chat_id, **body)
        assert response.status_code == 202, response.text
        result = wait_for_task_response(
            self.client, self.project_id, response.json()["operation_id"]
        )
        assert result["status"] == "succeeded", (
            result.get("error"),
            result.get("status_message"),
            result.get("result"),
        )
        return result

    def remove(self, chat_id: str):
        return self.client.delete(f"/api/projects/{self.project_id}/chats/{chat_id}/worktree")


@pytest.fixture
def harness(manifest, tmp_path):
    repository = Path(manifest.repository_map["repo-a"].path)
    _git(repository, "init", "--initial-branch=research")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "config", "user.name", "Worktree fixture")
    (repository / ".gitignore").write_text(".research/\n")
    (repository / "notes.txt").write_text("initial\n")
    _git(repository, "add", ".gitignore", "notes.txt")
    _git(repository, "commit", "-m", "Initial fixture")
    remote = tmp_path / "origin.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=release")
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "branch", "release")
    _git(repository, "push", "origin", "research", "release")
    _git(repository, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/release")
    result = _Harness(manifest, tmp_path / "data")
    append_fixture_patch(result.app.state.service, seed_patch())
    yield result
    result.client.close()


def test_chooser_requires_one_repository_and_first_work_turn(harness):
    chat = str(uuid.uuid4())
    response = harness.controls(chat)
    assert response.status_code == 200
    assert response.json()["can_choose"]
    multiple = harness.controls(chat, run_truth_scope=["repo-a", "repo-b"]).json()
    assert multiple["show_chooser"] and not multiple["can_choose"]
    assert "exactly one repository" in multiple["unavailable_reason"]
    refused = harness.send(chat, worktree=True, run_truth_scope=["repo-a", "repo-b"])
    assert refused.status_code == 422, refused.text
    assert harness.store.conversation_worktree(harness.project_id, chat) is None
    harness.turn(chat)
    assert not harness.controls(chat).json()["show_chooser"]
    refused = harness.send(chat, worktree=True)
    assert refused.status_code == 422 and "first Work turn" in refused.text


def test_http_binding_discuss_restart_and_other_chat_keep_exact_roots(harness):
    chat = str(uuid.uuid4())
    harness.turn(chat, worktree=True, message="edit notes")
    binding = harness.store.conversation_worktree(harness.project_id, chat)
    assert binding.status == "ready"
    worktree = Path(binding.worktree_path)
    assert (harness.repository / "notes.txt").read_text() == "initial\n"
    assert harness.launcher.launches[-1]["scope"].repository_roots == [str(worktree)]
    session = harness.launcher.sessions[chat]
    harness.turn(chat, mode="discuss", session_id=session)
    assert harness.launcher.launches[-1]["scope"] is None
    assert harness.launcher.launches[-1]["notes"] == f"edit in {chat}\n"
    assert harness.launcher.launches[-1]["session"] == session
    assert harness.store.conversation_worktree(harness.project_id, chat) == binding
    other = str(uuid.uuid4())
    harness.turn(other, message="edit notes")
    assert harness.store.conversation_worktree(harness.project_id, other) is None
    assert harness.launcher.launches[-1]["scope"].repository_roots == [str(harness.repository)]
    assert (worktree / "notes.txt").read_text() == f"edit in {chat}\n"
    restarted = _Harness(harness.manifest, harness.data_dir)
    try:
        assert restarted.controls(chat).json()["binding"]["worktree_path"] == str(worktree)
        restarted.turn(chat, mode="discuss", session_id=session)
        assert restarted.launcher.launches[-1]["root"] == str(worktree)
        assert restarted.launcher.launches[-1]["session"] == session
        refused = restarted.send(chat, worktree=True, run_truth_scope=["repo-b"])
        assert refused.status_code == 422
        assert restarted.store.conversation_worktree(restarted.project_id, chat) == binding
    finally:
        restarted.client.close()


def test_integration_resolves_targets_and_refuses_without_dispatch(harness):
    chat = str(uuid.uuid4())
    harness.turn(chat, worktree=True, message="edit notes")
    binding = harness.store.conversation_worktree(harness.project_id, chat)
    worktree = Path(binding.worktree_path)
    count = len(harness.launcher.launches)
    for choice in ("pull_request", "starting_branch", "default_branch"):
        refused = harness.send(chat, worktree_integration=choice)
        assert refused.status_code == 422 and "uncommitted changes" in refused.text
    assert len(harness.launcher.launches) == count
    _git(worktree, "add", "notes.txt")
    _git(worktree, "commit", "-m", "Chat edit")
    (harness.repository / "untracked.txt").write_text("shared dirty\n")
    controls = harness.controls(chat).json()
    assert [option["enabled"] for option in controls["integration_options"]] == [True, False, False]
    for choice in ("starting_branch", "default_branch"):
        refused = harness.send(chat, worktree_integration=choice)
        assert refused.status_code == 422 and "Shared checkout" in refused.text
    harness.turn(
        chat, worktree_integration="pull_request", session_id=harness.launcher.sessions[chat]
    )
    assert harness.launcher.launches[-1]["scope"].repository_roots == [str(worktree)]
    _git(harness.repository, "add", "untracked.txt")
    _git(harness.repository, "commit", "-m", "Shared edit")
    for choice, target, checked_out in (
        ("starting_branch", "research", True),
        ("default_branch", "release", False),
    ):
        harness.turn(
            chat,
            worktree_integration=choice,
            worktree_integration_target="attacker-branch",
            session_id=harness.launcher.sessions[chat],
            message="client instruction",
        )
        launch = harness.launcher.launches[-1]
        request = launch["request"]
        assert request.worktree_integration_target == target
        assert "client instruction" not in request.message
        assert "attacker-branch" not in request.message
        assert f'"target_branch": "{target}"' in request.message
        assert (
            f'"target_checked_out_in_shared_checkout": {str(checked_out).lower()}'
            in request.message
        )
        assert set(launch["scope"].repository_roots) == {str(worktree), str(harness.repository)}
    assert _git(harness.repository, "rev-parse", "research") != _git(worktree, "rev-parse", "HEAD")
    harness.turn(chat, session_id=harness.launcher.sessions[chat])
    assert harness.launcher.launches[-1]["scope"].repository_roots == [str(worktree)]


def test_remove_preserves_branch_and_tombstone_and_refuses_dirty(harness):
    chat = str(uuid.uuid4())
    harness.turn(chat, worktree=True, message="edit notes")
    binding = harness.store.conversation_worktree(harness.project_id, chat)
    worktree = Path(binding.worktree_path)
    refused = harness.remove(chat)
    assert refused.status_code == 422 and "uncommitted changes" in refused.text
    _git(worktree, "add", "notes.txt")
    _git(worktree, "commit", "-m", "Unmerged chat edit")
    assert harness.controls(chat).json()["ahead_count"] == 1
    assert harness.controls(chat).json()["remote_branch_exists"] is None
    assert harness.controls(chat, inspect_removal=True).json()["remote_branch_exists"] is False
    _git(worktree, "push", "origin", binding.branch)
    assert harness.controls(chat, inspect_removal=True).json()["remote_branch_exists"] is True
    response = harness.remove(chat)
    assert response.status_code == 200, response.text
    assert response.json()["binding"]["status"] == "removed"
    assert not worktree.exists()
    assert _git(harness.repository, "show", f"{binding.branch}:notes.txt") == f"edit in {chat}"
    refused = harness.send(chat, worktree=True)
    assert refused.status_code == 422 and "removed" in refused.text


@pytest.mark.parametrize("status", ["running", "paused"])
def test_remove_refuses_active_or_resumable_paused_chat(harness, status):
    chat = str(uuid.uuid4())
    harness.turn(chat, worktree=True)
    now = harness.store.now()
    harness.store.create_agent_task(
        AgentTaskRecord(
            operation_id=str(uuid.uuid4()),
            project_id=harness.project_id,
            kind="project_chat",
            status=status,
            request={"chat_id": chat},
            created_at=now,
            updated_at=now,
            status_message=status,
            native_session_id="paused-session",
        )
    )
    response = harness.remove(chat)
    assert response.status_code == 422 and "active or paused" in response.text
    assert not harness.controls(chat).json()["can_remove"]


def test_missing_worktree_refuses_before_launch(harness):
    chat = str(uuid.uuid4())
    harness.turn(chat, worktree=True)
    binding = harness.store.conversation_worktree(harness.project_id, chat)
    _git(harness.repository, "worktree", "remove", binding.worktree_path)
    before = len(harness.launcher.launches)
    for mode in ("work", "discuss"):
        response = harness.send(chat, mode=mode)
        assert response.status_code == 422 and "unavailable" in response.text
    assert len(harness.launcher.launches) == before
    assert harness.store.conversation_worktree(harness.project_id, chat) == binding


def test_retry_resumes_interrupted_integration_on_its_persisted_target(harness):
    chat = str(uuid.uuid4())
    harness.turn(chat, worktree=True)
    binding = harness.store.conversation_worktree(harness.project_id, chat)
    worktree = Path(binding.worktree_path)
    original_stream = harness.launcher.stream
    interrupted = False

    async def disconnect_after_target_checkout(provider, prompt, **kwargs):
        nonlocal interrupted
        async for event in original_stream(provider, prompt, **kwargs):
            if event.event == "answer" and not interrupted:
                _git(worktree, "checkout", "release")
                interrupted = True
                yield AgentEvent(event="error", text="Disconnected during integration")
                return
            if event.event == "answer":
                # The resumed provider has reached its original target checkout;
                # it can finish/abort its own work and restore the bound branch.
                assert _git(worktree, "branch", "--show-current") == "release"
                _git(worktree, "checkout", binding.branch)
            yield event

    harness.launcher.stream = disconnect_after_target_checkout
    response = harness.send(
        chat, worktree_integration="default_branch", session_id=harness.launcher.sessions[chat]
    )
    assert response.status_code == 202, response.text
    failed = wait_for_task_response(
        harness.client, harness.project_id, response.json()["operation_id"]
    )
    assert failed["status"] == "failed", failed
    assert failed["error"] == "Disconnected during integration"
    # A new turn cannot claim the temporary target as normal conversation state.
    refused = harness.send(chat)
    assert refused.status_code == 422 and "checked-out branch changed" in refused.text
    retry = harness.client.post(
        f"/api/projects/{harness.project_id}/tasks/{failed['operation_id']}/retry", json={}
    )
    assert retry.status_code == 202, retry.text
    retried = wait_for_task_response(
        harness.client, harness.project_id, retry.json()["operation_id"]
    )
    assert retried["status"] == "succeeded", retried.get("error")
    assert harness.launcher.launches[-1]["request"].worktree_integration_target == "release"
    assert _git(worktree, "branch", "--show-current") == binding.branch


def test_overlapping_catalog_owner_refuses_before_git_creation(harness, monkeypatch):
    from rcp.agents.write_scope import RegisteredRepositoryRoot
    from rcp.service import ProjectService
    from rcp.transport.conversation_worktree import execute

    chat = str(uuid.uuid4())
    planned = execute(
        {
            "operation": "plan",
            "shared_path": str(harness.repository),
            "chat_id": chat,
            "timeout_seconds": 10,
        }
    )
    original_inventory = ProjectService.repository_ownership_inventory

    def overlapping_inventory(service, *, project_id):
        return [
            *original_inventory(service, project_id=project_id),
            RegisteredRepositoryRoot(
                project_id="different-project",
                alias="other-root",
                machine="laptop",
                execution_host="",
                path=str(harness.repository.parent),
            ),
        ]

    monkeypatch.setattr(ProjectService, "repository_ownership_inventory", overlapping_inventory)
    response = harness.send(chat, worktree=True)
    assert response.status_code == 422, response.text
    assert "different-project" in response.text or "another project" in response.text
    assert harness.store.conversation_worktree(harness.project_id, chat) is None
    assert not Path(planned["worktree_path"]).exists()
    assert not _git(harness.repository, "branch", "--list", planned["branch"])
    assert not harness.launcher.launches


def test_remove_race_retains_dirty_worktree_and_restores_work_admission(harness, monkeypatch):
    from rcp import conversation_worktrees

    chat = str(uuid.uuid4())
    harness.turn(chat, worktree=True)
    binding = harness.store.conversation_worktree(harness.project_id, chat)
    worktree = Path(binding.worktree_path)
    original_command = conversation_worktrees.worktree_command

    def edit_after_preflight(store, *, operation, **kwargs):
        if operation == "remove":
            assert store.conversation_worktree(harness.project_id, chat).status == "removing"
            (worktree / "late-edit.txt").write_text("arrived after preflight\n")
        return original_command(store, operation=operation, **kwargs)

    monkeypatch.setattr(conversation_worktrees, "worktree_command", edit_after_preflight)
    response = harness.remove(chat)
    assert response.status_code == 422 and "uncommitted changes" in response.text
    assert (worktree / "late-edit.txt").read_text() == "arrived after preflight\n"
    assert harness.store.conversation_worktree(harness.project_id, chat).status == "ready"
    harness.turn(chat, session_id=harness.launcher.sessions[chat])
    assert harness.launcher.launches[-1]["scope"].repository_roots == [str(worktree)]
    assert _git(worktree, "branch", "--show-current") == binding.branch


def test_remote_binding_uses_execution_machine_canonical_path_without_ssh(harness, monkeypatch):
    from types import SimpleNamespace

    from rcp import conversation_worktrees
    from rcp.service import RunRequest

    chat = str(uuid.uuid4())
    harness.turn(chat, worktree=True)
    binding = harness.store.conversation_worktree(harness.project_id, chat).model_copy(
        update={
            "execution_host": "fixture-execution-account",
            "shared_path": "/canonical/repository",
        }
    )
    manifest = harness.manifest.model_copy(
        update={
            "machines": [
                machine.model_copy(update={"host": binding.execution_host})
                for machine in harness.manifest.machines
            ],
            "repositories": [
                repository.model_copy(update={"path": "/registered/repository-link"})
                if repository.alias == binding.repository_alias
                else repository
                for repository in harness.manifest.repositories
            ],
        }
    )
    service = SimpleNamespace(manifest=manifest, history=harness.app.state.service.history)
    request = RunRequest(
        chat_id=chat,
        chat_scope="project",
        run_on="laptop",
        run_truth_scope=[binding.repository_alias],
    )
    resolved = binding.shared_path
    calls = []

    def canonicalize_on_execution_account(store, *, host, operation, **kwargs):
        # This stub exercises remote identity semantics without calling SSH.
        assert host == binding.execution_host
        assert operation == "canonicalize"
        assert kwargs["shared_path"] == "/registered/repository-link"
        calls.append(kwargs)
        return {
            "canonical": {"/registered/repository-link": resolved},
            "account_home": "/home/fixture",
        }

    monkeypatch.setattr(
        conversation_worktrees, "worktree_command", canonicalize_on_execution_account
    )
    conversation_worktrees.validate_worktree_binding(service, request, binding, harness.store)
    assert len(calls) == 1
    resolved = "/relocated/repository"
    with pytest.raises(ValueError, match="registered repository moved"):
        conversation_worktrees.validate_worktree_binding(service, request, binding, harness.store)


@pytest.mark.parametrize("recovery", ["resume", "retry", "repair-graph-update"])
@pytest.mark.parametrize("change", ["shared", "worktree", "missing_target"])
def test_integration_recovery_rechecks_git_before_admission(harness, monkeypatch, recovery, change):
    chat = str(uuid.uuid4())
    harness.turn(chat, worktree=True)
    result = harness.turn(chat, worktree_integration="default_branch")
    binding = harness.store.conversation_worktree(harness.project_id, chat)
    worktree = Path(binding.worktree_path)
    if change == "missing_target":
        _git(worktree, "branch", "-D", "release")
    else:
        # Recovery may find its own target checked out, but it must still reject
        # newly dirty files on either side before making the shared root writable.
        _git(worktree, "checkout", "release")
        root = harness.repository if change == "shared" else worktree
        (root / "late-edit.txt").write_text("preserve this edit\n")

    def forbidden_recovery(*_args, **_kwargs):
        raise AssertionError("recovery must refuse before task admission")

    method = "repair_graph_update" if recovery == "repair-graph-update" else recovery
    monkeypatch.setattr(harness.app.state.background_tasks, method, forbidden_recovery)
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/tasks/{result['operation_id']}/{recovery}", json={}
    )
    assert response.status_code == 409, response.text
    expected = (
        "target branch does not exist" if change == "missing_target" else "uncommitted changes"
    )
    assert expected in response.text
    assert Path(binding.worktree_path).is_dir()
    if change != "missing_target":
        assert (root / "late-edit.txt").read_text() == "preserve this edit\n"


@pytest.mark.parametrize("change", ["shared", "worktree", "missing_target"])
def test_integration_rechecks_git_again_when_the_provider_turn_starts(harness, change):
    chat = str(uuid.uuid4())
    harness.turn(chat, worktree=True)
    binding = harness.store.conversation_worktree(harness.project_id, chat)
    worktree = Path(binding.worktree_path)
    original = harness.app.state.background_tasks.stream
    before = len(harness.launcher.launches)

    async def change_after_admission(project_id, kind, request, execution):
        if change == "missing_target":
            _git(worktree, "branch", "-D", "release")
        else:
            root = harness.repository if change == "shared" else worktree
            (root / "late-edit.txt").write_text("arrived while queued\n")
        async for frame in original(project_id, kind, request, execution):
            yield frame

    harness.app.state.background_tasks.stream = change_after_admission
    response = harness.send(chat, worktree_integration="default_branch")
    assert response.status_code == 202, response.text
    result = wait_for_task_response(
        harness.client, harness.project_id, response.json()["operation_id"]
    )
    assert result["status"] == "failed", result
    expected = (
        "target branch does not exist" if change == "missing_target" else "uncommitted changes"
    )
    assert expected in result["error"]
    assert len(harness.launcher.launches) == before
    assert Path(binding.worktree_path).is_dir()


@pytest.mark.parametrize("recovery", ["resume", "retry", "repair-graph-update"])
def test_removal_waits_for_recovery_admission_then_refuses_active_task(
    harness, monkeypatch, recovery
):
    from rcp.conversation_worktrees import conversation_worktree_locks

    chat = str(uuid.uuid4())
    result = harness.turn(chat, worktree=True)
    binding = harness.store.conversation_worktree(harness.project_id, chat)
    admitted = Event()
    release = Event()
    removal_requested = Event()

    def reserve_recovery(_operation_id, **_kwargs):
        # Pause at the existing BackgroundAgentTasks admission boundary. The API
        # must retain its conversation lock until the durable queued row exists.
        admitted.set()
        assert release.wait(TASK_SETTLE_TIMEOUT)
        record = AgentTaskRecord(
            operation_id=str(uuid.uuid4()),
            project_id=harness.project_id,
            kind="project_chat",
            status="queued",
            request={"chat_id": chat},
            created_at=harness.store.now(),
            updated_at=harness.store.now(),
            status_message="queued",
        )
        harness.store.create_agent_task(record)
        return record

    def remove():
        removal_requested.set()
        return harness.remove(chat)

    method = "repair_graph_update" if recovery == "repair-graph-update" else recovery
    monkeypatch.setattr(harness.app.state.background_tasks, method, reserve_recovery)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(
            harness.client.post,
            f"/api/projects/{harness.project_id}/tasks/{result['operation_id']}/{recovery}",
            json={},
        )
        try:
            assert admitted.wait(TASK_SETTLE_TIMEOUT)
            lock = conversation_worktree_locks(f"{harness.store.path}:{harness.project_id}:{chat}")
            assert lock.locked(), "recovery must own the same lock as removal"
            deletion = pool.submit(remove)
            assert removal_requested.wait(TASK_SETTLE_TIMEOUT)
        finally:
            release.set()
        assert pending.result().status_code == 202
        response = deletion.result()
    assert response.status_code == 422, response.text
    assert "active or paused" in response.text
    assert Path(binding.worktree_path).is_dir()


@pytest.mark.parametrize("recovery", ["resume", "retry", "repair-graph-update"])
def test_recovery_refuses_removed_binding_after_waiting_for_removal(harness, monkeypatch, recovery):
    from rcp import conversation_worktrees

    chat = str(uuid.uuid4())
    result = harness.turn(chat, worktree=True)
    binding = harness.store.conversation_worktree(harness.project_id, chat)
    deleting = Event()
    release = Event()
    original = conversation_worktrees.worktree_command

    def pause_removal(store, *, operation, **kwargs):
        if operation == "remove":
            deleting.set()
            assert release.wait(TASK_SETTLE_TIMEOUT)
        return original(store, operation=operation, **kwargs)

    def forbidden_admission(*_args, **_kwargs):
        raise AssertionError("a removed binding cannot admit recovery")

    monkeypatch.setattr(conversation_worktrees, "worktree_command", pause_removal)
    method = "repair_graph_update" if recovery == "repair-graph-update" else recovery
    monkeypatch.setattr(harness.app.state.background_tasks, method, forbidden_admission)
    with ThreadPoolExecutor(max_workers=2) as pool:
        deletion = pool.submit(harness.remove, chat)
        try:
            assert deleting.wait(TASK_SETTLE_TIMEOUT)
            pending = pool.submit(
                harness.client.post,
                f"/api/projects/{harness.project_id}/tasks/{result['operation_id']}/{recovery}",
                json={},
            )
        finally:
            release.set()
        assert deletion.result().status_code == 200
        response = pending.result()
    assert response.status_code == 409, response.text
    assert "removed" in response.text
    assert not Path(binding.worktree_path).exists()
    assert _git(harness.repository, "rev-parse", binding.branch) == binding.starting_commit
