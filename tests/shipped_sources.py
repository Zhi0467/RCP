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
import re
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


_PY39_BLOCK = re.compile(r'(?:^"[^"\n]+" = "py39"\n)+', re.MULTILINE)


def shipped_relative_paths() -> list[str]:
    shipped = _shipped_sources()
    if shipped.failures:
        raise ValueError("shipped readers with a computed target: " + "; ".join(shipped.failures))
    return _relative(shipped.files)


def py39_targets(pyproject_text: str) -> list[str]:
    ruff = tomllib.loads(pyproject_text)["tool"]["ruff"]
    globs = [glob for glob, version in ruff["per-file-target-version"].items() if version == "py39"]
    return _relative({path for glob in globs for path in ROOT.glob(glob)})


def rewrite_py39_block(pyproject_text: str, shipped: list[str]) -> str:
    """One exact `py39` line per shipped file, where the old lines were."""

    blocks = list(_PY39_BLOCK.finditer(pyproject_text))
    if len(blocks) != 1:
        raise ValueError(f"expected one py39 block, found {len(blocks)}")
    lines = "".join(f'"{path}" = "py39"\n' for path in sorted(shipped))
    return pyproject_text[: blocks[0].start()] + lines + pyproject_text[blocks[0].end() :]
