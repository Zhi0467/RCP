from __future__ import annotations

import ast
import gzip
import hashlib
import json
import os
import pwd
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import chdir
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import rcp.storage.base as storage_base_module
import rcp.storage.models as storage_models
from rcp.api import create_app
from rcp.config import load_manifest
from rcp.core.authority import WriteAuthority
from rcp.history import HistoryManager
from rcp.server_ops.deployment import UPDATE_SOURCE_FLOOR
from rcp.storage import AppStore
from tests.helpers import signed_in_client

from . import server_upgrade_harness
from .server_upgrade_harness import (
    REPOSITORY_ROOT,
    build_exact_base_checkout,
    build_exact_base_fixture,
    build_release_checkout,
    capture_release_update_with,
    exact_base_gate_enabled,
    exact_candidate_base,
    immutable_fixture_directories,
    published_release_tags,
    verify_fixture_integrity,
    verify_fixture_registry,
)


def test_immutable_server_boundary_registry_is_complete() -> None:
    verify_fixture_registry()


def test_release_catalog_is_complete_and_missing_sources_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert server_upgrade_harness.update_source_floor() == UPDATE_SOURCE_FLOOR
    tags = [f"v{UPDATE_SOURCE_FLOOR}", *[f"v1.0.{index}" for index in range(125)]]
    releases = [{"tagName": tag, "isDraft": False, "isPrerelease": False} for tag in tags] + [
        {"tagName": "v2.0.0", "isDraft": True, "isPrerelease": False},
        {"tagName": "v2.0.1", "isDraft": False, "isPrerelease": True},
        {"tagName": "build/1", "isDraft": False, "isPrerelease": False},
        {"tagName": "v0.4.4", "isDraft": False, "isPrerelease": False},
        {"tagName": "v1.0.0rc1", "isDraft": False, "isPrerelease": False},
    ]

    def release_list(argv: list[str], *, cwd: Path) -> str:
        assert argv[:3] == ["gh", "release", "list"]
        return json.dumps(releases[: int(argv[argv.index("--limit") + 1])])

    monkeypatch.setattr(server_upgrade_harness, "_capture", release_list)
    assert published_release_tags() == tags
    releases.pop(0)  # Newer releases alone cannot satisfy the floor gate.
    with pytest.raises(ValueError):
        published_release_tags()
    with pytest.raises(subprocess.CalledProcessError):
        build_release_checkout("refs/tags/upgrade-gate-missing-tag", tmp_path / "missing")


def test_pre_ledger_fixture_records_migrations_and_never_rescans(
    tmp_path: Path, monkeypatch
) -> None:
    fixture = next(
        path
        for path in immutable_fixture_directories()
        if path.name == "pre-storage-migration-ledger-v12-c3191bf"
    )
    copied = tmp_path / "fixture"
    shutil.copytree(fixture, copied)
    database = _materialize_database(copied / "data")

    AppStore(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT migration_version FROM storage_schema_migrations ORDER BY migration_version"
        ).fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
            (8,),
            (9,),
            (10,),
            (11,),
            (12,),
            (13,),
            (14,),
            (15,),
            (16,),
            (17,),
            (18,),
            (19,),
            (20,),
            (21,),
            (22,),
            (23,),
            (24,),
            (25,),
            (26,),
            (27,),
            (28,),
            (29,),
            (30,),
            (31,),
            (32,),
            (33,),
            (34,),
            (35,),
            (36,),
            (37,),
            (38,),
            (39,),
            (40,),
            (41,),
            (42,),
            (43,),
            (44,),
            (45,),
            (46,),
            (47,),
            (48,),
            (49,),
            (50,),
            (51,),
            (52,),
            (53,),
            (54,),
            (55,),
            (56,),
            (57,),
            (58,),
            (59,),
            (60,),
            (61,),
            (62,),
            (63,),
            (64,),
        ]

        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM graph_runs "
            "WHERE json_extract(request_json, '$.artifact_edit.episode_id') = ? "
            "AND visible = 1 ORDER BY created_at, operation_id",
            ("episode",),
        ).fetchall()
        assert any("USING INDEX graph_runs_artifact_edit_episode" in row[3] for row in plan)
        assert not any("TEMP B-TREE" in row[3] for row in plan)

    def unexpected_migration(*_args) -> None:
        raise AssertionError("completed legacy migration was rescanned")

    monkeypatch.setattr(
        AppStore,
        "_migrate_episode_lineage",
        classmethod(unexpected_migration),
    )
    monkeypatch.setattr(
        storage_base_module,
        "migrate_legacy_episodes",
        unexpected_migration,
    )
    monkeypatch.setattr(
        AppStore,
        "_migrate_experiment_episode_state",
        staticmethod(unexpected_migration),
    )
    monkeypatch.setattr(
        AppStore,
        "_migrate_agent_usage_counted_dedupe",
        staticmethod(unexpected_migration),
    )
    monkeypatch.setattr(
        AppStore,
        "_migrate_space_run_projection_indexes",
        staticmethod(unexpected_migration),
    )
    monkeypatch.setattr(
        AppStore,
        "_migrate_conversation_worktrees",
        staticmethod(unexpected_migration),
    )

    AppStore(database)


