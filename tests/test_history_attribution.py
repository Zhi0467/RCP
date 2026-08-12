from __future__ import annotations

import json
import uuid

import pytest

from rcp.config import load_manifest
from rcp.core.authority import (
    AgentDispatchAuthority,
    AgentDispatchScope,
    AgentTaskAuthority,
    require_apply,
)
from rcp.core.models import AuthorizedHuman, GraphState, Patch
from rcp.core.validation import validate_patch
from rcp.history import HistoryManager, build_revision_summaries
from tests.helpers import refresh_patch, seed_patch

from .helpers import fabricated_authorizer

PROJECT_ID = "project-one"


def _approval(summary: str = "Human approval") -> Patch:
    return Patch(kind="approval", author="human", summary=summary, ops=[])


def _review(node_id: str, standing: str, summary: str) -> Patch:
    return Patch(
        kind="approval",
        author="human",
        summary=summary,
        ops=[{"op": "set_standing", "node_id": node_id, "standing": standing}],
    )


def _agent_patch(operation_id: str | None = "operation-1") -> Patch:
    return seed_patch().model_copy(update={"source_operation_id": operation_id})


def _task_authority(
    operation_id: str,
    authorizer: AuthorizedHuman | None,
    *,
    patch_kind: str = "seed",
    project_id: str = PROJECT_ID,
    profile: str = "ordinary",
    task_contract: str = "scratch_patch",
    campaign_id: str | None = None,
) -> AgentTaskAuthority:
    return AgentTaskAuthority(
        operation_id=operation_id,
        project_id=project_id,
        authorized_by=authorizer,
        dispatch_authority=AgentDispatchAuthority(
            profile=profile,
            task_contract=task_contract,
            scope=AgentDispatchScope(
                run_truth_scope=["repo-a"],
                campaign_id=campaign_id,
                patch_kind=patch_kind,
            ),
        ),
        campaign_id=campaign_id,
    )


def test_opt_in_human_single_and_batch_use_explicit_snapshot(manifest) -> None:
    authorizer = fabricated_authorizer("Alice")
    history = HistoryManager(
        manifest,
        project_id=PROJECT_ID,
        require_attribution=True,
        agent_authority_resolver=lambda _project_id, operation_id: _task_authority(
            operation_id, authorizer
        ),
    )
    history.append(_agent_patch("seed-operation"))

    single, _ = history.append(
        _review("rq/learning-after-shift", "accepted", "Accepted the question").model_copy(
            update={"campaign_id": "agent-campaign"}
        ),
        authorized_by=authorizer,
    )
    batch, result = history.append_batch(
        [
            _review("rq/learning-after-shift", "contested", "Contested the question"),
            _review(
                "hyp/replanning-restores-plasticity",
                "accepted",
                "Accepted the hypothesis",
            ),
        ],
        authorized_by=authorizer,
    )

    assert single.authorized_by == authorizer
    assert single.profile is None
    assert single.task_id is None
    assert single.campaign_id is None
    assert [patch.authorized_by for patch in batch] == [authorizer, authorizer]
    assert result.state.revision == 4
    assert [patch.authorized_by for patch in history.load_patches()[1:]] == [
        authorizer,
        authorizer,
        authorizer,
    ]


def test_opt_in_human_from_state_stamps_the_whole_transaction(manifest) -> None:
    authorizer = fabricated_authorizer("Alice")
    history = HistoryManager(
        manifest,
        project_id=PROJECT_ID,
        require_attribution=True,
        agent_authority_resolver=lambda _project_id, operation_id: _task_authority(
            operation_id, authorizer
        ),
    )
    history.append(_agent_patch("seed-operation"))

    prepared, result = history.append_batch_from_state(
        lambda state: [
            _review(
                "rq/learning-after-shift",
                "accepted",
                f"Approval from revision {state.revision}",
            )
        ],
        authorized_by=authorizer,
    )

    assert prepared[0].authorized_by == authorizer
    assert result.state.revision == 2


def test_opt_in_human_rejects_missing_explicit_snapshot_without_revision(manifest) -> None:
    authorizer = fabricated_authorizer("Alice")
    history = HistoryManager(manifest, require_attribution=True)
    preattributed = _approval().model_copy(update={"authorized_by": authorizer})

    with pytest.raises(ValueError, match="explicit authorized_by"):
        history.append(preattributed)
    with pytest.raises(ValueError, match="explicit authorized_by"):
        history.append_batch([_approval(), _approval()])

    assert history.load_patches() == []
    assert history.state().revision == 0


def test_agent_candidate_and_append_use_resolved_direct_task_snapshot(manifest) -> None:
    authorizer = fabricated_authorizer("Alice")
    operation_id = "operation-1"
    history = HistoryManager(
        manifest,
        project_id=PROJECT_ID,
        require_attribution=True,
        agent_authority_resolver=lambda _project_id, requested: (
            _task_authority(requested, authorizer)
            if requested == operation_id
            else _task_authority(requested, None)
        ),
    )
    raw = _agent_patch(operation_id)

    candidate, report, state = history.validate_candidate(raw)

    assert report.rejected is False
    assert state.revision == 0
    assert candidate.authorized_by == authorizer
    assert candidate.profile == "ordinary"
    assert candidate.task_id == operation_id
    assert candidate.campaign_id is None
    assert history.load_patches() == []

    appended, result = history.append(raw)
    assert appended.authorized_by == authorizer
    assert appended.profile == "ordinary"
    assert appended.task_id == appended.source_operation_id == operation_id
    assert appended.campaign_id is None
    assert result.state.revision == 1


