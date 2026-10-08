from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from rcp.core.models import Decision, Edge, GatedCard, GraphState, Patch, Proposal
from rcp.digest import DigestProjector, assemble_digest, attribution, graph_event
from rcp.server_ops.maintenance import RuntimeAdmissionGate
from rcp.storage import AppStore, ProjectRecord
from rcp.storage.digest import digest_event

from .helpers import authorized_human


def _project_store(tmp_path):
    store = AppStore(tmp_path / "app.db")
    store.upsert_project(
        ProjectRecord(
            project_id="p",
            locator=str(tmp_path / "research.yaml"),
            name="Project",
            state_location=str(tmp_path / ".research"),
            state_remote=False,
            added_at=store.now(),
        )
    )
    return store


def _patch(revision=1, **kwargs):
    return Patch(kind="work", author="agent", summary="Update", ops=[], revision=revision, **kwargs)


def _decision(identifier="d/one", status="open"):
    return Decision(
        id=identifier, type="decision", title="Choice", question="Which?", status=status
    )


def _events(store):
    with store.connection() as conn:
        return [
            digest_event(row) for row in conn.execute("SELECT * FROM digest_events ORDER BY seq")
        ]


def test_projector_catches_missed_signal_and_rebaselines_changed_prefix(tmp_path):
    store = _project_store(tmp_path)
    state = GraphState(revision=1)
    patch = _patch()
    boundaries = [(GraphState(), patch, state)]
    history = SimpleNamespace(
        accepted_patch_boundaries=lambda: (SimpleNamespace(state=state), boundaries)
    )
    projector = DigestProjector(store, None, admission=RuntimeAdmissionGate())
    projector._project_history("p", "main", history)
    assert _events(store) == []
    next_state = GraphState(revision=2, nodes={"d/one": _decision(status="ready")})
    boundaries.append((state, _patch(2), next_state))
    state = next_state
    # Restart catch-up needs no surviving in-memory signal.
    DigestProjector(store, None, admission=RuntimeAdmissionGate())._project_history(
        "p", "main", history
    )
    assert len(_events(store)) == 1
    mark = {"seq": 0, "marked_at": store.now()}
    assert assemble_digest("p", "me", _events(store), mark, 1)["needs_you"]
    # A restored history removes the previously projected ready Decision.
    state = GraphState(revision=1)
    boundaries[:] = [(GraphState(), patch.model_copy(update={"summary": "Restored"}), state)]
    projector._project_history("p", "main", history)
    assert [event["kind"] for event in _events(store)] == ["graph_change", "reset"]
    result = assemble_digest("p", "me", _events(store), mark, 2)
    assert result["needs_you"] == result["changed"] == []
    projector._project_history("p", "main", history)
    assert len(_events(store)) == 2


def test_slower_projection_cannot_move_checkpoint_backward(tmp_path):
    store = _project_store(tmp_path)
    initial, first, second = GraphState(), GraphState(revision=1), GraphState(revision=2)
    boundaries = [(initial, _patch(), first)]
    history = SimpleNamespace(
        accepted_patch_boundaries=lambda: (SimpleNamespace(state=first), list(boundaries))
    )
    fast = DigestProjector(store, None, admission=RuntimeAdmissionGate())
    slow = DigestProjector(store, None, admission=RuntimeAdmissionGate())
    fast._project_history("p", "main", history)
    newer_head = None

    def delayed_replay():
        nonlocal newer_head
        stale = history.accepted_patch_boundaries()
        boundaries.append((first, _patch(2), second))
        fast._project_history("p", "main", history)
        with store.connection() as conn:
            newer_head = tuple(
                conn.execute("SELECT revision,patch_id FROM digest_heads").fetchone()
            )
        return stale

    slow._project_history("p", "main", SimpleNamespace(accepted_patch_boundaries=delayed_replay))
    with store.connection() as conn:
        assert (
            tuple(conn.execute("SELECT revision,patch_id FROM digest_heads").fetchone())
            == newer_head
        )
    assert newer_head[0] == 2
    assert [event["kind"] for event in _events(store)] == ["graph_change"]