@pytest.fixture(scope="module")
def baseline_schema(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[str, str, str, str]]:
    baseline = tmp_path_factory.mktemp("baseline") / "baseline.sqlite3"
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("rcp.rcp_home.rcp_home", lambda: baseline.parent / "rcp-home")
        AppStore(baseline).close()
    return _normalized_schema(baseline)


@pytest.mark.parametrize("fixture", immutable_fixture_directories(), ids=lambda path: path.name)
def test_immutable_server_boundaries_upgrade_and_start(
    fixture: Path,
    tmp_path: Path,
    baseline_schema: list[tuple[str, str, str, str]],
) -> None:
    verify_fixture_integrity(fixture)
    copied = tmp_path / "fixture"
    shutil.copytree(fixture, copied)

    _exercise_candidate_upgrade(copied, baseline_schema=baseline_schema)


def test_node_worker_upgrade_preserves_attempt_and_allows_project_seat(tmp_path: Path) -> None:
    fixture = next(
        path
        for path in immutable_fixture_directories()
        if path.name == "pre-graph-branches-v28-ae89ee3"
    )
    metadata = verify_fixture_integrity(fixture)
    copied = tmp_path / "fixture"
    shutil.copytree(fixture, copied)
    database = _materialize_database(copied / "data")
    operation = metadata["experiment_operation_id"]
    admitted_by = metadata["active_operation_id"]
    old_worker = (
        "old-worker",
        metadata["experiment_episode_id"],
        metadata["project_id"],
        "exp-1",
        operation,
        operation,
        admitted_by,
        "Measure",
        hashlib.sha256(b"Measure").hexdigest(),
        None,
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
    )
    old_attempt = (operation, "old-worker", operation, old_worker[-1])
    # Write the frozen old schema directly, before any current model or migration runs.
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO auto_research_child_work VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            old_worker,
        )
        connection.execute(
            "INSERT INTO auto_research_child_work_attempts VALUES (?, ?, ?, ?)", old_attempt
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE auto_research_child_work SET control_node_id = NULL")
    store = AppStore(database)
    with store.connection() as connection:
        assert (
            tuple(connection.execute("SELECT * FROM auto_research_child_work").fetchone())
            == old_worker
        )
        assert (
            tuple(connection.execute("SELECT * FROM auto_research_child_work_attempts").fetchone())
            == old_attempt
        )
        connection.execute("UPDATE auto_research_child_work SET control_node_id = NULL")
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    store.close()
    reopened = AppStore(database)
    worker = reopened.auto_research_child_work("old-worker")
    assert worker is not None and worker.control_node_id is None
    reopened.close()


@pytest.mark.skipif(not exact_base_gate_enabled(), reason="dedicated exact-base upgrade gate")
def test_exact_candidate_base_upgrades_and_starts(tmp_path: Path) -> None:
    checkout, base_commit = build_exact_base_checkout(tmp_path / "base-build")
    fixture = build_exact_base_fixture(checkout, base_commit, tmp_path)
    metadata = verify_fixture_integrity(fixture)

    assert metadata["created_with_commit"] == base_commit
    _exercise_candidate_upgrade(fixture)


@pytest.mark.skipif(not exact_base_gate_enabled(), reason="dedicated exact-base upgrade gate")
def test_new_storage_migrations_freeze_the_base_shape(tmp_path: Path) -> None:
    base_ref, _ = exact_candidate_base()
    migrations = "src/rcp/storage/base.py"
    base_head = max(_migration_versions(_git_capture("show", f"{base_ref}:{migrations}")))
    candidate = _migration_versions((REPOSITORY_ROOT / migrations).read_text(encoding="utf-8"))
    if max(candidate) <= base_head:
        return

    frozen = set()
    for fixture in immutable_fixture_directories():
        database = tmp_path / f"{fixture.name}.sqlite3"
        database.write_bytes(gzip.decompress((fixture / "data/rcp.sqlite3.gz").read_bytes()))
        with sqlite3.connect(database) as connection:
            ledger = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'storage_schema_migrations'"
            ).fetchone()
            if ledger is not None:
                frozen.add(
                    connection.execute(
                        "SELECT MAX(migration_version) FROM storage_schema_migrations"
                    ).fetchone()[0]
                )
    assert base_head in frozen, (
        f"this change adds storage migrations after {base_head}; add an immutable fixture "
        f"frozen at migration {base_head}, built by the base source "
        "(tests/fixtures/server_upgrade/README.md)"
    )


