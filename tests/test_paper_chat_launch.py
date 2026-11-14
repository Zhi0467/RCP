from __future__ import annotations

import asyncio
import json
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.agents import AgentEvent
from rcp.agents.write_scope import ProjectWriteScope
from rcp.background import BackgroundAgentTasks
from rcp.core.authority import AgentDispatchScope
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.runs.paper_turns import PAPER_TURN_WAIT_MESSAGE
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskAdmissionConflict
from tests.helpers import wait_for_task, wait_until
from tests.lifecycles.agent_task import admitted_launch_task, seeded_store, sse, work_request

PAPER = "/home/rcp/data/paper/project"


def _scope(paper_root: str | None = None) -> ProjectWriteScope:
    return ProjectWriteScope.create(
        project_id="project",
        machine_id=BACKEND_MACHINE_ID,
        capability="work_auto",
        stage_root="/tmp/stages/s",
        workspace_root="/tmp/stages/s/workspace",
        repositories=[],
        granted_roots=["/home/rcp", "/tmp"],
        protected_write_paths=["/home/rcp/.rcp", "/home/rcp/data"],
        granted_protected_paths=["/home/rcp/.rcp", "/home/rcp/data"],
        paper_root=paper_root,
    )


def _paper_request(chat_id: str, **updates: object) -> RunRequest:
    return work_request(chat_id).model_copy(update={"focus": "paper", **updates})


def test_paper_root_is_a_writable_root_that_changes_scope_identity() -> None:
    plain, paper = _scope(), _scope(PAPER)
    restored = ProjectWriteScope.model_validate_json(paper.model_dump_json())
    assert PAPER in restored.writable_roots and PAPER not in plain.writable_roots
    assert restored.fingerprint == paper.fingerprint != plain.fingerprint
    old_record = json.loads(plain.model_dump_json())
    old_record.pop("paper_root")
    assert ProjectWriteScope.model_validate(old_record).fingerprint == plain.fingerprint


@pytest.mark.parametrize("paper_root", [None, PAPER])
def test_each_provider_writes_the_paper_only_with_a_paper_scope(paper_root, tmp_path) -> None:
    from rcp.providers.claude.profile import _claude_write_settings
    from rcp.providers.codex.profile import _codex_permission_profile
    from rcp.providers.opencode.profile import _permission, _root_pattern

    scope = _scope(paper_root)
    granted = paper_root is not None

    claude = _claude_write_settings(scope)["permissions"]
    assert (f"Edit(/{PAPER}/**)" in claude["allow"]) is granted
    assert ("Edit(//home/rcp/data/**)" in claude["deny"]) is not granted

    codex = tomllib.loads(_codex_permission_profile(scope))["permissions"]["rcp_project"]
    assert (codex["workspace_roots"].get(PAPER) is True) is granted

    edit = _permission("work_auto", tmp_path, [], scope)["edit"]
    assert isinstance(edit, dict)
    order = list(edit)
    assert (edit.get(_root_pattern(PAPER)) == "allow") is granted
    if granted:
        assert order.index(_root_pattern(PAPER)) > order.index(_root_pattern("/home/rcp/data"))


