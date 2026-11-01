from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from rcp.core.models import Patch
from rcp.core.transition_models import GraphTargetRef
from rcp.runs.branch_merge_admission import settle_agentless_merge
from rcp.runs.merge_effects import reconcile_merge_effects
from rcp.storage import AppStore

from .test_branch_merge_api import _branch_patch, _create_branch_harness
from .test_branch_merge_proposals import _proposal


def _flag(harness, *, target=None, kind="overclaim", state="open", finding="Review this result."):
    flag_id = uuid.uuid4().hex
    with harness.store.connection() as connection:
        connection.execute(
            "INSERT INTO evidence_flags(flag_id,project_id,run_id,occurrence_date,target,"
            "evidence_id,evidence_revision,kind,finding,source,state,chat_id,operation_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                flag_id,
                harness.project_id,
                "source-run",
                "2026-01-01",
                target or harness.branch.branch_id,
                "ev/branch-result",
                harness.branch.head_ref().revision,
                kind,
                finding,
                "repo-a/results.txt:1",
                state,
                "source-chat",
                "source-address",
                harness.store.now(),
            ),
        )
    return harness.store.evidence_flag(harness.project_id, flag_id)


def _merge(harness, *, request_id=None):
    return harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge",
        json={"keep_branch_open": True, **({"request_id": request_id} if request_id else {})},
    )


def test_agentless_merge_publishes_warm_inbox_with_live_proposal(manifest, tmp_path, monkeypatch):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    harness.branch.append(
        Patch(
            kind="work",
            author="agent",
            summary="Record a hypothesis.",
            run_truth_scope=["repo-a"],
            source_operation_id=harness.root.operation_id,
            ops=[
                {
                    "op": "create_nodes",
                    "nodes": [
                        {
                            "id": "hyp/branch",
                            "type": "hypothesis",
                            "title": "Branch hypothesis",
                            "statement": "Original claim.",
                        }
                    ],
                }
            ],
        )
    )
    harness.branch.append(
        Patch(
            kind="work",
            author="agent",
            summary="Ask for review.",
            run_truth_scope=["repo-a"],
            source_operation_id=harness.root.operation_id,
            ops=[
                {
                    "op": "create_proposals",
                    "proposals": [
                        _proposal(
                            {
                                "op": "update_nodes",
                                "intent": "content_change",
                                "nodes": [
                                    {"id": "hyp/branch", "changes": {"statement": "Revised claim."}}
                                ],
                            }
                        )
                    ],
                }
            ],
        )
    )
    route = f"/api/projects/{harness.project_id}"
    before = harness.client.get(route).json()
    monkeypatch.setattr(
        harness.app.state.launcher, "stream", lambda *_a, **_kw: pytest.fail("provider launched")
    )
    response = _merge(harness)
    assert response.status_code == 202, response.text
    after = harness.client.get(route).json()
    canonical = harness.service.project_snapshot()
    assert after["revision"] > before["revision"]
    for field in ("revision", "attention", "counts"):
        assert after[field] == canonical[field]
    assert after["graph"]["proposals"]["prop/review"]["status"] == "pending"
    assert harness.store.project(harness.project_id).revision == canonical["revision"]


