from __future__ import annotations

from types import SimpleNamespace

import pytest

from .shards import parse_shard, shard_items


@pytest.mark.parametrize("count", [1, 2, 3])
def test_shards_run_every_test_once_and_keep_files_whole(count: int) -> None:
    items = [
        SimpleNamespace(nodeid=f"tests/test_{name}.py::test_{index}")
        for name, size in [("a", 5), ("b", 3), ("c", 3), ("d", 1)]
        for index in range(size)
    ]
    kept = [shard_items(items, shard, count)[0] for shard in range(1, count + 1)]  # type: ignore[arg-type]

    assert sorted(item.nodeid for part in kept for item in part) == sorted(
        item.nodeid for item in items
    )
    files = [{item.nodeid.split("::")[0] for item in part} for part in kept]
    assert sum(len(part) for part in files) == 4


@pytest.mark.parametrize("spec", ["0/2", "3/2", "1"])
def test_shard_spec_outside_the_range_is_refused(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_shard(spec)