def test_mark_checkpoints_main_before_commit_and_restart(manifest, tmp_path):
    from rcp.digest import read_digest

    from .helpers import append_fixture_patch, create_named_app, seed_patch

    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store, catalog = app.state.background_tasks.store, app.state.catalog
    project_id, user_id = app.state.default_project_id, store.local_owner.user_id
    app.state.digest_projector.run_pass()
    assert read_digest(store, catalog, project_id, user_id)["count"] == 0
    append_fixture_patch(catalog.open(project_id), seed_patch())
    # No projection after commit; restart must recover it from the durable head.
    DigestProjector(store, catalog, admission=RuntimeAdmissionGate()).run_pass()
    assert read_digest(store, catalog, project_id, user_id)["changed"]


def test_missing_episode_branch_does_not_block_main(manifest, tmp_path):
    from rcp.digest import read_digest

    from .helpers import append_fixture_patch
    from .test_branch_chats import _app_branch
    from .test_branch_history import _branch_patch

    app, main, episode, _ = _app_branch(manifest, tmp_path)
    store, catalog = app.state.background_tasks.store, app.state.catalog
    project_id, user_id = episode.project_id, store.local_owner.user_id
    app.state.digest_projector.run_pass()
    assert read_digest(store, catalog, project_id, user_id)["count"] == 0
    branch_root = main.history.root / "branches" / episode.graph_target.branch_id
    branch_root.rename(branch_root.with_name("absent-branch-fixture"))
    append_fixture_patch(main, _branch_patch("ev/main-digest"))
    projector = DigestProjector(store, catalog, admission=RuntimeAdmissionGate())
    projector.run_pass()
    assert read_digest(store, catalog, project_id, user_id)["changed"]
    assert not branch_root.exists()
    projector.run_pass()  # The periodic lag check also tolerates the missing branch.


def test_semantic_touches_and_attention_boundaries(tmp_path):
    store = AppStore(tmp_path / "app.db")
    before = GraphState(nodes={"d/one": _decision(), "d/two": _decision("d/two")})
    edge = Edge(id="e/one", source="d/one", target="d/two", relation="informs")
    after = before.model_copy(deep=True)
    after.edges[edge.id] = edge
    assert graph_event(store, "p", "main", before, _patch(), after)["node_ids"] == [
        "d/one",
        "d/two",
    ]
    removed = before.model_copy(deep=True)
    del removed.nodes["d/two"]
    assert graph_event(store, "p", "main", before, _patch(), removed)["node_ids"] == ["d/two"]
    proposal = Proposal(
        id="p/one", title="Review", card=GatedCard(), ops=[], related_node_ids=["d/one"]
    )
    proposed = before.model_copy(deep=True)
    proposed.proposals[proposal.id] = proposal
    event = graph_event(store, "p", "main", before, _patch(), proposed)
    assert event["node_ids"] == ["d/one"] and event["payload"]["attention"][0]["kind"] == "proposal"
    for old, new in [
        (None, "ready"),
        ("open", "ready"),
        ("decided", "revisit"),
        ("ready", "ready"),
    ]:
        initial = GraphState(nodes={"d/one": _decision(status=old)}) if old else GraphState()
        final = GraphState(
            nodes={"d/one": _decision(status=new).model_copy(update={"title": "Changed"})}
        )
        entries = graph_event(store, "p", "main", initial, _patch(), final)["payload"]["attention"]
        assert bool(entries) == (old != new)


def test_attribution_precedence_and_missing_task(tmp_path):
    store = AppStore(tmp_path / "app.db")
    human = authorized_human(store)
    patch = _patch().model_copy(
        update={"producer": "human", "author": "human", "authorized_by": human, "kind": "refresh"}
    )
    assert attribution(store, "p", patch)["source_kind"] == "member"
    assert (
        attribution(store, "p", patch.model_copy(update={"authorized_by": None}))["source_kind"]
        == "unattributed"
    )
    assert (
        attribution(store, "p", _patch().model_copy(update={"producer": "system"}))["source_kind"]
        == "system"
    )
    assert attribution(store, "p", _patch(task_id="missing"))["source_kind"] == "agent"
    assert (
        attribution(store, "p", _patch().model_copy(update={"kind": "refresh"}))["source_kind"]
        == "ingestion"
    )
    task = SimpleNamespace(
        kind="project_chat", request={"chat_id": "chat", "mode": "work"}, authorized_by=human
    )
    store.agent_task = lambda _: task
    chat = attribution(store, "p", _patch(task_id="task"))
    assert (chat["source_kind"], chat["actor_user_id"]) == ("chat", human.user_id)
    store.consolidation_run_for_operation = lambda _: SimpleNamespace(
        operation_id="task", run_id="run", report_artifact_id="report"
    )
    assert attribution(store, "p", _patch(task_id="task"))["source_kind"] == "consolidation"
    merge = SimpleNamespace(episode_id="episode")
    assert (
        attribution(store, "p", _patch(task_id="task").model_copy(update={"branch_merge": merge}))[
            "source_kind"
        ]
        == "episode"
    )


