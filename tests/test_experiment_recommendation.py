"""The recommended next step follows why a turn failed, not only that it did."""

from __future__ import annotations

import pytest

from rcp.api.experiment_controls import _experiment_recommendation
from rcp.control import ExperimentControlState, ExperimentOperationalState
from rcp.storage.models import AgentFailureKind


def _revoked_login(
    control: ExperimentControlState,
    *,
    task_control: str | None,
    awaiting_human: bool,
) -> bool:
    """The builder's own derivation, so these cases cannot drift from it."""

    return bool(
        control.operational.current_failure_kind == "provider_auth"
        and task_control is not None
        and awaiting_human
    )


def _control(failure_kind: AgentFailureKind | None) -> ExperimentControlState:
    return ExperimentControlState(
        ready=False,
        invocations_used=4,
        invocation_ceiling=10,
        invocations_remaining=6,
        episode_id="episode",
        paused=False,
        active=False,
        operational=ExperimentOperationalState(
            task_active=True,
            episode_live=True,
            current_operation_id="turn",
            current_status="failed",
            current_awaiting_human=True,
            current_failure_kind=failure_kind,
        ),
    )


@pytest.mark.parametrize("failure_kind", [None, "transport_lost"])
def test_an_ordinary_failure_still_recommends_the_task_control(
    failure_kind: AgentFailureKind | None,
) -> None:
    control = _control(failure_kind)
    recommendation = _experiment_recommendation(
        control,
        None,
        "needs_action",
        active=False,
        awaiting_human=True,
        task_control="retry",
        revoked_login=_revoked_login(control, task_control="retry", awaiting_human=True),
    )

    assert recommendation == "retry"


def test_a_revoked_login_asks_for_a_sign_in_rather_than_a_retry() -> None:
    """Retrying a revoked login loops forever; the human has to sign in again,
    and the card is where they should learn that."""

    control = _control("provider_auth")
    recommendation = _experiment_recommendation(
        control,
        None,
        "needs_action",
        active=False,
        awaiting_human=True,
        task_control="retry",
        revoked_login=_revoked_login(control, task_control="retry", awaiting_human=True),
    )

    assert recommendation == "reauthenticate_provider"


def test_a_live_turn_is_still_only_worth_waiting_for() -> None:
    """An active turn outranks a stale failure kind from an earlier attempt."""

    control = _control("provider_auth")
    recommendation = _experiment_recommendation(
        control,
        None,
        "agent_active",
        active=True,
        awaiting_human=False,
        task_control="retry",
        revoked_login=_revoked_login(control, task_control="retry", awaiting_human=False),
    )

    assert recommendation == "wait"


def test_signed_out_experiment_retry_creates_no_task_or_budget(tmp_path):
    from rcp.agents import AgentEvent
    from rcp.background import BackgroundAgentTasks

    from .helpers import fabricated_authorizer, wait_for_task
    from .test_background import _experiment_request, _sse, _store

    store = _store(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()

    async def stream(_project, _kind, _request, execution):
        execution.checkpoint_stage("", str(stage))
        yield _sse(AgentEvent(event="session", session_id="experiment-session"))
        yield _sse(AgentEvent(event="error", text="refresh_token_reused"))

    tasks = BackgroundAgentTasks(store, stream)
    root = tasks.start(
        "project",
        "node_chat",
        _experiment_request().model_copy(update={"run_on": "local"}),
        authorized_by=fabricated_authorizer(),
    )
    root = wait_for_task(store, root.operation_id, expect="failed")
    store.mark_provider_login_failed("codex", "", generation=0, detail="expired", source="turn")
    with store.connection() as connection:
        before = connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0]
    budget = store.episode(root.episode_id).invocations_used
    with pytest.raises(ValueError, match="signed out"):
        tasks.retry(root.operation_id)
    assert store.episode(root.episode_id).invocations_used == budget
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == before
