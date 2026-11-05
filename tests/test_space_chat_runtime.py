from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from rcp.agents import AgentEvent
from rcp.agents.launcher import AgentProcessControl
from rcp.agents.prompts import APP_PLACES
from rcp.background import AgentTaskExecution
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.runs.space_chat import (
    SPACE_MASTER_POLICY,
    SpaceThreadOwner,
    default_space_hidden_read_scope,
    stream_space_chat_run,
)
from rcp.service import read_space_chat_transcript
from tests.helpers import agent_patch_json, create_named_app, seed_patch, store_test_claude_token
from tests.test_dispatch_authority import _authorizer, _work_authority, _work_request
from tests.test_storage import _task_record


@pytest.mark.parametrize(
    "project_count, broken",
    [
        (0, None),
        (2, None),
        (2, "unavailable"),
        (2, "missing"),
        (2, "remote"),
        (2, "hidden"),
        (2, "collection"),
        (2, "directory"),
    ],
)
def test_space_runtime_real_storage_and_partial_apply(
    manifest, tmp_path, monkeypatch, project_count, broken
):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    human = _authorizer(store)
    if project_count == 0:
        with store.connection() as connection:
            connection.execute("DELETE FROM project_members WHERE user_id=?", (human.user_id,))
    else:
        other = tmp_path / "other"
        for directory in ("repo-a/.research", "repo-b", "claude", "codex"):
            (other / directory).mkdir(parents=True, exist_ok=True)
        locator = other / "repo-a/.research/manifest.toml"
        locator.write_text(manifest.path.read_text().replace(str(tmp_path), str(other)))
        app.state.catalog.register(
            str(locator), seat_member=human.user_id, identity_action="created"
        )
    unavailable_id = min(store.member_project_ids(human.user_id), default=None)
    if broken == "hidden":
        project_count -= 1
    if broken in {"unavailable", "missing", "remote"}:
        from rcp.transport import StateUnavailable

        original_open = app.state.catalog.open_execution

        def open_project(project_id):
            if project_id == unavailable_id:
                if broken == "remote":
                    pytest.fail("remote project was opened")
                raise (
                    StateUnavailable("offline") if broken == "unavailable" else KeyError(project_id)
                )
            return original_open(project_id)

        monkeypatch.setattr(app.state.catalog, "open_execution", open_project)
        if broken == "remote":
            original_record = store.project
            gpu = store.create_space_machine(name="gpu", host="gpu.example", os_account="")

            def remote_record(pid):
                record = original_record(pid)
                if pid == unavailable_id and record is not None:
                    return record.model_copy(update={"state_machine_id": gpu.machine_id})
                return record

            monkeypatch.setattr(store, "project", remote_record)
        project_count -= 1
    threads = store.ensure_agent_threads(
        human.user_id, list(store.member_project_ids(human.user_id)), default_names=("Pip",)
    )
    thread = next(item for item in threads if item.kind == "space")
    request = _work_request().model_copy(
        update={"chat_id": thread.thread_id, "provider": "claude", "run_truth_scope": []}
    )
    task = store.create_agent_task(
        _task_record(
            store.now(),
            kind="space_chat",
            status="failed",
            operation_id=str(uuid.uuid4()),
            project_id=store.space_id,
            request=request.model_dump(mode="json"),
            authorized_by=human,
            dispatch_authority=_work_authority(request),
        )
    )

    store.record_agent_task_receipt(
        task.operation_id,
        "operation_created",
        {"kind": "space_chat", "attempt": task.attempt, "has_parent": False, "resumed": False},
    )
    store_test_claude_token(store)
    owner = SpaceThreadOwner(store, app.state.catalog, thread.thread_id)
    execution = AgentTaskExecution(task.operation_id, store, AgentProcessControl())
    installed = []
    monkeypatch.setattr(
        "rcp.runs.space_chat.install_space_hidden_read_scope", lambda scope: installed.append(scope)
    )

    async def prepare_policy(service, *args, **kwargs):
        if broken == "hidden" and service.history.project_id == unavailable_id:
            from rcp.agents.provider_environment import unhidden_read_scope

            return unhidden_read_scope(machine_id=BACKEND_MACHINE_ID)
        return default_space_hidden_read_scope(
            owner, request, kwargs["workspace"], execution, kwargs["repository_inventory"]
        )

    monkeypatch.setattr("rcp.runs.shared._prepare_hidden_read_scope", prepare_policy)

    calls = []
    prompts = []

    async def provider(_provider, prompt, **kwargs):
        calls.append(kwargs.get("session_id"))
        prompts.append(prompt)
        stage = kwargs["cwd"]
        assert json.loads((stage / "workspace" / "agent-name.json").read_text()) == {
            "name": "Pip",
            "can_self_name": False,
        }
        scope = kwargs["write_scope"]
        assert len(scope.projects) == project_count
        notes = (tmp_path / "data").resolve() / "memory" / "members" / human.user_id
        assert scope.memory_roots == [str(notes)] and str(notes) in scope.writable_roots
        assert str(notes / "MEMORY.md") in prompt
        assert not any(
            notes.is_relative_to(root) for root in kwargs["hidden_read_scope"].hidden_directories
        )
        launched.append((scope, execution.git_access))
        assert stage.parent == tmp_path / "data" / "agent-threads" / human.user_id / "stages"
        assert not any(
            stage.is_relative_to(root) for root in kwargs["hidden_read_scope"].hidden_directories
        )
        assert len(kwargs["invocation_gate"].children) == project_count
        assert "Earlier conversation" not in prompt
        for index, project in enumerate(scope.projects):
            workspace = Path(project.workspace_root)
            if index == 0 and broken == "directory":
                (workspace / "patch.json").mkdir()
                continue
            if index == 0 and broken == "collection":
                from rcp.transport import StateUnavailable

                def unavailable(*args, **kwargs):
                    raise StateUnavailable("state became unavailable")

                monkeypatch.setattr(
                    app.state.catalog.open_execution(project.project_id).history,
                    "append",
                    unavailable,
                )
            (workspace / "patch.json").write_text(
                agent_patch_json(seed_patch())
                if index == 0 or broken in {"collection", "directory"}
                else "invalid patch"
            )
        (stage / "patch.json").write_text("stray")
        (stage / "watch.json").write_text("stray")
        artifact_directory = stage / "turns" / kwargs["operation_id"] / "artifacts"
        assert artifact_directory.is_dir()
        (artifact_directory / "note.md").write_text("# Result\nReady.")
        (stage / "outcome.json").write_text('{"status":"done"}')
        yield AgentEvent(event="session", session_id="space-native")
        yield AgentEvent(event="answer", text="Finished the requested work.")
        yield AgentEvent(event="done")

    monkeypatch.setattr(app.state.launcher, "stream", provider)

    from rcp.agents.git_access import ProviderGitAccess

    async def no_checkout_access(checkouts, *, host=""):
        return []

    monkeypatch.setattr("rcp.agents.git_access.ensure_team_checkout_access", no_checkout_access)
    launched = []

    async def run():
        return [
            frame
            async for frame in stream_space_chat_run(
                owner,
                app.state.launcher,
                request,
                tmp_path / "data",
                execution,
                project_git_access=lambda project_id, _service: ProviderGitAccess(
                    None, tmp_path, "", ((project_id, "key"),)
                ),
            )
        ]

    frames = asyncio.run(run())
    assert installed
    assert any('"event":"done"' in frame or "event: done" in frame for frame in frames)
    assert execution.project_results is not None
    assert execution.turn_outcome is not None
    assert execution.stage_root is not None
    assert any("artifact" in frame and "note.md" in frame for frame in frames)
    assert len(execution.project_results) == project_count
    if project_count:
        failed = "unavailable" if broken == "collection" else "rejected"
        assert set(execution.project_results.values()) == (
            {"applied", failed} if project_count == 2 else {"applied"}
        )
        if project_count == 2:
            (failed_id,) = [
                key for key, value in execution.project_results.items() if value == failed
            ]
            assert execution.turn_outcome.reason is not None
            record = store.project(failed_id)
            assert record is not None and record.name in execution.turn_outcome.reason
        from rcp.core.transition_models import GraphTargetRef

        for project_id in execution.project_results:
            assert (
                store.graph_seen_head(
                    project_id, thread.thread_id, "space-native", GraphTargetRef()
                )
                is not None
            )
        assert execution.turn_outcome.status == ("partial" if project_count == 2 else "done")
        assert (
            json.loads((Path(execution.stage_root) / "outcome.json").read_text())["status"]
            == execution.turn_outcome.status
        )
    else:
        assert execution.write_scope_fingerprint is None
        assert execution.turn_outcome.status == "done"
    if broken in {"unavailable", "missing", "remote", "hidden"}:
        scope, git_access = launched[0]
        assert unavailable_id not in {item.project_id for item in scope.projects}
        assert unavailable_id not in {repository for repository, _ in git_access.checkouts}
        assert not (Path(execution.stage_root) / "projects" / unavailable_id / "rcp").exists()
        (master,) = (Path(execution.stage_root) / "inputs").glob("space-master-*.md")
        assert str(master) in prompts[0] and f'"{unavailable_id}"' in master.read_text()
    transcript = read_space_chat_transcript(tmp_path / "data", human.user_id, thread.thread_id)
    assert transcript is not None
    assert [message.role for message in transcript.messages] == ["user", "assistant"]
    assert transcript.messages[-1].text == "Finished the requested work."
    assert transcript.messages[-1].turn_outcome == execution.turn_outcome
    assert (
        store.chat_session_context("claude", BACKEND_MACHINE_ID, "space-native").kind
        == "space_chat"
    )

    if broken is None:
        from rcp.service import RunRequest

        store.checkpoint_agent_task(task.operation_id, native_session_id="space-native")
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status='succeeded' WHERE operation_id=?",
                (task.operation_id,),
            )
        second = store.create_agent_task(
            task.model_copy(
                update={
                    "operation_id": str(uuid.uuid4()),
                    "kind": "space_chat",
                    "status": "queued",
                    "created_at": store.now(),
                    "updated_at": store.now(),
                    "finished_at": None,
                    "request": request.model_copy(update={"message": "Continue."}).model_dump(
                        mode="json"
                    ),
                }
            )
        )
        store.record_agent_task_receipt(
            second.operation_id,
            "operation_created",
            {
                "kind": "space_chat",
                "attempt": second.attempt,
                "has_parent": False,
                "resumed": False,
            },
        )
        second_request = RunRequest.model_validate(second.request)
        assert second_request.session_id == "space-native"
        next_execution = AgentTaskExecution(second.operation_id, store, AgentProcessControl())

        async def continue_run():
            return [
                frame
                async for frame in stream_space_chat_run(
                    owner, app.state.launcher, second_request, tmp_path / "data", next_execution
                )
            ]

        if project_count == 0:
            monkeypatch.setattr("rcp.runs.space_chat.SPACE_MASTER_POLICY", "space-master-next")
        asyncio.run(continue_run())
        assert calls == [None, "space-native"]
        inputs = Path(execution.stage_root) / "inputs"
        first_master = next(inputs.glob(f"{SPACE_MASTER_POLICY}-*.md"))
        assert str(first_master) in prompts[0] and len(prompts[1]) < 4000
        assert APP_PLACES in first_master.read_text()
        if project_count == 0:
            (restaged,) = inputs.glob("space-master-next-*.md")
            assert str(restaged) in prompts[1]
        else:
            assert str(first_master) in prompts[1]
        continued = read_space_chat_transcript(tmp_path / "data", human.user_id, thread.thread_id)
        assert continued is not None
        assert len(continued.messages) == 4
        if project_count:
            (receipt,) = store.agent_task_receipts_by_category(
                task.operation_id, "space_project_results"
            )
            last = json.dumps(receipt.payload["project_results"], ensure_ascii=False)
            assert "rejected" in last and last in prompts[1]


