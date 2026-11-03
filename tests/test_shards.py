from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from .shards import parse_shard, shard_items


@pytest.mark.parametrize("count", [1, 2, 3])
def test_shards_run_every_test_once_and_keep_files_whole(count: int, tmp_path: Path) -> None:
    items = []
    for name, size in [("a", 5), ("b", 3), ("c", 3), ("d", 1)]:
        path = tmp_path / f"test_{name}.py"
        path.write_text("#" * size * 100)
        items += [
            SimpleNamespace(nodeid=f"tests/test_{name}.py::test_{index}", path=path)
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


def test_lifecycle_explorers_spread_across_shards(tmp_path: Path) -> None:
    items = []
    for nodeid, size in [
        ("tests/test_big.py", 50),
        ("tests/test_mid.py", 40),
        ("tests/lifecycles/test_a.py", 1),
        ("tests/lifecycles/test_b.py", 1),
    ]:
        path = tmp_path / nodeid.replace("/", "_")
        path.write_text("#" * size * 100)
        items.append(SimpleNamespace(nodeid=f"{nodeid}::test", path=path))
    owners = {
        item.nodeid.split("::")[0]: shard
        for shard in (1, 2, 3)
        for item in shard_items(items, shard, 3)[0]  # type: ignore[arg-type]
    }

    assert owners["tests/lifecycles/test_a.py"] != owners["tests/lifecycles/test_b.py"]