@pytest.fixture(scope="module")
def release_checkout(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], Path]:
    """Build each release once; its current- and stale-cache cases only read the checkout."""
    root = tmp_path_factory.mktemp("releases")
    built: dict[str, Path] = {}

    def checkout(tag: str) -> Path:
        if tag not in built:
            built[tag] = build_release_checkout(tag, root / tag)
        return built[tag]

    return checkout


@pytest.mark.skipif(not exact_base_gate_enabled(), reason="dedicated exact-base upgrade gate")
@pytest.mark.parametrize(
    "release_tag",
    published_release_tags() if exact_base_gate_enabled() else ["gate-disabled"],
)
@pytest.mark.parametrize("stale_cache", [False, True], ids=["current-cache", "stale-cache"])
def test_release_update_from_every_published_release_validates(
    release_tag: str,
    stale_cache: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_checkout: Callable[[str], Path],
) -> None:
    """Every published release must migrate current and stale display caches.

    No supported source floor is declared yet; that policy decision remains open.

    This is the server's update across versions, from the releases a team server may
    still run: the copied-state validation, then the switched release's live check
    against the old release's data and display cache, current or left stale by a
    failed refresh. Then the candidate's merge self-check must accept a graph branch
    the old release populated.
    """
    from rcp.server_ops.deployment import (
        PrepareRequest,
        ValidateRequest,
        prepare,
        validate,
        verify_live_application,
    )

    # git archive fails for a missing tag; an incomplete fetch cannot skip a source.
    checkout = release_checkout(release_tag)
    root = tmp_path / "update"
    capture = capture_release_update_with(checkout, root, stale_cache=stale_cache)
    merge_branch_id = capture.pop("merge_branch_id")
    monkeypatch.setattr(
        storage_models,
        "DEFAULT_SERVER_LAYOUT",
        SimpleNamespace(
            service_account=pwd.getpwuid(os.geteuid()).pw_name,
            projects_root=root / "projects",
        ),
    )
    # Use the candidate's preparation path: installed releases cannot classify
    # scratch produced by every tool an agent may have used after their release.
    prepared = prepare(
        PrepareRequest(
            version=1,
            data_dir=str(root / "data"),
            output_dir=str(root / "prepared"),
            **capture,
        )
    )
    checked = validate(
        ValidateRequest(
            version=1,
            proof_path=str(prepared["proof_path"]),
            proof_sha256=str(prepared["proof_sha256"]),
            output_dir=str(tmp_path / "validated"),
        )
    )
    assert checked["status"] == "verified"
    live = create_app(data_dir=root / "data")
    assert verify_live_application(
        Path(checked["proof_path"]),
        proof_sha256=checked["proof_sha256"],
        background=live.state.background_tasks,
        catalog=live.state.catalog,
        store=live.state.background_tasks.store,
    )
    # On a real server the copied cache can be stale while the live one is current,
    # so each check may read either; a current cache must equal a fresh replay.
    for card in live.state.catalog.cards():
        status, cached = live.state.catalog.cached_snapshot_status(card["id"])
        _service, fresh = live.state.catalog.open_snapshot(card["id"])
        assert status == "valid" and cached is not None
        if stale_cache:
            assert cached["graph"]["revision"] < fresh["graph"]["revision"]
        else:
            assert cached["graph"] == fresh["graph"]
    _assert_old_branch_merge_validates(live, str(merge_branch_id))


