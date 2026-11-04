from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from rcp.agents.command_protocol import AskArguments
from rcp.core.models import AuthorizedHuman
from rcp.projects import ProjectCatalog
from rcp.runs.approval_cards import settle_interrupted_approval_cards
from rcp.service import ProjectService
from rcp.storage import AppStore
from rcp.storage.approval_cards import card_sha256
from tests import test_api_questions as api_fixtures
from tests import test_questions_store as fixtures
from tests.lifecycles.proposals import (
    EV,
    PROPOSAL,
    api_card,
    append_agent_patch,
    approve_action,
    file_proposal,
    real_card,
)

store = fixtures.store
origin = fixtures.origin


def test_action_is_strict_and_cannot_offer_choices():
    action = {"kind": "truth_scope", "project_id": "project", "alias": "code", "writable": True}
    parsed = AskArguments.model_validate({"question": "Allow?", "action": action})
    assert parsed.action is not None and parsed.action.kind == "truth_scope"
    with pytest.raises(ValidationError):
        AskArguments.model_validate({"question": "Allow?", "action": action, "choices": ["Yes"]})
    with pytest.raises(ValidationError):
        AskArguments.model_validate({"question": "Allow?", "action": {**action, "writable": "yes"}})


def _no_service(project_id):
    raise AssertionError("no graph read for this card")


def _scope_service(scope):
    from types import SimpleNamespace

    state = SimpleNamespace(project_truth_scope=scope, proposals={})
    return SimpleNamespace(history=SimpleNamespace(state=lambda: state))


def make_card(store, origin):
    action = {"kind": "truth_scope", "project_id": "project", "alias": "code", "writable": True}
    return store.create_or_get_question(
        origin=origin,
        key="approval",
        question="Allow?",
        choices=["Approve", "Decline"],
        card={"action": action, "pinned": {"writable": False}},
    )


def test_card_claim_is_single_use_and_survives_restart(store, origin):
    question = make_card(store, origin)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: store.claim_approval_card(question.question_id), range(2))
        )
    assert sorted(results) == [False, True]
    reopened = AppStore(store.path)
    assert not reopened.claim_approval_card(question.question_id)
    settle_interrupted_approval_cards(
        reopened,
        service_for=lambda project_id: _scope_service([]),
        claimed_before=reopened.now(),
    )
    answered = reopened.get_question(question.question_id)
    assert answered is not None and answered.answer == "failed: interrupted"
    card = reopened.approval_card(question.question_id)
    assert card is not None and card["result"]["outcome"] == "failed"


