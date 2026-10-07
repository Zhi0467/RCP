from __future__ import annotations

import asyncio
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.encoders import jsonable_encoder

import rcp.projects as projects_module
from rcp.agents import ProviderReadiness
from rcp.config import load_manifest, permissions_for
from rcp.core.models import Patch
from rcp.history import HistoryManager
from rcp.limits import COMPUTE_CONNECTION_MAX_COUNT
from rcp.providers import PROVIDER_IDS, ProviderUsage
from rcp.skill_registry import SkillDefaults, official_registry
from rcp.storage import AgentTaskRecord
from rcp.transport import StateUnavailable
from tests.helpers import sign_in_async_client, signed_in_client

from .helpers import (
    TASK_SETTLE_TIMEOUT,
    append_fixture_patch,
    async_wait_until,
    create_named_app,
    gated_patch,
    seed_patch,
    wait_for_entry,
    wait_until,
)


def _experiment_fixture_patch(
    experiment_id: str = "exp/bounded-loop",
    *,
    invocation_ceiling: int = 2,
) -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Added an experiment for control-loop tests.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": experiment_id,
                        "type": "experiment",
                        "title": "Bounded loop",
                        "objective": "Exercise the experiment control contract.",
                        "completion_criteria": ["The detached fixture exits cleanly."],
                        "invocation_ceiling": invocation_ceiling,
                    }
                ],
            }
        ],
    )


def test_project_display_boundary_completes_all_public_snapshots(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    append_fixture_patch(service, _experiment_fixture_patch())
    project_id = app.state.default_project_id
    assert project_id is not None
    draft = service.project_snapshot()

    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps(draft)
    with pytest.raises(ValueError, match="__dict__"):
        jsonable_encoder(draft)

    client = signed_in_client(app)
    generation = app.state.catalog.reserve_cached_snapshot_generation(project_id)
    assert app.state.catalog.commit_cached_snapshot(
        project_id,
        draft,
        generation=generation,
        patch_log_head=service.history.workspace.cached_patch_log_head(),
    )
    raw_saved = app.state.catalog.cached_snapshot(project_id)
    assert raw_saved is not None
    assert "experiment_control" not in raw_saved

    saved = client.get(f"/api/projects/{project_id}/cached")
    assert saved.status_code == 200
    assert set(saved.json()["experiment_control"]) == {"exp/bounded-loop"}

    app.state.catalog._cached_snapshot_path(project_id).unlink()
    current = client.get(f"/api/projects/{project_id}")
    assert current.status_code == 200
    assert set(current.json()["experiment_control"]) == {"exp/bounded-loop"}

    body = {
        "default_run_truth_scope": current.json()["default_run_truth_scope"],
        "agent_profiles": {
            surface: {key: profile[key] for key in ("provider", "model", "reasoning", "run_on")}
            for surface, profile in current.json()["agent_profiles"].items()
        },
    }
    settings = client.put(f"/api/projects/{project_id}/settings", json=body)
    assert settings.status_code == 200
    assert set(settings.json()["experiment_control"]) == {"exp/bounded-loop"}
    raw_after_settings = app.state.catalog.cached_snapshot(project_id)
    assert raw_after_settings is not None
    assert "experiment_control" not in raw_after_settings


def test_cached_project_migrates_retired_campaign_report_default(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    client = signed_in_client(app)
    assert client.get(f"/api/projects/{project_id}").status_code == 200

    cache_path = app.state.catalog._cached_snapshot_path(project_id)
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    envelope["snapshot"]["skill_defaults"]["skill_ids"] = [
        "campaign-report",
        "graph-audit",
    ]
    envelope["snapshot"]["skill_defaults_declared"] = ["skill_ids"]
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    saved = client.get(f"/api/projects/{project_id}/cached")

    assert saved.status_code == 200
    assert saved.json()["skill_defaults"]["skill_ids"] == ["episode-report", "graph-audit"]
    assert "campaign-report" in cache_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "corruption",
    ["attention", "attention_count", "asserted_count", "accepted_count", "contested_count"],
)
def test_cached_project_rejects_attention_that_disagrees_with_its_graph(
    manifest,
    tmp_path,
    corruption: str,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    client = signed_in_client(app)
    assert client.get(f"/api/projects/{project_id}").status_code == 200
    cache_path = app.state.catalog._cached_snapshot_path(project_id)
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    if corruption == "attention":
        envelope["snapshot"]["attention"]["open_blocker_ids"] = ["blk/not-in-graph"]
    elif corruption == "attention_count":
        envelope["snapshot"]["counts"]["open_blockers"] += 1
    else:
        envelope["snapshot"]["counts"][corruption.removesuffix("_count")] += 1
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    assert app.state.catalog.cached_snapshot_status(project_id) == ("invalid", None)


def test_project_revision_probe_is_small_and_does_not_replay_history(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    history = app.state.service.history

    assert client.get(f"/api/projects/{project_id}/revision").json() == {"revision": 1}
    append_fixture_patch(app.state.service, seed_patch())
    rejected, _ = append_fixture_patch(
        app.state.service,
        gated_patch(),
        raise_on_reject=False,
    )
    assert rejected.revision == 3
    assert rejected.admission == "rejected"

    monkeypatch.setattr(
        history,
        "materialize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("revision probe must not materialize history")
        ),
    )
    monkeypatch.setattr(
        history,
        "_replay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("revision probe must not replay history")
        ),
    )
    monkeypatch.setattr(
        history.workspace,
        "refresh_if_stale",
        lambda: (_ for _ in ()).throw(
            AssertionError("revision probe must not refresh canonical state")
        ),
    )
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("revision probe must not read canonical patch bodies")
        ),
    )
    monkeypatch.setattr(
        Path,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("revision probe must not open canonical patch files")
        ),
    )

    response = client.get(f"/api/projects/{project_id}/revision")

    assert response.status_code == 200
    assert response.json() == {"revision": 2}
    assert list(response.json()) == ["revision"]


