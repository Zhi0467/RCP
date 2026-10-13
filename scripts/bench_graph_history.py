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
from types import SimpleNamespace
from typing import TypeVar

from rcp import limits
from rcp.config import load_manifest
from rcp.core.authority import WriteAuthority
from rcp.core.models import AuthorizedHuman, GraphBranchMetadata, Patch
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.digest import DigestProjector
from rcp.history.branches import read_branch_snapshots
from rcp.history.manager import HistoryManager
from rcp.history.queries import HeadSpec
from rcp.paper import PaperService
from rcp.server_ops.maintenance import RuntimeAdmissionGate
from rcp.service import ProjectService
from rcp.storage import AppStore, ProjectRecord
from rcp.transport import LocalStateWorkspace

_T = TypeVar("_T")


def timed(action: Callable[[], _T]) -> tuple[_T, float]:
    start = time.perf_counter()
    result = action()
    return result, (time.perf_counter() - start) * 1000


def patch(revision: int, *, entities: int = 0, branch: int = 0, title: str = "") -> Patch:
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
                "nodes": [
                    {
                        "id": "ev/item-0",
                        "changes": {"title": title or f"Result {branch}:{revision}"},
                    }
                ],
            }
        ]
    )
    return Patch(
        revision=revision,
        kind="work",
        author="agent",
        admission="accepted",
        summary=f"Synthetic step {branch}:{revision}",
        run_truth_scope=["repo"],
        repositories_read=["repo"],
        ops=operations,
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
        # Put the long value at the tail, keeping earlier title edits small.
        value = patch(
            revision,
            entities=entities if revision == 1 else 0,
            title="界" * 60_000 if revision == count else "",
        )
        write_patch(directory, value)
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
        write_patch(branch / "patches", patch(revision, branch=1))


def drain(read, collection: str) -> dict[str, int]:
    pages = encoded_bytes = items = 0
    cursor = None
    while True:
        page = read(cursor)
        pages += 1
        encoded_bytes += len(page.model_dump_json().encode())
        items += len(getattr(page, collection))
        cursor = page.cursor
        if cursor is None:
            return {"pages": pages, "bytes": encoded_bytes, "items": items}


def measure(service, store, cache, branch, *, count=None) -> dict:
    manager, engine = service.history, service.history_engine

    def resolve(revision):
        return engine.resolve(HeadSpec(revision=revision), default_target=GraphTargetRef())

    revision = max(1, count - 7) if count else "head"
    current, cold = timed(lambda: engine.state_at(resolve(revision)))
    assert current.replay_status == "complete"
    end, start = resolve("head"), resolve(max(1, current.revision // 2))
    entity = (
        "ev/item-0"
        if count
        else max(
            [*current.nodes.values(), *current.edges.values()],
            key=lambda item: len(item.model_dump_json().encode()),
        ).id
    )
    result = {
        "patches": end.revision,
        "entities": len(current.nodes) + len(current.edges),
        "cold_ms": cold,
    }
    result["warm_ms"] = statistics.median(
        timed(lambda: engine.state_at(resolve(revision)))[1] for _ in range(3)
    )
    recent = resolve(max(0, end.revision - 1))
    projector = DigestProjector(
        store, SimpleNamespace(open=lambda _id: service), admission=RuntimeAdmissionGate()
    )
    actions = {
        "small_diff": lambda: engine.diff(recent, end),
        "diff": lambda: drain(lambda cursor: engine.diff(start, end, cursor=cursor), "changes"),
        "log": lambda: drain(lambda cursor: engine.log(entity, end, cursor=cursor), "entries"),
        "raw_history": lambda: manager.slice(1),
        "recent_history": lambda: manager.slice(recent.revision),
        "recent_summaries": lambda: manager.revision_summaries(recent.revision),
        "refresh_delta": manager.refresh_delta,
        "digest": lambda: projector.reconcile_project(service._project_id),
    }
    if branch:
        branch_head = engine.resolve(
            HeadSpec(target=branch.head.target, revision="head"), default_target=GraphTargetRef()
        )
        actions.update(
            branch_diff=lambda: drain(
                lambda cursor: engine.diff(start, branch_head, cursor=cursor), "changes"
            ),
            branch_view=lambda: read_branch_snapshots(
                manager, [(branch.branch_id, branch.episode_id, branch.project_id)]
            ),
        )
    for name, action in actions.items():
        value, result[f"{name}_ms"] = timed(action)
        if name in {"diff", "log", "branch_diff"}:
            result.update({f"{name}_{key}": number for key, number in value.items()})
    if count:
        assert result["log_items"] >= count and result["log_pages"] > 1

        def ingest():
            write_patch(manager.patches_dir, patch(count + 1))
            assert engine.state_at(resolve("head")).revision == count + 1

        _, result["ingest_ms"] = timed(ingest)
        # Only the synthetic, disposable history is admitted or appended to.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tests.helpers import unchecked_write_authority

        manager.write_authority = unchecked_write_authority()
        _, result["append_ms"] = timed(lambda: manager.append(patch(count + 2)))
        _, result["digest_catchup_ms"] = timed(actions["digest"])
    result["rss_MiB"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
        1024 * 1024 if sys.platform == "darwin" else 1024
    )
    result["disk_MB"] = sum(p.stat().st_size for p in cache.rglob("*") if p.is_file()) / 1e6
    return result


def worker(args):
    # Optional comparisons change this interpreter only; normal runs use K=256.
    limits.GRAPH_HISTORY_CHECKPOINT_INTERVAL = args.interval
    research = args.root.resolve() / ".research"
    manifest = load_manifest(research / "manifest.toml")
    branch_path = next(iter(sorted((research / "branches").glob("*/branch.json"))), None)
    branch = (
        GraphBranchMetadata.model_validate_json(branch_path.read_text()) if branch_path else None
    )
    project_id = branch.project_id if branch else "benchmark"
    args.cache.mkdir(parents=True, exist_ok=True)
    store = AppStore(args.cache / "benchmark.sqlite3")
    store.upsert_project(
        ProjectRecord(
            project_id=project_id,
            locator=str(manifest.path),
            name="Benchmark",
            state_location=str(research),
            state_remote=False,
            added_at=store.now(),
        )
    )
    manager = HistoryManager(
        manifest,
        workspace=LocalStateWorkspace(research, "benchmark-copy"),
        project_id=project_id,
        write_authority=WriteAuthority(),
    )
    service = ProjectService(
        manifest, manager, PaperService(manifest, store), data_dir=args.cache, project_id=project_id
    )
    if args.worker == "prepare":
        engine = service.history_engine
        for target in [GraphTargetRef(), *([branch.head.target] if branch else [])]:
            engine.state_at(engine.resolve(HeadSpec(revision="head"), default_target=target))
        return {}
    return {
        "input": "synthetic" if args.count is not None else "copy",
        "K": args.interval,
        **measure(service, store, args.cache, branch, count=args.count),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", nargs="+", type=int, default=[1000, 10000])
    parser.add_argument(
        "--compare-intervals",
        nargs="+",
        type=int,
        default=[],
        help="Optional checkpoint intervals to compare with the default K=256",
    )
    parser.add_argument("--entities", type=int, default=2100)
    parser.add_argument("--history-copy", type=Path)
    parser.add_argument("--worker", choices=["prepare", "measure"], help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cache", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--count", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--interval",
        type=int,
        default=limits.GRAPH_HISTORY_CHECKPOINT_INTERVAL,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args)), flush=True)
        return
    if any(interval < 1 for interval in args.compare_intervals):
        parser.error("comparison intervals must be positive")
    if args.entities < 1 or any(count < 2 for count in args.counts):
        parser.error("use at least two patches and one entity")
    print(
        "Times in ms; cold = fresh process, warm OS cache; ingest = receipt ingestion; append = admitted publication.",
        file=sys.stderr,
    )
    with tempfile.TemporaryDirectory(prefix="rcp-history-benchmark-") as temporary:
        counts = [None] if args.history_copy else args.counts
        for count in counts:
            for interval in dict.fromkeys(
                [limits.GRAPH_HISTORY_CHECKPOINT_INTERVAL, *args.compare_intervals]
            ):
                root = args.history_copy or Path(temporary) / f"history-{count}-{interval}"
                if count is not None:
                    fixture(root, count, args.entities)
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--root",
                    str(root),
                    "--cache",
                    str(Path(temporary) / f"cache-{count}-{interval}"),
                    "--interval",
                    str(interval),
                ]
                if count is not None:
                    command += ["--count", str(count)]
                subprocess.run(
                    [*command, "--worker", "prepare"], check=True, stdout=subprocess.DEVNULL
                )
                subprocess.run([*command, "--worker", "measure"], check=True)


if __name__ == "__main__":
    main()
