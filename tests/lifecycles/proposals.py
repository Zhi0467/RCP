"""Proposals and their approval cards.

A world is one real backend over the explorer's tree: the app, its store and
the project's graph history. The start state is the graph ``file_proposal``
seeds (one pending Proposal ``P0`` whose cause is an Evidence edge), the
member's running Work turn in their private thread, and one card ``C0`` that
approves ``P0``, built by ``real_card`` over the ``api`` tuple of
``tests/test_approval_cards.py`` without its client. ``boot`` applies two
instance patches: ``reconcile_question_answers`` is a ``Mock`` and ``store.now``
reads the explorer clock. ``seams`` points the ``datetime`` that
``rcp.storage.rows`` reads at the same clock.

Member actions call the route handlers in the explorer thread with the
dependencies FastAPI resolves and the owner already on the request, as the
owner-session middleware leaves it. Through the test client each request would
rebuild the fresh app's routes and open SQLite handles on new threads, about
0.4 s a tap. ``sync`` decides ``P0`` through ``sync_graph``. ``tap`` calls
``resolve_approval_card`` to approve or decline, during an outage
(``rcp.api.approval_cards.approval_pins`` raises ``StateUnavailable``), or with
its process stopping right after the claim or right after the action committed.

Agent actions apply a Patch from that Work turn through ``_apply_work_patch``,
so agent authority is the product's: ``move`` rewords the cause Evidence, which
makes ``P0`` stale, and ``withdraw`` withdraws it. Once a card has settled,
``ask`` asks a second card ``C1`` for ``P0`` through ``handle_ask``, as a
declined, stale or expired card tells the agent to.

``sweep`` runs the five-second expiry sweep after a tick, or once the clock has
passed every card's expiry; ``close`` closes the private thread. After a
crashed tap the only action is ``restart``, which runs what ``api/app.py`` runs
at startup for these tables, in its order:
``settle_interrupted_approval_cards(claimed_before=<clock>)``,
``expire_approval_cards`` and ``reconcile_question_answers``. Elsewhere a
restart would only run the sweep.

Views name the Proposal ``P0`` and the cards ``C0`` and ``C1`` by lineage. The
goal counts restart as allowed: settling a card a crashed tap left claimed is
restart's job.

Budget cuts: a decline, the outage and crashed taps, the Sync rejection and
``close`` are offered only while the card's Proposal is pending and fresh, and a
closed thread offers only sweeps; elsewhere they add only edges whose rules
the fresh case already exercises. Refiling, refresh and the old-hash tap are
left out: no rule reads them, and a refiled Proposal's card would need three
steps. Each action is atomic, so a write inside one tap, between its pin check
and its Sync, is out of reach here;
``test_a_write_between_pin_check_and_sync_never_decides_unseen_pins`` in
``tests/test_approval_cards.py`` interleaves it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple
from unittest import mock

from rcp.approval_actions import ApprovalOutcome
from rcp.core.models import Patch, Proposal
from rcp.limits import APPROVAL_CARD_TTL_HOURS
from rcp.machine_upgrade import _migrate_question_origins
from rcp.runs.approval_cards import settle_interrupted_approval_cards
from rcp.storage.approval_cards import ApprovalCardStoreMixin, _settle, insert_approval_card
from rcp.storage.episodes import EpisodeStoreMixin
from rcp.storage.experiments import ExperimentStoreMixin
from rcp.storage.projects import ProjectStoreMixin
from rcp.storage.question_models import QuestionRecord
from rcp.storage.questions import QuestionStoreMixin
from rcp.storage.restore_detachment import RestoreDetachmentStoreMixin
from tests import test_api_questions as api_fixtures
from tests.lifecycles.engine import RESTART, Action, Clock, Goal, Memory, Safety, Step

HYP = "hyp/replanning-restores-plasticity"
EV = "ev/replan-ablation"
PROPOSAL = "prop/activate-replanning"
_PROPOSALS = {PROPOSAL: "P0"}
_CARDS = {"card": "C0", "ask-again": "C1"}
_THREAD = "private"
_AGENT_TURN = "agent-turn"
_TICK = 5

ProposalStatus = Proposal.model_fields["status"].annotation
QuestionState = QuestionRecord.model_fields["state"].annotation


def append_agent_patch(service, ops, *, source_operation_id: str | None = None) -> None:
    """Append an agent Patch as fixture history, without a Work turn's checks."""

    from tests.helpers import append_fixture_patch

    append_fixture_patch(
        service,
        Patch(
            kind="refresh",
            author="agent",
            summary="Agent change.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            source_operation_id=source_operation_id,
            ops=ops,
        ),
    )


