from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from rcp.core.authority import AgentDispatchAuthority, AgentDispatchScope
from rcp.core.models import AuthorizedHuman
from rcp.runs.auto_research import AutoResearchRunRequest
from rcp.runs.auto_research_mail import (
    AUTO_RESEARCH_MAIL_MAX_BYTES,
    AutoResearchMailDelivery,
    auto_research_mail_claim_prefix,
    auto_research_mail_delivery,
    parse_auto_research_mail_delivery,
    stage_auto_research_mail_delivery,
)
from rcp.storage import (
    AgentTaskRecord,
    AppStore,
    AutoResearchMessageRecord,
    AutoResearchStateRecord,
    EpisodeRecord,
    ProjectRecord,
)
from rcp.transport.workspace_mailbox import RunStageMailbox, clear_turn_handoff_files

_RUN_TRUTH_SCOPE = ["repo-a"]


def _auto_research_authority(episode_id: str, role: str) -> AgentDispatchAuthority:
    return AgentDispatchAuthority(
        profile="orchestrator" if role == "orchestrator" else "ordinary",
        task_contract="orchestrate" if role == "orchestrator" else "work_auto",
        scope=AgentDispatchScope(
            run_truth_scope=_RUN_TRUTH_SCOPE,
            episode_id=episode_id,
            patch_kind="work",
        ),
    )


def _claimed_delivery(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator="/tmp/project/research.yaml",
            name="project",
            state_location="/tmp/project/.research",
            state_remote=False,
            added_at=store.now(),
        )
    )
    authorizer = AuthorizedHuman(
        space_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        display_name="AutoResearch owner",
    )
    now = store.now()
    auto_research, root = store.create_auto_research_episode_with_root_task(
        EpisodeRecord(
            episode_id="auto_research",
            project_id="project",
            mode="auto_research",
            status="queued",
            invocation_ceiling=5,
            authorized_by=authorizer,
            created_at=now,
            updated_at=now,
        ),
        AutoResearchStateRecord(
            episode_id="auto_research",
            starting_instruction=None,
            created_at=now,
            updated_at=now,
        ),
        AgentTaskRecord(
            operation_id="root",
            project_id="project",
            episode_id="auto_research",
            kind="auto_research",
            status="queued",
            request=AutoResearchRunRequest(
                episode_id="auto_research",
                role="orchestrator",
                actor_operation_id="root",
                run_truth_scope=_RUN_TRUTH_SCOPE,
            ).model_dump(mode="json"),
            created_at=now,
            updated_at=now,
            status_message="done",
            authorized_by=authorizer,
            dispatch_authority=_auto_research_authority("auto_research", "orchestrator"),
        ),
    )
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    root = store.agent_task(root.operation_id)
    assert root is not None
    worker = store.create_auto_research_agent_task(
        AgentTaskRecord(
            operation_id="worker",
            project_id="project",
            episode_id="auto_research",
            kind="auto_research",
            status="queued",
            request=AutoResearchRunRequest(
                episode_id="auto_research",
                role="worker",
                actor_operation_id="worker",
                run_truth_scope=_RUN_TRUTH_SCOPE,
                control_node_id="exp/check",
            ).model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message="done",
            parent_operation_id=root.operation_id,
            authorized_by=authorizer,
            dispatch_authority=_auto_research_authority("auto_research", "worker"),
        ),
        role="worker",
    )
    store.complete_agent_task(worker.operation_id, applied_revision=None, result={})
    messages = [
        AutoResearchMessageRecord(
            message_id="message-one",
            episode_id=auto_research.episode_id,
            sender_role="human",
            authorized_by=authorizer,
            recipient_task_id=root.operation_id,
            body="First result",
            created_at=store.now(),
        ),
        AutoResearchMessageRecord(
            message_id="message-two",
            episode_id=auto_research.episode_id,
            sender_role="worker",
            sender_task_id=worker.operation_id,
            recipient_task_id=root.operation_id,
            control_node_id="exp/check",
            body="Second result",
            created_at=store.now(),
        ),
    ]
    for message in messages:
        store.record_auto_research_message(message)
    store.mark_auto_research_messages_delivered(
        ["message-one", "message-two"],
        operation_id="mail-wake",
    )
    claimed = store.auto_research_messages(auto_research.episode_id)
    return auto_research_mail_delivery(
        episode_id=auto_research.episode_id,
        recipient_task_id=root.operation_id,
        delivery_operation_id="mail-wake",
        messages=claimed,
    )