def test_grouping_excludes_own_edit_retains_agent_touch_and_suppresses_merged_branch(tmp_path):
    store = AppStore(tmp_path / "app.db")
    base = GraphState()
    live = GraphState(nodes={"d/one": _decision()})
    event = graph_event(store, "p", "main", base, _patch(), live)
    event.update(seq=1, source_key="agent:one")
    own = dict(
        event,
        seq=2,
        source_key="member:me",
        actor_user_id="me",
        payload=dict(event["payload"], source_kind="member"),
    )
    merge = dict(
        event,
        seq=3,
        source_key="episode:episode",
        payload=dict(
            event["payload"],
            source_kind="episode",
            merged_branch_revision=4,
            non_node_edits=1,
            edited_node_ids=[],
        ),
        node_ids=[],
    )
    branch = dict(
        event,
        seq=4,
        kind="branch_change",
        item_id="4",
        payload=dict(episode_id="episode", title="Episode", edits=2, deep_link=None),
    )
    result = assemble_digest(
        "p", "me", [event, own, merge, branch], {"seq": 0, "marked_at": "now"}, 4, live
    )
    assert result["changed_node_ids"] == ["d/one"]
    assert result["changed"][0]["node_ids"] == ["d/one"]
    assert result["branches"] == []
    assert len(result["changed"]) == 2


def test_branch_reset_drops_replaced_history_but_keeps_operational_attention(tmp_path):
    store = AppStore(tmp_path / "app.db")
    live = GraphState(nodes={"d/one": _decision()})
    branch = graph_event(store, "p", "branch:b", GraphState(), _patch(), live)
    branch.update(seq=1, item_id="1", payload=dict(episode_id="e", title="E", edits=1))
    waiting = dict(
        branch, seq=2, kind="episode_attention", item_id="e", payload=dict(active=True, title="E")
    )
    reset = dict(branch, seq=3, kind="reset", payload={})
    mark = {"seq": 0, "marked_at": "now"}
    assert len(assemble_digest("p", "me", [branch], mark, 1, live)["branches"]) == 1
    result = assemble_digest("p", "me", [branch, waiting, reset], mark, 3, live)
    assert result["branches"] == []
    assert [item["item_id"] for item in result["needs_you"]] == ["e"]


def test_branch_accepted_hook_projects_without_main_changes(manifest, tmp_path):
    from rcp.digest import read_digest

    from .test_branch_chats import _app_branch
    from .test_branch_history import _branch_patch

    app, main, episode, root = _app_branch(manifest, tmp_path)
    store = app.state.background_tasks.store
    projector = app.state.digest_projector
    # The project has a baseline; this newly discovered branch has none yet.
    projector._project_history(episode.project_id, "main", main.history)
    user_id = store.local_owner.user_id
    assert read_digest(store, app.state.catalog, episode.project_id, user_id)["count"] == 0
    main_revision = main.history.current_accepted_revision()
    branch = main.for_graph_target(episode.graph_target).history
    branch.append(
        _branch_patch("ev/digest").model_copy(update={"source_operation_id": root.operation_id})
    )
    # A crash loses the signal, but must not silently baseline the new branch.
    projector = DigestProjector(store, app.state.catalog, admission=RuntimeAdmissionGate())
    projector.run_pass()
    result = read_digest(store, app.state.catalog, episode.project_id, user_id)
    assert main.history.current_accepted_revision() == main_revision
    assert len(result["branches"]) == 1
    assert result["branches"][0]["episode_id"] == episode.episode_id
    assert result["branches"][0]["edits"] == 1
    store.catch_up_digest(episode.project_id, user_id, result["cursor"])
    projector.run_pass()
    assert read_digest(store, app.state.catalog, episode.project_id, user_id)["count"] == 0
    # A missed branch signal is repaired by the cheap branch-head lag check.
    app.state.catalog.on_accepted_branch_transition = None
    branch.append_batch_from_state(
        lambda _state: [
            _branch_patch("ev/digest-missed").model_copy(
                update={"source_operation_id": root.operation_id}
            )
        ]
    )
    projector.run_pass()
    result = read_digest(store, app.state.catalog, episode.project_id, user_id)
    assert len(result["branches"]) == 1 and result["branches"][0]["edits"] == 1