def file_proposal(service) -> None:
    """Seed the graph, the replanning Evidence edge and one pending Proposal citing it."""

    from tests.helpers import append_fixture_patch, seed_patch

    append_fixture_patch(service, seed_patch())
    append_agent_patch(
        service,
        [
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": EV,
                        "type": "evidence",
                        "title": "Replanning ablation",
                        "observation": "Replanning recovers most plasticity.",
                        "origin": "analytic",
                    }
                ],
            },
            {
                "op": "create_edges",
                "edges": [
                    {
                        "id": "edge/replan",
                        "source": EV,
                        "target": HYP,
                        "relation": "supports",
                        "assessment": {"relevance": "direct", "weight": "moderate"},
                    }
                ],
            },
        ],
    )
    append_agent_patch(service, [proposal_filing(PROPOSAL)])


def proposal_filing(proposal_id: str) -> dict[str, Any]:
    """The operation that files one pending Proposal activating the replanning hypothesis."""

    return {
        "op": "create_proposals",
        "proposals": [
            {
                "id": proposal_id,
                "title": "Activate the replanning hypothesis",
                "card": {
                    "situation_cold": "s",
                    "why_human_now": "w",
                    "consequences": "c",
                    "decision_needed": "d",
                },
                "ops": [
                    {
                        "op": "update_nodes",
                        "intent": "status_change",
                        "nodes": [
                            {
                                "id": HYP,
                                "changes": {"status": "active"},
                                "cause": {"kind": "evidence_edge", "ref_id": "edge/replan"},
                            }
                        ],
                    }
                ],
            }
        ],
    }


def api_card(api, action=None):
    """A card question in the member's private thread, pinned to a placeholder head."""

    _app, _client, store, project_id = api
    with store.connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO agent_threads(thread_id,user_id,kind,project_id,created_at) "
            "VALUES (?,?,?,?,?)",
            (_THREAD, store.local_owner.user_id, "project", project_id, store.now()),
        )
    action = action or {
        "kind": "truth_scope",
        "project_id": project_id,
        "alias": "repo-a",
        "writable": False,
    }
    old = api_fixtures._question(store, project_id, owner_id=_THREAD, key="old")
    question = store.create_or_get_question(
        origin=old.origin,
        key="card",
        question="Allow?",
        choices=["Approve", "Decline"],
        card={"action": action, "pinned": {"head": "original"}},
    )
    return question, store.approval_card(question.question_id)


def real_card(api, action):
    """An ``api_card`` whose pins are the action's live pins, as ask would pin them."""

    from pydantic import TypeAdapter

    from rcp.approval_actions import ApprovalAction
    from rcp.runs.approval_cards import approval_pins
    from rcp.storage.approval_cards import card_sha256
    from tests.helpers import authorized_human

    app, _client, store, project_id = api
    question, _ = api_card(api, action)
    pins = approval_pins(
        app.state.catalog.open(project_id),
        store,
        TypeAdapter(ApprovalAction).validate_python(action),
        authorized_human(app),
    )
    with store.connection() as connection:
        connection.execute(
            "UPDATE approval_cards SET pinned_json=?,pinned_sha256=? WHERE question_id=?",
            (json.dumps(pins), card_sha256(action, pins), question.question_id),
        )
    return question, store.approval_card(question.question_id)


def approve_action(project_id: str, proposal_id: str) -> dict[str, str]:
    """The card action that approves one Proposal."""

    return {
        "kind": "proposal",
        "project_id": project_id,
        "proposal_id": proposal_id,
        "decision": "approved",
    }


class View(NamedTuple):
    proposals: tuple
    hypothesis: str
    cards: tuple
    closed: bool


class _Crash(Exception):
    """The tapping process stopped before its card settled."""


