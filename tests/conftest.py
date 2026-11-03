from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from rcp.config import Manifest

from .helpers import isolate_host, write_test_manifest
from .shards import SHARD_ENV, parse_shard, shard_items


def _skip_xcrun_git_trampoline() -> None:
    """macOS /usr/bin/git is an xcrun trampoline costing about 10 ms a call, so git-
    heavy tests run the binary it execs directly.
    """
    if sys.platform != "darwin" or shutil.which("git") != "/usr/bin/git":
        return
    found = subprocess.run(["xcrun", "-f", "git"], capture_output=True, text=True, check=False)
    real = Path(found.stdout.strip())
    if found.returncode or not real.is_absolute() or real.parent == Path("/usr/bin"):
        return
    if not os.access(real, os.X_OK):
        return
    entries = os.environ["PATH"].split(os.pathsep)
    usr_bin = next(index for index, entry in enumerate(entries) if Path(entry) == Path("/usr/bin"))
    entries.insert(usr_bin, str(real.parent))
    os.environ["PATH"] = os.pathsep.join(entries)


_skip_xcrun_git_trampoline()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    spec = os.environ.get(SHARD_ENV)
    if not spec:
        return
    kept, dropped = shard_items(items, *parse_shard(spec))
    items[:] = kept
    config.hook.pytest_deselected(items=dropped)


@pytest.fixture(autouse=True)
def isolated_host(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Apply `isolate_host` to every test and stop the engines it started."""

    discovery = request.node.get_closest_marker("real_provider_discovery") is not None
    engines = isolate_host(monkeypatch, tmp_path, provider_discovery=discovery)
    yield
    for engine in engines:
        engine.shutdown()
    from .paper_helpers import close_paper_owners

    close_paper_owners()


@pytest.fixture
def manifest(tmp_path: Path) -> Manifest:
    return write_test_manifest(tmp_path)