def test_space_output_clear_removes_agent_directories_without_following_links(tmp_path):
    from rcp.runs.space_projects import clear_space_output

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept").write_text("x")
    stage = tmp_path / "stage"
    made = stage / "projects/one/patch.json"
    made.mkdir(parents=True)
    (made / "inner").write_text("x")
    linked = stage / "projects/two/patch.json"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside)
    for path in (made, linked, stage / "outcome.json"):
        clear_space_output(path)
    assert not made.exists() and not linked.is_symlink()
    assert (outside / "kept").exists()


def test_fresh_space_turn_skips_a_linked_project_folder(tmp_path):
    from types import SimpleNamespace

    from rcp.runs.space_chat import _SpaceTurn

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "patch.json").write_text("{}")
    stage = (tmp_path / "stage").resolve()
    (stage / "projects/kept").mkdir(parents=True)
    (stage / "projects/kept/patch.json").write_text("{}")
    (stage / "projects/linked").symlink_to(outside, target_is_directory=True)
    turn = SimpleNamespace(
        execution=SimpleNamespace(continuation="fresh"),
        stage=stage,
        found=SimpleNamespace(names={"kept": "Kept", "linked": "Linked"}),
    )
    _SpaceTurn.clear_fresh_outputs(turn)  # pyright: ignore[reportArgumentType]
    assert not (stage / "projects/kept/patch.json").exists()
    assert (outside / "patch.json").exists()