@dataclass
class World:
    """One backend over the tree, with the action it is running."""

    app: Any
    clock: Clock
    action: Action | None = None
    revision: int = 0
    pinned: dict[str, str] = field(default_factory=dict)
    seen: View | None = None
    reply: Any = None

    @property
    def store(self):
        return self.app.state.background_tasks.store

    @property
    def project_id(self) -> str:
        return self.app.state.default_project_id

    @property
    def service(self):
        return self.app.state.catalog.open(self.project_id)

    def graph(self):
        return self.service.history.state()

    def card_ids(self) -> dict[str, str]:
        """Each card's lineage name to its question id."""

        return {
            _CARDS[question.key]: question.question_id
            for question in self.store.list_questions(
                project_id=self.project_id, owner_kind="chat", owner_id=_THREAD
            )
            if question.key in _CARDS
        }

    def request(self):
        """A request from the signed-in owner, as the owner-session middleware leaves it."""

        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "app": self.app,
                "method": "POST",
                "path": "/",
                "headers": [],
                "query_string": b"",
                "client": ("explorer", 0),
                "server": ("testserver", 80),
                "scheme": "http",
                "root_path": "",
            }
        )
        request.state.owner_user = self.store.local_owner
        return request


class Proposals:
    name = "proposals"
    bound = 2
    horizon = 1
    covers = {
        ProposalStatus: ("proposals", 1),
        ApprovalOutcome: ("cards", 3),
        QuestionState: ("cards", 4),
    }
    tables = frozenset({"approval_cards", "questions"})
    writers = (
        ApprovalCardStoreMixin.claim_approval_card,
        ApprovalCardStoreMixin.record_declined_rejection,
        ApprovalCardStoreMixin.release_approval_card,
        ApprovalCardStoreMixin.take_expired_approval_card,
        _settle,
        insert_approval_card,
        QuestionStoreMixin.create_or_get_question,
        QuestionStoreMixin._resolve_question,
        QuestionStoreMixin.claim_question_followup,
        QuestionStoreMixin.mark_question_answer_projected,
        QuestionStoreMixin.mark_question_dismissal_delivered,
        QuestionStoreMixin.record_question_continuation_receipt,
        QuestionStoreMixin.record_question_receipt,
        QuestionStoreMixin.reopen_unclaimed_answer,
        QuestionStoreMixin.set_episode_chain_questions_withdrawn_in_connection,
        QuestionStoreMixin.set_episode_questions_withdrawn,
        QuestionStoreMixin.withdraw_episode_question_if_stopped,
        EpisodeStoreMixin._withdraw_episode_question_chain,
        ExperimentStoreMixin.create_experiment_episode_with_invocation,
        ProjectStoreMixin.delete_project_records,
        RestoreDetachmentStoreMixin.detach_restored_lifecycle,
        _migrate_question_origins,
    )
    unreached_values = {
        "dismissed": "a card question cannot be dismissed; the route refuses plain resolution",
    }
    _DELIVERY = "answer delivery runs in reconcile_question_answers, which boot replaces"
    _EPISODE = "episode questions belong to Experiment episodes; the episode explorer drives them"
    unreached_writers = {
        QuestionStoreMixin._resolve_question: (
            "a card is answered only by its tap; the route refuses a plain answer or dismissal"
        ),
        QuestionStoreMixin.claim_question_followup: _DELIVERY,
        QuestionStoreMixin.mark_question_answer_projected: _DELIVERY,
        QuestionStoreMixin.mark_question_dismissal_delivered: _DELIVERY,
        QuestionStoreMixin.record_question_continuation_receipt: _DELIVERY,
        QuestionStoreMixin.record_question_receipt: _DELIVERY,
        QuestionStoreMixin.reopen_unclaimed_answer: _DELIVERY,
        QuestionStoreMixin.set_episode_chain_questions_withdrawn_in_connection: _EPISODE,
        QuestionStoreMixin.set_episode_questions_withdrawn: _EPISODE,
        QuestionStoreMixin.withdraw_episode_question_if_stopped: _EPISODE,
        _migrate_question_origins: "a migration runs when a store opens, never inside an action",
        EpisodeStoreMixin._withdraw_episode_question_chain: _EPISODE,
        ExperimentStoreMixin.create_experiment_episode_with_invocation: _EPISODE,
        ProjectStoreMixin.delete_project_records: (
            "project deletion is the agent task explorer's action"
        ),
        RestoreDetachmentStoreMixin.detach_restored_lifecycle: "restore is a catalog machine",
    }

    def __init__(self) -> None:
        self.manifest_path: Path | None = None
        self.clock = Clock()

    @contextmanager
    def seams(self) -> Iterator[None]:
        machine = self

        class ExplorerDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return machine.clock.now.astimezone(tz)

        with mock.patch("rcp.storage.rows.datetime", ExplorerDatetime):
            yield

    def databases(self, root: Path) -> list[Path]:
        return [root / "data" / "rcp.sqlite3"]

    def start(self, root: Path, clock: Clock) -> World:
        from tests.helpers import write_test_manifest

        self.manifest_path = write_test_manifest(root).path
        world = self.boot(root, clock)
        file_proposal(world.service)
        _seed_agent_turn(world)
        api = (world.app, None, world.store, world.project_id)
        real_card(api, approve_action(world.project_id, PROPOSAL))
        return world

    def boot(self, root: Path, clock: Clock) -> World:
        from tests.helpers import create_named_app

        assert self.manifest_path is not None
        app = create_named_app(str(self.manifest_path), data_dir=root / "data")
        app.state.reconcile_question_answers = mock.Mock()
        app.state.background_tasks.store.now = clock.iso
        self.clock = clock
        return World(app, clock)

    def close(self, world: World) -> None:
        world.store.close()

    def save_memory(self, world: World) -> Memory:
        return Memory(None, None)

    def restore_memory(self, world: World, memory: Memory) -> None:
        return None

    def actions(self, view: View) -> list[Action]:
        if any(_crashed(row) for row in view.cards):
            return [Action(RESTART)]
        open_cards = [row for row in view.cards if row[3] is None]
        if view.closed:
            return [Action("sweep", (wait,)) for wait in ("tick", "expiry") if open_cards]
        proposals = _proposals(view)
        pending = [name for name, row in proposals.items() if row[1] == "pending"]
        found: list[Action] = []
        if any(not proposals[name][2] for name in pending):
            found.append(Action("move"))
        found.extend(Action("withdraw", (name,)) for name in pending)
        for name, row in proposals.items():
            decisions = ("approved", "rejected") if row[1] == "pending" else ("rejected",)
            found.extend(
                Action("decide", (name, decision), environment=False) for decision in decisions
            )
        if not open_cards and len(view.cards) < len(_CARDS):
            found.append(Action("ask", ("P0",)))
        for name in pending:
            decisions = ("approved",) if proposals[name][2] else ("approved", "rejected")
            found.extend(
                Action("sync", (name, decision), environment=False) for decision in decisions
            )
        fresh = {row[0] for row in open_cards if proposals[row[1]][1:3] == ("pending", False)}
        for name, _proposal, claimed, outcome, _question, _past in view.cards:
            taps: tuple[str, ...] = ()
            if outcome is None:
                taps = ("approve", *(_FRESH_TAPS if name in fresh else ()))
            elif outcome == "expired" and not claimed:
                taps = ("approve",)
            found.extend(Action("tap", (name, tap), environment=False) for tap in taps)
        if open_cards:
            found.append(Action("sweep", ("expiry",)))
        if fresh:
            found.append(Action("close"))
        return found

    def apply(self, world: World, action: Action) -> None:
        world.action = action
        world.seen = None
        world.reply = None
        world.revision = world.graph().revision
        world.pinned = {
            name: row["pinned_sha256"]
            for name, question_id in world.card_ids().items()
            if (row := _card_row(world, question_id)) is not None
        }
        if action.name == "move":
            _agent_work(world, [_reword(world)])
        elif action.name == "withdraw":
            withdrawal = {"id": _proposal_id(action.args[0]), "reason": "Refiling."}
            _agent_work(world, [{"op": "withdraw_proposals", "proposals": [withdrawal]}])
        elif action.name == "ask":
            _ask(world, _proposal_id(action.args[0]))
        elif action.name == "sync":
            _sync(world, _proposal_id(action.args[0]), action.args[1])
        elif action.name == "decide":
            _decide(world, _proposal_id(action.args[0]), action.args[1])
        elif action.name == "tap":
            _TAPS[action.args[1]](world, action.args[0])
        elif action.name == "sweep":
            wait = APPROVAL_CARD_TTL_HOURS * 3600 + 1 if action.args[0] == "expiry" else _TICK
            world.clock.advance(wait)
            for project_id in world.store.expire_approval_cards():
                world.app.state.reconcile_question_answers(project_id)
        elif action.name == "close":
            world.store.close_agent_thread(_THREAD, reason="access_lost")
        else:
            raise ValueError(f"unknown action {action}")

    def restart(self, world: World) -> None:
        world.action = Action(RESTART)
        world.seen = None
        world.revision = world.graph().revision
        store = world.store
        settle_interrupted_approval_cards(
            store, service_for=world.app.state.catalog.open, claimed_before=world.clock.iso()
        )
        store.expire_approval_cards()
        world.app.state.reconcile_question_answers()

    def view(self, world: World) -> View:
        return _view(world)

    @property
    def rules(self):
        return RULES


