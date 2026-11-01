from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import cast

import pytest
from pydantic import ValidationError

from rcp.agents.command_protocol import AskArguments
from rcp.projects import ProjectCatalog
from rcp.service import ProjectService
from rcp.storage import AppStore
from rcp.storage.approval_cards import card_sha256
from tests import test_api_questions as api_fixtures
from tests import test_questions_store as fixtures

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
    reopened.fail_interrupted_approval_cards()
    answered = reopened.get_question(question.question_id)
    assert answered is not None and answered.answer == "failed: interrupted"
    card = reopened.approval_card(question.question_id)
    assert card is not None and card["result"]["outcome"] == "failed"


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


def api_card(questions_api, action=None):
    app, client, store, project_id = questions_api
    with store.connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO agent_threads VALUES (?,?,?,?,?,NULL,NULL)",
            ("private", store.local_owner.user_id, "project", project_id, store.now()),
        )
    action = action or {
        "kind": "truth_scope",
        "project_id": project_id,
        "alias": "repo-a",
        "writable": False,
    }
    old = api_fixtures._question(store, project_id, owner_id="private", key="old")
    question = store.create_or_get_question(
        origin=old.origin,
        key="card",
        question="Allow?",
        choices=["Approve", "Decline"],
        card={"action": action, "pinned": {"head": "original"}},
    )
    return question, store.approval_card(question.question_id)


@pytest.mark.parametrize(
    "action",
    [
        {"kind": "truth_scope", "alias": "repo-a", "writable": False},
        {"kind": "auto_research", "invocation_ceiling": 1},
        {"kind": "merge_branch", "branch_id": "b"},
    ],
)
def test_each_action_rechecks_transfer_admission(questions_api, monkeypatch, action):
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
    assert response.status_code == 200
    assert response.json() == {"outcome": "failed", "reason": "project_transfer_in_progress"}
    assert calls == []
    assert store.get_question(question.question_id).answer == "failed: project_transfer_in_progress"


@pytest.mark.parametrize("changed", ["client", "live", "none"])
def test_taps_recheck_pins_and_run_only_once(questions_api, monkeypatch, changed):
    from rcp.api import approval_cards

    app, client, store, project_id = questions_api
    question, card = api_card(questions_api)
    calls = []
    monkeypatch.setattr(
        approval_cards,
        "approval_pins",
        lambda *args: {"head": "changed" if changed == "live" else "original"},
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
    import sqlite3

    from rcp.storage.approval_cards import migrate_approval_cards
    from rcp.storage.questions import migrate_questions

    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        migrate_questions(connection)
        # Old writer SQL has neither action nor card; the migration must preserve it.
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
            "INSERT INTO agent_threads VALUES ('private',?,'project','project',?,NULL,NULL)",
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
    # Episode merge retains its native UUID contract; cards still mint UUID4 keys.
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
    monkeypatch.setattr(approval_cards, "approval_pins", lambda *args: {"writable": True})
    handler = work_command_handler(
        execution, None, service=cast(ProjectService, SimpleNamespace(project_catalog=None))
    )
    assert handler(request, identity()).status == "invalid"
    assert store.list_questions() == []
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_threads VALUES ('chat',?,'project','project',?,NULL,NULL)",
            (human.user_id, store.now()),
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
    # Keep the real route and fence; bound lock acquisition so a regression fails
    # instead of leaving pytest stuck inside a non-reentrant lock.
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


def real_card(api, action):
    from pydantic import TypeAdapter

    from rcp.approval_actions import ApprovalAction
    from rcp.runs.approval_cards import approval_pins
    from tests.helpers import authorized_human

    app, client, store, project_id = api
    question, _ = api_card(api, action)
    pins = approval_pins(
        app.state.catalog.open(project_id),
        store,
        TypeAdapter(ApprovalAction).validate_python(action),
        authorized_human(app),
    )
    import json

    with store.connection() as connection:
        connection.execute(
            "UPDATE approval_cards SET pinned_json=?,pinned_sha256=? WHERE question_id=?",
            (json.dumps(pins), card_sha256(action, pins), question.question_id),
        )
    return question, store.approval_card(question.question_id)


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
        assert response.json() == {"outcome": "failed", "reason": "project_transfer_in_progress"}
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
        assert response.json() == {"outcome": "failed", "reason": "project_transfer_in_progress"}
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
    assert response.json()["result"]["accepted_merge_task"]["operation_id"] == attempt.attempt_id


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
        # Advance after the card's optimistic recheck, just as a writer that
        # already holds admission can do while the merge waits for its lock.
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
    monkeypatch.setattr(approval_cards, "approval_pins", lambda *args: {"head": "original"})
    response = client.post(
        f"/api/approval-cards/{question.question_id}",
        json={"decision": "approve", "pinned_sha256": card["pinned_sha256"]},
    )
    assert response.json()["outcome"] == "approved", response.text
    assert seen == [UUID(card["request_id"])]
    assert seen[0].version == 4
