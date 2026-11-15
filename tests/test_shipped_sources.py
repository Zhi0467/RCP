"""Checks on the set `tests/shipped_sources.py` derives."""

from __future__ import annotations

from tests.architecture import SOURCE
from tests.restore_schemas import COMMAND
from tests.shipped_sources import (
    ROOT,
    _import_violations,
    _relative,
    _scan,
    _Shipped,
    _shipped_sources,
    py39_targets,
)


def test_shipped_set_is_derivable_and_matches_ruff_py39_targets() -> None:
    shipped = _shipped_sources()
    assert shipped.failures == []
    targets = py39_targets((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert targets == _relative(shipped.files), f"Run `{COMMAND}`."


def test_shipped_sources_import_only_python_39_stdlib_or_shipped_modules_at_top_level() -> None:
    shipped = _shipped_sources().files
    violations = {
        path.relative_to(ROOT).as_posix(): found
        for path in sorted(shipped)
        if (found := _import_violations(path.read_text(encoding="utf-8"), shipped))
    }
    assert violations == {}


def test_reader_scan_resolves_each_reader_shape() -> None:
    shipped = _Shipped()
    _scan(
        SOURCE / "transport" / "sample.py",
        """
import importlib.resources
import inspect
from pathlib import Path
from rcp import repository_window
from rcp.transport.state import _remote_script

def readers():
    root = Path(__file__).parent
    resources = importlib.resources.files("rcp")
    (Path(__file__).parent.parent / "agents" / "a.py").read_text()
    (root.parent / "b.py").open()
    resources.joinpath("c.py").read_bytes()
    importlib.resources.files("rcp.agents").joinpath("sub", "d.py").read_text()
    importlib.resources.files("rcp").joinpath("panel.js").read_text()
    _remote_script("e.py")
    inspect.getsource(repository_window)
    inspect.getsource(readers)
    Path("elsewhere.py").read_text()
""",
        shipped,
    )
    assert shipped.failures == []
    assert shipped.files == {
        SOURCE / "agents" / "a.py",
        SOURCE / "b.py",
        SOURCE / "c.py",
        SOURCE / "agents" / "sub" / "d.py",
        SOURCE / "transport" / "e.py",
        SOURCE / "repository_window.py",
    }


def test_reader_scan_fails_a_computed_target_outside_the_exempt_wrappers() -> None:
    shipped = _Shipped()
    _scan(
        SOURCE / "sample.py",
        """
import importlib.resources
from pathlib import Path

def _remote_script(name):
    return importlib.resources.files("rcp.transport").joinpath(name).read_text()

def caller(name, suffix):
    _remote_script(name)
    clip = Path(__file__).parent / f"check.{suffix}"
    clip.read_bytes()
""",
        shipped,
    )
    assert [failure.split(": ")[0] for failure in shipped.failures] == [
        "src/rcp/sample.py:9",
        "src/rcp/sample.py:11",
    ]


def test_import_rule_allows_python_39_stdlib_shipped_modules_and_guarded_imports() -> None:
    source = """
from __future__ import annotations
import os.path
import tomllib
from typing import TYPE_CHECKING
from rcp.artifact_preview import IDLE_ENV
from rcp.limits import X
from . import sibling
if TYPE_CHECKING:
    from rcp.models import Y

def later():
    import rcp
"""
    shipped = {SOURCE / "artifact_preview.py"}
    assert [violation.split(": ")[1] for violation in _import_violations(source, shipped)] == [
        "tomllib",
        "rcp.limits",
        ".",
    ]
