#!/usr/bin/env python3
"""Benchmark disposable graph histories; run with ``uv run python scripts/bench_graph_history.py``.

Cold means a fresh interpreter with previously generated checkpoints (OS caches
remain warm). Ingest measures retained receipt ingestion; append additionally
measures HistoryManager admission and publication using fixture authority.
Fixtures never touch an existing project or real data. ``--history-copy`` reads
an existing local copy without appending or following its configured workspace.
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar

from rcp import limits
from rcp.config import load_manifest
from rcp.core.authority import WriteAuthority
from rcp.core.models import AuthorizedHuman, GraphBranchMetadata, Patch
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.history.manager import HistoryManager
from rcp.history.memory_size import retained_size
from rcp.history.queries import HeadSpec
from rcp.transport import LocalStateWorkspace

_T = TypeVar("_T")


def timed(action: Callable[[], _T]) -> tuple[_T, float]:
    start = time.perf_counter()
    result = action()
    return result, (time.perf_counter() - start) * 1000


def patch(revision: int, *, entities: int = 0, branch: int = 0) -> Patch:
    operations = (
        [
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": f"ev/item-{index}",
                        "type": "evidence",
                        "title": f"Result {index}",
                        "observation": "Measured synthetic result.",
                        "origin": "internal_run",
                    }
                    for index in range(entities)
                ],
            }
        ]
        if entities
        else [
            {
                "op": "update_nodes",
                "nodes": [{"id": "ev/item-0", "changes": {"title": f"Result {branch}:{revision}"}}],
            }
        ]
    )
    return Patch.model_validate(
        {
            "revision": revision,
            "kind": "work",
            "author": "agent",
            "admission": "accepted",
            "summary": f"Synthetic step {branch}:{revision}",
            "run_truth_scope": ["repo"],
            "repositories_read": ["repo"],
            "ops": operations,
        }
    )


def write_patch(directory: Path, value: Patch) -> None:
    (directory / f"{value.revision:06d}.json").write_text(value.model_dump_json())


def fixture(root: Path, count: int, entities: int) -> None:
    research = root / ".research"
    directory = research / "patches"
    directory.mkdir(parents=True)
    (research / "manifest.toml").write_text(f'''name = "history-benchmark"
[[machines]]
alias = "local"
host = ""
[[repositories]]
alias = "repo"
machine = "local"
path = "{root}"
[project]
truth_scope = ["repo"]
[state]
repository = "repo"
[agent]
default_run_truth_scope = ["repo"]
[execution]
run_on = "local"
''')
    (research / "scope-base.json").write_text('{"truth_scope":["repo"]}')
    for revision in range(1, count + 1):
        write_patch(directory, patch(revision, entities=entities if revision == 1 else 0))
    for index in range(3):
        branch_id = str(uuid.uuid4())
        target = GraphTargetRef(kind="branch", branch_id=branch_id)
        base_revision = count // 2
        metadata = GraphBranchMetadata(
            branch_id=branch_id,
            episode_id=branch_id,
            project_id="benchmark",
            base_head=GraphHeadRef(revision=base_revision),
            head=GraphHeadRef(target=target, revision=base_revision + 8),
            authorized_by=AuthorizedHuman(
                space_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()), display_name="Benchmark"
            ),
        )
        branch = research / "branches" / branch_id
        (branch / "patches").mkdir(parents=True)
        (branch / "branch.json").write_text(metadata.model_dump_json())
        for revision in range(base_revision + 1, base_revision + 9):
            write_patch(branch / "patches", patch(revision, branch=index + 1))


def worker(args: argparse.Namespace) -> dict[str, float]:
    started = time.perf_counter()
    manager = HistoryManager(
        load_manifest(args.root / ".research" / "manifest.toml"),
        project_id="benchmark",
        write_authority=WriteAuthority(),
    )
    engine = manager.history_engine
    engine.checkpoint_interval = args.interval
    engine.configure_store(args.cache, "benchmark")
    target = GraphTargetRef()

    def resolve(revision: int | Literal["head"]) -> GraphHeadRef:
        return engine.resolve(HeadSpec(revision=revision), default_target=target)

    print(f"[{args.count}/{args.interval}] {args.worker}", file=sys.stderr, flush=True)
    if args.worker == "prepare":
        last = resolve("head")
        engine.state_at(last)
        for path in sorted((manager.root / "branches").iterdir()):
            branch_head = engine.resolve(
                HeadSpec(
                    target=GraphTargetRef(kind="branch", branch_id=path.name), revision="head"
                ),
                default_target=target,
            )
            engine.state_at(branch_head)
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak /= 1024 * 1024 if sys.platform == "darwin" else 1024
        print(
            f"[{args.count}/{args.interval}] prepare {(time.perf_counter() - started):.1f}s; peak {peak:.1f} MiB",
            file=sys.stderr,
            flush=True,
        )
        return {}

    revision = max(1, args.count - 7)
    at, cold = timed(lambda: engine.state_at(resolve(revision)))
    assert len(at.nodes) == args.entities
    retained = (
        retained_size(engine._generations["main"]) / 2**20 if "main" in engine._generations else 0
    )
    print(
        f"[{args.count}/{args.interval}] retained {retained:.1f} MiB; cached {list(engine._generations)}",
        file=sys.stderr,
        flush=True,
    )
    warm = statistics.median(timed(lambda: engine.state_at(resolve(revision)))[1] for _ in range(3))
    end = resolve("head")
    start = resolve(max(1, args.count // 2))
    recent = resolve(max(1, args.count - 1))
    engine.diff(recent, end)
    small_diff = statistics.median(timed(lambda: engine.diff(recent, end))[1] for _ in range(3))

    def diff() -> int:
        page = engine.diff(start, end)
        count = len(page.changes)
        while page.cursor:
            page = engine.diff(start, end, cursor=page.cursor)
            count += len(page.changes)
        return count

    _, diff_ms = timed(diff)

    def log() -> int:
        page = engine.log("ev/item-0", end)
        count = len(page.entries)
        while page.cursor:
            page = engine.log("ev/item-0", end, cursor=page.cursor)
            count += len(page.entries)
        return count

    entries, log_ms = timed(log)
    assert entries == args.count
    branch_path = next(iter(sorted((manager.root / "branches").iterdir())))
    branch_spec = HeadSpec(
        target=GraphTargetRef(kind="branch", branch_id=branch_path.name), revision="head"
    )
    _, branch_ms = timed(
        lambda: engine.diff(start, engine.resolve(branch_spec, default_target=target))
    )

    def append() -> None:
        write_patch(manager.patches_dir, patch(args.count + 1))
        assert engine.state_at(resolve("head")).revision == args.count + 1

    _, append_ms = timed(append)
    # Reuse the repository's explicit fixture authority; this is disposable data.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.helpers import unchecked_write_authority

    manager.write_authority = unchecked_write_authority()
    _, commit_ms = timed(lambda: manager.append(patch(args.count + 2)))
    print(
        f"[{args.count}/{args.interval}] admitted append {commit_ms:.1f} ms",
        file=sys.stderr,
        flush=True,
    )
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
    disk_mb = sum(path.stat().st_size for path in args.cache.rglob("*") if path.is_file()) / 1e6
    return {
        "cold": cold,
        "warm": warm,
        "small_diff": small_diff,
        "diff": diff_ms,
        "log": log_ms,
        "branch": branch_ms,
        "append": append_ms,
        "commit": commit_ms,
        "rss": rss_mb,
        "disk": disk_mb,
        "retained": retained,
    }


def history_copy(root: Path, interval: int) -> dict[str, float | int]:
    research = root.resolve() / ".research"
    manager = HistoryManager(
        load_manifest(research / "manifest.toml"),
        workspace=LocalStateWorkspace(research, "history-benchmark-copy"),
        project_id="benchmark",
        write_authority=WriteAuthority(),
    )
    engine = manager.history_engine
    engine.checkpoint_interval = interval

    def state():
        head = engine.resolve(HeadSpec(revision="head"), default_target=GraphTargetRef())
        return engine.state_at(head)

    with tempfile.TemporaryDirectory(prefix="rcp-history-copy-cache-") as temporary:
        engine.configure_store(Path(temporary), "benchmark")
        current, cold = timed(state)
        warm = statistics.median(timed(state)[1] for _ in range(3))
        head = engine.resolve(HeadSpec(revision="head"), default_target=GraphTargetRef())
        index = engine._generations["main"].index
        sizes: dict[str, int] = {}
        for step in index:
            for change in step.delta.nodes:
                sizes[change.node_id] = sizes.get(change.node_id, 0) + len(
                    change.model_dump_json().encode()
                )
            for change in step.delta.edges:
                sizes[change.edge_id] = sizes.get(change.edge_id, 0) + len(
                    change.model_dump_json().encode()
                )
        entity_id = max(sizes, key=lambda key: sizes[key])

        def log() -> tuple[int, int, int]:
            page = engine.log(entity_id, head)
            pages = encoded_bytes = entries = 0
            while True:
                pages += 1
                encoded_bytes += len(page.model_dump_json().encode())
                entries += len(page.entries)
                if page.cursor is None:
                    return pages, encoded_bytes, entries
                page = engine.log(entity_id, head, cursor=page.cursor)

        (pages, encoded_bytes, entries), log_ms = timed(log)
        return {
            "patches": len(index),
            "entities": len(current.nodes) + len(current.edges),
            "interval": interval,
            "cold_ms": cold,
            "warm_ms": warm,
            "log_ms": log_ms,
            "log_pages": pages,
            "log_bytes": encoded_bytes,
            "log_entries": entries,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", nargs="+", type=int, default=[1000, 10000])
    parser.add_argument("--intervals", nargs="+", type=int, default=[0, 16, 64, 256])
    parser.add_argument("--entities", type=int, default=2100)
    parser.add_argument("--worker", choices=["prepare", "measure"])
    parser.add_argument("--root", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--count", type=int)
    parser.add_argument("--interval", type=int)
    parser.add_argument("--history-copy", type=Path)
    args = parser.parse_args()
    if args.history_copy:
        interval = (
            limits.GRAPH_HISTORY_CHECKPOINT_INTERVAL if args.interval is None else args.interval
        )
        print(json.dumps(history_copy(args.history_copy, interval)), flush=True)
        return
    if args.worker:
        print(json.dumps(worker(args)), flush=True)
        return
    print(
        "Times in ms; cold = new process, warm OS cache; ingest = receipt ingestion; append = manager append.",
        flush=True,
    )
    print(
        "patches K cold@R warm@R small_diff diff full_log branch_diff ingest append RSS_MiB cache_MB retained_MiB",
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix="rcp-history-benchmark-") as temporary:
        for count in args.counts:
            root = Path(temporary) / f"history-{count}"
            fixture(root, count, args.entities)
            for interval in args.intervals:
                cache = Path(temporary) / f"cache-{count}-{interval}"
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--root",
                    str(root),
                    "--cache",
                    str(cache),
                    "--count",
                    str(count),
                    "--interval",
                    str(interval),
                    "--entities",
                    str(args.entities),
                ]
                subprocess.run(
                    [*command, "--worker", "prepare"], check=True, stdout=subprocess.DEVNULL
                )
                result = subprocess.run(
                    [*command, "--worker", "measure"], check=True, stdout=subprocess.PIPE, text=True
                )
                values = json.loads(result.stdout)
                for revision in (count + 1, count + 2):
                    (root / ".research" / "patches" / f"{revision:06d}.json").unlink()
                print(
                    f"{count:7} {interval:3} "
                    + " ".join(
                        f"{values[key]:.1f}"
                        for key in (
                            "cold",
                            "warm",
                            "small_diff",
                            "diff",
                            "log",
                            "branch",
                            "append",
                            "commit",
                            "rss",
                            "disk",
                            "retained",
                        )
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    main()