def test_project_revision_probe_returns_normal_project_not_found(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    response = signed_in_client(app).get(f"/api/projects/{uuid.uuid4()}/revision")

    assert response.status_code == 404


def test_cached_revision_heartbeat_is_cache_only_and_unchanged_head_starts_no_refresh(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    initial = signed_in_client(app).get(f"/api/projects/{project_id}").json()
    probes = 0

    def unchanged_head(requested_project_id):
        nonlocal probes
        assert requested_project_id == project_id
        probes += 1
        return "unchanged"

    monkeypatch.setattr(app.state.catalog, "probe_remote_patch_log_head", unchanged_head)
    monkeypatch.setattr(
        app.state.catalog,
        "reconcile_snapshot",
        lambda _project_id: (_ for _ in ()).throw(
            AssertionError("an unchanged head must not start a full refresh")
        ),
    )

    async def drive() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await sign_in_async_client(client, app)
            response = await client.get(f"/api/projects/{project_id}/cached/revision")
            for _ in range(100):
                if project_id not in app.state.project_reconciliation_tasks:
                    break
                await asyncio.sleep(0.01)
            return response

    response = asyncio.run(drive())

    assert response.status_code == 200
    assert response.json() == {
        "revision": initial["revision"],
        "snapshot_freshness": "fresh",
        "last_remote_sync_at": None,
        "compute_probes_probed_at": None,
    }
    assert probes == 1


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_unchanged_head_reconciles_cached_offline_state(
    manifest, tmp_path, monkeypatch, refresh_fails
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    initial = signed_in_client(app).get(f"/api/projects/{project_id}").json()
    offline = {
        **initial,
        "canonical_state": {
            **initial["canonical_state"],
            "remote": True,
            "reachable": False,
            "error": "Connection lost",
        },
        "snapshot_freshness": "stale",
    }
    app.state.catalog.write_cached_snapshot(project_id, offline)
    monkeypatch.setattr(
        app.state.catalog, "probe_remote_patch_log_head", lambda _project_id: "unchanged"
    )
    reconcile_snapshot = app.state.catalog.reconcile_snapshot
    refresh_calls = []

    def refresh(requested_project_id):
        refresh_calls.append(requested_project_id)
        if refresh_fails:
            raise OSError("Canonical lock remains unavailable")
        return reconcile_snapshot(requested_project_id)

    monkeypatch.setattr(app.state.catalog, "reconcile_snapshot", refresh)

    async def drive():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await sign_in_async_client(client, app)
            await client.get(f"/api/projects/{project_id}/cached/revision")
            await async_wait_until(
                lambda: project_id not in app.state.project_reconciliation_tasks,
                detail="offline project reconciliation did not finish",
            )
            return (await client.get(f"/api/projects/{project_id}")).json()

    refreshed = asyncio.run(drive())
    assert refresh_calls == [project_id]
    assert refreshed["revision"] == initial["revision"]
    assert refreshed["snapshot_freshness"] == ("stale" if refresh_fails else "fresh")
    assert refreshed["canonical_state"] == (
        offline["canonical_state"] if refresh_fails else initial["canonical_state"]
    )


def test_cached_revision_file_read_does_not_block_the_event_loop(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert signed_in_client(app).get(f"/api/projects/{project_id}").status_code == 200
    display_cache = app.state.services.project_display_cache
    original = display_cache.cached_project_snapshot
    entered = threading.Event()
    release = threading.Event()

    def blocked_snapshot(requested_project_id: str):
        entered.set()
        assert release.wait(TASK_SETTLE_TIMEOUT)
        return original(requested_project_id)

    monkeypatch.setattr(display_cache, "cached_project_snapshot", blocked_snapshot)

    async def drive() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await sign_in_async_client(client, app)
            revision_task = asyncio.create_task(
                client.get(f"/api/projects/{project_id}/cached/revision")
            )
            await asyncio.to_thread(wait_for_entry, entered)
            health = await asyncio.wait_for(client.get("/api/health"), timeout=TASK_SETTLE_TIMEOUT)
            # Ordering, not latency: health answered while the cached snapshot
            # read was still parked in the patched call.
            still_reading = not revision_task.done()
            release.set()
            return await revision_task, health, still_reading

    revision, health, still_reading = asyncio.run(drive())

    assert still_reading
    assert revision.status_code == 200
    assert health.status_code == 200


def test_cached_revision_heartbeat_enforces_three_second_probe_cooldown(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert signed_in_client(app).get(f"/api/projects/{project_id}").status_code == 200
    clock = 100.0
    probes = 0

    class FakeTime:
        @staticmethod
        def monotonic() -> float:
            return clock

    def unchanged_head(_project_id):
        nonlocal probes
        probes += 1
        return "unchanged"

    monkeypatch.setattr(projects_module, "time", FakeTime)
    monkeypatch.setattr(app.state.catalog, "probe_remote_patch_log_head", unchanged_head)

    async def wait_for_probe() -> None:
        for _ in range(100):
            if project_id not in app.state.project_reconciliation_tasks:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("background probe did not complete")

    async def drive() -> list[httpx.Response]:
        nonlocal clock
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await sign_in_async_client(client, app)
            first = await client.get(f"/api/projects/{project_id}/cached/revision")
            await wait_for_probe()
            clock = 102.999
            inside_cooldown = await client.get(f"/api/projects/{project_id}/cached/revision")
            await asyncio.sleep(0)
            assert project_id not in app.state.project_reconciliation_tasks
            clock = 103.0
            at_boundary = await client.get(f"/api/projects/{project_id}/cached/revision")
            await wait_for_probe()
            return [first, inside_cooldown, at_boundary]

    responses = asyncio.run(drive())

    assert all(response.status_code == 200 for response in responses)
    assert probes == 2


def test_moved_head_refreshes_in_background_singleflight(manifest, tmp_path, monkeypatch) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    initial = signed_in_client(app).get(f"/api/projects/{project_id}").json()
    append_fixture_patch(app.state.service, seed_patch())
    append_fixture_patch(app.state.service, _experiment_fixture_patch())
    entered = threading.Event()
    release = threading.Event()
    probe_calls = 0
    refresh_calls = 0
    reconcile_snapshot = app.state.catalog.reconcile_snapshot

    def moved_head(requested_project_id):
        nonlocal probe_calls
        assert requested_project_id == project_id
        probe_calls += 1
        return "moved"

    def blocked_reconcile(requested_project_id):
        nonlocal refresh_calls
        refresh_calls += 1
        entered.set()
        assert release.wait(TASK_SETTLE_TIMEOUT)
        return reconcile_snapshot(requested_project_id)

    monkeypatch.setattr(app.state.catalog, "probe_remote_patch_log_head", moved_head)
    monkeypatch.setattr(app.state.catalog, "reconcile_snapshot", blocked_reconcile)

    async def drive() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await sign_in_async_client(client, app)
            first = await client.get(f"/api/projects/{project_id}/cached/revision")
            await asyncio.to_thread(wait_for_entry, entered)
            second = await asyncio.wait_for(
                client.get(f"/api/projects/{project_id}/cached/revision"),
                timeout=TASK_SETTLE_TIMEOUT,
            )
            project = await asyncio.wait_for(
                client.get(f"/api/projects/{project_id}"),
                timeout=TASK_SETTLE_TIMEOUT,
            )
            release.set()
            await async_wait_until(
                lambda: project_id not in app.state.project_reconciliation_tasks,
                detail="background reconciliation never drained",
            )
            return first, second, project

    first, second, project = asyncio.run(drive())
    refreshed = app.state.catalog.cached_snapshot(project_id)

    assert first.json()["revision"] == initial["revision"]
    assert second.status_code == 200
    assert project.status_code == 200
    assert project.json()["revision"] == initial["revision"]
    assert probe_calls == 1
    assert refresh_calls == 1
    assert refreshed is not None
    assert refreshed["revision"] == 3
    assert refreshed["snapshot_freshness"] == "fresh"
    assert set(refreshed["experiment_control"]) == {"exp/bounded-loop"}


def test_local_patch_head_refreshes_cache_without_joining_the_write_path(
    manifest, tmp_path
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    initial = signed_in_client(app).get(f"/api/projects/{project_id}").json()
    append_fixture_patch(app.state.service, seed_patch())

    async def drive() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await sign_in_async_client(client, app)
            response = await client.get(f"/api/projects/{project_id}/cached/revision")
            for _ in range(100):
                if project_id not in app.state.project_reconciliation_tasks:
                    break
                await asyncio.sleep(0.01)
            return response

    response = asyncio.run(drive())
    refreshed = app.state.catalog.cached_snapshot(project_id)

    assert response.json()["revision"] == initial["revision"]
    assert refreshed is not None
    assert refreshed["revision"] == 2


def test_transient_head_probe_failure_marks_only_display_freshness_stale(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    initial = signed_in_client(app).get(f"/api/projects/{project_id}").json()
    monkeypatch.setattr(
        app.state.catalog,
        "probe_remote_patch_log_head",
        lambda _project_id: "unavailable",
    )

    async def drive() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await sign_in_async_client(client, app)
            await client.get(f"/api/projects/{project_id}/cached/revision")
            for _ in range(100):
                if project_id not in app.state.project_reconciliation_tasks:
                    break
                await asyncio.sleep(0.01)

    asyncio.run(drive())
    cached = app.state.catalog.cached_snapshot(project_id)

    assert cached is not None
    assert cached["snapshot_freshness"] == "stale"
    assert cached["canonical_state"] == initial["canonical_state"]


def test_project_get_creates_then_reuses_display_snapshot_without_reopening(
    manifest, tmp_path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    client = signed_in_client(app)
    project_id = app.state.default_project_id

    initial = client.get(f"/api/projects/{project_id}")

    assert initial.status_code == 200
    cached_files = list((data_dir / "project-snapshots").iterdir())
    assert len(cached_files) == 1
    cache_path = cached_files[0]
    initial_envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    assert initial_envelope["schema_version"] == 5
    assert initial_envelope["canonical_patch_head"] == 1
    assert initial_envelope["project_id"] == project_id
    assert initial_envelope["snapshot"] == initial.json()
    assert initial_envelope["snapshot"]["default_auto_research_invocation_ceiling"] == 10
    assert "default_campaign_invocation_ceiling" not in initial_envelope["snapshot"]

    monkeypatch.setattr(
        app.state.catalog,
        "open_snapshot",
        lambda _project_id: (_ for _ in ()).throw(
            AssertionError("cached project navigation must not open canonical state")
        ),
    )
    monkeypatch.setattr(
        app.state.catalog,
        "probe_remote_patch_log_head",
        lambda _project_id: (_ for _ in ()).throw(
            AssertionError("cached project navigation must not issue a remote probe")
        ),
    )
    cached = client.get(f"/api/projects/{project_id}")

    assert cached.status_code == 200
    assert cached.json() == initial.json()
    assert list((data_dir / "project-snapshots").iterdir()) == [cache_path]
    assert json.loads(cache_path.read_text(encoding="utf-8")) == initial_envelope
    assert client.get(f"/api/projects/{project_id}/cached").json() == initial.json()


def test_pre_branch_display_cache_decodes_as_main_and_new_cache_rejects_a_branch(
    manifest, tmp_path
) -> None:
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    initial = client.get(f"/api/projects/{project_id}").json()
    cache_path = next((data_dir / "project-snapshots").iterdir())
    envelope = json.loads(cache_path.read_text())
    envelope["schema_version"] = 4
    for field in ("graph_target", "graph_head", "graph_changes"):
        del envelope["snapshot"][field]
    cache_path.write_text(json.dumps(envelope))
    restored = client.get(f"/api/projects/{project_id}/cached")
    assert restored.status_code == 200
    assert restored.json() == initial

    envelope["schema_version"] = 5
    envelope["snapshot"] = restored.json()
    target = {"kind": "branch", "branch_id": str(uuid.uuid4())}
    envelope["snapshot"]["graph_target"] = target
    envelope["snapshot"]["graph_head"]["target"] = target
    cache_path.write_text(json.dumps(envelope))
    assert client.get(f"/api/projects/{project_id}/cached").status_code == 404


def test_cached_project_backfills_a_prior_choice_the_old_cache_never_stored(
    manifest, tmp_path
) -> None:
    # A cache written before `decision_prior_choices` existed decodes it as the
    # model default. For a project holding a Decision whose options no longer
    # offer its recorded choice the expected map is not empty, so without a
    # backfill the snapshot comparison rejects the cache and the project loses
    # its offline copy.
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    client.get(f"/api/projects/{project_id}")
    cache_path = next((data_dir / "project-snapshots").iterdir())
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))

    snapshot = envelope["snapshot"]
    snapshot["graph"]["nodes"]["dec/shape"] = {
        "id": "dec/shape",
        "type": "decision",
        "title": "Resource shape",
        "question": "Which resource shape?",
        "options": ["4xA100, with the revised sizing rule", "8xA100"],
        "selected_option": "4xA100",
        "status": "revisit",
        "standing": "asserted",
        "created_rev": 1,
        "updated_rev": 1,
        "source_refs": [],
        "extension_fields": {},
    }
    snapshot["counts"]["decisions_awaiting_choice"] += 1
    snapshot["counts"]["asserted"] += 1
    snapshot["attention"]["decisions_awaiting_choice_ids"] = ["dec/shape"]
    snapshot["attention"].pop("decision_prior_choices")
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    restored = client.get(f"/api/projects/{project_id}/cached")

    assert restored.status_code == 200
    assert restored.json()["attention"]["decision_prior_choices"] == {"dec/shape": "4xA100"}


def test_cached_project_serves_the_graph_a_fresh_replay_would(manifest, tmp_path) -> None:
    # A cache written before a graph field gained a default lacks that key. The
    # live release's projection must still equal the replayed one a release update
    # verified, so the cache is served in the current model's shape.
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    append_fixture_patch(app.state.service, seed_patch())
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    fresh = client.get(f"/api/projects/{project_id}").json()["graph"]
    cache_path = next((data_dir / "project-snapshots").iterdir())
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    edges = envelope["snapshot"]["graph"]["edges"]
    assert edges
    for edge in edges.values():
        edge.pop("expectation")
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    restored = client.get(f"/api/projects/{project_id}/cached")

    assert restored.status_code == 200
    assert restored.json()["graph"] == fresh


def test_cached_project_rejects_malformed_mismatched_and_oversize_files(
    manifest, tmp_path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    authoritative = client.get(f"/api/projects/{project_id}")
    assert authoritative.status_code == 200
    cache_path = next((data_dir / "project-snapshots").iterdir())

    cache_path.write_text("{", encoding="utf-8")
    assert client.get(f"/api/projects/{project_id}/cached").status_code == 404

    mismatched = {
        "schema_version": 1,
        "project_id": "different-project",
        "snapshot": authoritative.json(),
    }
    cache_path.write_text(json.dumps(mismatched), encoding="utf-8")
    assert client.get(f"/api/projects/{project_id}/cached").status_code == 404

    mismatched["project_id"] = project_id
    mismatched["snapshot"]["id"] = "different-project"
    cache_path.write_text(json.dumps(mismatched), encoding="utf-8")
    assert client.get(f"/api/projects/{project_id}/cached").status_code == 404

    legacy_snapshot = authoritative.json()
    legacy_snapshot["default_campaign_invocation_ceiling"] = legacy_snapshot.pop(
        "default_auto_research_invocation_ceiling"
    )
    legacy_snapshot["attention"].pop("proposal_actions")
    legacy_snapshot["attention"].pop("decision_prior_choices")
    legacy_snapshot.pop("graph_mutation")
    legacy = {
        "schema_version": 2,
        "project_id": project_id,
        "canonical_patch_head": 1,
        "snapshot": legacy_snapshot,
    }
    cache_path.write_text(json.dumps(legacy), encoding="utf-8")
    migrated = client.get(f"/api/projects/{project_id}/cached")
    assert migrated.status_code == 200
    assert migrated.json()["default_auto_research_invocation_ceiling"] == 10
    assert "default_campaign_invocation_ceiling" not in migrated.json()
    assert migrated.json()["attention"]["proposal_actions"] == {}
    assert migrated.json()["attention"]["decision_prior_choices"] == {}
    assert migrated.json()["graph_mutation"] == {"available": True, "reason": None}

    legacy["snapshot"]["default_auto_research_invocation_ceiling"] = 11
    cache_path.write_text(json.dumps(legacy), encoding="utf-8")
    assert client.get(f"/api/projects/{project_id}/cached").status_code == 404

    monkeypatch.setattr(projects_module, "PROJECT_DISPLAY_SNAPSHOT_MAX_BYTES", 16)
    cache_path.write_bytes(b"x" * 17)
    assert client.get(f"/api/projects/{project_id}/cached").status_code == 404


def test_cached_project_takes_this_release_rendering_of_proposal_actions(
    manifest, tmp_path
) -> None:
    """A cache written by an older release carries older Proposal action lines.

    The lines only render the cached graph's Proposals, so the cache stays valid
    and shows this release's rendering instead of being discarded on update.
    """
    from rcp.core.attention import project_counts, project_graph_attention
    from rcp.core.models import GraphState

    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    authoritative = client.get(f"/api/projects/{project_id}")
    assert authoritative.status_code == 200
    cache_path = next((data_dir / "project-snapshots").iterdir())

    snapshot = authoritative.json()
    snapshot["graph"]["nodes"]["hyp/vague"] = {
        "id": "hyp/vague",
        "type": "hypothesis",
        "title": "Replanning may help",
        "statement": "Replanning may help.",
        "status": "active",
    }
    snapshot["graph"]["proposals"]["prop/sharpen"] = {
        "id": "prop/sharpen",
        "title": "Sharpen the hypothesis",
        "card": {"decision_needed": "Approve the sharper wording."},
        "ops": [
            {
                "op": "update_nodes",
                "intent": "content_change",
                "nodes": [{"id": "hyp/vague", "changes": {"statement": "Replanning helps."}}],
            }
        ],
        "status": "pending",
    }
    graph = GraphState.model_validate(snapshot["graph"])
    attention = project_graph_attention(graph)
    snapshot["counts"].update(project_counts(graph, attention).model_dump(mode="json"))
    snapshot["attention"] = attention.model_dump(mode="json")
    expected_lines = snapshot["attention"]["proposal_actions"]["prop/sharpen"]
    snapshot["attention"]["proposal_actions"]["prop/sharpen"] = [
        {"label": "Node", "text": "Replanning may help"},
        {"label": "Current statement", "text": "“Replanning may help.”"},
        {"label": "Proposed statement", "text": "“Replanning helps.”"},
    ]
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "project_id": project_id,
                "canonical_patch_head": 1,
                "snapshot": snapshot,
            }
        ),
        encoding="utf-8",
    )

    cached = client.get(f"/api/projects/{project_id}/cached")
    assert cached.status_code == 200
    assert cached.json()["attention"]["proposal_actions"]["prop/sharpen"] == expected_lines
    assert expected_lines[1]["before"] == "Replanning may help."


def test_cached_snapshot_names_the_runtime_on_profiles_saved_before_selection(
    manifest, tmp_path
) -> None:
    """A cached profile predating runtime selection is still readable.

    `agent_profiles` is part of the cached payload, so the first read after the
    upgrade would otherwise hand the settings form a profile with no runtime.
    """

    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    authoritative = client.get(f"/api/projects/{project_id}")
    assert authoritative.status_code == 200
    cache_path = next((data_dir / "project-snapshots").iterdir())

    legacy_snapshot = authoritative.json()
    for profile in legacy_snapshot["agent_profiles"].values():
        del profile["runtime"]
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "project_id": project_id,
                "canonical_patch_head": 1,
                "snapshot": legacy_snapshot,
            }
        ),
        encoding="utf-8",
    )

    migrated = client.get(f"/api/projects/{project_id}/cached")
    assert migrated.status_code == 200
    profiles = migrated.json()["agent_profiles"]
    assert profiles
    for surface, profile in profiles.items():
        expected = "exec" if profile["provider"] == "codex" else "stream-json"
        assert profile["runtime"] == expected, surface


def _write_stale_catalog_cache(client, data_dir, project_id) -> Path:
    """Rewrite the display cache as an older release left it: one official skill fewer."""

    assert client.get(f"/api/projects/{project_id}").status_code == 200
    cache_path = next((data_dir / "project-snapshots").iterdir())
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    catalog = envelope["snapshot"]["skill_catalog"]
    envelope["snapshot"]["skill_catalog"] = catalog[1:]
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")
    return cache_path


def test_cached_snapshot_serves_the_running_release_skill_catalog(manifest, tmp_path) -> None:
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    _write_stale_catalog_cache(client, data_dir, project_id)

    for path in (f"/api/projects/{project_id}", f"/api/projects/{project_id}/cached"):
        served = client.get(path)
        assert served.status_code == 200
        assert served.json()["skill_catalog"] == official_registry().catalog()


def test_clearing_project_cache_rebuilds_the_display_snapshot(manifest, tmp_path) -> None:
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    cache_path = _write_stale_catalog_cache(client, data_dir, project_id)
    opened = app.state.catalog.store.project(project_id).last_opened_at

    cleared = client.delete(f"/api/projects/{project_id}/caches")
    assert cleared.status_code == 200
    assert cleared.json()["project_page_rebuilt"] is True
    # Cache maintenance is not an open: the landing page's recency is kept.
    assert app.state.catalog.store.project(project_id).last_opened_at == opened
    rebuilt = json.loads(cache_path.read_text(encoding="utf-8"))
    assert rebuilt["snapshot"]["skill_catalog"] == official_registry().catalog()


def _unreachable_reconcile(_self, _project_id):
    raise StateUnavailable("state host is unreachable")


def _cold_mirror_reconcile(self, project_id):
    service, snapshot = _ORIGINAL_RECONCILE(self, project_id)
    snapshot["canonical_state"] = {**snapshot["canonical_state"], "reachable": False}
    return service, snapshot


_ORIGINAL_RECONCILE = projects_module.ProjectCatalog.reconcile_snapshot


@pytest.mark.parametrize(
    ("target", "replacement"),
    [
        ("reconcile_snapshot", _unreachable_reconcile),
        ("reconcile_snapshot", _cold_mirror_reconcile),
        ("commit_cached_snapshot", lambda *_args, **_kwargs: False),
    ],
    ids=["unreachable", "cold-mirror", "lost-race"],
)
def test_failed_display_rebuild_keeps_the_offline_copy(
    manifest, tmp_path, monkeypatch, target, replacement
) -> None:
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    cache_path = _write_stale_catalog_cache(client, data_dir, project_id)
    before = cache_path.read_bytes()

    monkeypatch.setattr(projects_module.ProjectCatalog, target, replacement)
    cleared = client.delete(f"/api/projects/{project_id}/caches")
    assert cleared.status_code == 200
    assert cleared.json()["project_page_rebuilt"] is False
    assert cache_path.read_bytes() == before


def test_clearing_a_project_deleted_mid_rebuild_is_not_found(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    assert client.get(f"/api/projects/{project_id}").status_code == 200

    def deleted(_self, missing_id):
        raise KeyError(missing_id)

    monkeypatch.setattr(projects_module.ProjectCatalog, "reconcile_snapshot", deleted)
    assert client.delete(f"/api/projects/{project_id}/caches").status_code == 404


def test_cached_snapshot_refills_only_undeclared_skill_defaults(manifest, tmp_path) -> None:
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    assert client.get(f"/api/projects/{project_id}").status_code == 200
    cache_path = next((data_dir / "project-snapshots").iterdir())
    current = SkillDefaults().model_dump(mode="json")
    older = {**current, "skill_ids": current["skill_ids"][1:]}

    def serve(declared: list[str]) -> dict[str, object]:
        envelope = json.loads(cache_path.read_text(encoding="utf-8"))
        envelope["snapshot"]["skill_defaults"] = older
        envelope["snapshot"]["skill_defaults_declared"] = declared
        cache_path.write_text(json.dumps(envelope), encoding="utf-8")
        return client.get(f"/api/projects/{project_id}").json()["skill_defaults"]

    assert serve([]) == current
    assert serve(["skill_ids"])["skill_ids"] == older["skill_ids"]


def test_cache_predating_declared_defaults_rebuilds_once_in_background(manifest, tmp_path) -> None:
    data_dir = tmp_path / "data"
    app = create_named_app(str(manifest.path), data_dir=data_dir)
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    assert client.get(f"/api/projects/{project_id}").status_code == 200
    cache_path = next((data_dir / "project-snapshots").iterdir())
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    current = envelope["snapshot"]["skill_defaults"]
    envelope["snapshot"]["skill_defaults"] = {**current, "skill_ids": current["skill_ids"][1:]}
    del envelope["snapshot"]["skill_defaults_declared"]
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    assert client.get(f"/api/projects/{project_id}/cached").status_code == 200

    def rebuilt() -> dict[str, object] | None:
        snapshot = json.loads(cache_path.read_text(encoding="utf-8"))["snapshot"]
        return snapshot if "skill_defaults_declared" in snapshot else None

    assert wait_until(rebuilt, timeout=TASK_SETTLE_TIMEOUT)["skill_defaults"] == current


def test_project_readiness_does_not_open_or_materialize_project(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    app.state.catalog._services.clear()
    monkeypatch.setattr(
        HistoryManager,
        "initialize",
        lambda _history: (_ for _ in ()).throw(
            AssertionError("readiness must not materialize project history")
        ),
    )
    calls: list[tuple[str, bool]] = []
    compute_calls: list[list[str]] = []
    inventory_waits: list[tuple[str, str, str | None]] = []

    def readiness(provider: str, *, host: str = "", refresh: bool = False):
        calls.append((provider, refresh))
        return ProviderReadiness(
            provider=provider,
            installed=True,
            authenticated=True,
            version=f"{provider}-ready",
        )

    monkeypatch.setattr(app.state.catalog.launcher, "readiness", readiness)
    monkeypatch.setattr(
        "rcp.projects.probe_compute_connections",
        lambda probed_manifest: (
            compute_calls.append(
                [connection.id for connection in probed_manifest.compute_connections]
            )
            or {"laptop": {}}
        ),
    )
    skill_refreshes: list[tuple[str, str, str | None]] = []

    def refresh_skills(provider: str, host: str, binary: str | None, _readiness, *, reuse_cached):
        # The explicit Refresh is the product path that re-probes skills uncached.
        assert reuse_cached is False
        skill_refreshes.append((provider, host, binary))

    monkeypatch.setattr(app.state.provider_skills, "refresh", refresh_skills)
    monkeypatch.setattr(
        app.state.provider_skills,
        "wait",
        lambda provider, host, binary: inventory_waits.append((provider, host, binary)) or True,
    )

    response = client.get(f"/api/projects/{project_id}/readiness")
    refreshed = client.get(f"/api/projects/{project_id}/readiness?refresh=true")
    cached = client.get(f"/api/projects/{project_id}/readiness")

    assert response.status_code == refreshed.status_code == cached.status_code == 200
    assert response.json()["compute_status"] == {}
    assert refreshed.json()["compute_status"] == {"laptop": {}}
    assert cached.json()["compute_status"] == refreshed.json()["compute_status"]
    assert compute_calls == [[]]
    assert response.json()["provider_readiness"]["laptop"]["codex"]["version"] == ("codex-ready")
    assert response.json()["providers"] == response.json()["provider_readiness"]["laptop"]
    # The cold path exports the effective profiles too, without opening history.
    assert set(response.json()["agent_profiles"]) >= {"seed", "refresh", "node_chat"}
    assert all(
        "effective_model" in profile for profile in response.json()["agent_profiles"].values()
    )
    assert response.json()["provider_skill_inventories"]["laptop"]["codex"]["status"] == (
        "unavailable"
    )
    assert set(calls) == {
        (provider, refresh) for provider in PROVIDER_IDS for refresh in (False, True)
    }
    assert inventory_waits == [(provider, "", None) for provider in PROVIDER_IDS] * 3
    # Only the explicit refresh probed skills; the two implicit reads started none.
    assert sorted(skill_refreshes) == sorted((provider, "", None) for provider in PROVIDER_IDS)
    assert project_id not in app.state.catalog._services


def test_loaded_project_compute_readiness_probes_only_on_refresh_and_invalidates_on_change(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    before = client.get(f"/api/projects/{project_id}").json()
    body = {
        "default_run_truth_scope": before["default_run_truth_scope"],
        "agent_profiles": {
            surface: {
                key: profile[key] for key in ("provider", "runtime", "model", "reasoning", "run_on")
            }
            for surface, profile in before["agent_profiles"].items()
        },
    }
    connection = {
        "id": "gpu",
        "name": "GPU VM",
        "kind": "ssh",
        "ssh_target": "alice@gpu.example",
        "access_hint": "Use /scratch/shared",
    }
    saved = client.put(
        f"/api/projects/{project_id}/settings",
        json={**body, "compute_connections": [connection]},
    )
    assert saved.status_code == 200

    compute_calls: list[list[str]] = []
    status = {
        "laptop": {
            "gpu": {
                "compute_id": "gpu",
                "execution_machine": "laptop",
                "state": "reachable",
                "reachable": True,
                "diagnostic": "SSH connection succeeded.",
                "required_action": None,
                "status_label": "Reachable",
                "status_tone": "ready",
            }
        }
    }

    def probe(probed_manifest):
        compute_calls.append([connection.id for connection in probed_manifest.compute_connections])
        return status

    monkeypatch.setattr("rcp.projects.probe_compute_connections", probe)

    first = client.get(f"/api/projects/{project_id}/readiness")
    second = client.get(f"/api/projects/{project_id}/readiness")
    refreshed = client.get(f"/api/projects/{project_id}/readiness?refresh=true")
    cached = client.get(f"/api/projects/{project_id}/readiness")

    assert first.json()["compute_status"] == second.json()["compute_status"] == {}
    assert refreshed.json()["compute_status"] == cached.json()["compute_status"] == status
    assert compute_calls == [["gpu"]]

    changed_connection = {**connection, "ssh_target": "alice@gpu-2.example"}
    changed = client.put(
        f"/api/projects/{project_id}/settings",
        json={**body, "compute_connections": [changed_connection]},
    )
    after_change = client.get(f"/api/projects/{project_id}/readiness")
    assert changed.status_code == after_change.status_code == 200
    assert changed.json()["compute_status"] == {}
    assert after_change.json()["compute_status"] == {}
    assert compute_calls == [["gpu"]]

    failed_refreshes: list[str] = []

    def failed_probe(probed_manifest):
        failed_refreshes.append(probed_manifest.compute_connections[0].ssh_target)
        raise RuntimeError("compute probe refresh failed")

    monkeypatch.setattr("rcp.projects.probe_compute_connections", failed_probe)
    failed = client.get(f"/api/projects/{project_id}/readiness?refresh=true")
    still_empty = client.get(f"/api/projects/{project_id}/readiness")

    assert failed.status_code == 503
    assert failed.json()["detail"] == "compute probe refresh failed"
    assert still_empty.status_code == 200
    assert still_empty.json()["compute_status"] == {}
    assert failed_refreshes == ["alice@gpu-2.example"]


def test_stale_compute_refresh_does_not_overwrite_fresher_status(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    catalog = app.state.catalog
    project_id = app.state.default_project_id
    before = client.get(f"/api/projects/{project_id}").json()
    body = {
        "default_run_truth_scope": before["default_run_truth_scope"],
        "agent_profiles": {
            surface: {
                key: profile[key] for key in ("provider", "runtime", "model", "reasoning", "run_on")
            }
            for surface, profile in before["agent_profiles"].items()
        },
    }
    old_connection = {
        "id": "gpu",
        "name": "GPU VM",
        "kind": "ssh",
        "ssh_target": "alice@old-gpu.example",
        "access_hint": "",
    }
    saved = client.put(
        f"/api/projects/{project_id}/settings",
        json={**body, "compute_connections": [old_connection]},
    )
    assert saved.status_code == 200
    stale_manifest = app.state.service.manifest

    stale_waiting = threading.Barrier(2)
    fresh_stored = threading.Barrier(2)
    refresh_kind = threading.local()

    class OrderedProbeLock:
        def __enter__(self):
            if getattr(refresh_kind, "value", None) == "stale":
                stale_waiting.wait(timeout=5)
                fresh_stored.wait(timeout=5)
            return self

        def __exit__(self, *_args):
            if getattr(refresh_kind, "value", None) == "fresh":
                fresh_stored.wait(timeout=5)

    catalog._compute_probe_locks[project_id] = OrderedProbeLock()
    statuses = {
        "alice@old-gpu.example": {"laptop": {"gpu": {"status_label": "Old"}}},
        "alice@new-gpu.example": {"laptop": {"gpu": {"status_label": "Fresh"}}},
    }
    probe_order: list[str] = []

    def probe(probed_manifest):
        target = probed_manifest.compute_connections[0].ssh_target
        probe_order.append(target)
        return statuses[target]

    def refresh(probed_manifest, kind):
        refresh_kind.value = kind
        return catalog._compute_status_snapshot(project_id, probed_manifest, refresh=True)

    monkeypatch.setattr("rcp.projects.probe_compute_connections", probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stale = pool.submit(refresh, stale_manifest, "stale")
        stale_waiting.wait(timeout=5)
        new_connection = {**old_connection, "ssh_target": "alice@new-gpu.example"}
        changed = client.put(
            f"/api/projects/{project_id}/settings",
            json={**body, "compute_connections": [new_connection]},
        )
        assert changed.status_code == 200
        current_manifest = app.state.service.manifest
        fresh = pool.submit(refresh, current_manifest, "fresh")

        assert fresh.result(timeout=5) == statuses["alice@new-gpu.example"]
        assert stale.result(timeout=5) == statuses["alice@old-gpu.example"]

    cached = catalog._compute_status_snapshot(project_id, current_manifest, refresh=False)

    assert probe_order == ["alice@new-gpu.example", "alice@old-gpu.example"]
    assert cached == statuses["alice@new-gpu.example"]


def test_cached_compute_readiness_does_not_wait_behind_a_slow_refresh(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    catalog = app.state.catalog
    project_id = app.state.default_project_id
    before = client.get(f"/api/projects/{project_id}").json()
    body = {
        "default_run_truth_scope": before["default_run_truth_scope"],
        "agent_profiles": {
            surface: {
                key: profile[key] for key in ("provider", "runtime", "model", "reasoning", "run_on")
            }
            for surface, profile in before["agent_profiles"].items()
        },
        "compute_connections": [
            {
                "id": "gpu",
                "name": "GPU VM",
                "kind": "ssh",
                "ssh_target": "alice@gpu.example",
                "access_hint": "",
            }
        ],
    }
    saved = client.put(f"/api/projects/{project_id}/settings", json=body)
    assert saved.status_code == 200
    current_manifest = app.state.service.manifest

    last_matrix = {"laptop": {"gpu": {"status_label": "Reachable"}}}
    slow_matrix = {"laptop": {"gpu": {"status_label": "Unreachable"}}}
    monkeypatch.setattr("rcp.projects.probe_compute_connections", lambda _manifest: last_matrix)
    seeded = client.get(f"/api/projects/{project_id}/readiness?refresh=true")
    assert seeded.json()["compute_status"] == last_matrix

    probing = threading.Event()
    unblock_probe = threading.Barrier(2)

    def slow_probe(_manifest):
        probing.set()
        unblock_probe.wait(timeout=10)
        return slow_matrix

    monkeypatch.setattr("rcp.projects.probe_compute_connections", slow_probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow_refresh = pool.submit(
            catalog._compute_status_snapshot,
            project_id,
            current_manifest,
            refresh=True,
        )
        wait_until(
            probing.is_set,
            detail="the explicit refresh never reached the compute probe",
        )
        cached_read = pool.submit(
            catalog._compute_status_snapshot,
            project_id,
            current_manifest,
            refresh=False,
        )

        # The cached read returns the last matrix while the refresh still holds
        # the probe lock; blocking here would fail as a timeout.
        assert cached_read.result(timeout=5) == last_matrix
        unblock_probe.wait(timeout=5)
        assert slow_refresh.result(timeout=5) == slow_matrix

    assert catalog._compute_status_snapshot(project_id, current_manifest, refresh=False) == (
        slow_matrix
    )


def test_project_settings_reject_too_many_compute_connections_before_persistence(
    manifest, tmp_path
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    before = client.get(f"/api/projects/{project_id}").json()
    body = {
        "default_run_truth_scope": before["default_run_truth_scope"],
        "agent_profiles": {
            surface: {
                key: profile[key] for key in ("provider", "runtime", "model", "reasoning", "run_on")
            }
            for surface, profile in before["agent_profiles"].items()
        },
        "compute_connections": [
            {"id": f"compute-{index}", "name": f"Compute {index}", "kind": "local"}
            for index in range(COMPUTE_CONNECTION_MAX_COUNT + 1)
        ],
    }

    rejected = client.put(f"/api/projects/{project_id}/settings", json=body)

    assert rejected.status_code == 422
    assert client.get(f"/api/projects/{project_id}").json()["compute_connections"] == []


def test_unopened_compute_readiness_cache_survives_project_open(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    app.state.catalog._services.clear()
    status = {"laptop": {}}
    calls: list[str] = []
    monkeypatch.setattr(
        "rcp.projects.probe_compute_connections",
        lambda _manifest: calls.append("probe") or status,
    )

    refreshed = client.get(f"/api/projects/{project_id}/readiness?refresh=true")
    opened = client.get(f"/api/projects/{project_id}")
    cached = client.get(f"/api/projects/{project_id}/readiness")

    assert refreshed.status_code == opened.status_code == cached.status_code == 200
    assert cached.json()["compute_status"] == status
    assert calls == ["probe"]


def test_project_settings_persist_agent_defaults_and_repository_reads(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    before = client.get(f"/api/projects/{project_id}").json()
    assert before["default_auto_research_invocation_ceiling"] == 10
    assert "default_campaign_invocation_ceiling" not in before
    profiles = {
        surface: {
            key: profile[key] for key in ("provider", "runtime", "model", "reasoning", "run_on")
        }
        for surface, profile in before["agent_profiles"].items()
    }
    assert set(profiles) == {
        "seed",
        "refresh",
        "node_chat",
        "project_chat",
        "paper_coach",
        "orchestrator",
    }
    profiles["seed"]["provider"] = "claude"
    profiles["seed"]["runtime"] = "stream-json"
    profiles["seed"]["model"] = "claude-seed"
    profiles["node_chat"]["runtime"] = "app-server"
    profiles["orchestrator"]["model"] = "campaign-orchestrator"

    incomplete = client.put(
        f"/api/projects/{project_id}/settings",
        json={
            "default_run_truth_scope": ["repo-b"],
            "agent_profiles": {
                surface: profile
                for surface, profile in profiles.items()
                if surface != "orchestrator"
            },
        },
    )
    assert incomplete.status_code == 422

    mismatched_runtime = client.put(
        f"/api/projects/{project_id}/settings",
        json={
            "default_run_truth_scope": ["repo-b"],
            "agent_profiles": {
                **profiles,
                "refresh": {**profiles["refresh"], "provider": "claude", "runtime": "app-server"},
            },
        },
    )
    assert mismatched_runtime.status_code == 422
    # The settings form shows this text, so it names the profile to fix and
    # carries none of the Pydantic envelope around the reason.
    detail = mismatched_runtime.json()["detail"]
    assert "does not support runtime 'app-server'" in detail

    invalid_budget = client.put(
        f"/api/projects/{project_id}/settings",
        json={
            "default_run_truth_scope": ["repo-b"],
            "default_auto_research_invocation_ceiling": 0,
            "agent_profiles": profiles,
        },
    )
    assert invalid_budget.status_code == 422

    legacy_budget_key = client.put(
        f"/api/projects/{project_id}/settings",
        json={
            "default_run_truth_scope": ["repo-b"],
            "default_campaign_invocation_ceiling": 14,
            "agent_profiles": profiles,
        },
    )
    assert legacy_budget_key.status_code == 422

    response = client.put(
        f"/api/projects/{project_id}/settings",
        json={
            "default_run_truth_scope": ["repo-b"],
            "default_auto_research_invocation_ceiling": 14,
            "agent_profiles": profiles,
        },
    )

    assert response.status_code == 200
    assert response.json()["default_run_truth_scope"] == ["repo-b"]
    assert response.json()["default_auto_research_invocation_ceiling"] == 14
    assert "default_campaign_invocation_ceiling" not in response.json()
    assert response.json()["agent_profiles"]["seed"]["model"] == "claude-seed"
    assert response.json()["agent_profiles"]["node_chat"]["runtime"] == "app-server"
    assert response.json()["agent_profiles"]["orchestrator"]["model"] == "campaign-orchestrator"
    assert "write_path" not in response.json()["agent_profiles"]["refresh"]
    assert (
        client.get(f"/api/projects/{project_id}").json()["agent_profiles"]["seed"]["model"]
        == "claude-seed"
    )
    content = manifest.path.read_text(encoding="utf-8")
    assert "[execution]" not in content
    assert "[paper.coach]" not in content
    updated = load_manifest(manifest.path)
    assert updated.agent_profile("seed").provider == "claude"
    assert updated.agent_profile("node_chat").runtime == "app-server"
    assert updated.agent_profile("orchestrator").model == "campaign-orchestrator"
    assert updated.agent_profile("orchestrator").permissions == permissions_for("orchestrate")
    assert updated.agent.default_auto_research_invocation_ceiling == 14
    assert updated.agent_profile("paper_coach").permissions.write_graph_patch is False


def test_project_settings_merge_partial_provider_paths_and_preserve_omitted_values(
    manifest, tmp_path, monkeypatch
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    before = client.get(f"/api/projects/{project_id}").json()
    profiles = {
        surface: {key: profile[key] for key in ("provider", "model", "reasoning", "run_on")}
        for surface, profile in before["agent_profiles"].items()
    }
    base = {
        "default_run_truth_scope": before["default_run_truth_scope"],
        "agent_profiles": profiles,
    }
    invalidated: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        app.state.catalog.launcher,
        "invalidate_readiness",
        lambda provider, *, host="", binary=None: invalidated.append((provider, host, binary)),
    )

    first = client.put(
        f"/api/projects/{project_id}/settings",
        json={
            **base,
            "machine_provider_paths": {"laptop": {"codex": "/opt/agents/codex"}},
        },
    )
    omitted = client.put(f"/api/projects/{project_id}/settings", json=base)
    partial = client.put(
        f"/api/projects/{project_id}/settings",
        json={
            **base,
            "machine_provider_paths": {"laptop": {"claude": "/opt/agents/claude"}},
        },
    )

    assert first.status_code == omitted.status_code == partial.status_code == 200
    assert omitted.json()["machines"][0]["provider_paths"]["codex"] == "/opt/agents/codex"
    assert partial.json()["machines"][0]["provider_paths"] == {
        "codex": "/opt/agents/codex",
        "claude": "/opt/agents/claude",
    }
    updated = load_manifest(manifest.path)
    assert updated.machine_map["laptop"].host == ""
    assert updated.machine_map["laptop"].provider_paths["codex"] == "/opt/agents/codex"
    assert invalidated == [
        ("codex", "", "/opt/agents/codex"),
        ("claude", "", "/opt/agents/claude"),
    ]


def test_project_settings_persist_nonsecret_compute_metadata_without_moving_agents(
    manifest, tmp_path
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    before = client.get(f"/api/projects/{project_id}").json()
    profiles = {
        surface: {
            key: profile[key] for key in ("provider", "runtime", "model", "reasoning", "run_on")
        }
        for surface, profile in before["agent_profiles"].items()
    }
    body = {
        "default_run_truth_scope": before["default_run_truth_scope"],
        "agent_profiles": profiles,
    }
    connection = {
        "id": "gpu",
        "name": "GPU VM",
        "kind": "ssh",
        "ssh_target": "alice@gpu.example",
        "access_hint": "Use /scratch/shared",
    }

    saved = client.put(
        f"/api/projects/{project_id}/settings",
        json={**body, "compute_connections": [connection]},
    )
    omitted = client.put(f"/api/projects/{project_id}/settings", json=body)

    assert saved.status_code == omitted.status_code == 200
    assert saved.json()["compute_connections"] == [connection]
    assert omitted.json()["compute_connections"] == [connection]
    assert omitted.json()["agent_profiles"] == before["agent_profiles"]
    assert load_manifest(manifest.path).compute_connections[0].id == "gpu"

    credential_field = client.put(
        f"/api/projects/{project_id}/settings",
        json={
            **body,
            "compute_connections": [{**connection, "private_key": "not accepted"}],
        },
    )
    removed = client.put(
        f"/api/projects/{project_id}/settings",
        json={**body, "compute_connections": []},
    )

    assert credential_field.status_code == 422
    assert removed.status_code == 200
    assert removed.json()["compute_connections"] == []


def test_invalid_provider_path_update_is_atomic(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    before = client.get(f"/api/projects/{project_id}").json()
    content = manifest.path.read_text(encoding="utf-8")
    profiles = {
        surface: {key: profile[key] for key in ("provider", "model", "reasoning", "run_on")}
        for surface, profile in before["agent_profiles"].items()
    }

    response = client.put(
        f"/api/projects/{project_id}/settings",
        json={
            "default_run_truth_scope": before["default_run_truth_scope"],
            "agent_profiles": profiles,
            "machine_provider_paths": {"laptop": {"codex": "relative/codex"}},
        },
    )

    assert response.status_code == 422
    assert manifest.path.read_text(encoding="utf-8") == content


def test_explicit_provider_resolve_discovers_then_persists(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    append_fixture_patch(app.state.service, _experiment_fixture_patch())
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    calls: list[str | None] = []

    def readiness(
        provider: str,
        *,
        host: str = "",
        binary: str | None = None,
        refresh: bool = False,
    ):
        assert provider == "codex"
        assert host == ""
        assert refresh is True
        calls.append(binary)
        path = binary or "/opt/new-agent/codex"
        return ProviderReadiness(
            provider=provider,
            installed=True,
            authenticated=True,
            binary_path=path,
            path_state="resolved" if binary else "unconfigured",
        )

    app.state.catalog.launcher.readiness = readiness

    response = client.post(
        f"/api/projects/{project_id}/machines/laptop/providers/codex/resolve", json={}
    )

    assert response.status_code == 200
    assert calls == [None, "/opt/new-agent/codex"]
    assert response.json()["binary_path"] == "/opt/new-agent/codex"
    assert response.json()["readiness"]["path_state"] == "resolved"
    assert response.json()["project"]["machines"][0]["provider_paths"]["codex"] == (
        "/opt/new-agent/codex"
    )
    assert set(response.json()["project"]["experiment_control"]) == {"exp/bounded-loop"}
    assert load_manifest(manifest.path).machine_map["laptop"].provider_paths["codex"] == (
        "/opt/new-agent/codex"
    )


def test_project_settings_reject_invalid_scope_without_changing_manifest(
    manifest, tmp_path
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = signed_in_client(app)
    project_id = app.state.default_project_id
    before = client.get(f"/api/projects/{project_id}").json()
    content = manifest.path.read_text(encoding="utf-8")
    profiles = {
        surface: {key: profile[key] for key in ("provider", "model", "reasoning", "run_on")}
        for surface, profile in before["agent_profiles"].items()
    }

    response = client.put(
        f"/api/projects/{project_id}/settings",
        json={
            "default_run_truth_scope": ["not-a-project-repository"],
            "agent_profiles": profiles,
        },
    )

    assert response.status_code == 422
    assert manifest.path.read_text(encoding="utf-8") == content


def test_project_usage_endpoint_returns_counted_and_excluded_records(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    store = app.state.background_tasks.store
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id="usage-operation",
            project_id=project_id,
            kind="node_chat",
            status="succeeded",
            request={"provider": "codex", "model": "gpt"},
            created_at=now,
            updated_at=now,
            status_message="done",
        )
    )
    usage = ProviderUsage(
        provider_profile="codex.turn.v1",
        provider_event_type="turn.completed",
        dedupe_key="turn-1",
        processed_input_tokens=2_000,
        generated_tokens=200,
        cached_input_tokens=1_000,
    )
    store.record_agent_usage("usage-operation", usage)
    store.record_agent_usage("usage-operation", usage)

    response = signed_in_client(app).get(f"/api/projects/{project_id}/usage")

    assert response.status_code == 200
    payload = response.json()
    assert payload["counted_records"] == 1
    assert payload["excluded_records"] == 1
    assert payload["input_processed"]["total_tokens"] == 2_000
    assert payload["input_processed"]["cache_share"] == 0.5
    assert payload["generated"]["total_tokens"] == 200
    assert {record["counted"] for record in payload["records"]} == {True, False}


def test_project_readiness_exposes_cached_state_transfer_engine(manifest, tmp_path, monkeypatch):
    from rcp.transport import state_transfer

    content = manifest.path.read_text()
    manifest.path.write_text(
        content + '\n[[machines]]\nalias = "worker"\nhost = "research.example"\n'
    )
    engine = state_transfer.TransferEngine("tar", None, "openrsync: protocol version 29", "3.2.7")
    monkeypatch.setattr(state_transfer, "_CACHE", {"research.example": engine})
    monkeypatch.setattr(
        state_transfer, "_probe", lambda _argv: pytest.fail("readiness must not probe transfers")
    )
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    monkeypatch.setattr(
        app.state.catalog.launcher,
        "readiness",
        lambda provider, **_kwargs: ProviderReadiness(
            provider=provider, installed=False, authenticated=False
        ),
    )
    response = signed_in_client(app).get(f"/api/projects/{app.state.default_project_id}/readiness")
    assert response.status_code == 200, response.text
    assert response.json()["state_transfers"] == {"worker": engine.as_dict()}
