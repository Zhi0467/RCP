"""The recommended next step follows why a turn failed, not only that it did."""

from __future__ import annotations

import pytest

from rcp.api.experiment_controls import _experiment_recommendation
from rcp.control import ExperimentControlState, ExperimentOperationalState
from rcp.storage.models import AgentFailureKind


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


@pytest.mark.parametrize("failure_kind", [None, "transport_lost", "other"])
def test_an_ordinary_failure_still_recommends_the_task_control(
    failure_kind: AgentFailureKind | None,
) -> None:
    recommendation = _experiment_recommendation(
        _control(failure_kind),
        None,
        "needs_action",
        active=False,
        awaiting_human=True,
        task_control="retry",
    )

    assert recommendation == "retry"


def test_a_revoked_login_asks_for_a_sign_in_rather_than_a_retry() -> None:
    """Retrying a revoked login loops forever; the human has to sign in again,
    and the card is where they should learn that."""

    recommendation = _experiment_recommendation(
        _control("provider_auth"),
        None,
        "needs_action",
        active=False,
        awaiting_human=True,
        task_control="retry",
    )

    assert recommendation == "reauthenticate_provider"


def test_a_live_turn_is_still_only_worth_waiting_for() -> None:
    """An active turn outranks a stale failure kind from an earlier attempt."""

    recommendation = _experiment_recommendation(
        _control("provider_auth"),
        None,
        "agent_active",
        active=True,
        awaiting_human=False,
        task_control="retry",
    )

    assert recommendation == "wait"
