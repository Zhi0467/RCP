from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from rcp.agents import AgentEvent
from rcp.background import BackgroundAgentTasks
from rcp.runs.provider_login import MachineDependenciesMissing
from rcp.service import RunRequest
from rcp.storage import AppStore

from .helpers import fabricated_authorizer
from .test_auto_research_delivery import _sse, _store
from .test_chat_question_followup import _answered


class _Checker:
    """Refuses every host in `refused` with its own reason; records each host asked."""

    def __init__(self, *refused: str) -> None:
        self.refused = set(refused)
        self.hosts: list[str] = []

    def launch_refusal(self, host: str) -> str | None:
        self.hosts.append(host)
        return f"missing on {host or 'local'}" if host in self.refused else None


def _task_count(store: AppStore) -> int:
    with store.connection() as connection:
        return connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0]


async def _done(*_args):
    yield _sse(AgentEvent(event="done"))


@pytest.mark.parametrize("refused", [True, False])
def test_admission_asks_the_execution_machine_before_any_row(tmp_path, refused):
    store = _store(tmp_path)
    checker = _Checker("") if refused else _Checker()
    tasks = BackgroundAgentTasks(store, _done, dependency_checker=checker)  # pyright: ignore[reportArgumentType]
    request = RunRequest(
        provider="codex",
        run_on="local",
        chat_scope="project",
        chat_id=str(uuid4()),
        message="Inspect the result.",
        mode="work",
    )
    if refused:
        with pytest.raises(MachineDependenciesMissing):
            tasks.start("project", "project_chat", request, authorized_by=fabricated_authorizer())
        assert _task_count(store) == 0
    else:
        record = tasks.start(
            "project", "project_chat", request, authorized_by=fabricated_authorizer()
        )
        assert store.agent_task(record.operation_id) is not None
    assert set(checker.hosts) == {""}


def test_refused_question_followup_creates_no_task_and_keeps_the_answer_unclaimed(
    tmp_path, monkeypatch
):
    from rcp.runs.chat import reconcile_chat_question_answers

    store = AppStore(tmp_path / "store.sqlite3")
    question = _answered(store)
    checker = _Checker("")
    tasks = BackgroundAgentTasks(store, _done, dependency_checker=checker)  # pyright: ignore[reportArgumentType]
    monkeypatch.setattr("rcp.runs.chat.project_chat_question_answer", lambda *_args: None)
    before = _task_count(store)
    statuses = reconcile_chat_question_answers(
        store,
        tasks,
        lambda _: SimpleNamespace(for_graph_target=lambda _target: None),  # pyright: ignore[reportArgumentType]
    )
    assert statuses == {question.question_id: checker.launch_refusal("")}
    assert _task_count(store) == before
    unclaimed = store.get_question(question.question_id)
    assert unclaimed is not None and unclaimed.followup_operation_id is None


def test_api_refuses_a_run_on_a_machine_missing_dependencies(manifest, tmp_path, monkeypatch):
    from .helpers import create_named_app, signed_in_client

    checker = _Checker("")
    monkeypatch.setattr("rcp.api.app.DependencyChecker", lambda: checker)
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    assert app.state.dependency_checker is checker
    store = app.state.background_tasks.store
    response = signed_in_client(app).post(
        f"/api/projects/{app.state.default_project_id}/tasks/project_chat",
        json={
            "chat_id": str(uuid4()),
            "message": "Choose the next route.",
            "run_truth_scope": ["repo-a"],
            "mode": "work",
        },
    )
    assert response.status_code == 422, response.text
    assert checker.hosts == [""]
    assert _task_count(store) == 0
