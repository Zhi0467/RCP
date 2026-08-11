from __future__ import annotations

import uuid

import pytest

from rcp.config import load_manifest
from rcp.core.models import AuthorizedHuman, Patch
from rcp.history import HistoryManager
from tests.helpers import refresh_patch, seed_patch


def _authorizer(display_name: str = "Alice") -> AuthorizedHuman:
    return AuthorizedHuman(
        space_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        display_name=display_name,
    )


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


def test_opt_in_human_single_and_batch_use_explicit_snapshot(manifest) -> None:
    authorizer = _authorizer()
    history = HistoryManager(
        manifest,
        require_attribution=True,
        agent_authorizer_resolver=lambda _operation_id: authorizer,
    )
    history.append(_agent_patch("seed-operation"))

    single, _ = history.append(
        _review("rq/learning-after-shift", "accepted", "Accepted the question"),
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
    assert [patch.authorized_by for patch in batch] == [authorizer, authorizer]
    assert result.state.revision == 4
    assert [patch.authorized_by for patch in history.load_patches()[1:]] == [
        authorizer,
        authorizer,
        authorizer,
    ]


def test_opt_in_human_from_state_stamps_the_whole_transaction(manifest) -> None:
    authorizer = _authorizer()
    history = HistoryManager(
        manifest,
        require_attribution=True,
        agent_authorizer_resolver=lambda _operation_id: authorizer,
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
    authorizer = _authorizer()
    history = HistoryManager(manifest, require_attribution=True)
    preattributed = _approval().model_copy(update={"authorized_by": authorizer})

    with pytest.raises(ValueError, match="explicit authorized_by"):
        history.append(preattributed)
    with pytest.raises(ValueError, match="explicit authorized_by"):
        history.append_batch([_approval(), _approval()])

    assert history.load_patches() == []
    assert history.state().revision == 0


def test_agent_candidate_and_append_use_resolved_direct_task_snapshot(manifest) -> None:
    authorizer = _authorizer()
    operation_id = "operation-1"
    history = HistoryManager(
        manifest,
        require_attribution=True,
        agent_authorizer_resolver=lambda requested: (
            authorizer if requested == operation_id else None
        ),
    )
    raw = _agent_patch(operation_id)

    candidate, report, state = history.validate_candidate(raw)

    assert report.rejected is False
    assert state.revision == 0
    assert candidate.authorized_by == authorizer
    assert candidate.profile == "ordinary"
    assert candidate.task_id == operation_id
    assert history.load_patches() == []

    appended, result = history.append(raw)
    assert appended.authorized_by == authorizer
    assert appended.profile == "ordinary"
    assert appended.task_id == appended.source_operation_id == operation_id
    assert result.state.revision == 1


def test_rogue_agent_attribution_cannot_replace_resolved_snapshot(manifest) -> None:
    canonical = _authorizer("Canonical human")
    rogue = _authorizer("Rogue human")
    history = HistoryManager(
        manifest,
        require_attribution=True,
        agent_authorizer_resolver=lambda _operation_id: canonical,
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
        ("missing-resolver", "agent_authorizer_resolver"),
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

        def resolver(_operation_id: str) -> AuthorizedHuman | None:
            raise KeyError(_operation_id)

    elif case == "legacy-task":

        def resolver(_operation_id: str) -> AuthorizedHuman | None:
            return None

    elif case == "unnamed-authorizer":

        def resolver(_operation_id: str) -> AuthorizedHuman | None:
            return AuthorizedHuman.model_construct(
                space_id=str(uuid.uuid4()),
                user_id=str(uuid.uuid4()),
                display_name=" ",
            )

    else:

        def resolver(_operation_id: str) -> AuthorizedHuman | None:
            return _authorizer()

    history = HistoryManager(
        manifest,
        require_attribution=True,
        agent_authorizer_resolver=resolver,
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


def test_default_manager_remains_legacy_compatible(manifest) -> None:
    history = HistoryManager(manifest)

    appended, result = history.append(seed_patch())

    assert appended.authorized_by is None
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


def test_authorizer_rename_does_not_change_existing_agent_snapshot(manifest) -> None:
    current = _authorizer("Before rename")

    def resolve(_operation_id: str) -> AuthorizedHuman:
        return current

    history = HistoryManager(
        manifest,
        require_attribution=True,
        agent_authorizer_resolver=resolve,
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
