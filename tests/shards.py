"""Split one collected suite into CI shards by whole test file.

`RCP_TEST_SHARD=<index>/<total>` (1-based) keeps one shard. Whole files stay
together, so a module fixture is built in one shard only and `--dist loadfile`
sees the same files it would without sharding. Each file weighs its CI seconds
recorded in shard_timings.json, and a file missing from the record weighs the
median. Files go heaviest first to the lightest shard. Every shard computes the
same assignment from the same checkout and collection, so each test runs in
exactly one shard. `scripts/update_shard_timings.py` rewrites the record.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

SHARD_ENV = "RCP_TEST_SHARD"
TIMINGS = Path(__file__).with_name("shard_timings.json")


def parse_shard(spec: str) -> tuple[int, int]:
    index, _, total = spec.partition("/")
    shard, count = int(index), int(total)
    if not 1 <= shard <= count:
        raise ValueError(f"{SHARD_ENV} must be <index>/<total> with 1 <= index <= total")
    return shard, count


def load_timings() -> dict[str, float]:
    return json.loads(TIMINGS.read_text(encoding="utf-8"))


def plan_shards(
    paths: Iterable[str], timings: Mapping[str, float], count: int
) -> tuple[dict[str, int], list[float]]:
    """Return each file's 1-based shard and each shard's predicted seconds."""

    default = statistics.median(timings.values()) if timings else 1.0
    weights = {path: timings.get(path, default) for path in paths}
    loads = [0.0] * count
    owner: dict[str, int] = {}
    for path, weight in sorted(weights.items(), key=lambda entry: (-entry[1], entry[0])):
        target = loads.index(min(loads))
        owner[path] = target + 1
        loads[target] += weight
    return owner, loads


def shard_items(
    items: list[pytest.Item], shard: int, count: int, timings: Mapping[str, float]
) -> tuple[list[pytest.Item], list[pytest.Item]]:
    """Return the items this shard keeps and the ones it deselects."""

    owner, _ = plan_shards({item.nodeid.split("::", 1)[0] for item in items}, timings, count)
    kept, dropped = [], []
    for item in items:
        (kept if owner[item.nodeid.split("::", 1)[0]] == shard else dropped).append(item)
    return kept, dropped
