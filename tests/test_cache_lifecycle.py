from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rcp.agents import ContextAssembler
from rcp.config import MachineConfig
from rcp.core.models import GraphState
from rcp.sources import (
    REMOTE_SOURCE_CACHE_LIMITS,
    SESSION_SLICE_CACHE_LIMITS,
    CacheLimits,
    ConversationIndexer,
    ConversationSession,
    RebuildableCache,
)
from rcp.sources.indexer import _REMOTE_SLICE_SCRIPT


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def test_process_wide_pin_protects_an_active_entry_from_another_cache_instance(
    tmp_path,
) -> None:
    clock = FakeClock()
    root = tmp_path / "session-slices"
    entry = root / "active" / "records.jsonl"
    root.mkdir()
    limits = CacheLimits(ttl_seconds=1, max_count=1, max_bytes=1)
    active_task_cache = RebuildableCache(root, limits, layout="directories", clock=clock)
    concurrent_cache = RebuildableCache(root, limits, layout="directories", clock=clock)
    clock.advance(seconds=2)

    with active_task_cache.pin_scope() as pin:
        pin(entry)
        entry.parent.mkdir()
        entry.write_text("active", encoding="utf-8")
        metrics = concurrent_cache.sweep()
        assert entry.exists()
        assert metrics.reclaimable_count == 0

    concurrent_cache.sweep()
    assert not entry.exists()


def test_remote_source_is_pinned_before_rsync_makes_it_visible(
    manifest, tmp_path, monkeypatch
) -> None:
    cache_root = tmp_path / "source-cache"
    indexer = ConversationIndexer(manifest, cache_root)
    remote_path = "/provider/session.jsonl"
    expected = cache_root / "gpu" / "codex" / remote_path.lstrip("/")
    pinned: list[Path] = []

    def fake_run(arguments, **_kwargs):
        assert pinned == [expected]
        expected.parent.mkdir(parents=True, exist_ok=True)
        expected.write_text("{}\n", encoding="utf-8")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    cached = indexer._cache_remote_files(
        "research.example",
        "gpu",
        "codex",
        [remote_path],
        pin_artifact=pinned.append,
    )

    assert cached == {remote_path: expected}


def test_cache_limits_match_the_rebuildable_storage_contract() -> None:
    assert CacheLimits(
        ttl_seconds=30 * 24 * 60 * 60,
        max_count=256,
        max_bytes=1024 * 1024 * 1024,
    ) == REMOTE_SOURCE_CACHE_LIMITS
    assert CacheLimits(
        ttl_seconds=14 * 24 * 60 * 60,
        max_count=512,
        max_bytes=512 * 1024 * 1024,
    ) == SESSION_SLICE_CACHE_LIMITS


def test_file_cache_expires_then_uses_deterministic_lru_and_ignores_mtime(
    tmp_path,
) -> None:
    clock = FakeClock()
    root = tmp_path / "source-cache"
    root.mkdir()
    cache = RebuildableCache(
        root,
        CacheLimits(ttl_seconds=10, max_count=2, max_bytes=100),
        layout="files",
        clock=clock,
    )

    expired = root / "z-expired.jsonl"
    expired.write_text("z", encoding="utf-8")
    cache.touch(expired)
    clock.advance(seconds=8)
    a = root / "a.jsonl"
    b = root / "b.jsonl"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")
    os.utime(a, (0, 0))
    cache.touch(a)
    cache.touch(b)
    clock.advance(seconds=3)
    newest = root / "newest.jsonl"
    newest.write_text("n", encoding="utf-8")
    cache.touch(newest)

    before = cache.metrics()
    after = cache.sweep()

    assert before.count == 4
    assert before.bytes == 4
    assert before.reclaimable_count == 4
    assert before.reclaimable_bytes == 4
    assert not expired.exists()
    assert not a.exists()  # Equal access times are broken by relative path.
    assert b.exists()
    assert newest.exists()
    assert after.count == 2
    assert after.bytes == 2
    assert after.reclaimable_count == 2