def test_warning_carry_recovers_after_success_and_keeps_dismissal(manifest, tmp_path, monkeypatch):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    original = _flag(harness, state="addressing")
    carry = harness.store.carry_merge_evidence_flags
    monkeypatch.setattr(
        harness.store,
        "carry_merge_evidence_flags",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("carry unavailable")),
    )
    response = _merge(harness)
    assert response.status_code == 202, response.text
    task_id = response.json()["accepted_merge_task"]["operation_id"]
    assert harness.store.agent_task(task_id).status == "succeeded"
    assert [task.operation_id for task in harness.store.pending_merge_effect_tasks()] == [task_id]
    head = harness.service.history.head_ref()
    monkeypatch.setattr(harness.store, "carry_merge_evidence_flags", carry)
    # The internal markers remain readable after inspector/count overflow.
    for index in range(450):
        harness.store.record_agent_task_receipt(task_id, "noise", {"index": index})
    path = harness.store.path
    reopened = AppStore(path)
    harness.app.state.background_tasks.store = reopened
    settle_agentless_merge(harness.app.state.background_tasks, harness.service, task_id)
    [carried] = [
        flag
        for flag in reopened.list_evidence_flags(harness.project_id)
        if flag["target"] == "main"
    ]
    assert carried["origin_flag_id"] == original["flag_id"]
    assert carried["finding"] == original["finding"]
    assert carried["source"] == original["source"]
    assert carried["state"] == "open" and carried["chat_id"] is None
    reopened.dismiss_evidence_flag(
        harness.project_id, carried["flag_id"], user_id=harness.episode.authorized_by.user_id
    )
    settle_agentless_merge(harness.app.state.background_tasks, harness.service, task_id)
    assert reopened.evidence_flag(harness.project_id, carried["flag_id"])["state"] == "dismissed"
    assert reopened.evidence_flag(harness.project_id, original["flag_id"]) == original
    assert not reopened.pending_merge_effect_tasks(), [
        receipt.payload
        for receipt in reopened.agent_task_receipts_by_category(task_id, "merge_effects_failed")
    ]
    assert harness.service.history.head_ref() == head
    reopened.close()


def test_episode_request_replays_original_acceptance_after_later_delivery(manifest, tmp_path):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    request_id = str(uuid.uuid4())
    first = _merge(harness, request_id=request_id)
    assert first.status_code == 202, first.text
    task_a = first.json()["accepted_merge_task"]["operation_id"]
    harness.branch.append(_branch_patch(harness.root.operation_id, "evidence", suffix="-later"))
    second = _merge(harness, request_id=str(uuid.uuid4()))
    assert second.status_code == 202, second.text
    task_b = second.json()["accepted_merge_task"]["operation_id"]
    assert task_a != task_b
    head = harness.service.history.head_ref()
    replay = _merge(harness, request_id=request_id)
    assert replay.status_code == 202, replay.text
    assert replay.json()["accepted_merge_task"]["operation_id"] == task_a
    assert replay.json()["isolation_state"]["merge_attempt"]["attempt_id"] == task_b
    assert harness.service.history.head_ref() == head
    route = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge"
    changed = harness.client.post(route, json={"request_id": request_id})
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "episode_merge_request_conflict"


@pytest.mark.parametrize(
    "variant",
    [
        "matching",
        "stamps",
        "source_changed",
        "destination_changed",
        "missing",
        "retired",
        "dismissed",
    ],
)
@pytest.mark.parametrize("destination_kind", ["main", "branch"])
def test_warning_carry_compares_recorded_semantic_evidence(
    manifest, tmp_path, variant, destination_kind
):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    original = _flag(harness, state="dismissed" if variant == "dismissed" else "open")
    recorded = harness.branch.state()
    source = recorded.model_copy(deep=True)
    destination = recorded.model_copy(deep=True)
    node = destination.nodes["ev/branch-result"]
    if variant == "stamps":
        destination.nodes[node.id] = node.model_copy(update={"created_rev": 99, "updated_rev": 101})
    elif variant == "source_changed":
        source.nodes[node.id] = node.model_copy(update={"observation": "A changed result."})
    elif variant == "destination_changed":
        destination.nodes[node.id] = node.model_copy(update={"interpretation": "A changed claim."})
    elif variant == "missing":
        del destination.nodes[node.id]
    elif variant == "retired":
        destination.nodes[node.id] = node.model_copy(update={"validity": "superseded"})
    target = (
        GraphTargetRef()
        if destination_kind == "main"
        else GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4()))
    )
    arguments = dict(
        source=harness.branch.graph_target,
        destination=target,
        source_state=source,
        destination_state=destination,
        recorded_state=lambda _revision: recorded,
    )
    created = harness.store.carry_merge_evidence_flags(harness.project_id, **arguments)
    assert len(created) == int(variant in {"matching", "stamps"})
    if created:
        [carried] = [
            harness.store.evidence_flag(harness.project_id, identity) for identity in created
        ]
        assert carried["target"] == (target.branch_id if target.kind == "branch" else "main")
        assert carried["run_id"] == original["run_id"]
        assert carried["occurrence_date"] == original["occurrence_date"]
        harness.store.dismiss_evidence_flag(
            harness.project_id, carried["flag_id"], user_id=harness.episode.authorized_by.user_id
        )
        assert harness.store.carry_merge_evidence_flags(harness.project_id, **arguments) == []
    assert harness.store.evidence_flag(harness.project_id, original["flag_id"]) == original