def test_restart_settles_a_committed_action_from_its_receipt(store, origin):
    """The episode merge accepted its task before the process stopped."""
    action = {"kind": "merge_episode", "project_id": "project", "episode_id": "episode"}
    question = store.create_or_get_question(
        origin=origin,
        key="merge",
        question="Merge?",
        choices=["Approve", "Decline"],
        card={"action": action, "pinned": {}},
    )
    assert store.claim_approval_card(question.question_id)
    request_id = store.approval_card(question.question_id)["request_id"]
    with store.connection() as connection:
        connection.execute(
            """INSERT INTO episode_merge_request_receipts(project_id,owner_episode_id,
            request_id,acting_member,choices_json,choices_digest,attempt_id,attempt_json,
            accepted_task_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("project", "episode", request_id, "alex", "{}", "d", "a", "{}", "merge-task", "t"),
        )
    reopened = AppStore(store.path)
    settle_interrupted_approval_cards(
        reopened, service_for=_no_service, claimed_before=reopened.now()
    )
    card = reopened.approval_card(question.question_id)
    assert card is not None and card["result"]["outcome"] == "approved"
    answered = reopened.get_question(question.question_id)
    assert answered is not None and answered.answer == "approved"


def test_expiry_answers_question_and_claim_cannot_run(store, origin):
    question = make_card(store, origin)
    with store.connection() as connection:
        connection.execute("UPDATE approval_cards SET expires_at=?", (store.now(),))
    card = store.approval_card(question.question_id)
    assert card["state"] == "expired"
    assert store.get_question(question.question_id).state == "answered"
    assert store.get_question(question.question_id).answer == "expired"
    assert not store.claim_approval_card(question.question_id)


def test_digest_pins_action_and_has_stable_key_order():
    assert card_sha256({"a": 1, "b": 2}, {"head": 3}) == card_sha256({"b": 2, "a": 1}, {"head": 3})
    assert card_sha256({"a": 1}, {"head": 3}) != card_sha256({"a": 1}, {"head": 4})


questions_api = api_fixtures.questions_api


@pytest.mark.parametrize(
    "action",
    [
        {"kind": "truth_scope", "alias": "repo-a", "writable": False},
        {"kind": "auto_research", "invocation_ceiling": 1},
        {"kind": "merge_branch", "branch_id": "b"},
    ],
)
def test_each_action_rechecks_transfer_admission(questions_api, monkeypatch, action):
    """A closed admission is not the member's answer, so the card stays open for a later tap."""
    from rcp.api import approval_cards

    app, client, store, project_id = questions_api
    question, card = api_card(questions_api, {**action, "project_id": project_id})
    calls = []
    monkeypatch.setattr(approval_cards, "_execute", lambda *args: calls.append(args))

    def fenced(project_id):
        raise ValueError("project_transfer_in_progress")

    monkeypatch.setattr(store, "require_project_accepts_new_work", fenced)
    response = client.post(
        f"/api/approval-cards/{question.question_id}",
        json={"decision": "approve", "pinned_sha256": card["pinned_sha256"]},
    )
    assert response.status_code == 409
    assert calls == []
    assert store.get_question(question.question_id).state == "pending"
    assert store.claim_approval_card(question.question_id)


def test_an_outage_releases_the_card_and_a_failure_keeps_its_reason(questions_api, monkeypatch):
    from rcp.api import approval_cards
    from rcp.core.models import ValidationMessage
    from rcp.core.validation.report import ValidationReport
    from rcp.history import PatchRejected
    from rcp.transport.state import StateUnavailable

    app, client, store, project_id = questions_api
    question, card = api_card(questions_api)
    url = f"/api/approval-cards/{question.question_id}"
    body = {"decision": "approve", "pinned_sha256": card["pinned_sha256"]}

    def offline(*args, **kwargs):
        raise StateUnavailable("base unreachable")

    monkeypatch.setattr(approval_cards, "approval_pins", offline)
    assert client.post(url, json=body).status_code == 503
    assert store.get_question(question.question_id).state == "pending"

    report = ValidationReport()
    report.messages.append(ValidationMessage(level="reject", code="cycle", message="cycle"))

    def rejected(*args):
        raise PatchRejected(report)

    monkeypatch.setattr(
        approval_cards, "approval_pins", lambda *args, **kwargs: {"head": "original"}
    )
    monkeypatch.setattr(approval_cards, "_execute", rejected)
    result = client.post(url, json=body).json()
    assert result["outcome"] == "failed"
    assert result["reason"] == str(PatchRejected(report))


def test_unknown_merge_branch_is_refused_at_ask_with_its_code(questions_api):
    from uuid import uuid4

    from rcp.approval_actions import MergeBranchAction
    from rcp.runs.approval_cards import approval_pins

    app, client, store, project_id = questions_api
    service = app.state.catalog.open(project_id)
    action = MergeBranchAction(kind="merge_branch", project_id=project_id, branch_id=str(uuid4()))
    with pytest.raises(ValueError, match="branch_not_found"):
        approval_pins(service, store, action, store.local_owner)


def test_snapshot_carries_card_action_and_result_ids(store, origin):
    import json

    from rcp.runs.question_snapshots import question_snapshot

    question = make_card(store, origin)
    assert store.claim_approval_card(question.question_id)
    store.settle_approval_card(
        question.question_id, {"outcome": "approved", "result": {"operation_id": "merge-task"}}
    )
    snapshot = question_snapshot(
        store,
        project_id="project",
        owner_kind="chat",
        owner_ids=["chat"],
        operation_id=origin.operation_id,
    )
    entry = json.loads(snapshot.text)["questions"][0]
    assert entry["card"]["action"]["kind"] == "truth_scope"
    assert entry["card"]["result"]["result"] == {"operation_id": "merge-task"}


def test_restart_settles_a_committed_truth_scope_card(store, origin):
    """Membership already matches the action because the change committed before the stop."""
    question = make_card(store, origin)
    assert store.claim_approval_card(question.question_id)
    settle_interrupted_approval_cards(
        store,
        service_for=lambda project_id: _scope_service(["code"]),
        claimed_before=store.now(),
    )
    assert store.get_question(question.question_id).answer == "approved"


def test_restart_leaves_a_card_this_process_claimed_running(store, origin):
    """A live tap claimed it after this process started; its own request settles it."""
    started = store.now()
    question = make_card(store, origin)
    assert store.claim_approval_card(question.question_id)
    settle_interrupted_approval_cards(store, service_for=_no_service, claimed_before=started)
    card = store.approval_card(question.question_id)
    assert card is not None and card["result"] is None
    assert store.get_question(question.question_id).state == "pending"


def test_sweep_expires_cards_without_a_read(store, origin):
    question = make_card(store, origin)
    with store.connection() as connection:
        connection.execute("UPDATE approval_cards SET expires_at=?", (store.now(),))
    assert store.expire_approval_cards() == {"project"}
    assert store.get_question(question.question_id).answer == "expired"
    assert store.expire_approval_cards() == set()


@pytest.mark.parametrize("changed", ["client", "live", "none"])
def test_taps_recheck_pins_and_run_only_once(questions_api, monkeypatch, changed):
    from rcp.api import approval_cards

    app, client, store, project_id = questions_api
    question, card = api_card(questions_api)
    calls = []
    monkeypatch.setattr(
        approval_cards,
        "approval_pins",
        lambda *args, **kwargs: {"head": "changed" if changed == "live" else "original"},
    )
    monkeypatch.setattr(
        approval_cards, "_execute", lambda *args: calls.append(args) or {"ok": True}
    )
    body = {
        "decision": "approve",
        "pinned_sha256": "changed" if changed == "client" else card["pinned_sha256"],
    }
    url = f"/api/approval-cards/{question.question_id}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(url, json=body), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    result = next(response.json() for response in responses if response.status_code == 200)
    assert result["outcome"] == ("approved" if changed == "none" else "stale")
    assert len(calls) == (1 if changed == "none" else 0)
    app.state.reconcile_question_answers.assert_called_once_with(project_id)


def test_private_questions_and_cards_only_owner_and_no_plain_resolution(questions_api):
    app, client, store, project_id = questions_api
    question, card = api_card(questions_api)
    plain = api_fixtures._question(store, project_id, owner_id="private", key="plain")
    base = f"/api/projects/{project_id}"
    assert (
        client.post(
            f"{base}/questions/{question.question_id}/answer", json={"answer": "approved"}
        ).json()["detail"]["code"]
        == "approval_card"
    )
    assert (
        client.post(f"{base}/questions/{question.question_id}/dismiss", json={}).json()["detail"][
            "code"
        ]
        == "approval_card"
    )
    listed = client.get(f"{base}/chats/private/questions").json()
    assert (
        next(row for row in listed if row["question_id"] == question.question_id)["card"][
            "pinned_sha256"
        ]
        == card["pinned_sha256"]
    )
    with store.connection() as connection:
        connection.execute("UPDATE agent_threads SET user_id='someone-else'")
    assert client.get(f"{base}/chats/private/questions").status_code == 404
    for item in (question, plain):
        assert (
            client.post(
                f"{base}/questions/{item.question_id}/answer", json={"answer": "yes"}
            ).status_code
            == 404
        )
        assert (
            client.post(f"{base}/questions/{item.question_id}/dismiss", json={}).status_code == 404
        )
    assert (
        client.post(
            f"/api/approval-cards/{question.question_id}",
            json={"decision": "approve", "pinned_sha256": card["pinned_sha256"]},
        ).status_code
        == 404
    )


def test_old_writer_question_is_unchanged_by_card_migration(tmp_path, origin):
    """Old writer SQL has neither action nor card, and the migration must preserve it."""
    import sqlite3

    from rcp.storage.approval_cards import migrate_approval_cards
    from rcp.storage.questions import migrate_questions

    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        migrate_questions(connection)
        connection.execute("""INSERT INTO questions(question_id,project_id,owner_kind,owner_id,key,
            origin_json,argument_hash,question,choices_json,multiple,state,created_at)
            VALUES ('old','project','chat','chat','key','{}','old-hash','Pick?','[]',0,'pending','2020-01-01')""")
        before = connection.execute("SELECT * FROM questions").fetchall()
        migrate_approval_cards(connection)
        assert connection.execute("SELECT * FROM questions").fetchall() == before
        assert connection.execute("SELECT count(*) FROM approval_cards").fetchone()[0] == 0


def test_private_question_push_has_only_owner_recipient(tmp_path):
    from types import SimpleNamespace

    from rcp.notifications import NotificationSender
    from rcp.server_ops.maintenance import RuntimeAdmissionGate
    from tests.test_episode_api_serialization import _project

    store, bootstrap = AppStore.initialize_team_space(tmp_path / "db", "Team")
    owner, token = store.enroll_team_member(bootstrap, "Owner")
    _, code = store.create_team_invitation(owner.user_id)
    other, other_token = store.enroll_team_member(code, "Other")
    _project(store)
    devices = {}
    for member, credential in [(owner, token), (other, other_token)]:
        store.seat_project_member("project", member.user_id)
        store.create_team_session(credential)
        session = store.team_sessions(member.user_id)[0]
        devices[member.user_id] = store.register_notification_device(
            member.user_id, session_id=session.session_id
        )["device_id"]
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_threads(thread_id,user_id,kind,project_id,created_at) "
            "VALUES ('private',?,'project','project',?)",
            (owner.user_id, store.now()),
        )
    question = api_fixtures._question(store, "project", owner_id="private")
    sender = NotificationSender(
        store,
        cast(
            ProjectCatalog,
            SimpleNamespace(open=lambda _: (_ for _ in ()).throw(OSError("offline"))),
        ),
        admission=RuntimeAdmissionGate(),
    )
    sender.run_pass()
    assert [(row["device_id"], row["item_id"]) for row in store.notification_outbox()] == [
        (devices[owner.user_id], question.question_id)
    ]