def test_space_seed_keeps_the_newest_tail_only_for_a_fresh_session():
    from types import SimpleNamespace

    from rcp.limits import AGENT_THREAD_TAIL_MAX_BYTES
    from rcp.runs.space_chat import _space_seed
    from rcp.service import RunRequest

    prior = SimpleNamespace(
        messages=[
            SimpleNamespace(role="user", text="x" * AGENT_THREAD_TAIL_MAX_BYTES),
            SimpleNamespace(role="user", text="older"),
            SimpleNamespace(role="assistant", text="newer"),
        ]
    )
    fresh = RunRequest(message="m", mode="work")
    seed = _space_seed(fresh, prior)
    assert seed.index("older") < seed.index("newer") and "x" * 100 not in seed
    assert _space_seed(fresh.model_copy(update={"session_id": "s"}), prior) == ""
    assert _space_seed(fresh, None) == ""


def test_space_seed_cuts_an_oversized_newest_message_to_the_budget():
    """Quotes double under JSON escaping, so the cut accounts for it."""
    import json
    from types import SimpleNamespace

    from rcp.limits import AGENT_THREAD_TAIL_MAX_BYTES
    from rcp.runs.space_chat import _space_seed
    from rcp.service import RunRequest

    newest = '"é' * AGENT_THREAD_TAIL_MAX_BYTES
    prior = SimpleNamespace(
        messages=[
            SimpleNamespace(role="user", text="older"),
            SimpleNamespace(role="assistant", text=newest),
        ]
    )
    seed = _space_seed(RunRequest(message="m", mode="work"), prior)
    (line,) = seed.splitlines()[1:]
    kept = json.loads(line)
    assert kept["truncated"] and newest.startswith(kept["text"]) and kept["text"]
    assert len(line.encode()) + 1 <= AGENT_THREAD_TAIL_MAX_BYTES