def test_active_entry_is_never_expired_or_evicted(tmp_path) -> None:
    clock = FakeClock()
    root = tmp_path / "session-slices"
    root.mkdir()
    cache = RebuildableCache(
        root,
        CacheLimits(ttl_seconds=10, max_count=1, max_bytes=4),
        layout="directories",
        clock=clock,
    )
    active = root / "active"
    inactive = root / "inactive"
    active.mkdir()
    inactive.mkdir()
    active_file = active / "records.jsonl"
    active_file.write_bytes(b"1234")
    (inactive / "records.jsonl").write_bytes(b"5678")
    cache.touch(active_file)
    cache.touch(inactive / "records.jsonl")
    clock.advance(seconds=11)

    metrics = cache.sweep(active_paths=[active_file])

    assert active_file.exists()
    assert not inactive.exists()
    assert metrics.count == 1
    assert metrics.bytes == 4
    assert metrics.reclaimable_count == 0


def test_size_pressure_evicts_oldest_slice_directory(tmp_path) -> None:
    clock = FakeClock()
    root = tmp_path / "session-slices"
    root.mkdir()
    cache = RebuildableCache(
        root,
        CacheLimits(ttl_seconds=100, max_count=10, max_bytes=5),
        layout="directories",
        clock=clock,
    )
    older = root / "older"
    newer = root / "newer"
    older.mkdir()
    newer.mkdir()
    (older / "records.jsonl").write_bytes(b"1234")
    (newer / "records.jsonl").write_bytes(b"5678")
    cache.touch(older / "records.jsonl")
    clock.advance(seconds=1)
    cache.touch(newer / "records.jsonl")

    metrics = cache.sweep()

    assert not older.exists()
    assert newer.exists()
    assert metrics.count == 1
    assert metrics.bytes == 4


def test_clear_is_scoped_to_rebuildable_roots_and_preserves_active_paths(
    manifest, tmp_path
) -> None:
    cache_root = tmp_path / "data" / "source-cache"
    slice_root = cache_root.parent / "session-slices"
    cached_source = cache_root / "remote" / "codex" / "source.jsonl"
    cached_source.parent.mkdir(parents=True)
    cached_source.write_text("cached", encoding="utf-8")
    slice_file = slice_root / "slice-id" / "records.jsonl"
    slice_file.parent.mkdir(parents=True)
    slice_file.write_text("derived", encoding="utf-8")
    original = tmp_path / "provider-original.jsonl"
    original.write_text("original", encoding="utf-8")
    chat = manifest.research_dir / "chat" / "human.jsonl"
    facts = manifest.research_dir / "facts" / "collector.json"
    chat.parent.mkdir(exist_ok=True)
    facts.parent.mkdir(exist_ok=True)
    chat.write_text("chat", encoding="utf-8")
    facts.write_text("fact", encoding="utf-8")
    indexer = ConversationIndexer(manifest, cache_root)

    retained = indexer.clear_rebuildable_caches(active_paths=[cached_source])

    assert cached_source.exists()
    assert not slice_file.exists()
    assert retained.remote_sources.count == 1
    assert retained.session_slices.count == 0
    assert original.read_text(encoding="utf-8") == "original"
    assert chat.read_text(encoding="utf-8") == "chat"
    assert facts.read_text(encoding="utf-8") == "fact"

    cleared = indexer.clear_rebuildable_caches()
    assert not cached_source.exists()
    assert cleared.remote_sources.count == 0


def test_source_index_build_sweeps_expired_rebuildable_entries(manifest, tmp_path) -> None:
    clock = FakeClock()
    cache_root = tmp_path / "data" / "source-cache"
    stale = cache_root / "remote" / "codex" / "stale.jsonl"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")
    indexer = ConversationIndexer(manifest, cache_root, clock=clock)
    lifecycle = RebuildableCache(
        cache_root,
        REMOTE_SOURCE_CACHE_LIMITS,
        layout="files",
        clock=clock,
    )
    lifecycle.touch(stale)
    clock.advance(days=31)

    indexer.build()

    assert not stale.exists()


