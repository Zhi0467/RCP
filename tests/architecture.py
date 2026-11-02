"""AST helpers shared by the static architecture tests."""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "src" / "rcp"


def _python_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py") if path.is_file())


def _module_package(module: Path | None) -> list[str]:
    """Dotted package parts of a module under `src/rcp`, e.g. ['rcp', 'runs']."""
    if module is None:
        return []
    return ["rcp", *module.relative_to(SOURCE).parent.parts]


def _import_aliases(tree: ast.AST, module: Path | None = None) -> dict[str, str]:
    """Local name to dotted origin for every import in a module.

    `import os as o` maps `o` to `os`; `from io import open as io_open` maps
    `io_open` to `io.open`; a plain `import os` maps `os` to itself. A relative
    import is resolved against the module's own package, so `from .chat import
    _chat_path` inside `rcp/runs/` maps to `rcp.runs.chat._chat_path`. A name
    not in the map is a local binding, not a module.
    """
    package = _module_package(module)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    aliases[root] = root
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                anchor = package[: len(package) - (node.level - 1)] if package else []
                base = ".".join([*anchor, *([node.module] if node.module else [])])
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{base}.{alias.name}" if base else alias.name
    return aliases


def _call_name(call: ast.Call, aliases: dict[str, str]) -> tuple[str, ast.expr | None]:
    """The callee's canonical dotted name, and its receiver when it is a method
    call on something other than an imported module (None otherwise).

    An attribute chain `a.b.c(...)` is walked to its root: a root that is an
    imported name makes the whole chain module-qualified, anything else is a
    method call.
    """
    function = call.func
    if isinstance(function, ast.Name):
        return aliases.get(function.id, function.id), None
    if isinstance(function, ast.Attribute):
        chain = [function.attr]
        owner = function.value
        while isinstance(owner, ast.Attribute):
            chain.append(owner.attr)
            owner = owner.value
        if isinstance(owner, ast.Name) and owner.id in aliases:
            return ".".join([aliases[owner.id], *reversed(chain)]), None
        return f".{function.attr}", function.value
    return "", None