def test_expired_tap_is_single_use(questions_api):
    app, client, store, project_id = questions_api
    question, card = api_card(questions_api)
    with store.connection() as connection:
        connection.execute("UPDATE approval_cards SET expires_at=?", (store.now(),))
    url = f"/api/approval-cards/{question.question_id}"
    body = {"decision": "approve", "pinned_sha256": card["pinned_sha256"]}
    assert client.post(url, json=body).json() == {"outcome": "expired"}
    assert client.post(url, json=body).status_code == 409


def test_card_request_id_is_persisted_uuid4(store, origin):
    """Episode merge retains its native UUID contract while cards still mint UUID4 keys."""
    from uuid import NAMESPACE_URL, UUID, uuid5

    from rcp.api.episode_routes import MergeBranchBody
    from rcp.approval_actions import MergeEpisodeBody

    question = make_card(store, origin)
    card = store.approval_card(question.question_id)
    key = UUID(card["request_id"])
    assert key.version == 4
    assert AppStore(store.path).approval_card(question.question_id)["request_id"] == str(key)
    assert make_card(store, origin).question_id == question.question_id
    assert store.approval_card(question.question_id)["request_id"] == str(key)
    for model in (MergeBranchBody, MergeEpisodeBody):
        assert model(request_id=key).request_id == key
    with pytest.raises(ValidationError):
        MergeBranchBody(request_id=uuid5(NAMESPACE_URL, "card"))
    episode_key = uuid5(NAMESPACE_URL, "card")
    assert MergeEpisodeBody(request_id=episode_key).request_id == episode_key
    store.settle_approval_card(question.question_id, {"outcome": "declined"})
    assert store.approval_card(question.question_id)["result"]["request_id"] == str(key)


def test_staged_ask_action_roundtrips_to_strict_broker(tmp_path):
    import json

    from rcp.agents.staged_command_client import ClientInputError, _parser, _request_arguments

    action = {"kind": "truth_scope", "project_id": "p", "alias": "code", "writable": True}
    args = [
        "--broker",
        "broker",
        "--timeout",
        "1",
        "--workspace",
        str(tmp_path),
        "ask",
        "--key",
        "approve",
        "--question",
        "Allow?",
        "--action",
        json.dumps(action),
    ]
    verb, key, arguments = _request_arguments(_parser().parse_args(args), tmp_path)
    assert (verb, key) == ("ask", "approve")
    parsed = AskArguments.model_validate(arguments)
    assert parsed.action is not None and parsed.action.model_dump() == action
    with pytest.raises(ClientInputError):
        _request_arguments(_parser().parse_args([*args, "--choice", "Yes"]), tmp_path)


def test_ask_action_requires_private_owner_and_persists_exact_action(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from rcp.runs import approval_cards
    from rcp.runs.questions import work_command_handler
    from tests.test_ask_protocol import ask_request
    from tests.test_work_questions import identity, work_execution

    execution, human = work_execution(tmp_path)
    store = execution.store
    action = {"kind": "truth_scope", "project_id": "project", "alias": "repo-a", "writable": False}
    request = ask_request(arguments={"question": "Allow?", "action": action})
    monkeypatch.setattr(approval_cards, "approval_pins", lambda *args, **kwargs: {"writable": True})
    handler = work_command_handler(
        execution, None, service=cast(ProjectService, SimpleNamespace(project_catalog=None))
    )
    assert handler(request, identity()).status == "invalid"
    assert store.list_questions() == []
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_threads(thread_id,user_id,kind,project_id,created_at) "
            "VALUES ('chat',?,'project','project',?)",
            (human.user_id, store.now()),
        )
    handler = work_command_handler(
        execution, None, service=cast(ProjectService, SimpleNamespace(project_catalog=None))
    )
    response = handler(request, identity())
    assert response.status == "ok"
    question_id = response.result["question_id"]
    assert isinstance(question_id, str)
    question = store.get_question(question_id)
    assert question is not None and question.choices == ["Approve", "Decline"]
    card = store.approval_card(question.question_id)
    assert card is not None and card["action"] == action
    assert handler(request, identity()).result["question_id"] == question.question_id
    changed = ask_request(arguments={"question": "Allow?", "action": {**action, "writable": True}})
    assert handler(changed, identity()).status == "invalid"
    assert len(store.list_questions()) == 1


def test_truth_card_executes_real_human_route(questions_api):
    import json

    from rcp.approval_actions import TruthScopeAction
    from rcp.runs.approval_cards import approval_pins
    from rcp.storage.approval_cards import card_sha256
    from tests.helpers import authorized_human

    app, client, store, project_id = questions_api
    service = app.state.catalog.open(project_id)
    writable = "repo-b" in service.history.state().project_truth_scope
    action = TruthScopeAction(
        kind="truth_scope", project_id=project_id, alias="repo-b", writable=not writable
    )
    question, _ = api_card(questions_api, action.model_dump())
    pins = approval_pins(service, store, action, authorized_human(store))
    digest = card_sha256(action.model_dump(), pins)
    with store.connection() as connection:
        connection.execute(
            "UPDATE approval_cards SET pinned_json=?,pinned_sha256=?", (json.dumps(pins), digest)
        )
    response = client.post(
        f"/api/approval-cards/{question.question_id}",
        json={"decision": "approve", "pinned_sha256": digest},
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "approved", response.json()
    assert ("repo-b" in service.history.state().project_truth_scope) == (not writable)


def test_closed_thread_expires_card_on_list(questions_api):
    app, client, store, project_id = questions_api
    question, card = api_card(questions_api)
    with store.connection() as connection:
        connection.execute(
            "UPDATE agent_threads SET closed_at=?,closed_reason='access_lost'", (store.now(),)
        )
    listed = client.get(f"/api/projects/{project_id}/chats/private/questions").json()
    row = next(row for row in listed if row["question_id"] == question.question_id)
    assert row["card"]["state"] == "expired"
    assert row["state"] == "answered"
    assert row["answer"] == "expired"


@pytest.mark.parametrize("settlement", [None, "declined", "expired"])
def test_private_questions_never_publish_digest_text(questions_api, settlement):
    app, client, store, project_id = questions_api
    question, card = api_card(questions_api)
    if settlement:
        store.settle_approval_card(question.question_id, {"outcome": settlement})
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM digest_events WHERE kind='question_attention'"
            ).fetchone()[0]
            == 0
        )