@pytest.mark.parametrize("stale_cache", [False, True], ids=["current-cache", "stale-cache"])
def test_old_writer_link_rules_survive_upgrade_and_merge(manifest, tmp_path, stale_cache):
    """Frozen v3 bytes retain legacy links through replay, cache restart and merge."""
    from rcp.core.materialize import materialize_patches_from_state
    from rcp.core.models import Patch, upgrade_graph_projection
    from rcp.core.transition_models import GraphHeadRef
    from rcp.core.validation import ValidationContext
    from rcp.history.delta import semantic_delta
    from rcp.history.manager import PatchRejected
    from rcp.runs.merge_contract import MergeAnswers, MergePlanningContext
    from rcp.runs.merge_patch import prepare_merge_candidate
    from rcp.runs.merge_plan import plan_merge
    from tests.helpers import unchecked_write_authority
    from tests.test_merge_patch import plan_from_context

    # Captured using cc9ca220's actual models and HistoryManager before narrowing
    # the relation rules. Never regenerate this bundle with the candidate writer.
    payload = (Path(__file__).parent / "fixtures/ontology_links_v3.json.gz").read_bytes()
    assert hashlib.sha256(payload).hexdigest() == (
        "1a762af908739cec6509bdec5b19c209a874a3014d96e8a0094a6799d92f69a8"
    )
    bundle = json.loads(gzip.decompress(payload))
    for relative, content in bundle["files"].items():
        path = manifest.research_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    canonical = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in manifest.research_dir.rglob("*.json")
        if "patches" in path.parts or path.name == "scope-base.json"
    }
    expected_document = json.loads(bundle["files"]["graph.json"])
    upgrade_graph_projection(expected_document)
    if stale_cache:
        (manifest.research_dir / "graph.json").write_text(json.dumps(bundle["stale_cache"]))
    history = HistoryManager(manifest, write_authority=unchecked_write_authority())
    patches = history.load_patches()
    assert patches[0].transition is None
    assert all(p.transition.ruleset_tag == "rcp.lifecycle.v3" for p in patches[1:])
    assert any(
        event.node_id == "ev/old" and event.after == "superseded"
        for patch in patches[1:]
        for event in patch.transition.lifecycle_events
    )
    expected = history.materialize().state
    assert expected.model_dump(mode="json") == expected_document
    assert json.loads((manifest.research_dir / "graph.json").read_text()) == expected_document
    engine = history.history_engine
    engine.configure_store(tmp_path / "history-cache", "project")
    main_head = history.head_ref()
    assert engine.state_at(main_head) == expected
    assert (
        engine.state_at(GraphHeadRef(revision=1)).model_dump(mode="json") == bundle["stale_cache"]
    )
    restarted = HistoryManager(manifest, write_authority=unchecked_write_authority())
    restarted.history_engine.configure_store(tmp_path / "history-cache", "project")
    assert restarted.history_engine.state_at(main_head) == expected

    def context(branch_id):
        metadata = history.branch_metadata(branch_id)
        source = engine.state_at(metadata.head)
        base = engine.state_at(metadata.base_head)
        plan = plan_from_context(
            SimpleNamespace(
                metadata=metadata,
                merge_task_id="merge-old-links",
                authorized_by=metadata.authorized_by,
                main_head=main_head,
                base_graph=base,
                branch_graph=source,
                main_graph=expected,
                run_truth_scope=["repo-a"],
            )
        )
        return MergePlanningContext.model_validate(
            {
                key: value
                for key, value in plan.model_dump().items()
                if key in MergePlanningContext.model_fields
            }
        )

    inherited = context(bundle["branches"]["inherited"])
    plan = plan_merge(inherited)
    assert not plan.conflicts and not plan.review_groups
    prepared = prepare_merge_candidate(plan, MergeAnswers(answers={}, cards={}), history)
    merged = prepared.projection.graph
    assert merged.edges == expected.edges
    assert "ev/branch" in merged.nodes
    assert merged.proposals["prop/pending"] == expected.proposals["prop/pending"]
    source_proposal = inherited.source_graph.proposals["prop/branch"]
    carried = merged.proposals[source_proposal.id]
    assert carried.ops == source_proposal.ops and carried.card == source_proposal.card
    assert carried.base_rev == expected.revision and carried.raised_rev == merged.revision
    assert carried.created_by_operation_id == plan.merge_task_id
    replayed = materialize_patches_from_state(
        [prepared.patch],
        initial_state=expected,
        context=ValidationContext(project_truth_scope=expected.project_truth_scope),
    )
    assert semantic_delta(replayed.state, merged).is_empty
    # A branch link of a since-retired shape merges as main's own legacy links do.
    for index in range(4):
        legacy_plan = plan_merge(context(bundle["branches"][f"legacy-{index}"]))
        assert not legacy_plan.conflicts and not legacy_plan.review_groups
        carried_edges = prepare_merge_candidate(
            legacy_plan, MergeAnswers(answers={}, cards={}), history
        ).projection.graph.edges
        source_edges = legacy_plan.source_graph.edges
        (branch_edge,) = [edge for edge in source_edges if edge.startswith(f"edge/branch/{index}")]
        assert carried_edges[branch_edge].relation == source_edges[branch_edge].relation
    legacy = [edge for edge in expected.edges.values() if edge.id.startswith("edge/plain/")]
    removal = {"op": "remove_edges", "edge_ids": [edge.id for edge in legacy]}
    edit = {
        "kind": "work",
        "author": "agent",
        "summary": "Clean up old links.",
        "run_truth_scope": ["repo-a"],
    }
    replacement = {
        "op": "create_edges",
        "edges": [edge.model_dump(exclude={"layer", "created_rev"}) for edge in legacy],
    }
    with pytest.raises(PatchRejected) as rejected:
        history.append(
            Patch.model_validate({**edit, "ops": [removal, replacement]}),
            discard_on_reject=True,
        )
    assert {edge.id for edge in legacy} <= {
        edge_id
        for message in rejected.value.report.messages
        for edge_id in message.related_edge_ids
    }
    assert history.head_ref() == main_head
    _, cleaned = history.append(
        Patch.model_validate(
            {
                **edit,
                "ops": [
                    removal,
                    {
                        "op": "upsert_glossary",
                        "terms": [{"term": "Trial", "plain_definition": "A bounded measurement."}],
                    },
                ],
            }
        )
    )
    assert cleaned.state.edges == {
        key: edge for key, edge in expected.edges.items() if edge not in legacy
    }
    assert "Trial" in cleaned.state.glossary
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in canonical} == canonical


