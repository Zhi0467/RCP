from __future__ import annotations

from pathlib import Path

import pytest

from rcp.agents import AgentProcessControl
from rcp.agents.continuation_prompt import changed_since_master, master_key
from rcp.agents.prompts import chat_master_contract_key
from rcp.background import AgentTaskExecution
from rcp.runs.chat import (
    _commit_chat_prompt_state,
    _prepare_chat_prompt_state,
    chat_continuation_master,
)
from rcp.runs.session_master import record_session_master
from rcp.runs.tasks.experiment_loop import EXPERIMENT_LOOP_POLICY_VERSION, _experiment_master
from rcp.service import RunRequest
from rcp.storage import AgentTaskRecord, AppStore, EpisodeRecord
from tests.test_auto_research_children_storage import _identity, _project
from tests.test_remote_provider_stream_fence import _Stage


@pytest.fixture
def session(tmp_path):
    store = AppStore(tmp_path / "state.sqlite3")
    _project(store)
    stage = tmp_path / "stage"
    stage.mkdir()
    request = RunRequest(
        chat_scope="project", chat_id="chat", provider="codex", run_on="laptop", message="turn"
    )
    now = store.now()
    store.create_episode(
        EpisodeRecord(
            episode_id="episode",
            project_id="project",
            mode="experiment_loop",
            control_node_id="experiment",
            status="queued",
            invocation_ceiling=4,
            authorized_by=_identity(store),
            created_at=now,
            updated_at=now,
        )
    )

    def task(operation_id, *, episode=False, kind="project_chat", launch_kind=None):
        now = store.now()
        payload = request.model_dump(mode="json")
        if episode:
            payload["control_episode_id"] = "episode"
        if launch_kind:
            payload["artifact_edit"] = {"launch_kind": launch_kind}
        record = AgentTaskRecord(
            operation_id=operation_id,
            project_id="project",
            kind=kind,
            status="running",
            request=payload,
            native_session_id="session",
            stage_root=str(stage),
            episode_id="episode" if episode else None,
            created_at=now,
            updated_at=now,
            status_message="fixture",
            authorized_by=_identity(store),
        )
        if episode:
            store.allocate_episode_invocation("episode", record)
        else:
            store.create_agent_task(record)
        return AgentTaskExecution(
            operation_id=operation_id, store=store, control=AgentProcessControl()
        )

    def succeed(execution):
        store.complete_agent_task(execution.operation_id, applied_revision=None, result={})

    return store, stage, request, task, succeed


def prepare(execution, request, stage):
    return _prepare_chat_prompt_state(
        execution,
        request,
        local_stage=stage,
        remote_stage=None,
        master_context="chat contract",
        contract_key=chat_master_contract_key(ontology_extensions=False),
        values={"current": {"graph_revision": 1}},
    )[1]


@pytest.mark.parametrize("phase", ["fresh", "resume", "retry"])
@pytest.mark.parametrize("baseline", [False, True])
@pytest.mark.parametrize("remote", [False, True])
def test_human_reopens_own_master_after_experiment(session, phase, baseline, remote):
    store, stage, request, task, succeed = session
    if baseline:
        first = task("human-first")
        prepare(first, request, stage)
        _commit_chat_prompt_state(first, request, "session")
        succeed(first)
    experiment = task("experiment", episode=True)
    record_session_master(store, experiment.operation_id, "experiment contract", "experiment-key")
    succeed(experiment)
    human = task("human-next")
    request = request.model_copy(update={"session_id": "session"})
    if phase == "fresh":
        master = _prepare_chat_prompt_state(
            human,
            request,
            local_stage=None if remote else stage,
            remote_stage=_Stage() if remote else None,
            master_context="chat contract",
            contract_key=chat_master_contract_key(ontology_extensions=False),
            values={},
        )[1]
    else:
        human.continuation = phase
        master = chat_continuation_master(
            human,
            request,
            session_id="session",
            local_stage=None if remote else stage,
            remote_stage=_Stage() if remote else None,
            policy_version="chat",
            ontology_extensions=False,
            render=lambda: "chat contract",
            values={},
        )
    assert master.bootstrap and master.replaces
    assert store.agent_task_contract(human.operation_id, "session_master") == "chat contract"
    succeed(human)
    assert store.latest_session_master("project", "session")[0] == human.operation_id


