"""Split one collected suite into CI shards by whole test file.

`RCP_TEST_SHARD=<index>/<total>` (1-based) keeps one shard. Whole files stay
together, so a module fixture is built in one shard only and `--dist loadfile`
sees the same files it would without sharding. Files go largest first to the
lightest shard. A file's size in bytes predicts its run time far better than its
test count, whose split left one shard almost twice as long on CI. Every shard
computes the same assignment from the same checkout and collection, so each test
runs in exactly one shard.
"""

from __future__ import annotations

import pytest

SHARD_ENV = "RCP_TEST_SHARD"


def parse_shard(spec: str) -> tuple[int, int]:
    index, _, total = spec.partition("/")
    shard, count = int(index), int(total)
    if not 1 <= shard <= count:
        raise ValueError(f"{SHARD_ENV} must be <index>/<total> with 1 <= index <= total")
    return shard, count


def shard_items(
    items: list[pytest.Item], shard: int, count: int
) -> tuple[list[pytest.Item], list[pytest.Item]]:
    """Return the items this shard keeps and the ones it deselects."""

    sizes = {item.nodeid.split("::", 1)[0]: item.path.stat().st_size for item in items}
    loads = [0] * count
    owner: dict[str, int] = {}
    for path, size in sorted(sizes.items(), key=lambda entry: (-entry[1], entry[0])):
        target = loads.index(min(loads))
        owner[path] = target + 1
        loads[target] += size
    kept, dropped = [], []
    for item in items:
        (kept if owner[item.nodeid.split("::", 1)[0]] == shard else dropped).append(item)
    return kept, dropped