def test_indexer_records_source_access_explicitly_without_changing_provider_mtime(
    manifest, tmp_path
) -> None:
    clock = FakeClock()
    cache_root = tmp_path / "data" / "source-cache"
    cached = cache_root / "remote" / "codex" / "session.jsonl"
    cached.parent.mkdir(parents=True)
    cached.write_text(
        '{"type":"response_item","payload":{"id":"terminal",'
        '"type":"message","role":"assistant"}}\n',
        encoding="utf-8",
    )
    os.utime(cached, (0, 0))
    indexer = ConversationIndexer(manifest, cache_root, clock=clock)
    indexer.cache_metrics()
    clock.advance(seconds=5)
    session = ConversationSession(
        key="repo-a/remote/codex/session",
        provider="codex",
        source_machine="remote",
        truth_repository="repo-a",
        session_id="session",
        cwd=manifest.repository_map["repo-a"].path,
        path=str(cached),
        last_uuid="terminal",
        record_count=1,
    )

    assert [record.uuid for record in indexer.read_records(session)] == ["terminal"]
    metrics = indexer.cache_metrics().remote_sources

    assert metrics.oldest_accessed_at == clock.now
    assert cached.stat().st_mtime == 0


def test_same_machine_remote_source_builds_slice_without_a_permanent_log_copy(
    manifest, tmp_path, monkeypatch
) -> None:
    manifest.machines.append(MachineConfig(alias="remote-1", host="research.example"))
    manifest.repositories[0].machine = "remote-1"
    manifest.repositories[0].path = "/remote/project"
    cache_root = tmp_path / "data" / "source-cache"
    indexer = ConversationIndexer(manifest, cache_root)

    def inspect_remote(_host, _root, provider, _machine_alias):
        if provider != "codex":
            return []
        return [
            {
                "path": "/remote/sessions/codex.jsonl",
                "cwd": "/remote/project",
                "session_id": "remote-session",
                "first_timestamp": None,
                "last_timestamp": None,
                "last_uuid": "last",
                "record_count": 2,
            }
        ]

    monkeypatch.setattr(indexer, "_inspect_remote_root", inspect_remote)
    index = indexer.build(execution_machine="remote-1")
    session = index.sessions[0]

    def reconstruct(_session, _from_uuid: str | None, destination: Path) -> None:
        destination.write_text(
            '{"uuid":"remote-session","timestamp":null,"role":"unknown",'
            '"text":"","raw_type":"session_meta:"}\n'
            '{"uuid":"last","timestamp":null,"role":"assistant",'
            '"text":"","raw_type":"response_item:message"}\n',
            encoding="utf-8",
        )
        return None

    monkeypatch.setattr(indexer, "_write_remote_slice", reconstruct)
    evidence_slice = indexer.materialize_slice(session)

    assert Path(evidence_slice.path).is_file()
    assert evidence_slice.record_count == 2
    assert not list(cache_root.rglob("*.jsonl"))

    def unavailable(_session, _from_uuid: str | None, _destination: Path) -> None:
        raise OSError("provider original disappeared")

    monkeypatch.setattr(indexer, "_write_remote_slice", unavailable)
    context = ContextAssembler(manifest, indexer).assemble(
        GraphState(project_truth_scope=manifest.project.truth_scope),
        index,
    )
    assert context.sessions == []
    assert any("provider original disappeared" in error for error in context.source_errors)


def test_remote_slice_script_emits_only_the_normalized_increment(tmp_path) -> None:
    source = tmp_path / "remote.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "session", "cwd": "/remote/project"},
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "id": "cursor",
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "already read"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "id": "terminal",
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "new evidence"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    payload = json.dumps(
        {
            "path": str(source),
            "provider": "codex",
            "record_count": 3,
            "last_uuid": "terminal",
            "from_uuid": "cursor",
            "session_key": "repo/remote/codex/session",
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", _REMOTE_SLICE_SCRIPT, payload],
        capture_output=True,
        text=True,
        check=True,
    )

    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert records == [
        {
            "uuid": "terminal",
            "timestamp": None,
            "role": "assistant",
            "text": "new evidence",
            "raw_type": "response_item:message",
        }
    ]
    assert json.loads(result.stderr) == {"cursor_repair": None}