def test_deduplicated_warning_carry_does_not_reopen_dismissed_destination(manifest, tmp_path):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    _flag(harness)
    existing = _flag(harness, target="main", finding="An existing destination warning.")
    state = harness.branch.state()
    arguments = dict(
        source=harness.branch.graph_target,
        destination=GraphTargetRef(),
        source_state=state,
        destination_state=state,
        recorded_state=lambda _revision: state,
    )
    assert harness.store.carry_merge_evidence_flags(harness.project_id, **arguments) == []
    harness.store.dismiss_evidence_flag(
        harness.project_id, existing["flag_id"], user_id=harness.episode.authorized_by.user_id
    )
    assert harness.store.carry_merge_evidence_flags(harness.project_id, **arguments) == []
    assert [flag["target"] for flag in harness.store.list_evidence_flags(harness.project_id)] == [
        harness.branch.branch_id
    ]


def test_no_change_delivery_recovers_publication_without_second_merge(
    manifest, tmp_path, monkeypatch
):
    harness = _create_branch_harness(manifest, tmp_path, change="bookkeeping")
    tasks = harness.app.state.background_tasks
    publisher = tasks.on_stream_closed
    monkeypatch.setattr(tasks, "on_stream_closed", lambda *_args: False)
    response = _merge(harness)
    assert response.status_code == 202, response.text
    task = response.json()["accepted_merge_task"]
    [receipt] = harness.branch.merge_receipts()
    assert receipt.outcome == "no_change"
    assert harness.store.pending_merge_effect_tasks()
    head = harness.service.history.head_ref()
    monkeypatch.setattr(tasks, "on_stream_closed", publisher)
    assert reconcile_merge_effects(tasks, harness.service) == 1
    assert not harness.store.pending_merge_effect_tasks()
    assert harness.service.history.head_ref() == head
    assert harness.branch.merge_receipts() == [receipt]
    assert harness.store.agent_task(task["operation_id"]).status == "succeeded"


def test_legacy_effects_select_latest_delivery_and_only_contemporaneous_warnings(
    manifest, tmp_path
):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    first = _merge(harness)
    assert first.status_code == 202, first.text
    harness.branch.append(_branch_patch(harness.root.operation_id, "evidence", suffix="-later"))
    second = _merge(harness)
    assert second.status_code == 202, second.text
    earlier = _flag(harness)
    too_late = _flag(harness, kind="underclaim")
    latest_receipt = max(harness.branch.merge_receipts(), key=lambda item: item.created_at)
    with harness.store.connection() as connection:
        connection.execute(
            "UPDATE evidence_flags SET created_at=? WHERE flag_id=?",
            (first.json()["created_at"], earlier["flag_id"]),
        )
        connection.execute(
            "UPDATE evidence_flags SET created_at=? WHERE flag_id=?",
            ("2999-01-01T00:00:00+00:00", too_late["flag_id"]),
        )
        connection.execute(
            "DELETE FROM graph_run_receipts WHERE category IN ('merge_effects_required','merge_effects_complete','merge_warning_carry_complete','merge_display_publication_complete')"
        )
    assert reconcile_merge_effects(harness.app.state.background_tasks, harness.service) == 1
    [carried] = [
        flag
        for flag in harness.store.list_evidence_flags(harness.project_id)
        if flag["target"] == "main"
    ]
    assert carried["origin_flag_id"] == earlier["flag_id"]
    assert not harness.store.legacy_merge_effect_tasks(harness.project_id)
    assert reconcile_merge_effects(harness.app.state.background_tasks, harness.service) == 0
    assert (
        latest_receipt.provenance.merge_task_id
        == second.json()["accepted_merge_task"]["operation_id"]
    )