@pytest.mark.parametrize("focus", [None, "paper"])
def test_launch_scope_adds_the_paper_folder_only_for_a_paper_turn(focus, tmp_path, monkeypatch):
    from rcp.agents import memory_files
    from rcp.runs import chat

    monkeypatch.setattr(memory_files, "prepare_project_memory", lambda *a, **kw: (None, None))
    monkeypatch.setattr(chat, "resolve_project_write_scope", lambda **kwargs: kwargs)
    monkeypatch.setattr(chat, "write_repositories", lambda state: [])
    monkeypatch.setattr(chat, "_machine_writable_paths", lambda *args: [])
    request = _paper_request("coach") if focus else work_request("chat")
    task = SimpleNamespace(
        kind="project_chat",
        project_id="project",
        episode_id=None,
        request={},
        dispatch_authority=resolve_dispatch_authority("project_chat", request),
    )
    store = SimpleNamespace(
        path=tmp_path / "rcp.sqlite3",
        agent_task=lambda _: task,
        agent_thread=lambda _: None,
    )
    result = chat._project_write_scope(
        SimpleNamespace(repositories=[]),
        SimpleNamespace(
            history=SimpleNamespace(state=lambda: None),
            manifest=None,
            repository_ownership_inventory=lambda **kwargs: [],
        ),
        "local",
        workspace=tmp_path / "stage",
        remote_stage=None,
        data_dir=tmp_path,
        execution=SimpleNamespace(store=store, operation_id="turn"),
        capability="work_auto",
        stage_only=True,
    )
    folder = tmp_path.resolve() / "paper" / "project"
    assert result["paper_root"] == (str(folder) if focus else None)
    assert folder.is_dir() is bool(focus)


def test_a_paper_request_always_works_and_records_its_focus() -> None:
    request = RunRequest(chat_scope="project", chat_id="coach", mode="discuss", focus="paper")
    authority = resolve_dispatch_authority("project_chat", request)
    assert request.mode == "work"
    assert authority is not None and authority.task_contract == "work_auto"
    assert authority.scope.focus == "paper"
    with pytest.raises(ValueError):
        RunRequest(chat_scope="node", node_id="n", chat_id="c", focus="paper")
    with pytest.raises(ValueError):
        resolve_dispatch_authority("space_chat", request)


def test_rows_from_before_focus_load_without_one() -> None:
    plain = work_request("chat")
    assert "focus" not in plain.model_dump(mode="json")
    authority = resolve_dispatch_authority("project_chat", plain)
    assert authority is not None
    stored = authority.scope.model_dump(mode="json")
    assert "focus" not in stored
    assert AgentDispatchScope.model_validate(stored).focus is None