def _view(world: World) -> View:
    """The world's view, read once per state: the rules read it after the action too."""

    if world.seen is None:
        world.seen = _read_view(world)
    return world.seen


def _read_view(world: World) -> View:
    from rcp.core.validation.proposals import proposal_is_stale

    state = world.graph()
    proposals = tuple(
        sorted(
            (
                _PROPOSALS[proposal.id],
                proposal.status,
                proposal.status == "pending" and proposal_is_stale(state, proposal),
                proposal.resolved_by,
            )
            for proposal in state.proposals.values()
        )
    )
    cards = []
    for name, question_id in sorted(world.card_ids().items()):
        question = world.store.get_question(question_id)
        row = _card_row(world, question_id)
        assert question is not None and row is not None
        action = json.loads(row["action_json"])
        result = json.loads(row["result_json"]) if row["result_json"] else None
        cards.append(
            (
                name,
                _PROPOSALS[action["proposal_id"]],
                row["running_since"] is not None,
                result["outcome"] if result else None,
                question.state,
                world.clock.iso() >= row["expires_at"],
            )
        )
    thread = world.store.agent_thread(_THREAD)
    return View(
        proposals,
        state.nodes[HYP].status,
        tuple(cards),
        thread is not None and thread.closed_at is not None,
    )


def _card_row(world: World, question_id: str):
    """The raw card row, read without the expiring read ``approval_card`` performs."""

    with world.store.connection() as connection:
        return connection.execute(
            "SELECT * FROM approval_cards WHERE question_id=?", (question_id,)
        ).fetchone()


