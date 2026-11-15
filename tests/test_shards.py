from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from .shards import TIMINGS, parse_shard, plan_shards, shard_items


def _items(sizes: dict[str, int]) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(nodeid=f"{path}::test_{index}")
        for path, size in sizes.items()
        for index in range(size)
    ]


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_shards_run_every_test_once_and_keep_files_whole(count: int) -> None:
    items = _items(
        {"tests/test_a.py": 5, "tests/test_b.py": 3, "tests/test_c.py": 3, "tests/d.py": 1}
    )
    timings = {"tests/test_a.py": 9.0, "tests/test_b.py": 4.0}
    kept = [shard_items(items, shard, count, timings)[0] for shard in range(1, count + 1)]  # type: ignore[arg-type]

    assert sorted(item.nodeid for part in kept for item in part) == sorted(
        item.nodeid for item in items
    )
    files = [{item.nodeid.split("::")[0] for item in part} for part in kept]
    assert sum(len(part) for part in files) == 4


@pytest.mark.parametrize("spec", ["0/2", "3/2", "1"])
def test_shard_spec_outside_the_range_is_refused(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_shard(spec)


def test_recorded_time_not_test_count_balances_shards() -> None:
    owner, loads = plan_shards(
        ["slow.py", "many.py", "fast_a.py", "fast_b.py"],
        {"slow.py": 100.0, "many.py": 50.0, "fast_a.py": 30.0, "fast_b.py": 20.0},
        2,
    )

    assert owner["slow.py"] != owner["many.py"]
    assert loads == [100.0, 100.0]


def test_unrecorded_file_weighs_the_median() -> None:
    _, loads = plan_shards(
        ["a.py", "b.py", "c.py", "new.py"], {"a.py": 1.0, "b.py": 5.0, "c.py": 9.0}, 1
    )

    assert loads == [20.0]


def test_update_script_averages_versions_and_resolves_class_names(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "update_shard_timings", TIMINGS.parents[1] / "scripts/update_shard_timings.py"
    )
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    (tmp_path / "tests/sub").mkdir(parents=True)
    (tmp_path / "tests/sub/test_x.py").touch()
    (tmp_path / "tests/test_y.py").touch()
    reports = []
    for name, x_seconds in [("py311.xml", "2.0"), ("py312.xml", "4.0")]:
        report = tmp_path / name
        report.write_text(
            "<testsuites><testsuite>"
            f'<testcase classname="tests.sub.test_x" name="a" time="{x_seconds}"/>'
            '<testcase classname="tests.sub.test_x.TestGroup" name="b[1.5]" time="1.0"/>'
            '<testcase classname="tests.test_y" name="c" time="0.5"/>'
            "</testsuite></testsuites>"
        )
        reports.append(report)

    assert script.file_timings(reports, tmp_path) == {
        "tests/sub/test_x.py": 4.0,
        "tests/test_y.py": 0.5,
    }
