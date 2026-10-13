from __future__ import annotations

import json

import pytest

from rcp.core.transition_models import GraphTargetRef
from rcp.history import HistoryManager
from rcp.history.queries import parse_head
from tests.helpers import seed_patch, unchecked_write_authority
from tests.test_branch_history import _branch_metadata, _branch_patch


@pytest.fixture
def history(manifest):
    owner = HistoryManager(manifest, write_authority=unchecked_write_authority())
    owner.append(seed_patch())
    return owner


def head(engine, text):
    return engine.resolve(parse_head(text), default_target=GraphTargetRef())


def test_resolve_inheritance_cross_target_diff_and_absent_log(history):
    engine = history.history_engine
    assert engine.state_at(head(engine, "main@0")).revision == 0
    metadata = _branch_metadata(history)
    branch = history.create_episode_branch(metadata)
    branch.append(_branch_patch("ev/branch"))
    history.append(_branch_patch("ev/main"))
    branch_head = head(engine, f"branch:{metadata.branch_id}@head")
    assert branch.history_engine is engine
    assert [r.inherited for r in engine.steps(branch_head, include_inherited=True)] == [True, False]
    with pytest.raises(ValueError):
        head(engine, f"branch:{metadata.branch_id}@0")
    with pytest.raises(ValueError):
        head(engine, "main@99")
    page = engine.diff(head(engine, "main@head"), branch_head)
    assert {(c.id, c.change) for c in page.changes} >= {
        ("ev/main", "removed"),
        ("ev/branch", "created"),
    }
    assert engine.log("ev/missing", branch_head).entries == []
    assert engine.log("ev/branch", branch_head).entries[0].step.inherited is False


def test_prefix_proof_and_incremental_replay(history, monkeypatch):
    import rcp.history.engine as module

    engine = history.history_engine
    first = head(engine, "main@head")
    original = module.materialize_patches_from_state
    lengths = []

    def replay(patches, **kwargs):
        lengths.append(len(patches))
        return original(patches, **kwargs)

    monkeypatch.setattr(module, "materialize_patches_from_state", replay)
    assert engine.state_at(first).revision == 1
    assert lengths == []
    history.append(_branch_patch("ev/next"))
    assert head(engine, "main@head").revision == 2
    assert lengths == [1]
    path = history._patch_paths()[-1]
    text = path.read_text()
    path.write_text(text + "\n")
    engine.state_at(first)
    assert lengths == [1, 2]


def test_stat_cache_reverifies_changed_earlier_patch(history, monkeypatch):
    from pathlib import Path

    history.append(_branch_patch("ev/later"))
    engine = history.history_engine
    current = head(engine, "main@head")
    engine.state_at(current)
    paths = history._patch_paths()
    reads = []
    original = Path.read_bytes

    def read(path):
        if path in paths:
            reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    engine.state_at(current)
    assert reads == []
    paths[0].write_text(paths[0].read_text() + "\n")
    assert engine.state_at(current).revision == current.revision
    assert reads == paths