def _crashed(row) -> bool:
    return row[2] and row[3] is None


def _proposal_id(name: str) -> str:
    return next(proposal_id for proposal_id, known in _PROPOSALS.items() if known == name)


def _reword(world: World) -> dict[str, Any]:
    return {
        "op": "update_nodes",
        "nodes": [{"id": EV, "changes": {"observation": f"Narrower at {world.revision}."}}],
    }


def _seed_agent_turn(world: World) -> None:
    """The running Work turn in the member's private thread whose Patches the agent actions apply."""

    from rcp.service import RunRequest, resolve_dispatch_authority
    from rcp.storage import AgentTaskRecord
    from tests.helpers import authorized_human

    store = world.store
    request = RunRequest(
        chat_scope="project",
        chat_id=_THREAD,
        message="Refile the Proposal.",
        mode="work",
        provider="codex",
        run_truth_scope=["repo-a"],
    )
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=_AGENT_TURN,
            project_id=world.project_id,
            kind="project_chat",
            status="running",
            request=request.model_dump(mode="json"),
            created_at=store.now(),
            updated_at=store.now(),
            status_message="Running",
            authorized_by=authorized_human(world.app),
            dispatch_authority=resolve_dispatch_authority("project_chat", request),
        )
    )


def _agent_work(world: World, ops: list[dict[str, Any]]) -> None:
    """Apply one Patch from the agent's Work turn; a rejected Patch changes nothing."""

    from rcp.runs.tasks.work import _apply_work_patch

    action = world.action
    assert action is not None
    _apply_work_patch(
        world.service,
        None,
        json.dumps({"summary": "Agent change.", "ops": ops, "repositories_read": ["repo-a"]}),
        run_truth_scope=["repo-a"],
        read_scope=None,
        source_operation_id=_AGENT_TURN,
        source_effect_id=f"{action.name}-{world.revision}",
    )


def _ask(world: World, proposal_id: str) -> None:
    """The agent asks a new card approving ``proposal_id``, from the start card's turn."""

    from rcp.runs.questions import handle_ask
    from tests.helpers import authorized_human
    from tests.test_ask_protocol import ask_request

    question = world.store.get_question(world.card_ids()["C0"])
    assert question is not None
    request = ask_request(
        idempotency_key=next(key for key, name in _CARDS.items() if name == "C1"),
        arguments={
            "question": "Approve the Proposal?",
            "action": approve_action(world.project_id, proposal_id),
        },
    )
    handle_ask(
        world.store,
        request,
        question.origin,
        service=world.service,
        approval_card_member=authorized_human(world.app),
    )