def _assert_old_branch_merge_validates(live, branch_id: str) -> None:
    """The candidate's merge self-check accepts a branch the old release populated."""
    from rcp.core.transition_models import GraphTargetRef
    from rcp.runs.branch_merge import merge_planning_context
    from rcp.runs.branch_merge_admission import start_branch_merge
    from rcp.runs.branch_merge_context import load_branch_merge_context
    from rcp.runs.branch_merge_request import BranchMergeRunRequest
    from rcp.runs.merge_contract import MergeAnswers
    from rcp.runs.merge_patch import prepare_merge_candidate
    from rcp.runs.merge_plan import plan_merge
    from rcp.storage import EpisodeRecord

    (card,) = live.state.catalog.cards()
    service, _snapshot = live.state.catalog.open_snapshot(card["id"])
    store = live.state.background_tasks.store
    metadata = service.history.branch_metadata(branch_id)
    now = store.now()
    # The old seed wrote the branch alone; the merge reads its owner through the episode.
    episode = store.create_episode(
        EpisodeRecord(
            episode_id=branch_id,
            project_id=card["id"],
            mode="auto_research",
            graph_target=GraphTargetRef(kind="branch", branch_id=branch_id),
            graph_base_head=metadata.base_head,
            status="queued",
            invocation_ceiling=1,
            authorized_by=metadata.authorized_by,
            created_at=now,
            updated_at=now,
        )
    )

    task = start_branch_merge(
        live.state.background_tasks,
        card["id"],
        BranchMergeRunRequest(
            episode_id=branch_id,
            provider="codex",
            model="qualification",
            reasoning="medium",
            run_on="server",
            run_truth_scope=["paper"],
            chat_scope="project",
            mode="work",
            patch_kind="work",
        ),
        authorized_by=metadata.authorized_by,
        launch=False,
    )

    def load_context():
        return load_branch_merge_context(
            service,
            store,
            episode,
            operation_id=task.operation_id,
            authorized_by=metadata.authorized_by,
        )

    context = load_context()
    plan = plan_merge(
        merge_planning_context(
            context, service.history, created_at=datetime.fromisoformat(store.now())
        )
    )
    assert not plan.conflicts
    assert not plan.review_groups
    assert any(change.collection == "proposals" for change in plan.direct_changes)
    prepared = prepare_merge_candidate(plan, MergeAnswers(answers={}, cards={}), service.history)
    assert prepared.patch.transition is not None
    source_ids = set(context.branch_graph.proposals) - set(context.base_graph.proposals)
    assert source_ids
    assert source_ids <= set(prepared.projection.graph.proposals)