def test_episode_request_receipt_conflicts_by_member_and_preserves_historical_attempt(
    manifest, tmp_path
):
    from rcp.runs.episodes.merge import MergeEpisodeBody
    from rcp.storage.conversation_worktrees import EpisodeMergeRequestConflict

    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    body = MergeEpisodeBody(request_id=uuid.uuid4(), keep_branch_open=True)
    response = _merge(harness, request_id=str(body.request_id))
    assert response.status_code == 202, response.text
    kwargs = dict(
        acting_member=harness.episode.authorized_by.user_id,
        choices=body.model_dump(mode="json", exclude={"request_id"}),
    )
    receipt = harness.store.episode_merge_request(
        harness.project_id, harness.episode.episode_id, str(body.request_id), **kwargs
    )
    assert receipt["accepted_task_id"] == response.json()["accepted_merge_task"]["operation_id"]
    assert receipt["accepted_graph_delivery_json"] is not None
    assert '"phase":"done"' in receipt["attempt_json"]
    with pytest.raises(EpisodeMergeRequestConflict):
        harness.store.episode_merge_request(
            harness.project_id,
            harness.episode.episode_id,
            str(body.request_id),
            **{**kwargs, "acting_member": "another-member"},
        )


def test_carry_crash_before_receipt_does_not_reopen_human_dismissal(
    manifest, tmp_path, monkeypatch
):
    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    _flag(harness)
    record = harness.store.record_agent_task_receipt

    def interrupted(operation_id, category, *args, **kwargs):
        if category == "merge_warning_carry_complete":
            raise OSError("interrupted after carry")
        return record(operation_id, category, *args, **kwargs)

    monkeypatch.setattr(harness.store, "record_agent_task_receipt", interrupted)
    response = _merge(harness)
    assert response.status_code == 202, response.text
    [carried] = [
        flag
        for flag in harness.store.list_evidence_flags(harness.project_id)
        if flag["target"] == "main"
    ]
    harness.store.dismiss_evidence_flag(
        harness.project_id, carried["flag_id"], user_id=harness.episode.authorized_by.user_id
    )
    monkeypatch.setattr(harness.store, "record_agent_task_receipt", record)
    assert reconcile_merge_effects(harness.app.state.background_tasks, harness.service) == 1
    assert (
        harness.store.evidence_flag(harness.project_id, carried["flag_id"])["state"] == "dismissed"
    )
    assert len(harness.branch.merge_receipts()) == 1


