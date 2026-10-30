from __future__ import annotations

import pytest

from rcp.agents import AgentProcessControl
from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.provider_accounts import ProviderAccounts
from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.background import AgentTaskExecution
from rcp.runs.experiment_questions import reconcile_experiment_question_answers
from rcp.runs.questions import work_command_handler
from rcp.service import RunRequest, resolve_dispatch_authority
from rcp.storage import AgentTaskRecord
from rcp.transport.workspace_mailbox import RunStageMailbox

from .helpers import append_fixture_patch, authorized_human, seed_patch, wait_for_task
from .helpers import create_named_app as create_app
from .provider_login_switch_helpers import switch_background
from .test_experiment_questions import _ready_answer
from .test_provider_login_resolver import _login
from .test_work_experiment_kickoff import WORK_TURN, _experiment_patch, _kickoff


@pytest.mark.parametrize("direction", ["personal_to_team", "team_to_personal"])
@pytest.mark.parametrize("operation", ["work_kickoff", "experiment_answer_wake"])
def test_experiment_entry_point_resolves_usable_login(manifest, tmp_path, direction, operation):
    selected = []
    stage = tmp_path / "stage"
    stage.mkdir()
    if operation == "work_kickoff":
        app = create_app(str(manifest.path), data_dir=tmp_path / "data")
        append_fixture_patch(app.state.service, seed_patch())
        append_fixture_patch(app.state.service, _experiment_patch())
        tasks = app.state.background_tasks
        store = tasks.store
        member = authorized_human(store)
        request = RunRequest(
            provider="codex",
            run_truth_scope=["repo-a"],
            chat_scope="project",
            chat_id="work-chat",
            message="Run the comparison.",
            mode="work",
        )
        store.create_agent_task(
            AgentTaskRecord(
                operation_id=WORK_TURN,
                project_id=app.state.default_project_id,
                kind="project_chat",
                status="running",
                request=request.model_dump(mode="json"),
                created_at=store.now(),
                updated_at=store.now(),
                status_message="Working.",
                authorized_by=member,
                dispatch_authority=resolve_dispatch_authority("project_chat", request),
                native_session_id="session",
            )
        )
        previous = WORK_TURN
        tasks.stream = switch_background(store, stage, selected).stream
    else:
        store, episode_id, question, _ = _ready_answer(tmp_path)
        member = store.episode(episode_id).authorized_by
        previous = question.origin.operation_id
        tasks = switch_background(store, stage, selected)
    accounts = ProviderAccounts(store, ProviderCredentialStore.for_data_dir(store.path.parent))
    own = _login(accounts, "codex", own=True, member=member.user_id)
    shared = _login(accounts, "codex", machine=True, member=member.user_id)
    old, new = (own, shared) if direction == "personal_to_team" else (shared, own)
    store.set_provider_login_setting(member.user_id, "codex", "", "own_then_shared")
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET login_id=? WHERE operation_id=?", (old.login_id, previous)
        )
    store.record_agent_task_receipt(previous, "provider_runtime_selected", {})
    store.mark_provider_login_signed_out(
        old, member_id=member.user_id, source="restore", detail="expired"
    )
    if operation == "work_kickoff":
        (stage / "goal.md").write_text("Run the comparison.", encoding="utf-8")
        execution = AgentTaskExecution(
            operation_id=WORK_TURN,
            store=store,
            control=AgentProcessControl(),
            experiment_starter=tasks.experiment_starter,
        )
        result = work_command_handler(execution, None, mailbox=RunStageMailbox(stage, None))(
            _kickoff("switched"),
            CommandTurnIdentity(
                episode_id=None, task_id=WORK_TURN, turn_id="turn", authority="broker"
            ),
        )
        assert result.status == "ok", result.message
        operation_id = store.episode(result.result["episode_id"]).root_operation_id
    else:
        reconcile_experiment_question_answers(tasks)
        operation_id = store.get_question(question.question_id).followup_operation_id
    assert operation_id is not None
    finished = wait_for_task(store, operation_id, expect="succeeded")
    assert finished.login_id == new.login_id
    assert selected == [(new, None if operation == "work_kickoff" else "native-session", None)]
    assert store.agent_task(previous).login_id == old.login_id
