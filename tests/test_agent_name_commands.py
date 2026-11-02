from __future__ import annotations

import json

import pytest

from rcp.agents import staged_command_client
from rcp.agents.auto_research_commands import auto_research_allowed_verbs
from rcp.agents.command_protocol import NameArguments, NameCommandRequest, validate_command_request
from rcp.runs.agent_names import stage_agent_name_pointer
from rcp.runs.questions import discuss_command_handler, work_command_handler

from .test_work_questions import identity, work_execution


def name_request(name):
    return NameCommandRequest(
        mailbox_id="a" * 32,
        request_id="b" * 32,
        credential="c" * 64,
        verb="name",
        arguments=NameArguments(name=name),
    )


@pytest.mark.parametrize("mode", ["work", "discuss"])
def test_self_name_bound_to_caller_and_snapshot(tmp_path, mode):
    execution, _ = work_execution(tmp_path, mode=mode)
    handler = (
        work_command_handler(execution, None)
        if mode == "work"
        else discuss_command_handler(execution)
    )
    state = execution.store.agent_name_state("project", "chat")
    assert state == {"name": "Choose", "can_self_name": True}
    stage_agent_name_pointer(execution, tmp_path, None)
    assert json.loads((tmp_path / "workspace" / "agent-name.json").read_text()) == state
    request = name_request("Theta0 reference probes")
    assert handler(request, identity(task_id="other")).status == "invalid"
    assert handler(request, identity("validate_only")).status == "invalid"
    result = handler(request, identity())
    assert result.status == "ok"
    assert execution.store.agent_name_state("project", "chat") == {
        "name": "Theta0 reference probes",
        "can_self_name": False,
    }
    if mode == "work":
        refused = handler(name_request("Second name"), identity())
        assert refused.status == "invalid" and refused.result["code"] == "chat_name_not_available"


def test_name_client_round_trip_and_surfaces(tmp_path):
    parser = staged_command_client._parser()
    parsed = parser.parse_args(
        [
            "--broker",
            "broker",
            "--timeout",
            "1",
            "--workspace",
            str(tmp_path),
            "name",
            "Theta0 reference probes",
        ]
    )
    verb, key, arguments = staged_command_client._request_arguments(parsed, str(tmp_path))
    original = name_request(arguments["name"])
    assert (verb, key) == ("name", None)
    assert validate_command_request(original.model_dump_json()) == original
    assert "name" in auto_research_allowed_verbs("orchestrator", bulletin=False, can_self_name=True)
    assert "name" not in auto_research_allowed_verbs("orchestrator", can_self_name=False)
    assert "name" not in auto_research_allowed_verbs("worker", can_self_name=True)
    with pytest.raises(ValueError):
        NameArguments(name="Mine", chat_id="someone-else")


def test_name_refused_after_task_ends(tmp_path):
    execution, _ = work_execution(tmp_path)
    handler = work_command_handler(execution, None)
    with execution.store.connection() as connection:
        connection.execute("UPDATE graph_runs SET status='succeeded' WHERE operation_id='turn'")
    response = handler(name_request("Late name"), identity())
    assert response.status == "invalid" and response.result["code"] == "chat_name_forbidden"
    assert execution.store.agent_name_state("project", "chat")["can_self_name"] is True


def test_orchestrator_names_its_conversation_once(tmp_path):
    from .test_auto_research_commands import _dispatcher, _Effects, _setup_auto_research

    store, episode, root = _setup_auto_research(tmp_path)
    dispatcher = _dispatcher(store, _Effects(store, episode, root).bundle())
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status='running' WHERE operation_id=?", (root.operation_id,)
        )
    response = dispatcher.dispatch(root.operation_id, name_request("Reference probe sweep"))
    assert response.status == "ok", response
    assert store.agent_name_state(root.project_id, episode.episode_id) == {
        "name": "Reference probe sweep",
        "can_self_name": False,
    }