@pytest.mark.parametrize("landing", ["prepared", "agent", "agent_target_moves"])
def test_keyed_code_delivery_receipt_survives_a_later_graph_attempt(
    manifest, tmp_path, monkeypatch, landing
):
    from .helpers import wait_for_task
    from .test_conversation_worktree_git import git
    from .test_episode_merge import _conflicting, _isolated, _merge_turn

    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    verified_landing = []
    if landing != "prepared":
        shared, binding = _conflicting(harness, tmp_path, graph=True)
        monkeypatch.setattr(harness.app.state.launcher, "stream", _merge_turn(harness, shared, []))
    else:
        shared, binding = _isolated(harness, tmp_path)
        (Path(binding.worktree_path) / "file").write_text("delivered code")
    if landing == "agent_target_moves":
        from rcp.runs.episodes import merge

        original_git = merge._git

        def move_target_after_verification(store, binding, operation, **values):
            result = original_git(store, binding, operation, **values)
            if operation == "verify_landing" and result["verified"] and not verified_landing:
                verified_landing.append(result["target_commit"])
                (shared / "later.txt").write_text("Later target work.\n")
                git(shared, "add", "later.txt")
                git(shared, "commit", "-m", "Later target work")
            return result

        monkeypatch.setattr(merge, "_git", move_target_after_verification)
    key = str(uuid.uuid4())
    first = _merge(harness, request_id=key)
    assert first.status_code == 202, first.text
    accepted = first.json()["accepted_merge_task"]["operation_id"]
    wait_for_task(harness.store, accepted, expect="succeeded")
    with harness.store.connection() as connection:
        first_receipt = dict(
            connection.execute(
                "SELECT * FROM episode_merge_request_receipts WHERE request_id=?", (key,)
            ).fetchone()
        )
    landed_commit = verified_landing[0] if verified_landing else git(shared, "rev-parse", "main")
    if verified_landing:
        assert landed_commit != git(shared, "rev-parse", "main")
    code = json.loads(first_receipt["accepted_code_delivery_json"])
    assert code["landed_commit"] == landed_commit
    assert code["tree"] == git(shared, "rev-parse", f"{landed_commit}^{{tree}}")
    attempt = harness.store.episode_isolation_state(
        harness.project_id, harness.episode.episode_id
    ).merge_attempt
    assert code == {name: getattr(attempt, name) for name in code}
    harness.branch.append(_branch_patch(harness.root.operation_id, "evidence", suffix="-later"))
    second = _merge(harness, request_id=str(uuid.uuid4()))
    assert second.status_code == 202, second.text
    replay = _merge(harness, request_id=key)
    assert replay.status_code == 202, replay.text
    assert replay.json()["accepted_merge_task"]["operation_id"] == accepted
    with harness.store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM episode_merge_request_receipts WHERE request_id=?", (key,)
        ).fetchone()
    code = json.loads(row["accepted_code_delivery_json"])
    assert row["accepted_code_delivery_json"] == first_receipt["accepted_code_delivery_json"]
    assert code["landed_commit"] == landed_commit
    assert (
        json.loads(row["accepted_graph_delivery_json"])["provenance"]["merge_task_id"] == accepted
    )


@pytest.mark.parametrize("reconciliation_owner", ["route", "coordinator"])
def test_fresh_request_that_settles_an_older_merge_is_not_accepted_as_that_merge(
    manifest, tmp_path, monkeypatch, reconciliation_owner
):
    import rcp.runs.episodes.merge as coordinator
    from rcp.api import episode_routes

    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    request_a, request_b = str(uuid.uuid4()), str(uuid.uuid4())

    def interrupted_cleanup(*_args, **_kwargs):
        raise ValueError("injected_cleanup_failure")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(coordinator, "_cleanup", interrupted_cleanup)
        first = _merge(harness, request_id=request_a)
    assert first.status_code == 409, first.text
    state = harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
    assert state.merge_reservation is not None
    task_a = state.merge_attempt.attempt_id
    before = harness.store.agent_tasks(harness.project_id)
    head = harness.service.history.head_ref()
    with monkeypatch.context() as admission:
        if reconciliation_owner == "coordinator":
            # The older attempt appeared after the route's reconciliation read.
            admission.setattr(episode_routes, "reconcile_episode_merge", lambda *_args: None)
        refused = _merge(harness, request_id=request_b)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "episode_merge_previous_attempt_settled"
    assert harness.store.agent_tasks(harness.project_id) == before
    assert harness.service.history.head_ref() == head
    with harness.store.connection() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM episode_merge_request_receipts WHERE request_id=?", (request_b,)
            ).fetchone()
            is None
        )
    replay = _merge(harness, request_id=request_a)
    assert replay.status_code == 202, replay.text
    assert replay.json()["accepted_merge_task"]["operation_id"] == task_a
    harness.branch.append(_branch_patch(harness.root.operation_id, "evidence", suffix="-later"))
    accepted = _merge(harness, request_id=request_b)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["accepted_merge_task"]["operation_id"] != task_a