def refuse_transfer(_project):
    raise ValueError("project_transfer_in_progress")


def bounded_admission_lock(monkeypatch):
    """The real route and fence stay; lock acquisition is bounded so a regression fails instead of
    hanging in a non-reentrant lock.
    """
    from contextlib import contextmanager
    from threading import Lock

    from rcp.api import dependencies

    lock = Lock()

    @contextmanager
    def acquire(_project):
        assert lock.acquire(timeout=0.1), "nested project admission deadlock"
        try:
            yield
        finally:
            lock.release()

    monkeypatch.setattr(dependencies, "get_experiment_operation_lock", lambda _: acquire)


@pytest.mark.parametrize("fenced", [False, True])
def test_proposal_approval_real_route(questions_api, monkeypatch, fenced):
    from tests.test_sync import append_decision_fixture

    app, client, store, project_id = questions_api
    service = app.state.catalog.open(project_id)
    append_decision_fixture(service, with_proposals=True)
    action = {
        "kind": "proposal",
        "project_id": project_id,
        "proposal_id": "prop/evaluation-matched",
        "decision": "rejected",
    }
    question, card = real_card(questions_api, action)
    bounded_admission_lock(monkeypatch)
    if fenced:
        monkeypatch.setattr(store, "require_project_accepts_new_work", refuse_transfer)
    response = client.post(
        f"/api/approval-cards/{question.question_id}",
        json={"decision": "approve", "pinned_sha256": card["pinned_sha256"]},
    )
    if fenced:
        assert response.status_code == 409
        assert store.get_question(question.question_id).state == "pending"
        return
    assert response.json()["outcome"] == "approved", response.text
    assert service.history.state().proposals[action["proposal_id"]].status == "rejected"


@pytest.mark.parametrize("fenced", [False, True])
def test_merge_episode_approval_real_route(manifest, tmp_path, monkeypatch, fenced):
    from tests.test_branch_merge_api import _create_branch_harness

    harness = _create_branch_harness(manifest, tmp_path, change="status")
    api = (harness.app, harness.client, harness.store, harness.project_id)
    action = {
        "kind": "merge_episode",
        "project_id": harness.project_id,
        "episode_id": harness.episode.episode_id,
    }
    question, card = real_card(api, action)
    bounded_admission_lock(monkeypatch)
    if fenced:
        monkeypatch.setattr(harness.store, "require_project_accepts_new_work", refuse_transfer)
    response = harness.client.post(
        f"/api/approval-cards/{question.question_id}",
        json={"decision": "approve", "pinned_sha256": card["pinned_sha256"]},
    )
    if fenced:
        assert response.status_code == 409
        assert harness.store.get_question(question.question_id).state == "pending"
        return
    assert response.json()["outcome"] == "approved", response.text
    assert len(harness.branch.merge_receipts()) == 1
    attempt = harness.store.episode_isolation_state(
        harness.project_id, harness.episode.episode_id
    ).merge_attempt
    from rcp.approval_actions import MergeEpisodeBody

    receipt = harness.store.episode_merge_request(
        harness.project_id,
        harness.episode.episode_id,
        card["request_id"],
        acting_member=harness.episode.authorized_by.user_id,
        choices=MergeEpisodeBody().model_dump(mode="json", exclude={"request_id"}),
    )
    assert receipt["attempt_id"] == attempt.attempt_id
    assert receipt["accepted_task_id"] == attempt.attempt_id
    assert response.json()["result"]["operation_id"] == attempt.attempt_id


def test_card_table_includes_required_request_id_from_creation(store):
    import sqlite3

    with store.connection() as connection:
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(approval_cards)")}
        assert columns["request_id"][2:4] == ("TEXT", 1)
        with pytest.raises(sqlite3.IntegrityError, match="request_id"):
            connection.execute(
                "INSERT INTO approval_cards(question_id,action_json,pinned_json,pinned_sha256,expires_at) "
                "VALUES ('card','{}','{}','pin','2099-01-01')"
            )


@pytest.mark.parametrize("changed_head", ["source", "main"])
def test_merge_episode_card_rechecks_heads_under_admission(
    manifest, tmp_path, monkeypatch, changed_head
):
    from contextlib import contextmanager

    from rcp.api import dependencies
    from tests.helpers import append_fixture_patch
    from tests.test_branch_merge_api import _branch_patch, _create_branch_harness

    harness = _create_branch_harness(manifest, tmp_path, change="status")
    action = {
        "kind": "merge_episode",
        "project_id": harness.project_id,
        "episode_id": harness.episode.episode_id,
    }
    question, card = real_card(
        (harness.app, harness.client, harness.store, harness.project_id), action
    )
    admissions = []

    @contextmanager
    def acquire(project_id):
        """Advances after the card's optimistic recheck, as a writer already holding admission can
        while the merge waits for its lock.
        """
        patch = _branch_patch(harness.root.operation_id, "evidence", suffix="-raced")
        if changed_head == "source":
            harness.branch.append(patch, expected_revision=harness.branch.head_ref().revision)
        else:
            append_fixture_patch(harness.service, patch)
        admissions.append(project_id)
        yield

    monkeypatch.setattr(dependencies, "get_experiment_operation_lock", lambda _: acquire)
    response = harness.client.post(
        f"/api/approval-cards/{question.question_id}",
        json={"decision": "approve", "pinned_sha256": card["pinned_sha256"]},
    )
    assert response.status_code == 200
    assert response.json() == {"outcome": "stale"}
    assert admissions == [harness.project_id]
    assert harness.branch.merge_receipts() == []
    assert (
        harness.store.episode_isolation_state(harness.project_id, harness.episode.episode_id)
        is None
    )
    assert harness.store.get_question(question.question_id).answer == "stale"


@pytest.mark.parametrize(
    "kind,handler", [("auto_research", "start_episode"), ("merge_branch", "merge_graph_branch")]
)
def test_card_forwards_persisted_uuid4(questions_api, monkeypatch, kind, handler):
    from uuid import UUID

    from rcp.api import approval_cards, episode_routes

    app, client, store, project_id = questions_api
    action = {"kind": kind, "project_id": project_id}
    action.update({"invocation_ceiling": 1} if kind == "auto_research" else {"branch_id": "branch"})
    question, card = api_card(questions_api, action)
    seen = []

    def execute(*args, **kwargs):
        key = kwargs["idempotency_key"] if kind == "auto_research" else args[2].request_id
        seen.append(key)
        return {}

    monkeypatch.setattr(episode_routes, handler, execute)
    monkeypatch.setattr(
        approval_cards, "approval_pins", lambda *args, **kwargs: {"head": "original"}
    )
    response = client.post(
        f"/api/approval-cards/{question.question_id}",
        json={"decision": "approve", "pinned_sha256": card["pinned_sha256"]},
    )
    assert response.json()["outcome"] == "approved", response.text
    assert seen == [UUID(card["request_id"])]
    assert seen[0].version == 4