def test_append_hashes_only_new_patch_and_reuses_resolved_pin(history, monkeypatch):
    from pathlib import Path

    engine = history.history_engine
    engine.state_at(head(engine, "main@head"))
    history.append(_branch_patch("ev/next"))
    reads = []
    original = Path.read_bytes

    def read(path):
        if path.parent == history.patches_dir:
            reads.append(path)
        return original(path)

    captures = []
    capture = engine._capture

    def observe(*args, **kwargs):
        captures.append(True)
        return capture(*args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(engine, "_capture", observe)
    assert engine.state_at(head(engine, "main@head")).revision == 2
    assert reads == [history._patch_paths()[-1]]
    assert len(captures) == 1


def test_returned_snapshot_cannot_mutate_retained_history(history):
    engine = history.history_engine
    current = head(engine, "main@head")
    state = engine.state_at(current)
    expected = state.model_copy(deep=True)
    next(iter(state.nodes.values())).title = "Changed by caller"
    state.project_truth_scope.clear()
    state.nodes.clear()
    assert engine.state_at(current) == expected


def test_rejected_receipt_and_refresh_failure(history, monkeypatch):
    from rcp.transport import StateUnavailable

    engine = history.history_engine
    first = head(engine, "main@head")
    rejected = _branch_patch("ev/rejected").model_copy(
        update={"revision": 2, "admission": "rejected"}
    )
    history._atomic_text(history.patches_dir / "000002.json", rejected.model_dump_json())
    last = head(engine, "main@head")
    assert last.revision == 2 and last.transition_id == first.transition_id
    assert not engine.steps(last)[-1].accepted
    assert "ev/rejected" not in engine.state_at(last).nodes
    monkeypatch.setattr(history.workspace, "refresh_if_stale", lambda *args: False)
    with pytest.raises(StateUnavailable):
        engine.state_at(last)


def test_long_values_continue_with_pinned_diff_and_log_cursors(history):
    from rcp import limits

    engine = history.history_engine
    before = head(engine, "main@head")
    title = '中文"\\' * 20000
    patch = _branch_patch("ev/long", title=title)
    history.append(patch)
    after = head(engine, "main@head")
    page = engine.diff(before, after, ids=frozenset({"ev/long"}))
    assert page.cursor is not None
    with pytest.raises(ValueError):
        engine.diff(before, after, ids=frozenset({"ev/other"}), cursor=page.cursor)
    history.append(_branch_patch("ev/later"))
    pieces = []
    steps = []
    while True:
        assert len(page.model_dump_json().encode()) <= limits.GRAPH_HISTORY_PAGE_MAX_BYTES
        assert len(page.changes) + len(page.steps) <= limits.GRAPH_HISTORY_PAGE_MAX_ENTRIES
        pieces.extend(
            field.after
            for change in page.changes
            for field in change.fields
            if field.field == "title"
        )
        steps.extend(page.steps)
        if page.cursor is None:
            break
        page = engine.diff(before, after, ids=frozenset({"ev/long"}), cursor=page.cursor)
    assert "".join(pieces) == title
    assert [step.summary for step in steps] == [patch.summary]
    log_page = engine.log("ev/long", after)
    pieces = []
    while True:
        assert len(log_page.model_dump_json().encode()) <= limits.GRAPH_HISTORY_PAGE_MAX_BYTES
        assert len(log_page.entries) <= limits.GRAPH_HISTORY_PAGE_MAX_ENTRIES
        pieces.extend(
            field.after
            for entry in log_page.entries
            for field in entry.change.fields
            if field.field == "title"
        )
        assert all(entry.step.head == "main@2" for entry in log_page.entries)
        if log_page.cursor is None:
            break
        log_page = engine.log("ev/long", after, cursor=log_page.cursor)
    assert "".join(pieces) == title


def test_checkpoint_restart_corruption_and_projection_parity(history, tmp_path, monkeypatch):
    import rcp.history.engine as module
    from rcp.history.engine import ProjectHistoryEngine

    engine = history.history_engine
    engine.configure_store(tmp_path / "cache-data", "project")
    engine.checkpoint_interval = 2
    history.append(_branch_patch("ev/checkpoint"))
    expected = history.materialize(write_outputs=False)
    last = head(engine, "main@head")
    expected_steps = engine.steps(last)
    assert engine.store is not None
    assert list(engine.store.root.glob("*.checkpoint.json"))
    original = module.materialize_patches_from_state

    def forbidden(*args, **kwargs):
        raise AssertionError("verified index/checkpoint should not replay")

    monkeypatch.setattr(module, "materialize_patches_from_state", forbidden)
    cold = ProjectHistoryEngine(history)
    cold.configure_store(tmp_path / "cache-data", "project")
    assert cold.state_at(last) == expected.state
    assert cold.steps(last) == expected_steps
    result, records = engine.projection(GraphTargetRef())
    assert result.processed_cursors == expected.processed_cursors
    assert len(records) == 2
    monkeypatch.setattr(module, "materialize_patches_from_state", original)
    for path in engine.store.root.glob("*.json"):
        if path.name != ".last-access.json":
            path.write_text("{broken")
    rebuilt = ProjectHistoryEngine(history)
    rebuilt.configure_store(tmp_path / "cache-data", "project")
    assert rebuilt.state_at(last) == expected.state


def test_build_is_rechecked_before_publication(history, monkeypatch):
    import rcp.history.engine as module

    engine = history.history_engine
    original = module.materialize_patches_from_state
    appended = False

    def replay(patches, **kwargs):
        nonlocal appended
        result = original(patches, **kwargs)
        if not appended:
            appended = True
            history.append(_branch_patch("ev/raced"))
        return result

    monkeypatch.setattr(module, "materialize_patches_from_state", replay)
    last = head(engine, "main@head")
    assert last.revision == 2
    assert "ev/raced" in engine.state_at(last).nodes


def test_memory_eviction_reloads_verified_store(history, tmp_path, monkeypatch):
    from rcp import limits

    engine = history.history_engine
    engine.configure_store(tmp_path, "project")
    monkeypatch.setattr(limits, "GRAPH_HISTORY_MEMORY_PROJECT_BYTES", 1)
    last = head(engine, "main@head")
    assert engine._generations == {}
    assert engine.state_at(last) == history.materialize(write_outputs=False).state


def test_log_keeps_creation_and_removal_when_net_diff_is_empty(history):
    from rcp.core.models import Patch
    from rcp.core.operations import RemoveNodesOperation

    engine = history.history_engine
    before = head(engine, "main@head")
    history.append(_branch_patch("ev/transient"))
    history.append(
        Patch(
            kind="work",
            author="agent",
            producer="agent",
            summary="Remove temporary result",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=[RemoveNodesOperation(op="remove_nodes", node_ids=["ev/transient"])],
        )
    )
    after = head(engine, "main@head")
    assert not engine.diff(before, after, ids=frozenset({"ev/transient"})).changes
    assert [entry.change.change for entry in engine.log("ev/transient", after).entries] == [
        "created",
        "removed",
    ]


def test_cached_snapshots_preserve_derived_edge_layers(tmp_path):
    from rcp.core.materialize import MaterializationResult
    from rcp.core.models import Edge, GraphState
    from rcp.core.transition_models import GraphHeadRef
    from rcp.history.delta import semantic_delta
    from rcp.history.state_delta import apply_state_delta, capture_state_delta
    from rcp.history.store import GraphHistoryStore, IndexedStep

    edge = Edge(
        id="rq/item::blocked_by::exp/item",
        source="rq/item",
        target="exp/item",
        relation="blocked_by",
        layer="seam",
    ).model_copy(update={"layer": "seam"})
    before = GraphState()
    after = GraphState(revision=1, edges={edge.id: edge})
    before_head, after_head = GraphHeadRef(revision=0), GraphHeadRef(revision=1)
    assert apply_state_delta(before, capture_state_delta(before, after)) == after
    store = GraphHistoryStore(tmp_path, "project")
    digest = "a" * 64
    store.save_checkpoint(digest, after_head, MaterializationResult(after))
    checkpoint = store.load_checkpoint(digest)
    assert checkpoint is not None and checkpoint.result.state == after
    index = IndexedStep(
        before_head=before_head,
        after_head=after_head,
        patch=seed_patch(),
        delta=semantic_delta(before, after),
        changed_node_ids=frozenset(),
        changed_edge_ids=frozenset({edge.id}),
        accepted=True,
        state_delta=capture_state_delta(before, after),
    )
    digests = ("b" * 64, digest)
    store.save_step_segments(digests, [index], 256)
    loaded = store.load_step_segments(digests, 256)
    assert loaded is not None and loaded == [index]
    assert apply_state_delta(before, loaded[0].state_delta) == after


def test_branch_patch_inventory_refuses_symlinks(history, tmp_path):
    metadata = _branch_metadata(history)
    branch = history.create_episode_branch(metadata)
    outside = tmp_path / "outside-patch.json"
    outside.write_text(
        _branch_patch("ev/outside")
        .model_copy(
            update={"revision": metadata.base_head.revision + 1, "admission": "accepted"},
        )
        .model_dump_json()
    )
    (branch.patches_dir / f"{metadata.base_head.revision + 1:06d}.json").symlink_to(outside)
    with pytest.raises(ValueError):
        head(history.history_engine, f"branch:{metadata.branch_id}@head")


def test_shared_branch_prefix_is_charged_once_to_memory_budget(history, monkeypatch):
    from rcp import limits
    from rcp.history.memory_size import retained_size

    metadata = _branch_metadata(history)
    history.create_episode_branch(metadata).append(_branch_patch("ev/shared"))
    engine = history.history_engine
    generations = engine._pin([metadata.head.target])
    shared_bytes = retained_size(tuple(generations.values()))
    assert shared_bytes < sum(retained_size(value) for value in generations.values())
    monkeypatch.setattr(limits, "GRAPH_HISTORY_MEMORY_PROJECT_BYTES", shared_bytes)
    engine._pin([metadata.head.target])
    assert set(engine._generations) == {"main", metadata.head.target.key}


@pytest.mark.parametrize("warm", [False, True])
def test_malformed_tail_preserves_coherent_history_and_recovers(history, tmp_path, warm):
    engine = history.history_engine
    engine.configure_store(tmp_path / "cache", "project")
    if warm:
        head(engine, "main@head")
    history.append(_branch_patch("ev/later"))
    path = history._patch_paths()[-1]
    original = path.read_text()
    document = json.loads(original)
    document["kind"] = "retired-patch-kind"
    path.write_text(json.dumps(document))

    for _ in range(2):
        result, steps = engine.projection(GraphTargetRef())
        assert result.state.revision == 1
        assert result.state.replay_status == "degraded"
        assert result.state.replay_failure is not None
        assert result.state.replay_failure.revision == 2
        assert result.state.replay_failure.code == "patch-schema-invalid"
        assert [step.patch.revision for step in steps] == [1]
        assert "ev/later" not in result.state.nodes

    path.write_text(original)
    result, steps = engine.projection(GraphTargetRef())
    assert result.state.replay_status == "complete"
    assert [step.patch.revision for step in steps] == [1, 2]
    assert "ev/later" in result.state.nodes


@pytest.mark.parametrize("read", ["metadata", "changes", "revision"])
def test_canonical_append_lock_is_free_during_cold_publication(
    history, tmp_path, monkeypatch, read
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    import rcp.history.engine as module
    from rcp.api.graph_changes import branch_changes
    from rcp.api.project_state import _branch_revision

    metadata = _branch_metadata(history)
    branch = history.create_episode_branch(metadata)
    branch.append(_branch_patch("ev/branch"))
    engine = history.history_engine
    engine.configure_store(tmp_path / "cache", "project")
    engine.checkpoint_interval = 2
    assert engine.store is not None
    publishing, release = Event(), Event()
    original = engine.store.save_checkpoint
    replay = module.materialize_patches_from_state

    def assert_unlocked():
        with ThreadPoolExecutor(max_workers=1) as threads:

            def acquire():
                acquired = history.workspace.snapshot_lock.acquire(timeout=2)
                if acquired:
                    history.workspace.snapshot_lock.release()
                return acquired

            assert threads.submit(acquire).result(timeout=3)

    def replay_outside_lock(*args, **kwargs):
        assert_unlocked()
        return replay(*args, **kwargs)

    def blocked(*args, **kwargs):
        publishing.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "materialize_patches_from_state", replay_outside_lock)
    monkeypatch.setattr(engine.store, "save_checkpoint", blocked)
    service = SimpleNamespace(history=branch)
    if read == "metadata":
        query = branch.branch_metadata
    elif read == "changes":

        def query():
            return branch_changes(service)
    else:
        import rcp.api.project_state as project_state

        monkeypatch.setattr(project_state, "get_graph_service", lambda *a, **kw: service)
        monkeypatch.setattr(project_state, "active_merge", lambda *a: False)
        monkeypatch.setattr(project_state, "experiment_signal", lambda *a: "signal")
        cache = SimpleNamespace(complete_transition_control=lambda *a: {"experiment_control": {}})
        catalog = SimpleNamespace(resolve_project_id=lambda p: p)
        store = SimpleNamespace(
            latest_agent_task_id=lambda p: None, latest_watcher_id=lambda p: None
        )

        def query():
            return _branch_revision("project", branch.branch_id, cache, catalog, store)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(query)
        try:
            assert publishing.wait(5)
            assert_unlocked()
        finally:
            release.set()
        future.result(timeout=5)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_valid_segments_repair_checkpoint_for_next_cold_engine(
    history, tmp_path, monkeypatch, damage
):
    import rcp.history.engine as module
    from rcp.history.engine import ProjectHistoryEngine

    history.append(_branch_patch("ev/checkpoint"))
    engine = history.history_engine
    engine.configure_store(tmp_path / "cache", "project")
    engine.checkpoint_interval = 2
    last = head(engine, "main@head")
    expected = engine.state_at(last)
    assert engine.store is not None
    for path in engine.store.root.glob("*.checkpoint.json"):
        if damage == "missing":
            path.unlink()
        else:
            path.write_text("{broken")
    repaired = ProjectHistoryEngine(history)
    repaired.checkpoint_interval = 2
    repaired.configure_store(tmp_path / "cache", "project")
    assert repaired.state_at(last) == expected

    def forbidden(*args, **kwargs):
        raise AssertionError("repaired checkpoint must serve a second cold engine without replay")

    monkeypatch.setattr(module, "materialize_patches_from_state", forbidden)
    cold = ProjectHistoryEngine(history)
    cold.checkpoint_interval = 2
    cold.configure_store(tmp_path / "cache", "project")
    assert cold.state_at(last) == expected


def test_metadata_projections_do_not_reconstruct_history(history, monkeypatch):
    from types import SimpleNamespace

    import rcp.history.engine as module
    from rcp.api.graph_changes import branch_changes

    metadata = _branch_metadata(history)
    branch = history.create_episode_branch(metadata)
    branch.append(_branch_patch("ev/branch"))
    engine = history.history_engine
    engine.snapshot(branch.graph_target)

    def forbidden(*args, **kwargs):
        raise AssertionError("metadata/delta projection must not construct step states")

    monkeypatch.setattr(module, "apply_state_delta", forbidden)
    monkeypatch.setattr(engine, "_steps", forbidden)
    assert [step.patch.revision for step in engine.indexed_steps(metadata.head)] == []
    assert [row["revision"] for row in history.slice(from_revision=1)] == [1]
    assert [row["revision"] for row in branch.slice(from_revision=2)] == [2]
    assert branch.slice(from_revision=3) == []
    history.refresh_delta()
    _, changes = branch_changes(SimpleNamespace(history=branch))
    assert "ev/branch" in changes.changed_node_ids
    snapshots = history.branch_read_snapshots(
        [(metadata.branch_id, metadata.episode_id, metadata.project_id)]
    )
    assert snapshots[metadata.branch_id].metadata.head.revision == 2


def test_stateful_projection_starts_at_requested_predecessor(history, monkeypatch):
    import rcp.history.engine as module

    for number in range(4):
        history.append(_branch_patch(f"ev/step-{number}"))
    engine = history.history_engine
    latest = head(engine, "main@head")
    calls = []
    apply = module.apply_state_delta

    def record(before, delta):
        calls.append(before.revision)
        return apply(before, delta)

    monkeypatch.setattr(module, "apply_state_delta", record)
    summaries = history.revision_summaries(from_revision=latest.revision)
    assert [item["to_revision"] for item in summaries] == [latest.revision]
    assert calls == [latest.revision - 1]