def _sync(world: World, proposal_id: str, decision: str) -> None:
    from fastapi import HTTPException

    from rcp.api.dependencies import (
        get_catalog,
        get_identity_access,
        get_project_display_cache,
        get_store,
        get_watcher_delivery,
    )
    from rcp.api.sync import sync_graph
    from rcp.history import PatchRejected
    from rcp.service import GraphSyncRequest

    request = world.request()
    body = GraphSyncRequest.model_validate(
        {
            "base_revision": world.revision,
            "proposals": [{"proposal_id": proposal_id, "decision": decision}],
        }
    )
    try:
        sync_graph(
            world.project_id,
            body,
            request,
            catalog=get_catalog(request),
            store=get_store(request),
            identity_access=get_identity_access(request),
            project_display_cache=get_project_display_cache(request),
            watcher_delivery=get_watcher_delivery(request),
        )
    except (HTTPException, PatchRejected):
        return


def _decide(world: World, proposal_id: str, decision: str) -> None:
    """A member's tap on the derived Proposal card, from a browser that may lag the Inbox."""

    from fastapi import HTTPException

    from rcp.api.approval_cards import decide_proposal

    try:
        world.reply = decide_proposal(world.request(), world.project_id, proposal_id, decision)
    except HTTPException as exc:
        world.reply = exc.detail


def _resolve(world: World, name: str, decision: str = "approve") -> None:
    from fastapi import HTTPException

    from rcp.api.approval_cards import ResolveApprovalBody, resolve_approval_card
    from rcp.api.dependencies import get_catalog, get_identity_access, get_store

    request = world.request()
    body = ResolveApprovalBody(decision=decision, pinned_sha256=world.pinned[name])
    try:
        world.reply = resolve_approval_card(
            world.card_ids()[name],
            body,
            request,
            store=get_store(request),
            catalog=get_catalog(request),
            identity_access=get_identity_access(request),
        )
    except HTTPException:
        return


def _decline(world: World, name: str) -> None:
    _resolve(world, name, "decline")


def _outage(world: World, name: str) -> None:
    from rcp.transport.state import StateUnavailable

    def offline(*_args, **_kwargs):
        raise StateUnavailable("base unreachable")

    with mock.patch("rcp.api.approval_cards.approval_pins", offline):
        _resolve(world, name)


def _stopped(world: World, name: str, target: str) -> None:
    """A tap whose process stops where ``target`` would run."""

    def stop(*_args, **_kwargs):
        raise _Crash

    with mock.patch(target, stop):
        try:
            _resolve(world, name)
        except _Crash:
            return
    raise AssertionError(f"the tap never reached {target}")


def _stop_after_claim(world: World, name: str) -> None:
    _stopped(world, name, "rcp.api.approval_cards._resolve")


def _stop_after_commit(world: World, name: str) -> None:
    _stopped(world, name, "rcp.storage.AppStore.settle_approval_card")


_TAPS = {
    "approve": _resolve,
    "decline": _decline,
    "outage": _outage,
    "stop-after-claim": _stop_after_claim,
    "stop-after-commit": _stop_after_commit,
}
_FRESH_TAPS = ("decline", "outage", "stop-after-claim", "stop-after-commit")


def _always(*_args) -> bool:
    return True


def _proposals(view) -> dict[str, tuple]:
    return {row[0]: row for row in view.proposals}


def _cards(view) -> dict[str, tuple]:
    return {row[0]: row for row in view.cards}


def cards_answer_their_question(world: World) -> list[str]:
    """A settled card's question is answered with its outcome; an open card's stays pending."""

    found = []
    for name, question_id in world.card_ids().items():
        row = _card_row(world, question_id)
        question = world.store.get_question(question_id)
        assert row is not None and question is not None
        result = json.loads(row["result_json"]) if row["result_json"] else None
        if result is None and question.state != "pending":
            found.append(f"{name} is open but its question is {question.state}")
        if result is not None:
            outcome = result["outcome"]
            expected = f"failed: {result.get('reason')}" if outcome == "failed" else outcome
            if (question.state, question.answer) != ("answered", expected):
                found.append(f"{name} settled {outcome} but its question reads {question.answer}")
    return found