def _exercise_candidate_upgrade(
    fixture: Path, *, baseline_schema: list[tuple[str, str, str, str]] | None = None
) -> None:
    metadata = verify_fixture_integrity(fixture)
    original_patches = _canonical_patch_hashes(fixture)
    data_dir = fixture / "data"
    database = _materialize_database(data_dir)
    experiment_episode_id = _metadata_optional_string(metadata, "experiment_episode_id")
    experiment_operation_id = _metadata_optional_string(metadata, "experiment_operation_id")
    expected_repair = metadata.get("expected_repair")
    _assert_raw_member_auth_rows(database, metadata)
    if expected_repair not in {None, "impossible_legacy_experiment_wrapup"}:
        raise ValueError("server-upgrade fixture names an unknown expected repair")
    if expected_repair is not None:
        assert experiment_episode_id is not None
        _assert_raw_impossible_experiment_wrapup(database, experiment_episode_id)
    with chdir(fixture):
        store = AppStore(database)
        if baseline_schema is not None:
            # Every immutable boundary converges on the schema a fresh store creates.
            assert _normalized_schema(database) == baseline_schema
        space_id = _metadata_string(metadata, "space_id")
        user_id = _metadata_string(metadata, "user_id")
        project_id = _metadata_string(metadata, "project_id")
        operation_id = _metadata_string(metadata, "active_operation_id")
        assert store.space_id == space_id
        assert store.space_kind == "team"
        member = store.space_user(user_id)
        assert member is not None
        assert member.removal_started_at is None
        assert member.removed_at is None
        assert {member.user_id for member in store.project_members(project_id)} == {user_id}
        _assert_member_auth_rows(store, metadata)
        upgraded_task = store.agent_task(operation_id)
        assert upgraded_task is not None
        assert upgraded_task.status == "running"
        assert upgraded_task.history_only is False
        _assert_provisioning_rows(store, metadata)
        if experiment_episode_id is not None:
            assert store.experiment_episode(experiment_episode_id) is not None
            if expected_repair is not None:
                assert store.episode_wrapup(experiment_episode_id) is None

        record = store.project(project_id)
        assert record is not None
        manifest = load_manifest(record.locator)
        replay = (
            HistoryManager(
                manifest,
                expected_space_id=space_id,
                project_id=project_id,
                write_authority=WriteAuthority(),
            )
            .initialize()
            .state
        )
        assert replay.replay_status == "complete"
        assert replay.revision == int(metadata["expected_revision"])

        app = create_app(
            data_dir=data_dir,
            acceptance_agent=True,
            trusted_principal_resolver=lambda _request, opened: opened.space_user(user_id),
        )
        with signed_in_client(app) as client:
            health = client.get("/api/health")
            assert health.status_code == 200
            health_payload = health.json()
            expected_health = {
                "status": "ok",
                "space_id": space_id,
                "space_kind": "team",
                "agent_mode": "acceptance",
            }
            assert {name: health_payload[name] for name in expected_health} == expected_health

            projects = client.get("/api/projects")
            assert projects.status_code == 200
            assert [item["id"] for item in projects.json()] == [project_id]

            projection = client.get(f"/api/projects/{project_id}")
            assert projection.status_code == 200
            payload = projection.json()
            assert payload["id"] == project_id
            assert payload["home_space_id"] == space_id
            assert payload["graph"]["revision"] == int(metadata["expected_revision"])

            tasks = client.get(f"/api/projects/{project_id}/tasks")
            assert tasks.status_code == 200
            task_payload = tasks.json()
            recovered = next(item for item in task_payload if item["operation_id"] == operation_id)
            assert recovered["status"] == "interrupted"
            assert recovered["active"] is False
            if experiment_operation_id is not None:
                experiment_task = next(
                    item for item in task_payload if item["operation_id"] == experiment_operation_id
                )
                assert experiment_task["active"] is False

        reopened = AppStore(database)
        _assert_member_auth_rows(reopened, metadata)
        _assert_provisioning_rows(reopened, metadata)
        assert reopened.agent_task(operation_id).status == "interrupted"
        if experiment_episode_id is not None:
            assert reopened.experiment_episode(experiment_episode_id) is not None
            if expected_repair is not None:
                assert reopened.episode_wrapup(experiment_episode_id) is None
        assert _canonical_patch_hashes(fixture) == original_patches
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _metadata_string(metadata: dict[str, object], name: str) -> str:
    value = metadata.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"server-upgrade fixture metadata is missing {name}")
    return value