def test_closed_thread_card_tap_is_refused_before_acting(questions_api, monkeypatch):
    from rcp.api import approval_cards

    app, client, store, project_id = questions_api
    question, card = api_card(questions_api)
    monkeypatch.setattr(approval_cards, "_execute", lambda *args: pytest.fail("acted"))
    store.close_agent_thread("private", reason="access_lost")
    response = client.post(
        f"/api/approval-cards/{question.question_id}",
        json={"decision": "approve", "pinned_sha256": card["pinned_sha256"]},
    )
    assert response.status_code == 409
    assert store.get_question(question.question_id).answer is None


def test_usage_names_every_action_field_and_outcome():
    from typing import get_args

    from rcp.approval_actions import ApprovalAction, ApprovalOutcome
    from rcp.runs.approval_cards import (
        THREAD_CARD_KINDS,
        approval_card_usage,
        space_card_usage,
    )

    usage = approval_card_usage("rcp")
    for model in get_args(get_args(ApprovalAction)[0]):
        assert all(name in usage for name in model.model_fields)
    assert all(outcome in usage for outcome in get_args(ApprovalOutcome))
    space = space_card_usage()
    for model in get_args(get_args(ApprovalAction)[0]):
        kind = get_args(model.model_fields["kind"].annotation)[0]
        assert (f"{kind} (" in space) == (kind in THREAD_CARD_KINDS["space"])


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        ({"code": "merge_heads_changed"}, {"outcome": "stale"}),
        ({"code": "branch_name_taken"}, {"outcome": "failed", "reason": "branch_name_taken"}),
        ({"code": "x", "message": "why"}, {"outcome": "failed", "reason": "why"}),
        ("plain", {"outcome": "failed", "reason": "plain"}),
        (503, None),
        ("state", None),
    ],
)
def test_handler_outcomes_map_to_card_results(raised, expected):
    from fastapi import HTTPException

    from rcp.api.approval_cards import _NotNow, _run
    from rcp.transport.state import StateUnavailable

    def execute():
        if raised == "state":
            raise StateUnavailable("offline")
        if raised == 503:
            raise HTTPException(status_code=503, detail="offline")
        raise HTTPException(status_code=409, detail=raised)

    if expected is None:
        with pytest.raises(_NotNow):
            _run(execute, "request")
    else:
        assert _run(execute, "request") == expected


@pytest.mark.parametrize(
    ("installed", "authenticated", "problem", "refused"),
    [(True, True, None, False), (True, False, None, True), (True, True, "sandbox", True)],
)
def test_ask_refuses_an_auto_research_card_whose_provider_cannot_launch(
    monkeypatch, installed, authenticated, problem, refused
):
    from types import SimpleNamespace

    from rcp.approval_actions import AutoResearchAction
    from rcp.runs import approval_cards

    profile = {"provider": "codex", "run_on": "local"}
    monkeypatch.setattr(
        approval_cards, "approval_pins", lambda *args, **kwargs: {"profile": profile}
    )
    readiness = SimpleNamespace(
        installed=installed,
        authenticated=authenticated,
        reason=None,
        work_like_available=problem is None,
        work_like_reason=problem,
    )
    service = SimpleNamespace(
        manifest=SimpleNamespace(machine_map={"local": SimpleNamespace(host="")}),
        launcher=SimpleNamespace(
            member_readiness=lambda *args, **kwargs: readiness,
            provider_binary=lambda *args: "codex",
        ),
    )
    action = AutoResearchAction(kind="auto_research", project_id="p", invocation_ceiling=1)
    human = SimpleNamespace(user_id="alex")
    if refused:
        with pytest.raises(ValueError, match="provider_not_ready"):
            approval_cards.approval_card_pins(service, None, action, human)  # type: ignore[arg-type]
    else:
        assert approval_cards.approval_card_pins(service, None, action, human) == {  # type: ignore[arg-type]
            "profile": profile
        }


def test_request_receipts_need_their_durable_row(store):
    from rcp.runs.approval_cards import committed_receipt

    for kind in ("auto_research", "merge_branch", "merge_episode"):
        card = {
            "action": {"kind": kind, "project_id": "project"},
            "request_id": "r",
            "user_id": "alex",
        }
        assert committed_receipt(store, _no_service, card) is None
    card = {
        "action": {"kind": "merge_branch", "project_id": "p"},
        "request_id": "r",
        "user_id": None,
    }
    assert committed_receipt(store, _no_service, card) is None


def _traced(store, monkeypatch):
    """Record every statement the store runs."""
    from contextlib import contextmanager

    statements: list[str] = []
    connection = store.connection

    @contextmanager
    def traced():
        with connection() as opened:
            opened.set_trace_callback(statements.append)
            yield opened

    monkeypatch.setattr(store, "connection", traced)
    return statements


def test_sweep_with_nothing_due_takes_no_write_lock(store, origin, monkeypatch):
    make_card(store, origin)
    statements = _traced(store, monkeypatch)
    assert store.expire_approval_cards() == set()
    assert not [statement for statement in statements if "BEGIN" in statement]


def test_question_list_reads_do_not_grow_with_answered_history(questions_api, monkeypatch):
    app, client, store, project_id = questions_api
    api_card(questions_api)
    url = f"/api/projects/{project_id}/chats/private/questions"

    human = AuthorizedHuman(
        space_id=store.space_id, user_id=store.local_owner.user_id, display_name="Member"
    )
    statements = _traced(store, monkeypatch)

    def statements_per_list() -> int:
        statements.clear()
        assert client.get(url).status_code == 200
        return len(statements)

    before = statements_per_list()
    for index in range(5):
        answered = api_fixtures._question(store, project_id, owner_id="private", key=f"k{index}")
        store.dismiss_question(answered.question_id, resolved_by=human)
    assert len(client.get(url).json()) == 7
    assert statements_per_list() == before


_RACED_WRITES = {
    "reword": [{"op": "update_nodes", "nodes": [{"id": EV, "changes": {"observation": "N."}}]}],
    "withdraw": [{"op": "withdraw_proposals", "proposals": [{"id": PROPOSAL, "reason": "r"}]}],
    "unrelated": [
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "ev/unrelated",
                    "type": "evidence",
                    "title": "Unrelated",
                    "observation": "Elsewhere.",
                    "origin": "analytic",
                }
            ],
        }
    ],
}