@pytest.mark.parametrize("edit", [None, "discuss", "revoking"])
def test_experiment_repair_keeps_own_values_and_owner_revocation(session, edit):
    store, stage, request, task, succeed = session
    # Only a revoking edit replaces the session's instructions; a Discuss edit runs
    # under the chat master and must not force a reopening.
    revocation = edit == "revoking"
    experiment = task("experiment", episode=True)
    key = master_key(EXPERIMENT_LOOP_POLICY_VERSION, ontology_extensions=False)
    record_session_master(
        store, experiment.operation_id, "experiment contract", key, {"path": "old"}
    )
    succeed(experiment)
    if edit:
        succeed(task("edit", kind="artifact_edit", launch_kind=edit))
    human = task("human")
    master = prepare(human, request.model_copy(update={"session_id": "session"}), stage)
    assert master.after_report == revocation
    succeed(human)
    repair = task("repair", episode=True)
    master = _experiment_master(
        repair,
        stage,
        None,
        session_id="session",
        episode_id="episode",
        ontology_extensions=False,
        render=None,
    )
    assert master.bootstrap and master.replaces
    assert master.after_report == revocation
    assert Path(master.path).read_text() == "experiment contract"
    assert master.values == {"path": "old"}
    assert changed_since_master(master, {"path": "new"}) == {"path": "new"}
    if revocation:

        def pending():
            return store.episode_report_rebootstrap_pending(
                "project", "session", stage_host=None, stage_root=str(stage)
            )

        assert pending()  # Recording a launch alone cannot clear revocation.
        succeed(repair)
        assert not pending()


@pytest.mark.asyncio
async def test_report_human_success_preserves_experiment_report_rebootstrap(manifest, tmp_path):
    from rcp.runs.tasks.episode_report import stream_episode_report_run
    from tests.test_episode_report import _events, _ReportLauncher, _setup_report

    service, store, report, execution, stage = _setup_report(manifest, tmp_path)
    events = await _events(
        stream_episode_report_run(service, _ReportLauncher(["valid"]), report, execution)
    )
    assert events[-1].event == "done"

    def operational(operation_id, episode=False):
        now = store.now()
        request = RunRequest(
            provider="codex",
            run_on="laptop",
            session_id=report.session_id,
            control_episode_id="episode" if episode else None,
        )
        store.create_agent_task(
            AgentTaskRecord(
                operation_id=operation_id,
                project_id="project",
                kind="node_chat",
                status="running",
                request=request.model_dump(mode="json"),
                native_session_id=report.session_id,
                stage_root=str(stage),
                created_at=now,
                updated_at=now,
                status_message="fixture",
            )
        )
        return request, AgentTaskExecution(
            operation_id=operation_id, store=store, control=AgentProcessControl()
        )

    request, human = operational("human")
    master = chat_continuation_master(
        human,
        request,
        session_id=report.session_id,
        local_stage=stage,
        remote_stage=None,
        policy_version="chat",
        ontology_extensions=False,
        render=lambda: "human master",
    )
    assert master.bootstrap and master.after_report
    store.complete_agent_task("human", applied_revision=None, result={})
    assert not store.episode_report_rebootstrap_pending(
        "project", report.session_id, stage_host=None, stage_root=str(stage), owner="chat"
    )
    _, continuation = operational("continued", episode=True)
    master = _experiment_master(
        continuation,
        stage,
        None,
        session_id=report.session_id,
        episode_id="episode",
        ontology_extensions=False,
        render=lambda: "experiment master",
        values={},
    )
    assert master.bootstrap and master.after_report and master.replaces
    store.complete_agent_task("continued", applied_revision=None, result={})
    assert not store.episode_report_rebootstrap_pending(
        "project", report.session_id, stage_host=None, stage_root=str(stage)
    )
