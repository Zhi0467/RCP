"""Python that RCP ships as source must run under a host's Python 3.9 `python3`.

A file is shipped when RCP reads it whole to send it to a host. The set is
derived from the readers in `src/rcp`, never listed by hand:

- every module `remote_bundle_modules()` returns;
- every literal target of a whole-file wrapper in `WRAPPERS`;
- every `.py` target of a `.read_text()`, `.read_bytes()` or `.open()` whose
  receiver is rooted at `Path(__file__)` or `importlib.resources.files(<literal>)`,
  directly or through a local bound to one in the same function, following
  `.parent`, `/` and `.joinpath` with literal segments;
- every module passed to `inspect.getsource`.

A reader or wrapper whose target is not literal fails with its location,
except inside `EXEMPT_FUNCTIONS`. Ruff checks the syntax through the `py39`
entries of `[tool.ruff.per-file-target-version]`, which must name exactly this
set. This file checks the imports: a shipped file imports at top level only
the Python 3.9 standard library and other shipped modules, which their
readers send alongside it.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from rcp.providers import remote_bundle_modules
from tests.architecture import SOURCE, _call_name, _import_aliases, _python_files

ROOT = SOURCE.parent.parent
WRAPPERS = {"_remote_script": SOURCE / "transport", "terminal_source": SOURCE / "terminals"}
EXEMPT_FUNCTIONS = {*WRAPPERS, "_module_source"}
READERS = {"read_text", "read_bytes", "open"}
NEWER_THAN_39 = {"tomllib"}


class _NotLiteral(Exception):
    pass


@dataclass
class _Shipped:
    files: set[Path] = field(default_factory=set)
    failures: list[str] = field(default_factory=list)


def _module_file(dotted: str) -> Path | None:
    base = SOURCE.parent.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _literal(node: ast.expr | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise _NotLiteral


class _ReaderScan(ast.NodeVisitor):
    def __init__(self, path: Path, aliases: dict[str, str], shipped: _Shipped) -> None:
        self.path = path
        self.aliases = aliases
        self.shipped = shipped
        self.function = ""
        self.bindings: dict[str, ast.expr] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        saved = self.function, self.bindings
        self.function = node.name
        self.bindings = {
            target.id: assign.value
            for assign in ast.walk(node)
            if isinstance(assign, ast.Assign)
            for target in assign.targets
            if isinstance(target, ast.Name)
        }
        self.generic_visit(node)
        self.function, self.bindings = saved

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        if self.function not in EXEMPT_FUNCTIONS:
            try:
                target = self._target(node)
            except _NotLiteral:
                self.shipped.failures.append(
                    f"{self.path.relative_to(ROOT)}:{node.lineno}: {ast.unparse(node)[:80]}"
                )
            else:
                if target is not None and target.suffix == ".py":
                    self.shipped.files.add(target)
        self.generic_visit(node)

    def _target(self, call: ast.Call) -> Path | None:
        name, _ = _call_name(call, self.aliases)
        wrapper = name.rpartition(".")[2]
        if wrapper in WRAPPERS and isinstance(call.func, ast.Name):
            return WRAPPERS[wrapper] / _literal(call.args[0] if call.args else None)
        if name == "inspect.getsource" and call.args and isinstance(call.args[0], ast.Name):
            module = self.aliases.get(call.args[0].id)
            return _module_file(module) if module else None
        if isinstance(call.func, ast.Attribute) and call.func.attr in READERS:
            return self._path(call.func.value, self.bindings)
        return None

    def _path(self, node: ast.expr, bindings: dict[str, ast.expr]) -> Path | None:
        if isinstance(node, ast.Name) and node.id in bindings:
            rest = {name: value for name, value in bindings.items() if name != node.id}
            return self._path(bindings[node.id], rest)
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            base = self._path(node.value, bindings)
            return None if base is None else base.parent
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            base = self._path(node.left, bindings)
            return None if base is None else base / _literal(node.right)
        if not isinstance(node, ast.Call):
            return None
        name, _ = _call_name(node, self.aliases)
        arguments = node.args
        if name in {"Path", "pathlib.Path"}:
            from_file = [ast.unparse(argument) for argument in arguments] == ["__file__"]
            return self.path if from_file else None
        if name == "importlib.resources.files":
            package = _literal(arguments[0] if arguments else None)
            return SOURCE.parent.joinpath(*package.split("."))
        if isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath":
            base = self._path(node.func.value, bindings)
            if base is None:
                return None
            return base.joinpath(*(_literal(argument) for argument in arguments))
        return None


def _scan(path: Path, source: str, shipped: _Shipped) -> None:
    tree = ast.parse(source)
    _ReaderScan(path, _import_aliases(tree, path), shipped).visit(tree)


def _shipped_sources() -> _Shipped:
    shipped = _Shipped()
    for name in remote_bundle_modules():
        module = _module_file(name)
        assert module is not None, name
        shipped.files.add(module)
    for path in _python_files(SOURCE):
        _scan(path, path.read_text(encoding="utf-8"), shipped)
    return shipped


def _import_violations(source: str, shipped: set[Path]) -> list[str]:
    allowed = sys.stdlib_module_names - NEWER_THAN_39
    violations = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = ["." if node.level else node.module or ""]
        else:
            continue
        violations += [
            f"{node.lineno}: {module}"
            for module in modules
            if module.split(".")[0] not in allowed and _module_file(module) not in shipped
        ]
    return violations


def _relative(paths: set[Path]) -> list[str]:
    return sorted(path.relative_to(ROOT).as_posix() for path in paths)


def test_shipped_set_is_derivable_and_matches_ruff_py39_targets() -> None:
    shipped = _shipped_sources()
    assert shipped.failures == []
    ruff = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["ruff"]
    globs = [glob for glob, version in ruff["per-file-target-version"].items() if version == "py39"]
    assert _relative({path for glob in globs for path in ROOT.glob(glob)}) == _relative(
        shipped.files
    )


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