def test_fresh_session_history_is_a_staged_file_behind_one_pointer(tmp_path):
    from types import SimpleNamespace

    from rcp.runs.agent_threads import staged_history_seed
    from rcp.runs.space_chat import _space_seed
    from rcp.service import RunRequest

    prior = SimpleNamespace(
        messages=[
            SimpleNamespace(role="user", text=f"turn {index} " + "y" * 1500) for index in range(40)
        ]
    )
    fresh = RunRequest(message="m", mode="work")
    tail = _space_seed(fresh, prior)
    assert len(tail) > 30_000
    pointer = asyncio.run(
        staged_history_seed(
            None, fresh, None, tail, prepare_scope=None, local_stage=tmp_path, remote_stage=None
        )
    )
    assert len(pointer) < 1000
    (staged,) = (tmp_path / "inputs").glob("thread-history-*.md")
    assert str(staged) in pointer and "turn 39 " in staged.read_text()


@pytest.mark.parametrize(
    "written, status",
    [
        (None, "not_stated"),
        ("{not json", "could_not"),
        ('{"status": "nope"}', "could_not"),
        ("link", "could_not"),
    ],
)
def test_space_outcome_settles_even_when_missing_or_invalid(tmp_path, written, status):
    """An answer with no outcome file is unstated, as in project Work, not a failure."""
    from rcp.runs.space_chat import settle_space_turn_outcome

    if written == "link":
        (tmp_path / "elsewhere.json").write_text('{"status": "done"}')
        (tmp_path / "outcome.json").symlink_to(tmp_path / "elsewhere.json")
    elif written is not None:
        (tmp_path / "outcome.json").write_text(written)
    assert settle_space_turn_outcome(tmp_path, {}).status == status
    failed = {"p": {"name": "P", "status": "rejected"}}
    assert settle_space_turn_outcome(tmp_path, failed).status == "partial"
    if written == "link":
        assert (tmp_path / "elsewhere.json").read_text() == '{"status": "done"}'
        assert not (tmp_path / "outcome.json").is_symlink()