@pytest.mark.parametrize("competing", ["second_tap", *_RACED_WRITES])
def test_a_write_between_pin_check_and_sync_never_decides_unseen_pins(
    questions_api, monkeypatch, competing
):
    """The competing tap or graph write lands after the pin check and before Sync appends."""
    from rcp.api import approval_cards

    app, client, store, project_id = questions_api
    service = app.state.catalog.open(project_id)
    file_proposal(service)
    question, card = real_card(questions_api, approve_action(project_id, PROPOSAL))
    url = f"/api/approval-cards/{question.question_id}"
    body = {"decision": "approve", "pinned_sha256": card["pinned_sha256"]}
    second = []
    run = approval_cards._run

    def interleaved(execute, request_id):
        if competing == "second_tap":
            second.append(client.post(url, json=body))
        else:
            append_agent_patch(service, _RACED_WRITES[competing])
        return run(execute, request_id)

    monkeypatch.setattr(approval_cards, "_run", interleaved)
    first = client.post(url, json=body)
    status = service.history.state().proposals[PROPOSAL].status
    if competing == "second_tap":
        assert second[0].status_code == 409
        assert second[0].json()["detail"] == {"code": "card_taken"}
        assert first.json()["outcome"] == "approved", first.text
        assert status == "approved"
        return
    assert first.status_code == 409, first.text
    assert first.json()["detail"] == {"code": "graph_changed"}
    assert store.get_question(question.question_id).state == "pending"
    assert status == ("withdrawn" if competing == "withdraw" else "pending")
    monkeypatch.setattr(approval_cards, "_run", run)
    retap = client.post(url, json=body).json()
    assert retap["outcome"] == ("approved" if competing == "unrelated" else "stale")
    assert store.get_question(question.question_id).answer == retap["outcome"]


def _thread_turn(store, project_id, operation_id, *, human, thread_id="private", parent=None):
    """A finished Work turn of the private thread, authorized by `human`."""
    from rcp.service import RunRequest, resolve_dispatch_authority
    from rcp.storage import AgentTaskRecord

    request = RunRequest(
        chat_scope="project",
        chat_id=thread_id,
        message="File it.",
        mode="work",
        provider="codex",
        run_truth_scope=["repo-a"],
    )
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind="project_chat",
            status="succeeded",
            request=request.model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message="Done",
            authorized_by=human,
            dispatch_authority=resolve_dispatch_authority("project_chat", request),
            parent_operation_id=parent,
        )
    )


def _raised_proposals(questions_api):
    """Proposals on main: two from the member's thread turns, one from another member's turn
    in the same chat, one decided, and the fixture's own.
    """
    from tests.helpers import authorized_human
    from tests.lifecycles.proposals import proposal_filing

    app, client, store, project_id = questions_api
    api_card(questions_api)
    service = app.state.catalog.open(project_id)
    file_proposal(service)
    member = authorized_human(app)
    other = AuthorizedHuman(space_id=store.space_id, user_id=str(uuid4()), display_name="Other")
    _thread_turn(store, project_id, "turn-a", human=member)
    _thread_turn(store, project_id, "turn-a-retry", human=member, parent="turn-a")
    _thread_turn(store, project_id, "turn-other", human=other)
    _thread_turn(store, project_id, "turn-b", human=member)
    raised = {
        "prop/raised/first": "turn-a",
        "prop/raised/retried": "turn-a-retry",
        "prop/raised/other": "turn-other",
        "prop/raised/decided": "turn-b",
        "prop/raised/later": "turn-b",
    }
    for proposal_id, operation_id in raised.items():
        append_agent_patch(
            service, [proposal_filing(proposal_id)], source_operation_id=operation_id
        )
    decide = client.post(
        f"/api/approval-cards/proposals/{project_id}/prop%2Fraised%2Fdecided",
        json={"decision": "rejected"},
    )
    assert decide.status_code == 200, decide.text
    return service


def test_thread_cards_are_this_threads_pending_proposals(questions_api):
    app, client, store, project_id = questions_api
    _raised_proposals(questions_api)
    body = client.get("/api/agent-threads/cards", params={"thread_id": "private"}).json()
    cards = body["proposal_cards"]
    ids = [card["proposal"]["id"] for card in cards]
    assert ids[0] == "prop/raised/later"
    assert set(ids[1:]) == {"prop/raised/retried", "prop/raised/first"}
    turns = {card["proposal"]["id"]: card["turn_id"] for card in cards}
    assert turns["prop/raised/retried"] == turns["prop/raised/first"] == "turn-a"
    card = cards[0]
    assert (card["project_id"], card["thread_id"], card["stale"]) == (project_id, "private", False)
    assert card["project_name"] == store.project(project_id).name
    assert card["turn_created_at"] == store.agent_task("turn-b").created_at
    assert card["changes"]
    assert client.get("/api/agent-threads/cards").json()["proposal_cards"] == cards


def test_space_closed_and_archived_threads_have_no_cards(questions_api):
    app, client, store, project_id = questions_api
    _raised_proposals(questions_api)
    space = next(
        thread
        for thread in store.ensure_agent_threads(
            store.local_owner.user_id, [], default_names=("Pip",)
        )
        if thread.kind == "space"
    )
    empty = {"proposal_cards": [], "asked_cards": []}
    assert client.get(f"/api/agent-threads/cards?thread_id={space.thread_id}").json() == empty
    store.set_chat_archived(project_id, "private", store.local_owner.user_id, archived=True)
    assert client.get("/api/agent-threads/cards?thread_id=private").json() == empty
    assert client.get("/api/agent-threads/cards").json() == empty
    store.set_chat_archived(project_id, "private", store.local_owner.user_id, archived=False)
    store.close_agent_thread("private", reason="access_lost")
    assert client.get("/api/agent-threads/cards?thread_id=private").json() == empty
    assert client.get("/api/agent-threads/cards").json()["proposal_cards"] == []


def test_member_cards_skip_an_unreadable_project_and_list_open_asked_cards(
    questions_api, monkeypatch
):
    from rcp.transport.state import StateUnavailable

    app, client, store, project_id = questions_api
    _raised_proposals(questions_api)
    open_card, _ = api_card(questions_api)
    origin = open_card.origin

    def asked(key):
        return store.create_or_get_question(
            origin=origin,
            key=key,
            question="Allow?",
            choices=["Approve", "Decline"],
            card={
                "action": {"kind": "merge_branch", "project_id": project_id, "branch_id": key},
                "pinned": {},
            },
        )

    claimed, expired, declined = asked("claimed"), asked("expired"), asked("declined")
    assert store.claim_approval_card(claimed.question_id)
    store.settle_approval_card(declined.question_id, {"outcome": "declined"})
    with store.connection() as connection:
        connection.execute(
            "UPDATE approval_cards SET expires_at=? WHERE question_id=?",
            (store.now(), expired.question_id),
        )

    def unreadable(_project_id):
        raise StateUnavailable("offline")

    monkeypatch.setattr(app.state.catalog, "open_display", unreadable)
    assert client.get("/api/agent-threads/cards?thread_id=private").status_code == 503
    body = client.get("/api/agent-threads/cards").json()
    assert body["proposal_cards"] == []
    assert [row["question_id"] for row in body["asked_cards"]] == [
        claimed.question_id,
        open_card.question_id,
    ]
    assert {row["project_name"] for row in body["asked_cards"]} == {store.project(project_id).name}
    assert body["asked_cards"][0]["card"]["state"] == "running"