def only_humans_resolve(before, world: World) -> list[str]:
    """Only a member's Sync or card tap approves or rejects a Proposal; agents only withdraw."""

    action = world.action
    assert action is not None
    found = []
    for name, row in _proposals(_view(world)).items():
        previous = _proposals(before).get(name)
        if previous is None or previous[1] == row[1]:
            continue
        if row[1] in {"approved", "rejected"}:
            if action.environment or row[3] != "human":
                found.append(f"{name} {row[1]} by {row[3]} on {action}")
        elif row[1] == "withdrawn" and action.environment and action.name != "withdraw":
            found.append(f"{name} withdrawn on {action}")
    return found


def _status_moved(before, world: World) -> bool:
    after = _proposals(_view(world))
    return any(after[name][1] != row[1] for name, row in _proposals(before).items())


def status_changes_only_from_pending(before, world: World) -> list[str]:
    """A Proposal's status changes only while it is pending."""

    after = _proposals(_view(world))
    return [
        f"{name} moved from {row[1]} to {after[name][1]}"
        for name, row in _proposals(before).items()
        if row[1] != "pending" and after[name][1] != row[1]
    ]


def _resolved_before(before, _world: World) -> bool:
    return any(row[1] != "pending" for row in before.proposals)


def _approved_proposal(before, action: Action) -> str | None:
    """The Proposal a member action approves, when that action is an approval."""

    if action.name == "sync" and action.args[1] == "approved":
        return action.args[0]
    if action.name == "tap" and action.args[1] in {"approve", "stop-after-commit"}:
        card = _cards(before).get(action.args[0])
        return card[1] if card is not None and card[3] is None else None
    return None


def stale_approval_applies_nothing(before, world: World) -> list[str]:
    """Approving a stale Proposal applies none of its changes, and Sync withdraws it."""

    action = world.action
    assert action is not None
    name = _approved_proposal(before, action)
    assert name is not None
    view = _view(world)
    found = []
    if view.hypothesis != before.hypothesis:
        found.append(f"stale {name} changed the hypothesis to {view.hypothesis}")
    status = _proposals(view)[name][1]
    if action.name == "sync" and status != "withdrawn":
        found.append(f"Sync left stale {name} {status}")
    return found


def _approves_stale(before, world: World) -> bool:
    action = world.action
    assert action is not None
    name = _approved_proposal(before, action)
    return name is not None and _proposals(before)[name][1:3] == ("pending", True)


def cards_are_single_use(before, world: World) -> list[str]:
    """A settled card keeps its outcome and its question's answer."""

    after = _cards(_view(world))
    return [
        f"{name} moved from {row[3]}/{row[4]} to {after[name][3]}/{after[name][4]}"
        for name, row in _cards(before).items()
        if row[3] is not None and (after[name][3], after[name][4]) != (row[3], row[4])
    ]


def _a_card_settled_before(before, _world: World) -> bool:
    return any(row[3] is not None for row in before.cards)


def stale_only_when_pins_change(before, world: World) -> list[str]:
    """An approving tap settles stale exactly when its Proposal is no longer pending and fresh."""

    action = world.action
    assert action is not None
    name = action.args[0]
    card = _cards(_view(world))[name]
    proposal = _proposals(before)[card[1]]
    moved = proposal[1] != "pending" or proposal[2]
    if (card[3] == "stale") != moved:
        return [f"{name} settled {card[3]} with its Proposal {proposal[1]}, stale={proposal[2]}"]
    return []


def _taps_an_open_card(before, world: World) -> bool:
    action = world.action
    assert action is not None
    if action.name != "tap" or action.args[1] != "approve":
        return False
    card = _cards(before).get(action.args[0])
    return card is not None and card[3] is None and not card[2]


def outage_releases_the_card(before, world: World) -> list[str]:
    """A tap during a state outage leaves its card open for another tap and changes nothing."""

    action = world.action
    assert action is not None
    name = action.args[0]
    card = _cards(_view(world))[name]
    found = []
    if card[2] or card[3] is not None or card[4] != "pending":
        found.append(f"{name} is {card} after an outage")
    if world.graph().revision != world.revision:
        found.append("an outage tap changed the graph")
    return found