def test_a_retried_space_turn_records_the_members_message_once(manifest, tmp_path):
    from rcp.runs.space_chat import append_space_message

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    human = _authorizer(store)
    thread = next(
        item
        for item in store.ensure_agent_threads(human.user_id, [], default_names=("Pip",))
        if item.kind == "space"
    )
    request = _work_request().model_copy(update={"chat_id": thread.thread_id})
    owner = SpaceThreadOwner(store, app.state.catalog, thread.thread_id)
    parent = None
    for attempt in (1, 2, 3):
        task = store.create_agent_task(
            _task_record(
                store.now(),
                kind="space_chat",
                status="failed" if attempt < 3 else "succeeded",
                operation_id=f"attempt-{attempt}",
                project_id=store.space_id,
                request=request.model_dump(mode="json"),
                authorized_by=human,
                dispatch_authority=_work_authority(request),
                attempt=attempt,
                parent_operation_id=parent,
            ),
            continuation_cause="fresh" if parent is None else "retry",
        )
        append_space_message(owner, request, task.operation_id, role="user", text="Say hi")
        parent = task.operation_id
    append_space_message(owner, request, "attempt-3", role="assistant", text="Hi")
    transcript = read_space_chat_transcript(tmp_path / "data", human.user_id, thread.thread_id)
    assert transcript is not None
    assert [(item.role, item.operation_id) for item in transcript.messages] == [
        ("user", "attempt-1"),
        ("assistant", "attempt-3"),
    ]


def _space_owner_and_turn(app, store):
    human = _authorizer(store)
    thread = next(
        item
        for item in store.ensure_agent_threads(human.user_id, [], default_names=("Pip",))
        if item.kind == "space"
    )
    request = _work_request().model_copy(update={"chat_id": thread.thread_id})
    task = store.create_agent_task(
        _task_record(
            store.now(),
            kind="space_chat",
            status="running",
            operation_id="steered",
            project_id=store.space_id,
            request=request.model_dump(mode="json"),
            authorized_by=human,
            dispatch_authority=_work_authority(request),
        )
    )
    return SpaceThreadOwner(store, app.state.catalog, thread.thread_id), request, task


def test_a_delivered_space_steer_keeps_its_final_receipt(manifest, tmp_path):
    import uuid
    from concurrent.futures import Future
    from types import SimpleNamespace
    from typing import Any, cast

    from rcp.providers import ProviderSteerReceipt
    from rcp.runs.steering import _receipt, _record_steer, finish_chat_steer

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    owner, request, task = _space_owner_and_turn(app, store)
    stored = _record_steer(
        owner,
        request,
        task,
        message_id=str(uuid.uuid4()),
        text="Change course",
        receipt=_receipt(1, "turn", "unknown", "Waiting for the provider."),
    )
    delivered: Future[ProviderSteerReceipt] = Future()
    delivered.set_result(ProviderSteerReceipt("delivered"))
    background = cast(Any, SimpleNamespace(bump_steer_revision=lambda _operation_id: None))
    for _ in range(2):
        finish_chat_steer(owner, background, task, (stored, delivered))
    transcript = read_space_chat_transcript(
        tmp_path / "data", owner.thread.user_id, owner.thread_id
    )
    assert transcript is not None
    (steer,) = [item for item in transcript.messages if item.steering is not None]
    assert steer.steering is not None and steer.steering.status == "delivered"


def test_a_crash_cut_last_line_never_locks_the_space_thread(manifest, tmp_path):
    from rcp.runs.space_chat import append_space_message
    from rcp.service import canonical_space_chat_path

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    owner, request, task = _space_owner_and_turn(app, store)
    thread = owner.thread
    append_space_message(owner, request, task.operation_id, role="user", text="Hello")
    path = canonical_space_chat_path(tmp_path / "data", thread.user_id, thread.thread_id)
    with path.open("ab") as handle:
        handle.write(b'{"sessionId": "\xc3')
    transcript = read_space_chat_transcript(tmp_path / "data", thread.user_id, thread.thread_id)
    assert transcript is not None and [item.text for item in transcript.messages] == ["Hello"]
    append_space_message(owner, request, task.operation_id, role="assistant", text="Hi")
    transcript = read_space_chat_transcript(tmp_path / "data", thread.user_id, thread.thread_id)
    assert transcript is not None
    assert [item.text for item in transcript.messages] == ["Hello", "Hi"]