def _decide(client, project_id, proposal_id, decision="approved"):
    from urllib.parse import quote

    return client.post(
        f"/api/approval-cards/proposals/{project_id}/{quote(proposal_id, safe='')}",
        json={"decision": decision},
    )


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_a_proposal_card_decides_once(questions_api, decision):
    app, client, store, project_id = questions_api
    service = _raised_proposals(questions_api)
    response = _decide(client, project_id, "prop/raised/first", decision)
    assert response.json() == {"proposal_id": "prop/raised/first", "status": decision}
    proposal = service.history.state().proposals["prop/raised/first"]
    assert (proposal.status, proposal.resolved_by) == (decision, "human")
    again = _decide(client, project_id, "prop/raised/first", decision)
    assert again.status_code == 409
    assert again.json()["detail"] == {"code": "proposal_not_pending"}


def test_a_double_tap_decides_once(questions_api):
    app, client, store, project_id = questions_api
    service = _raised_proposals(questions_api)
    revision = service.history.state().revision
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda _: _decide(client, project_id, "prop/raised/first"), range(2))
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    refused = next(response for response in responses if response.status_code == 409)
    assert refused.json()["detail"] == {"code": "proposal_not_pending"}
    assert service.history.state().revision == revision + 1


@pytest.mark.parametrize("ending", ["withdraw", "stale"])
def test_a_withdrawn_or_stale_proposal_is_not_pending(questions_api, ending):
    app, client, store, project_id = questions_api
    service = _raised_proposals(questions_api)
    write = _RACED_WRITES["withdraw"] if ending == "withdraw" else _RACED_WRITES["reword"]
    append_agent_patch(service, write)
    revision = service.history.state().revision
    response = _decide(client, project_id, PROPOSAL)
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "proposal_not_pending"}
    assert service.history.state().revision == revision


@pytest.mark.parametrize(
    ("write", "code"), [("unrelated", "graph_changed"), ("withdraw", "proposal_not_pending")]
)
def test_a_write_before_the_decision_lands_refuses_it(questions_api, monkeypatch, write, code):
    from rcp.api import approval_cards

    app, client, store, project_id = questions_api
    service = _raised_proposals(questions_api)
    sync = approval_cards._sync_proposal
    target = PROPOSAL if write == "withdraw" else "prop/raised/first"
    before = service.history.state().proposals[target].status

    def raced(*args):
        append_agent_patch(service, _RACED_WRITES[write])
        return sync(*args)

    monkeypatch.setattr(approval_cards, "_sync_proposal", raced)
    response = _decide(client, project_id, target)
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": code}
    assert service.history.state().proposals[target].status == (
        "pending" if write == "unrelated" else "withdrawn"
    )
    assert before == "pending"


def test_a_decision_needs_admission_and_membership(questions_api, monkeypatch):
    app, client, store, project_id = questions_api
    service = _raised_proposals(questions_api)
    monkeypatch.setattr(store, "require_project_accepts_new_work", refuse_transfer)
    assert _decide(client, project_id, "prop/raised/first").status_code == 409
    monkeypatch.setattr(store, "is_project_member", lambda *_: False)
    assert _decide(client, project_id, "prop/raised/first").status_code == 404
    assert service.history.state().proposals["prop/raised/first"].status == "pending"


def _decline(client, question, card):
    return client.post(
        f"/api/approval-cards/{question.question_id}",
        json={"decision": "decline", "pinned_sha256": card["pinned_sha256"]},
    )


@pytest.mark.parametrize("before", ["none", "inbox", "fenced", "stale"])
def test_declining_an_approve_card_rejects_or_says_why(questions_api, monkeypatch, before):
    app, client, store, project_id = questions_api
    service = app.state.catalog.open(project_id)
    file_proposal(service)
    question, card = real_card(questions_api, approve_action(project_id, PROPOSAL))
    if before == "inbox":
        inbox = client.post(
            f"/api/projects/{project_id}/sync",
            json={
                "base_revision": service.history.state().revision,
                "proposals": [
                    {"proposal_id": PROPOSAL, "decision": "approved", "reason": "From the Inbox"}
                ],
            },
        )
        assert inbox.status_code == 200, inbox.text
    if before == "fenced":
        monkeypatch.setattr(store, "require_project_accepts_new_work", refuse_transfer)
    if before == "stale":
        append_agent_patch(service, _RACED_WRITES["reword"])
    result = _decline(client, question, card).json()
    assert result["outcome"] == "declined"
    assert store.get_question(question.question_id).answer == "declined"
    status = service.history.state().proposals[PROPOSAL].status
    stored = store.approval_card(question.question_id)["result"]
    listed = client.get(
        f"/api/projects/{project_id}/chats/{question.origin.owner_id}/questions"
    ).json()
    shown = next(item for item in listed if item["question_id"] == question.question_id)
    assert shown["card"]["result"] == {
        key: stored[key] for key in ("outcome", "rejected", "reason") if key in stored
    }
    if before == "none":
        assert result["rejected"] is True
        assert status == "rejected"
        assert stored["rejected"] is True
        return
    if before == "inbox":
        assert "rejected" not in result
        assert status == "approved"
        assert "rejected" not in stored
        return
    assert result["rejected"] is False
    assert result["reason"]
    assert status == "pending"
    assert stored["rejected"] is False
    assert stored["reason"] == result["reason"]


def test_declining_a_reject_card_runs_nothing(questions_api, monkeypatch):
    from rcp.api import approval_cards

    app, client, store, project_id = questions_api
    service = app.state.catalog.open(project_id)
    file_proposal(service)
    action = {**approve_action(project_id, PROPOSAL), "decision": "rejected"}
    question, card = real_card(questions_api, action)
    monkeypatch.setattr(approval_cards, "decide_proposal", lambda *_: pytest.fail("decided"))
    assert _decline(client, question, card).json() == {"outcome": "declined"}
    assert service.history.state().proposals[PROPOSAL].status == "pending"


def test_ask_refuses_a_card_for_a_proposal_this_thread_raised(questions_api):
    from rcp.runs.approval_cards import approval_card_for_ask
    from tests.helpers import authorized_human
    from tests.test_ask_protocol import ask_request

    app, client, store, project_id = questions_api
    service = _raised_proposals(questions_api)
    question, _ = api_card(questions_api)
    member = authorized_human(app)

    def ask(proposal_id):
        request = ask_request(
            arguments={"question": "Approve?", "action": approve_action(project_id, proposal_id)}
        )
        return approval_card_for_ask(store, request, question.origin, service, member)

    with pytest.raises(ValueError, match="proposal_card_shown"):
        ask("prop/raised/first")
    assert ask("prop/raised/other")["action"]["proposal_id"] == "prop/raised/other"
    assert ask(PROPOSAL)["action"]["proposal_id"] == PROPOSAL