def test_campaign_worker_keeps_ordinary_profile_and_campaign_id(manifest) -> None:
    authorizer = fabricated_authorizer("Alice")
    operation_id = "worker-operation"
    campaign_id = "campaign-one"
    authority = _task_authority(
        operation_id,
        authorizer,
        patch_kind="work",
        profile="ordinary",
        task_contract="work_auto",
        campaign_id=campaign_id,
    )
    history = HistoryManager(
        manifest,
        project_id=PROJECT_ID,
        require_attribution=True,
        agent_authority_resolver=lambda _project_id, _operation_id: authority,
    )
    raw = Patch(
        kind="work",
        author="agent",
        summary="Campaign worker result",
        ops=[],
        run_truth_scope=["repo-a"],
        source_operation_id=operation_id,
    )

    appended, result = history.append(raw)

    assert appended.profile == "ordinary"
    assert appended.campaign_id == campaign_id
    assert history.load_patches()[0].campaign_id == campaign_id
    summary = build_revision_summaries(history.load_patches(), result)[0]
    assert summary.profile == "ordinary"
    assert summary.campaign_id == campaign_id


def test_campaign_orchestrator_patch_keeps_campaign_id(manifest) -> None:
    authorizer = fabricated_authorizer("Alice")
    operation_id = "orchestrator-operation"
    campaign_id = "campaign-one"
    authority = _task_authority(
        operation_id,
        authorizer,
        patch_kind="work",
        profile="orchestrator",
        task_contract="orchestrate",
        campaign_id=campaign_id,
    )
    history = HistoryManager(
        manifest,
        project_id=PROJECT_ID,
        require_attribution=True,
        agent_authority_resolver=lambda _project_id, _operation_id: authority,
    )

    appended, _ = history.append(
        Patch(
            kind="work",
            author="agent",
            summary="Orchestrator result",
            ops=[],
            run_truth_scope=["repo-a"],
            source_operation_id=operation_id,
        )
    )

    assert appended.profile == "orchestrator"
    assert appended.campaign_id == campaign_id


def test_orchestrator_without_canonical_campaign_id_is_refused(manifest) -> None:
    authorizer = fabricated_authorizer("Alice")
    operation_id = "orchestrator-operation"
    authority = AgentTaskAuthority(
        operation_id=operation_id,
        project_id=PROJECT_ID,
        authorized_by=authorizer,
        dispatch_authority=AgentDispatchAuthority(
            profile="orchestrator",
            task_contract="orchestrate",
            scope=AgentDispatchScope(
                run_truth_scope=["repo-a"],
                campaign_id="campaign-one",
                patch_kind="work",
            ),
        ),
        campaign_id=None,
    )
    history = HistoryManager(
        manifest,
        project_id=PROJECT_ID,
        require_attribution=True,
        agent_authority_resolver=lambda _project_id, _operation_id: authority,
    )
    raw = Patch(
        kind="work",
        author="agent",
        summary="Orchestrator result",
        ops=[],
        run_truth_scope=["repo-a"],
        source_operation_id=operation_id,
    )

    with pytest.raises(ValueError, match="orchestrator.*campaign_id"):
        history.append(raw)

    assert history.load_patches() == []


def test_supplied_campaign_id_must_match_canonical_task(manifest) -> None:
    authorizer = fabricated_authorizer("Alice")
    operation_id = "worker-operation"
    authority = _task_authority(
        operation_id,
        authorizer,
        patch_kind="work",
        task_contract="work_auto",
        campaign_id="campaign-one",
    )
    history = HistoryManager(
        manifest,
        project_id=PROJECT_ID,
        require_attribution=True,
        agent_authority_resolver=lambda _project_id, _operation_id: authority,
    )
    raw = Patch(
        kind="work",
        author="agent",
        summary="Campaign worker result",
        ops=[],
        run_truth_scope=["repo-a"],
        source_operation_id=operation_id,
        campaign_id="campaign-other",
    )

    with pytest.raises(ValueError, match="does not match the canonical"):
        history.append(raw)

    assert history.load_patches() == []