def _metadata_optional_string(metadata: dict[str, object], name: str) -> str | None:
    value = metadata.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"server-upgrade fixture metadata has invalid {name}")
    return value


def _migration_versions(source: str) -> list[int]:
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_STORAGE_SCHEMA_MIGRATIONS"
            and node.value is not None
        ):
            return [version for version, _ in ast.literal_eval(node.value)]
    raise ValueError("storage migration registry not found")


def _git_capture(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=REPOSITORY_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout


def _materialize_database(data_dir: Path) -> Path:
    database = data_dir / "rcp.sqlite3"
    compressed = data_dir / "rcp.sqlite3.gz"
    if database.exists() or not compressed.is_file():
        raise ValueError("server-upgrade fixture database compression is invalid")
    database.write_bytes(gzip.decompress(compressed.read_bytes()))
    compressed.unlink()
    return database


def _canonical_patch_hashes(fixture: Path) -> dict[str, str]:
    patch_root = fixture / "project" / ".research" / "patches"
    return {
        path.relative_to(patch_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(patch_root.rglob("*"))
        if path.is_file()
    }


def _normalized_schema(database: Path) -> list[tuple[str, str, str, str]]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
    return [
        (object_type, name, table_name, re.sub(r"\s+", " ", sql or "").strip())
        for object_type, name, table_name, sql in rows
    ]


def _assert_member_auth_rows(store: AppStore, metadata: dict[str, object]) -> None:
    user_id = _metadata_string(metadata, "user_id")
    token_id = _metadata_string(metadata, "member_token_id")
    token_hash = _metadata_string(metadata, "member_token_hash")
    session_hash = _metadata_string(metadata, "member_session_hash")
    with store.connection() as connection:
        credential = connection.execute(
            """
            SELECT token_hash, revoked_at FROM team_member_tokens
            WHERE token_id = ? AND user_id = ?
            """,
            (token_id, user_id),
        ).fetchone()
        session = connection.execute(
            "SELECT user_id FROM team_sessions WHERE session_hash = ?",
            (session_hash,),
        ).fetchone()
    assert credential is not None
    assert credential["token_hash"] == token_hash
    assert credential["revoked_at"] is None
    assert session is not None
    assert session["user_id"] == user_id


def _assert_raw_member_auth_rows(database: Path, metadata: dict[str, object]) -> None:
    user_id = _metadata_string(metadata, "user_id")
    token_id = _metadata_string(metadata, "member_token_id")
    token_hash = _metadata_string(metadata, "member_token_hash")
    session_hash = _metadata_string(metadata, "member_session_hash")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        credential = connection.execute(
            """
            SELECT token_hash, revoked_at FROM team_member_tokens
            WHERE token_id = ? AND user_id = ?
            """,
            (token_id, user_id),
        ).fetchone()
        session = connection.execute(
            "SELECT user_id FROM team_sessions WHERE session_hash = ?",
            (session_hash,),
        ).fetchone()
    assert credential is not None
    assert credential["token_hash"] == token_hash
    assert credential["revoked_at"] is None
    assert session is not None
    assert session["user_id"] == user_id


def _assert_provisioning_rows(store: AppStore, metadata: dict[str, object]) -> None:
    request_id = _metadata_optional_string(metadata, "provisioning_request_id")
    proposed_project_id = _metadata_optional_string(metadata, "provisioning_project_id")
    if request_id is None and proposed_project_id is None:
        return
    assert request_id is not None
    assert proposed_project_id is not None
    request = store.project_provisioning_request(request_id)
    assert request is not None
    assert request.proposed_project_id == proposed_project_id
    assert request.status == "setup_in_progress"
    assert request.revision == 1
    expected_configuration = metadata.get("provisioning_configuration_complete")
    if expected_configuration is not None:
        assert isinstance(expected_configuration, bool)
        assert request.configuration_complete is expected_configuration
    legacy_request_id = _metadata_optional_string(
        metadata,
        "legacy_provisioning_request_id",
    )
    if legacy_request_id is not None:
        legacy = store.project_provisioning_request(legacy_request_id)
        assert legacy is not None
        assert legacy.status == "setup_in_progress"
        assert legacy.configuration_complete is False
    assert store.project(proposed_project_id) is None
    receipts = store.project_provisioning_step_receipts(request_id)
    assert [receipt.receipt_id for receipt in receipts] == ["fixture-setup-started"]


def _assert_raw_impossible_experiment_wrapup(database: Path, episode_id: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        episode = connection.execute(
            "SELECT status, ending, wrapup_state FROM episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        wrapup = connection.execute(
            "SELECT state FROM episode_wrapups WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
    assert episode is not None
    assert episode["status"] in {"queued", "running", "stopping"}
    assert episode["ending"] is None
    assert episode["wrapup_state"] == "not_started"
    assert wrapup is not None
    assert wrapup["state"] == "legacy_unavailable"