def test_claimed_inbound_mail_is_staged_as_one_exact_hearsay_only_batch(tmp_path) -> None:
    delivery = _claimed_delivery(tmp_path)
    workspace = tmp_path / "stage"
    workspace.mkdir()
    for name in ("patch.json", "watch.json", "messages.json"):
        (workspace / name).write_text("stale", encoding="utf-8")
    mailbox = RunStageMailbox.for_stage(local_stage=workspace, remote_stage=None)

    clear_turn_handoff_files(mailbox)
    stage_auto_research_mail_delivery(mailbox, delivery)
    parsed = parse_auto_research_mail_delivery((workspace / "messages.json").read_text(encoding="utf-8"))

    assert parsed == delivery
    assert parsed.graph_authority == "none"
    assert parsed.epistemic_status == "hearsay"
    assert parsed.delivery_operation_id == "mail-wake"
    assert parsed.message_ids == ["message-one", "message-two"]
    assert [message.body for message in parsed.messages] == ["First result", "Second result"]
    assert parsed.messages[0].sender_role == "human"
    assert parsed.messages[0].authorized_by is not None
    assert parsed.messages[0].authorized_by.display_name == "AutoResearch owner"
    assert parsed.messages[1].sender_role == "worker"
    assert parsed.messages[1].authorized_by is None
    assert not (workspace / "patch.json").exists()
    assert not (workspace / "watch.json").exists()


def test_legacy_v1_human_mail_without_sender_snapshot_still_parses(tmp_path) -> None:
    delivery = _claimed_delivery(tmp_path)
    payload = delivery.model_dump(mode="json")
    human = next(message for message in payload["messages"] if message["sender_role"] == "human")
    human.pop("authorized_by")

    parsed = parse_auto_research_mail_delivery(json.dumps(payload))

    parsed_human = next(message for message in parsed.messages if message.sender_role == "human")
    assert parsed_human.authorized_by is None


def test_auto_research_mail_batch_validation_is_all_or_none(tmp_path) -> None:
    delivery = _claimed_delivery(tmp_path)
    payload = delivery.model_dump(mode="json")
    payload["messages"][1]["delivery_operation_id"] = "another-wake"
    with pytest.raises(ValidationError, match="crosses claimed wake operations"):
        parse_auto_research_mail_delivery(json.dumps(payload))

    payload = delivery.model_dump(mode="json")
    payload["messages"].append(payload["messages"][0])
    with pytest.raises(ValidationError, match="duplicate message"):
        AutoResearchMailDelivery.model_validate(payload)

    unclaimed = delivery.messages[0].model_dump(mode="json")
    unclaimed["delivered_at"] = None
    with pytest.raises(ValidationError):
        AutoResearchMailDelivery.model_validate(
            {
                **delivery.model_dump(mode="json"),
                "messages": [unclaimed],
            }
        )


def test_claim_prefix_stops_at_the_shared_count_and_exact_wire_boundary(
    monkeypatch,
) -> None:
    messages = [
        AutoResearchMessageRecord(
            message_id=f"message-{index}",
            episode_id="auto_research",
            sender_role="human",
            recipient_task_id="root",
            body=f"Result {index}",
            created_at=f"2026-08-12T00:00:0{index}+00:00",
        )
        for index in range(3)
    ]
    claimed = [
        message.model_copy(
            update={
                "delivered_at": "2026-08-12T00:01:00+00:00",
                "delivery_operation_id": "mail-wake",
            }
        )
        for message in messages[:2]
    ]
    two_message_delivery = auto_research_mail_delivery(
        episode_id="auto_research",
        recipient_task_id="root",
        delivery_operation_id="mail-wake",
        messages=claimed,
    )
    exact_boundary = len((two_message_delivery.model_dump_json() + "\n").encode("utf-8"))
    assert exact_boundary < AUTO_RESEARCH_MAIL_MAX_BYTES
    monkeypatch.setattr("rcp.runs.auto_research_mail.AUTO_RESEARCH_MAIL_MAX_MESSAGES", 2)
    monkeypatch.setattr(
        "rcp.runs.auto_research_mail.AUTO_RESEARCH_MAIL_MAX_BYTES",
        exact_boundary,
    )

    selected = auto_research_mail_claim_prefix(
        episode_id="auto_research",
        recipient_task_id="root",
        delivery_operation_id="mail-wake",
        delivered_at="2026-08-12T00:01:00+00:00",
        messages=messages,
    )

    assert [message.message_id for message in selected] == ["message-0", "message-1"]
    rendered = two_message_delivery.model_dump_json() + "\n"
    assert parse_auto_research_mail_delivery(rendered) == two_message_delivery
    with pytest.raises(ValueError, match="exceeds .* bytes"):
        parse_auto_research_mail_delivery(rendered + " ")
    with pytest.raises(ValidationError, match="exceeds 2 messages"):
        auto_research_mail_delivery(
            episode_id="auto_research",
            recipient_task_id="root",
            delivery_operation_id="mail-wake",
            messages=[
                message.model_copy(
                    update={
                        "delivered_at": "2026-08-12T00:01:00+00:00",
                        "delivery_operation_id": "mail-wake",
                    }
                )
                for message in messages
            ],
        )

    monkeypatch.setattr(
        "rcp.runs.auto_research_mail.AUTO_RESEARCH_MAIL_MAX_BYTES",
        exact_boundary - 1,
    )
    smaller = auto_research_mail_claim_prefix(
        episode_id="auto_research",
        recipient_task_id="root",
        delivery_operation_id="mail-wake",
        delivered_at="2026-08-12T00:01:00+00:00",
        messages=messages,
    )
    assert [message.message_id for message in smaller] == ["message-0"]