@pytest.mark.parametrize("mode", ["work", "discuss"])
def test_named_conversation_keeps_name_context_without_name_grant(tmp_path, mode):
    from rcp.runs.agent_names import name_command_usage
    from rcp.runs.tasks.discuss import _discuss_execution_instructions

    execution, _ = work_execution(tmp_path, mode=mode)
    execution.store.self_name_chat_for_task(execution.operation_id, "Reference probes")
    handler = (
        work_command_handler(execution, None)
        if mode == "work"
        else discuss_command_handler(execution)
    )
    assert "name" not in handler.allowed_verbs
    assert stage_agent_name_pointer(execution, tmp_path, None)
    assert json.loads((tmp_path / "workspace" / "agent-name.json").read_text()) == {
        "name": "Reference probes",
        "can_self_name": False,
    }
    if mode == "discuss":
        assert name_command_usage("<command client>") not in _discuss_execution_instructions(
            handler
        )


@pytest.mark.parametrize("kind", ["space", "project"])
def test_private_agent_name_is_reread_each_turn_without_naming_permission(tmp_path, kind):
    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution
    from tests.helpers import authorized_human
    from tests.test_storage import _task_record

    execution, _ = work_execution(tmp_path)
    store = execution.store
    human = authorized_human(store)
    thread = next(
        t
        for t in store.ensure_agent_threads(human.user_id, ["project"], default_names=("Pip",))
        if t.kind == kind
    )
    scope = thread.project_id or store.space_id
    for turn, name in enumerate((store.chat_name(scope, thread.thread_id, ""), "New name")):
        if turn:
            store.set_chat_title(scope, thread.thread_id, human.user_id, name)
        task = store.create_agent_task(
            _task_record(
                store.now(),
                operation_id=f"private-{turn}",
                project_id=scope,
                kind=f"{kind}_chat",
                authorized_by=human,
                request={"chat_id": thread.thread_id, "mode": "work"},
            )
        )
        launch = AgentTaskExecution(task.operation_id, store, AgentProcessControl())
        assert stage_agent_name_pointer(launch, tmp_path, None)
        assert json.loads((tmp_path / "workspace" / "agent-name.json").read_text()) == {
            "name": name,
            "can_self_name": False,
        }
        assert "name" not in work_command_handler(launch, None).allowed_verbs
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status='succeeded' WHERE operation_id=?",
                (task.operation_id,),
            )


def test_name_prompt_renders_enforced_limit():
    from rcp.limits import AGENT_NAME_MAX_CHARS
    from rcp.runs.agent_names import name_command_usage

    assert str(AGENT_NAME_MAX_CHARS) in name_command_usage("client")


def test_child_work_conversation_has_name_grant(tmp_path):
    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution
    from rcp.runs.tasks.auto_research_child_work import _child_commands
    from tests.test_auto_research_children_storage import _setup_parent, _work_pair

    store, parent, root = _setup_parent(tmp_path)
    work, task = _work_pair(store, parent, root, worker_id="worker")
    store.create_auto_research_child_work(work, task)
    execution = AgentTaskExecution(task.operation_id, store, AgentProcessControl())
    assert "name" in _child_commands(execution, None, None)
    assert stage_agent_name_pointer(execution, tmp_path, None)


def test_seated_worker_has_no_name_context(tmp_path, manifest):
    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution
    from tests.test_auto_research_stream import _service, _setup_branch_auto_research

    main_service = _service(manifest, tmp_path)
    _service_branch, store, _episode, _root, worker = _setup_branch_auto_research(
        main_service, tmp_path / "store"
    )
    execution = AgentTaskExecution(worker.operation_id, store, AgentProcessControl())
    assert stage_agent_name_pointer(execution, tmp_path, None) == ""
    assert not (tmp_path / "workspace" / "agent-name.json").exists()


def test_consolidation_stages_current_name_without_name_grant(tmp_path):
    from .test_lessons_store import consolidate

    execution, human = work_execution(tmp_path)
    consolidate(execution.store)
    with execution.store.connection() as connection:
        connection.execute(
            "UPDATE consolidation_runs SET authorized_by_json=?", (human.model_dump_json(),)
        )
    execution.store.set_chat_title("project", "chat", human.user_id, "Named consolidation")
    stage_agent_name_pointer(execution, tmp_path, None)
    assert json.loads((tmp_path / "workspace" / "agent-name.json").read_text()) == {
        "name": "Named consolidation",
        "can_self_name": False,
    }
