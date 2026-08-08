from __future__ import annotations

from pydantic import ValidationError

from rcp.service import ChatMessage, GraphUpdateResult, RunRequest


def test_conversation_requests_carry_mode_and_nothing_else_authorizes_the_graph() -> None:
    request = RunRequest(mode="work", message="Run the experiment.")

    assert request.mode == "work"
    assert request.model_dump(mode="json") == {
        "provider": None,
        "run_truth_scope": None,
        "model": None,
        "reasoning": None,
        "run_on": None,
        "chat_scope": "node",
        "node_id": None,
        "message": "Run the experiment.",
        "chat_id": None,
        "session_id": None,
        "mode": "work",
        "trigger": "human",
        "patch_kind": "work",
        "control_node_id": None,
        "control_revision": None,
        "control_episode_id": None,
        "control_invocation": None,
        "control_invocation_ceiling": None,
        "control_decision_bundle": [],
        "control_completion_criteria": [],
        "watcher_ids": [],
        "workflow_ids": None,
        "skill_ids": None,
        "invoked_workflow_ids": [],
        "invoked_skill_ids": [],
        "invoked_provider_skill_names": [],
        "resolved_provider_skills": [],
        "resolved_skill_packages": None,
        "attachment_set_id": None,
        "attachment_client_id": None,
        "attachment_batch_id": None,
        "attachments": [],
    }


def test_the_retired_graph_gate_grants_no_authority() -> None:
    request = RunRequest.model_validate(
        {"message": "Update the graph.", "allow_graph_change": True}
    )

    assert request.mode == "discuss"
    assert "allow_graph_change" not in request.model_dump(mode="json")


def test_graph_update_result_round_trips_through_a_chat_message() -> None:
    graph_update = GraphUpdateResult(
        status="rejected",
        change_summary=["Recorded the experiment outcome."],
        proposal_ids=["prop/review-next-run"],
        validation_messages=["Patch revision is stale."],
        correction_rounds=2,
    )
    message = ChatMessage(
        message_id="message-1",
        role="assistant",
        text="The experiment completed.",
        timestamp="2026-08-01T12:00:00+00:00",
        mode="work",
        graph_update=graph_update,
    )

    assert message.graph_update == graph_update
    assert message.model_dump(mode="json")["graph_update"]["status"] == "rejected"


def test_conversation_mode_is_closed() -> None:
    try:
        RunRequest(mode="auto")
    except ValidationError:
        pass
    else:
        raise AssertionError("conversation mode must be discuss or work")