def _is_outage(_before, world: World) -> bool:
    action = world.action
    return action is not None and action.name == "tap" and action.args[1] == "outage"


def restart_never_reruns(before, world: World) -> list[str]:
    """Restart settles a card a crashed tap left claimed from its effect and never reruns it."""

    found = []
    if world.graph().revision != world.revision:
        found.append("restart changed the graph")
    after = _cards(_view(world))
    proposals = _proposals(before)
    for name, row in _cards(before).items():
        if not _crashed(row):
            continue
        expected = "approved" if proposals[row[1]][1] == "approved" else "failed"
        if after[name][3] != expected:
            found.append(f"{name} settled {after[name][3]}, expected {expected}")
    return found


def _restarts_a_crash(before, world: World) -> bool:
    action = world.action
    return (
        action is not None and action.name == RESTART and any(_crashed(row) for row in before.cards)
    )


def cards_pin_a_live_proposal(before, world: World) -> list[str]:
    """Ask creates a card only for a Proposal that is pending and fresh."""

    action = world.action
    assert action is not None
    created = set(_cards(_view(world))) - set(_cards(before))
    target = action.args[0]
    proposal = _proposals(before)[target]
    return [
        f"{name} pins {target} while it is {proposal[1]}, stale={proposal[2]}"
        for name in created
        if proposal[1:3] != ("pending", False)
    ]


def _asks(_before, world: World) -> bool:
    return world.action is not None and world.action.name == "ask"


def card_decisions_need_a_live_proposal(before, world: World) -> list[str]:
    """A Proposal card decides a pending, fresh Proposal once; otherwise it is not pending."""

    action = world.action
    assert action is not None
    name, decision = action.args
    proposal = _proposals(before)[name]
    status = _proposals(_view(world))[name][1]
    if proposal[1:3] == ("pending", False):
        return [] if status == decision else [f"{name} is {status} after deciding {decision}"]
    if status != proposal[1] or world.reply != {"code": "proposal_not_pending"}:
        return [f"deciding {proposal[1]} {name} moved it to {status}, replying {world.reply}"]
    return []


def _decides(_before, world: World) -> bool:
    return world.action is not None and world.action.name == "decide"


def declining_an_approval_rejects_or_says_why(before, world: World) -> list[str]:
    """Declining a card that approves a Proposal rejects it, or says why it stays pending."""

    action = world.action
    assert action is not None
    card = _cards(_view(world))[action.args[0]]
    status = _proposals(_view(world))[card[1]][1]
    reply = world.reply or {}
    if card[3] != "declined":
        return [f"{action.args[0]} settled {card[3]} on a decline"]
    if reply.get("rejected") is True and status == "rejected":
        return []
    if reply.get("rejected") is False and reply.get("reason") and status != "rejected":
        return []
    return [f"a decline left {card[1]} {status}, replying {reply}"]


def _declines(_before, world: World) -> bool:
    action = world.action
    return action is not None and action.name == "tap" and action.args[1] == "decline"


def cards_end(view) -> bool:
    """Every card settles."""

    return all(row[3] is not None for row in view.cards)


RULES = (
    Safety(cards_answer_their_question, _always, "code guard at storage.approval_cards._settle"),
    Step(only_humans_resolve, _status_moved, "invariant 3"),
    Step(status_changes_only_from_pending, _resolved_before, "code guard at resolve_proposals"),
    Step(stale_approval_applies_nothing, _approves_stale, "graph spec: Proposals"),
    Step(cards_are_single_use, _a_card_settled_before, "conversations spec: approval cards"),
    Step(stale_only_when_pins_change, _taps_an_open_card, "conversations spec: approval cards"),
    Step(outage_releases_the_card, _is_outage, "code guard at api.approval_cards._run"),
    Step(
        restart_never_reruns, _restarts_a_crash, "code guard at settle_interrupted_approval_cards"
    ),
    Step(cards_pin_a_live_proposal, _asks, "code guard at approval_card_for_ask"),
    Step(card_decisions_need_a_live_proposal, _decides, "code guard at decide_proposal"),
    Step(
        declining_an_approval_rejects_or_says_why,
        _declines,
        "conversations spec: approval cards",
    ),
    Goal(
        cards_end,
        "conversations spec: approval cards",
        allowed=lambda action: action.environment or action.name == RESTART,
    ),
)