def _space_turn(questions_api):
    """A running turn of the member's Space thread with its session and stage, and its handler."""
    from rcp.runs.agent_thread_commands import ThreadCommandHandler
    from tests.helpers import authorized_human
    from tests.test_storage import _task_record

    app, _client, store, _project_id = questions_api
    member = authorized_human(app)
    space = next(
        thread
        for thread in store.ensure_agent_threads(member.user_id, [], default_names=("Pip",))
        if thread.kind == "space"
    )
    store.create_agent_task(
        _task_record(
            store.now(),
            operation_id="space-turn",
            project_id=store.space_id,
            kind="space_chat",
            status="running",
            authorized_by=member,
            request={"chat_id": space.thread_id, "mode": "work", "provider": "codex"},
            native_session_id="space-session",
            stage_root="/space-stage",
        )
    )
    return ThreadCommandHandler(store, app.state.catalog, "space-turn")


def _space_ask(handler, action, key="card"):
    from rcp.agents.command_mailbox import CommandTurnIdentity
    from tests.test_ask_protocol import ask_request

    arguments = {"question": "Decide?", **({"action": action} if action else {})}
    request = ask_request(idempotency_key=key, arguments=arguments)
    return handler(request, CommandTurnIdentity(None, "space-turn", "turn", "broker"))


def _tap(client, store, question_id, decision="approve"):
    card = store.approval_card(question_id)
    return client.post(
        f"/api/approval-cards/{question_id}",
        json={"decision": decision, "pinned_sha256": card["pinned_sha256"]},
    )


def test_a_space_ask_raises_a_card_in_pips_dock_that_decides_the_proposal(
    questions_api, monkeypatch
):
    app, client, store, project_id = questions_api
    service = app.state.catalog.open(project_id)
    file_proposal(service)
    handler = _space_turn(questions_api)
    asked = _space_ask(handler, approve_action(project_id, PROPOSAL))
    assert asked.status == "ok", asked.message
    question_id = asked.result["question_id"]
    dock = client.get("/api/agent-threads/cards").json()["asked_cards"]
    assert [(row["question_id"], row["project_name"]) for row in dock] == [
        (question_id, store.project(project_id).name)
    ]
    bounded_admission_lock(monkeypatch)
    tapped = _tap(client, store, question_id)
    assert tapped.json()["outcome"] == "approved", tapped.text
    assert service.history.state().proposals[PROPOSAL].status == "approved"
    assert _space_ask(handler, approve_action(project_id, PROPOSAL)).result["state"] == "answered"


def test_a_space_ask_carries_any_inbox_card_kind_for_a_member_project_only(
    questions_api, monkeypatch
):
    app, _client, store, project_id = questions_api
    service = app.state.catalog.open(project_id)
    writable = "repo-b" in service.history.state().project_truth_scope
    truth = {"kind": "truth_scope", "project_id": project_id, "alias": "repo-b"}
    handler = _space_turn(questions_api)
    created = _space_ask(handler, {**truth, "writable": not writable})
    assert created.status == "ok", created.message
    assert store.approval_card(created.result["question_id"])["kind"] == "truth_scope"
    opened = []
    monkeypatch.setattr(app.state.catalog, "open_execution", opened.append)
    refused = [
        _space_ask(handler, {**truth, "project_id": "not-a-member", "writable": True}, "other"),
        _space_ask(
            handler, {"kind": "auto_research", "project_id": project_id, "invocation_ceiling": 1}
        ),
        _space_ask(handler, None, "plain"),
    ]
    assert [response.status for response in refused] == ["invalid"] * 3
    assert opened == [project_id]


def test_a_space_ask_is_refused_for_a_proposal_card_its_dock_already_draws(questions_api):
    _app, _client, _store, project_id = questions_api
    _raised_proposals(questions_api)
    handler = _space_turn(questions_api)
    drawn = _space_ask(handler, approve_action(project_id, "prop/raised/first"))
    assert (drawn.status, drawn.message) == ("invalid", "proposal_card_shown")
    other = _space_ask(handler, approve_action(project_id, "prop/raised/other"), "other")
    assert other.status == "ok", other.message


def _branch_with_proposal(questions_api) -> str:
    """A graph branch forked from main while main's Proposal is pending."""
    from rcp.core.models import GraphBranchMetadata
    from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
    from tests.helpers import authorized_human

    app, _client, _store, project_id = questions_api
    service = app.state.catalog.open(project_id)
    file_proposal(service)
    base = service.history.head_ref()
    branch_id = str(uuid4())
    service.history.create_branch(
        GraphBranchMetadata(
            branch_id=branch_id,
            episode_id=branch_id,
            project_id=project_id,
            base_head=base,
            head=GraphHeadRef(
                target=GraphTargetRef(kind="branch", branch_id=branch_id),
                revision=base.revision,
                transition_id=base.transition_id,
            ),
            authorized_by=authorized_human(app),
        )
    )
    return branch_id


@pytest.mark.parametrize("decided_first", [False, True])
def test_a_branch_proposal_card_decides_it_on_its_branch(questions_api, monkeypatch, decided_first):
    from rcp.core.transition_models import GraphTargetRef

    app, client, store, project_id = questions_api
    branch_id = _branch_with_proposal(questions_api)
    main = app.state.catalog.open(project_id)
    branch = main.for_graph_target(GraphTargetRef(kind="branch", branch_id=branch_id))
    action = {**approve_action(project_id, PROPOSAL), "branch_id": branch_id}
    question, card = real_card(questions_api, action)
    assert card["pinned"]["proposal"]["id"] == PROPOSAL
    bounded_admission_lock(monkeypatch)
    if decided_first:
        inbox = client.post(
            f"/api/projects/{project_id}/sync",
            params={"branch_id": branch_id},
            json={
                "base_revision": branch.history.state().revision,
                "proposals": [{"proposal_id": PROPOSAL, "decision": "rejected"}],
            },
        )
        assert inbox.status_code == 200, inbox.text
    tapped = _tap(client, store, question.question_id)
    assert tapped.json()["outcome"] == ("stale" if decided_first else "approved"), tapped.text
    expected = "rejected" if decided_first else "approved"
    assert branch.history.state().proposals[PROPOSAL].status == expected
    assert main.history.state().proposals[PROPOSAL].status == "pending"


def test_an_old_proposal_card_row_without_a_branch_settles_on_main(questions_api):
    """A row stored before `branch_id` existed parses and settles from main's status."""
    app, client, store, project_id = questions_api
    service = app.state.catalog.open(project_id)
    file_proposal(service)
    question, card = real_card(questions_api, approve_action(project_id, PROPOSAL))
    assert "branch_id" not in card["action"]
    assert store.claim_approval_card(question.question_id)
    assert _decide(client, project_id, PROPOSAL).status_code == 200
    reopened = AppStore(store.path)
    settle_interrupted_approval_cards(
        reopened, service_for=app.state.catalog.open, claimed_before=reopened.now()
    )
    assert reopened.approval_card(question.question_id)["result"]["outcome"] == "approved"