def test_rogue_agent_attribution_cannot_replace_resolved_snapshot(manifest) -> None:
    canonical = fabricated_authorizer("Canonical human")
    rogue = fabricated_authorizer("Rogue human")
    history = HistoryManager(
        manifest,
        project_id=PROJECT_ID,
        require_attribution=True,
        agent_authority_resolver=lambda _project_id, operation_id: _task_authority(
            operation_id, canonical
        ),
    )
    patch = _agent_patch().model_copy(
        update={
            "authorized_by": rogue,
            "profile": "ordinary",
            "task_id": "operation-1",
        }
    )

    with pytest.raises(ValueError, match="does not match the canonical"):
        history.append(patch)

    assert history.load_patches() == []
    assert history.state().revision == 0


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing-source", "source_operation_id"),
        ("missing-resolver", "agent_authority_resolver"),
        ("unknown-task", "unknown agent task"),
        ("legacy-task", "has no authorizer snapshot"),
        ("unnamed-authorizer", "valid authorizer snapshot"),
    ],
)
def test_agent_attribution_failures_do_not_write_or_spend_revision(
    manifest,
    case: str,
    message: str,
) -> None:
    operation_id = None if case == "missing-source" else "operation-1"
    if case == "missing-resolver":
        resolver = None
    elif case == "unknown-task":

        def resolver(_project_id: str, operation_id: str) -> AgentTaskAuthority:
            raise KeyError(operation_id)

    elif case == "legacy-task":

        def resolver(_project_id: str, operation_id: str) -> AgentTaskAuthority:
            return _task_authority(operation_id, None)

    elif case == "unnamed-authorizer":

        def resolver(_project_id: str, operation_id: str) -> AgentTaskAuthority:
            return _task_authority(
                operation_id,
                AuthorizedHuman.model_construct(
                    space_id=str(uuid.uuid4()),
                    user_id=str(uuid.uuid4()),
                    display_name=" ",
                ),
            )

    else:

        def resolver(_project_id: str, operation_id: str) -> AgentTaskAuthority:
            return _task_authority(operation_id, fabricated_authorizer("Alice"))

    history = HistoryManager(
        manifest,
        project_id=PROJECT_ID,
        require_attribution=True,
        agent_authority_resolver=resolver,
    )

    with pytest.raises(ValueError, match=message):
        history.append(_agent_patch(operation_id))

    assert history.load_patches() == []
    assert history.state().revision == 0


def test_identity_claim_stays_system_owned_under_attribution_policy(manifest) -> None:
    space_id = str(uuid.uuid4())
    history = HistoryManager(
        manifest,
        expected_space_id=space_id,
        require_attribution=True,
    )

    identity = history.claim_project_identity("created")
    stored = history.load_patches()[0]

    assert identity.home_space_id == space_id
    assert stored.kind == "identity"
    assert stored.authorized_by is None
    assert stored.profile is None
    assert stored.task_id is None
    assert stored.campaign_id is None


def test_default_manager_remains_legacy_compatible(manifest) -> None:
    history = HistoryManager(manifest)

    appended, result = history.append(seed_patch())

    assert appended.authorized_by is None
    assert appended.campaign_id is None
    assert result.state.revision == 1


def test_attribution_policy_does_not_apply_during_legacy_replay(manifest) -> None:
    HistoryManager(manifest).append(seed_patch())
    guarded = HistoryManager(
        load_manifest(manifest.path),
        require_attribution=True,
    )

    state = guarded.state()

    assert state.revision == 1
    assert guarded.load_patches()[0].authorized_by is None
    assert guarded.load_patches()[0].campaign_id is None


def test_campaign_id_is_inert_to_validation_and_apply_permission() -> None:
    authorizer = fabricated_authorizer("Alice")
    operation_id = "operation-1"
    authority = _task_authority(operation_id, authorizer)
    base = _agent_patch(operation_id).model_copy(update={"revision": 1})
    verdicts: list[str] = []

    for campaign_id in (None, "campaign-one", "garbage-id"):
        patch = base.model_copy(update={"campaign_id": campaign_id})
        report = validate_patch(
            GraphState(),
            patch,
            ["repo-a"],
            repository_aliases=["repo-a"],
            default_run_truth_scope=["repo-a"],
        )
        dispatch = require_apply(authority, patch)
        verdicts.append(
            json.dumps(
                {
                    "validation": [message.model_dump(mode="json") for message in report.messages],
                    "permission": dispatch.model_dump(mode="json"),
                },
                sort_keys=True,
            )
        )

    assert len(set(verdicts)) == 1


def test_authorizer_rename_does_not_change_existing_agent_snapshot(manifest) -> None:
    current = fabricated_authorizer("Before rename")

    def resolve(_project_id: str, operation_id: str) -> AgentTaskAuthority:
        return _task_authority(
            operation_id,
            current,
            patch_kind="refresh" if operation_id == "operation-2" else "seed",
        )

    history = HistoryManager(
        manifest,
        project_id=PROJECT_ID,
        require_attribution=True,
        agent_authority_resolver=resolve,
    )
    history.append(_agent_patch("operation-1"))
    current = current.model_copy(update={"display_name": "After rename"})
    history.append(
        refresh_patch("rq/after-rename").model_copy(update={"source_operation_id": "operation-2"})
    )

    stored = history.load_patches()
    assert stored[0].authorized_by is not None
    assert stored[0].authorized_by.display_name == "Before rename"
    assert stored[1].authorized_by is not None
    assert stored[1].authorized_by.display_name == "After rename"