def test_failed_merge_without_canonical_delivery_finishes_its_effect_obligation(
    manifest, tmp_path, monkeypatch
):
    from rcp.runs.merge_effects import settle_merge_effects

    from .test_owned_merge_storage import _owned_merge

    harness, task = _owned_merge(manifest, tmp_path, requires_agent=False)
    harness.store.create_branch_merge_task(task)
    harness.store.fail_agent_task(task.operation_id, "Failed before graph delivery.")
    assert harness.store.pending_merge_effect_tasks() == [
        harness.store.agent_task(task.operation_id)
    ]
    monkeypatch.setattr(
        harness.app.state.background_tasks,
        "on_stream_closed",
        lambda *_args: pytest.fail("non-delivery was published"),
    )
    with monkeypatch.context() as unavailable:
        unavailable.setattr(
            harness.service.history.workspace,
            "transaction",
            lambda: (_ for _ in ()).throw(OSError("canonical state cannot be confirmed")),
        )
        assert not settle_merge_effects(
            harness.app.state.background_tasks, harness.service, task.operation_id
        )
        assert harness.store.pending_merge_effect_tasks()
    assert settle_merge_effects(
        harness.app.state.background_tasks, harness.service, task.operation_id
    )
    assert not harness.store.pending_merge_effect_tasks()
    [receipt] = harness.store.agent_task_receipts_by_category(
        task.operation_id, "merge_effects_complete"
    )
    assert receipt.payload["outcome"] == "not_applicable"
    assert harness.store.agent_task(task.operation_id).status == "failed"


@pytest.mark.parametrize("legacy", [False, True])
def test_successful_code_only_task_on_delivered_branch_has_finite_effect_recovery(
    manifest, tmp_path, monkeypatch, legacy
):
    from .helpers import wait_for_task
    from .test_conversation_worktree_git import git
    from .test_episode_merge import _isolated, _merge_turn

    harness = _create_branch_harness(manifest, tmp_path, change="evidence")
    shared, binding = _isolated(harness, tmp_path, shared=tmp_path / "repo-a")
    first = _merge(harness)
    assert first.status_code == 202, first.text
    graph_head = harness.service.history.head_ref()
    (shared / "file").write_text("target change")
    git(shared, "commit", "-am", "target change")
    (Path(binding.worktree_path) / "file").write_text("episode change")
    turns = []
    tasks = harness.app.state.background_tasks
    monkeypatch.setattr(tasks, "on_task_settled", None)
    monkeypatch.setattr(harness.app.state.launcher, "stream", _merge_turn(harness, shared, turns))
    second = _merge(harness)
    assert second.status_code == 202, second.text
    task_id = second.json()["accepted_merge_task"]["operation_id"]
    wait_for_task(harness.store, task_id, expect="succeeded")
    assert turns
    assert (shared / "file").read_text() == "episode change"
    assert not any(
        receipt.provenance.merge_task_id == task_id for receipt in harness.branch.merge_receipts()
    )
    if legacy:
        with harness.store.connection() as connection:
            connection.execute(
                "DELETE FROM graph_run_receipts WHERE operation_id=? AND category IN "
                "('merge_effects_required','merge_warning_carry_complete',"
                "'merge_display_publication_complete','merge_effects_complete')",
                (task_id,),
            )
    reconcile_merge_effects(tasks, harness.service)
    assert not harness.store.pending_merge_effect_tasks()
    assert not harness.store.legacy_merge_effect_tasks(harness.project_id)
    [receipt] = harness.store.agent_task_receipts_by_category(task_id, "merge_effects_complete")
    assert receipt.payload["outcome"] == "not_applicable"
    assert reconcile_merge_effects(tasks, harness.service) == 0
    assert harness.service.history.head_ref() == graph_head
