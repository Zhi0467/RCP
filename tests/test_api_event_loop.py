from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager

import httpx
import pytest

from .helpers import create_named_app


def test_health_sqlite_wait_does_not_block_event_loop(manifest, tmp_path, monkeypatch) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.services.store
    original_connection = store.connection
    entered = threading.Event()
    release = threading.Event()

    @contextmanager
    def blocked_connection():
        entered.set()
        assert release.wait(timeout=3)
        with original_connection() as connection:
            yield connection

    monkeypatch.setattr(store, "connection", blocked_connection)

    async def drive() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            request = asyncio.create_task(client.get("/api/health"))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                await asyncio.wait_for(asyncio.sleep(0), timeout=0.2)
            finally:
                release.set()
            return await request

    response = asyncio.run(drive())

    assert response.status_code == 200


def test_project_cache_hit_does_not_block_event_loop(manifest, tmp_path, monkeypatch) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    display_cache = app.state.services.project_display_cache
    original_cached_snapshot = display_cache.cached_project_snapshot
    entered = threading.Event()
    release = threading.Event()

    def blocked_cached_snapshot(candidate_project_id: str):
        entered.set()
        assert release.wait(timeout=3)
        return original_cached_snapshot(candidate_project_id)

    monkeypatch.setattr(display_cache, "cached_project_snapshot", blocked_cached_snapshot)

    async def drive() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            request = asyncio.create_task(client.get(f"/api/projects/{project_id}"))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                await asyncio.wait_for(asyncio.sleep(0), timeout=0.2)
            finally:
                release.set()
            return await request

    response = asyncio.run(drive())

    assert response.status_code == 200


@pytest.mark.parametrize("blocked_step", ["reserve", "open", "patch_head", "commit", "fallback"])
def test_project_cache_miss_does_not_block_event_loop(
    manifest,
    tmp_path,
    monkeypatch,
    blocked_step,
) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    project_id = app.state.default_project_id
    assert project_id is not None
    catalog = app.state.services.catalog
    display_cache = app.state.services.project_display_cache
    original_cached_snapshot = display_cache.cached_project_snapshot
    original_reserve = catalog.reserve_cached_snapshot_generation
    original_open = display_cache.open_snapshot
    original_commit = catalog.commit_cached_snapshot
    entered = threading.Event()
    release = threading.Event()
    cache_reads = 0

    def block() -> None:
        entered.set()
        assert release.wait(timeout=3)

    def miss_then_fallback(candidate_project_id: str):
        nonlocal cache_reads
        cache_reads += 1
        if cache_reads == 1:
            return None
        if blocked_step == "fallback":
            block()
        return original_cached_snapshot(candidate_project_id)

    def blocked_reserve(candidate_project_id: str):
        if blocked_step == "reserve":
            block()
        return original_reserve(candidate_project_id)

    def blocked_open(candidate_project_id: str):
        if blocked_step == "open":
            block()
        service, snapshot = original_open(candidate_project_id)
        if blocked_step == "patch_head":
            workspace = service.history.workspace
            original_patch_head = workspace.cached_patch_log_head

            def blocked_patch_head():
                block()
                return original_patch_head()

            monkeypatch.setattr(workspace, "cached_patch_log_head", blocked_patch_head)
        return service, snapshot

    def blocked_commit(*args, **kwargs):
        if blocked_step == "commit":
            block()
        original_commit(*args, **kwargs)
        return False

    monkeypatch.setattr(display_cache, "cached_project_snapshot", miss_then_fallback)
    monkeypatch.setattr(catalog, "reserve_cached_snapshot_generation", blocked_reserve)
    monkeypatch.setattr(display_cache, "open_snapshot", blocked_open)
    monkeypatch.setattr(catalog, "commit_cached_snapshot", blocked_commit)

    async def drive() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            request = asyncio.create_task(client.get(f"/api/projects/{project_id}"))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                await asyncio.wait_for(asyncio.sleep(0), timeout=0.2)
            finally:
                release.set()
            return await request

    response = asyncio.run(drive())

    assert response.status_code == 200
    assert cache_reads == 2