def test_the_first_turn_fixes_the_chat_focus(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    store.admit_chat_focus("project", "coach", "paper")
    admitted_launch_task(store, operation_id="first", request=_paper_request("coach"))
    store.admit_chat_focus("project", "coach", "paper")
    with pytest.raises(AgentTaskAdmissionConflict):
        store.admit_chat_focus("project", "coach", None)
    store.set_chat_archived("project", "coach", "alice", archived=False)
    store.admit_chat_focus("project", "coach", "paper")

    store.admit_chat_focus("project", "plain", None)
    admitted_launch_task(store, operation_id="plain-first", request=work_request("plain"))
    with pytest.raises(AgentTaskAdmissionConflict):
        store.admit_chat_focus("project", "plain", "paper")


class _Gate:
    """A provider stream that holds each turn until released, failing when asked to."""

    def __init__(self) -> None:
        self.release: dict[str, threading.Event] = {}
        self.fail: set[str] = set()
        self.started: list[str] = []

    async def stream(self, _project_id, _kind, _request, execution):
        operation_id = execution.operation_id
        self.started.append(operation_id)
        event = self.release.setdefault(operation_id, threading.Event())
        while not event.is_set():
            await asyncio.sleep(0.01)
        if operation_id in self.fail:
            raise RuntimeError("provider failed")
        yield sse(AgentEvent(event="done"))


def _paper_turn(store, gate: _Gate, operation_id: str, chat_id: str):
    gate.release[operation_id] = threading.Event()
    return admitted_launch_task(store, operation_id=operation_id, request=_paper_request(chat_id))


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_a_second_paper_turn_waits_and_starts_when_the_first_settles(
    outcome, tmp_path: Path
) -> None:
    store = seeded_store(tmp_path)
    gate = _Gate()
    tasks = BackgroundAgentTasks(store, gate.stream)
    try:
        first = _paper_turn(store, gate, "first", "coach-a")
        second = _paper_turn(store, gate, "second", "coach-b")
        other = admitted_launch_task(store, operation_id="other", request=work_request("plain"))
        tasks.launch_admitted(first.operation_id)
        waiting = tasks.launch_admitted(second.operation_id)
        tasks.launch_admitted(other.operation_id)
        wait_until(lambda: "first" in gate.started and "other" in gate.started)

        assert waiting.status == "queued"
        assert waiting.status_message == PAPER_TURN_WAIT_MESSAGE
        assert "second" not in gate.started

        if outcome == "failed":
            gate.fail.add("first")
        gate.release["first"].set()
        wait_for_task(store, "first", expect=outcome)
        wait_until(lambda: "second" in gate.started)
        gate.release["second"].set()
        gate.release["other"].set()
        wait_for_task(store, "second", expect="succeeded")
    finally:
        for event in gate.release.values():
            event.set()
        tasks.shutdown()


def test_a_waiting_paper_turn_can_be_cancelled(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    gate = _Gate()
    tasks = BackgroundAgentTasks(store, gate.stream)
    try:
        first = _paper_turn(store, gate, "first", "coach-a")
        cancelled = _paper_turn(store, gate, "cancelled", "coach-b")
        third = _paper_turn(store, gate, "third", "coach-c")
        for task in (first, cancelled, third):
            tasks.launch_admitted(task.operation_id)
        wait_until(lambda: "first" in gate.started)

        assert tasks.pause(cancelled.operation_id).status == "paused"
        gate.release["first"].set()
        wait_until(lambda: "third" in gate.started)
        gate.release["third"].set()
        wait_for_task(store, "third", expect="succeeded")
        assert "cancelled" not in gate.started
    finally:
        for event in gate.release.values():
            event.set()
        tasks.shutdown()


def _work_contract(scope: ProjectWriteScope) -> str:
    from rcp.agents.prompts import PromptFactory

    return PromptFactory.work_task_contract(
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=False,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        focused_node_id=None,
        repositories=[],
        paper_path=PAPER,
        human_request_path=None,
        patch_path="/stage/patch.json",
        artifact_path="/stage/artifacts",
        output_schema_path="/stage/schema.json",
        command_client="client",
        write_scope=scope,
    )


@pytest.mark.parametrize("paper_root", [None, PAPER])
def test_only_a_paper_scope_names_the_paper_writable_and_carries_its_guidance(paper_root):
    from rcp.agents.prompts import write_scope_section

    scope = _scope(paper_root)
    plain = _work_contract(_scope())
    assert (PAPER in write_scope_section(scope)) is (paper_root is not None)
    assert PAPER in plain
    assert (_work_contract(scope).count(PAPER) > plain.count(PAPER)) is (paper_root is not None)


def test_the_auto_research_orchestrator_names_the_paper() -> None:
    from rcp.agents import auto_research_prompt as auto
    from rcp.agents.auto_research_commands import auto_research_allowed_verbs

    arguments = dict(
        graph_path="/stage/graph.json",
        research_path="/stage/research.md",
        repositories=[],
        patch_path="/stage/patch.json",
        output_schema_path="/stage/schema.json",
        command_client="client",
        write_scope=_scope(),
    )
    contract = auto.auto_research_orchestrator_task_contract(
        **arguments,
        project_name="Example",
        allowed_verbs=auto_research_allowed_verbs("orchestrator"),
        paper_path=PAPER,
    )
    values = auto.auto_research_prompt_values(**arguments, group_context=None, paper_path=PAPER)
    assert PAPER in contract and values["paper"] == PAPER


def test_the_chat_inventory_names_a_paper_chat_by_its_focus(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    store.admit_chat_focus("project", "coach", "paper")
    admitted_launch_task(store, operation_id="coach-turn", request=_paper_request("coach"))
    store.admit_chat_focus("project", "plain", None)
    store.set_chat_title("project", "plain", "alice", "Plain")
    admitted_launch_task(store, operation_id="plain-turn", request=work_request("plain"))
    focus = {row["chat_id"]: row.get("focus") for row in store.chat_inventory("project")}
    assert focus == {"coach": "paper", "plain": None}