def test_first_pass_baselines_existing_branches_before_main(manifest, tmp_path):
    from rcp.digest import read_digest

    from .test_branch_chats import _app_branch
    from .test_branch_history import _branch_patch

    app, main, episode, root = _app_branch(manifest, tmp_path)
    store = app.state.background_tasks.store
    # Branch history that predates the digest, as on an upgraded project.
    main.for_graph_target(episode.graph_target).history.append(
        _branch_patch("ev/old").model_copy(update={"source_operation_id": root.operation_id})
    )
    projector = DigestProjector(store, app.state.catalog, admission=RuntimeAdmissionGate())
    # Even a first pass driven by a main signal baselines every branch first.
    projector.reconcile_project(episode.project_id, targets={"main"})
    user_id = store.local_owner.user_id
    assert read_digest(store, app.state.catalog, episode.project_id, user_id)["count"] == 0
    projector.run_pass()
    assert read_digest(store, app.state.catalog, episode.project_id, user_id)["count"] == 0


def test_episode_needing_action_after_the_first_mark_reaches_the_digest(
    manifest, tmp_path, monkeypatch
):
    import rcp.digest as digest

    from .test_branch_chats import _app_branch

    app, _, episode, _ = _app_branch(manifest, tmp_path)
    store = app.state.background_tasks.store
    user_id = store.local_owner.user_id
    real = digest._episode_attention
    needs = {"value": False}

    def attention(store, project_id, episode_ids=None):
        episodes, active = real(store, project_id, episode_ids)
        return episodes, dict.fromkeys(active, needs["value"])

    monkeypatch.setattr(digest, "_episode_attention", attention)
    projector = DigestProjector(store, app.state.catalog, admission=RuntimeAdmissionGate())
    projector.reconcile_project(episode.project_id)
    # The member's mark lands before the projector's episode pass.
    digest.read_digest(store, app.state.catalog, episode.project_id, user_id)
    needs["value"] = True
    projector.run_pass()
    result = digest.read_digest(store, app.state.catalog, episode.project_id, user_id)
    assert [item["item_id"] for item in result["needs_you"]] == [episode.episode_id]


def test_viewer_does_not_see_their_own_chat_agent_edits_once_read(tmp_path):
    # A Work turn the member asked for leaves their digest once their chat read
    # marker reaches it; one that finished after they left stays news until they
    # read the chat. A teammate always sees it, under the chat as its latest source.
    store = _project_store(tmp_path)
    live = GraphState(nodes={"d/one": _decision()})
    event = graph_event(store, "p", "main", GraphState(), _patch(), live)
    event.update(seq=1, source_key="agent:one", created_at="2026-10-07T10:00:00+00:00")
    own_chat = dict(
        event,
        seq=2,
        source_key="chat:mine",
        actor_user_id="me",
        created_at="2026-10-07T12:00:00+00:00",
        payload=dict(event["payload"], source_kind="chat"),
    )
    mark = {"seq": 0, "marked_at": "now"}

    def groups(user, reads):
        result = assemble_digest("p", user, [event, own_chat], mark, 2, live, reads)
        return [group["source_key"] for group in result["changed"]]

    seen = {"baseline": None, "reads": {"mine": "2026-10-07T12:00:05.000000+00:00"}}
    unseen = {"baseline": None, "reads": {"mine": "2026-10-07T11:00:00.000000+00:00"}}
    assert groups("me", seen) == ["agent:one"]
    assert groups("me", unseen) == ["chat:mine"]
    assert groups("me", {"baseline": None, "reads": {}}) == ["chat:mine"]
    assert groups("me", {"baseline": "2026-10-08T00:00:00+00:00", "reads": {}}) == ["agent:one"]
    assert groups("teammate", seen) == ["chat:mine"]

    # The store reads exactly the member's own markers and the marker baseline.
    store.mark_chat_read("p", "mine", "me", datetime(2026, 10, 7, 12, 0, 5, tzinfo=UTC))
    store.mark_chat_read("p", "mine", "teammate", datetime(2026, 10, 7, 9, tzinfo=UTC))
    markers = store.chat_read_markers("p", "me")
    assert markers["reads"] == {"mine": "2026-10-07T12:00:05.000000+00:00"}
    assert groups("me", markers) == ["agent:one"]
