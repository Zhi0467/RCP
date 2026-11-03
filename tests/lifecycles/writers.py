"""Find every function in ``src/rcp`` that writes a table.

The scan reads string literals and f-strings for ``UPDATE [OR <word>] <t>``,
``INSERT [OR <word>] INTO <t>`` and ``DELETE FROM <t>`` (any case, any
whitespace between keywords, ``<t>`` optionally quoted), plus calls to declared
gates. A literal ``<t>`` counts only when it is a table of a freshly created
store, so prose such as "update the row" never reads as a table; an interpolated
``<t>`` is recorded as the unknown table ``{}``. The writer is the outermost
function or method around the match, since a nested ``def`` cannot be imported
or counted; SQL at module level is attributed to ``<module>``.

Blind spots: a helper that receives the table name as an argument, and SQL
assembled outside one literal.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

UNKNOWN = "{}"
GATES: Mapping[str, str] = {"_transition_agent_task": "graph_runs"}

_WRITE = re.compile(
    r"\b(?:UPDATE(?:\s+OR\s+\w+)?|INSERT(?:\s+OR\s+\w+)?\s+INTO|DELETE\s+FROM)\s+"
    r"[\"`]?(\{\}|\w+)",
    re.IGNORECASE,
)


def _text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
            else UNKNOWN
            for part in node.values
        )
    return None


def _owners(tree: ast.Module) -> list[tuple[int, int, str]]:
    spans = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            spans.append((node.lineno, node.end_lineno or node.lineno, node.name))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    spans.append(
                        (item.lineno, item.end_lineno or item.lineno, f"{node.name}.{item.name}")
                    )
    return spans


def _owner(spans: list[tuple[int, int, str]], line: int) -> str:
    return next((name for start, end, name in spans if start <= line <= end), "<module>")


def scan_source(source: str, module: str, tables: Iterable[str]) -> set[tuple[str, str]]:
    """``(writer, table)`` pairs in one module's source, writers as ``module:qualname``."""

    known = {table.lower(): table for table in tables}
    tree = ast.parse(source)
    spans = _owners(tree)
    found: set[tuple[str, str]] = set()
    nested: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            nested.update(id(part) for part in node.values)
    for node in ast.walk(tree):
        if id(node) in nested:
            continue
        if isinstance(node, ast.Call):
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
            if name in GATES:
                found.add((f"{module}:{_owner(spans, node.lineno)}", GATES[name]))
            continue
        text = _text(node)
        if text is None:
            continue
        for match in _WRITE.finditer(text):
            name = match.group(1)
            table = UNKNOWN if name == UNKNOWN else known.get(name.lower())
            if table is not None:
                found.add((f"{module}:{_owner(spans, node.lineno)}", table))
    return found


def scan(root: Path, tables: Iterable[str]) -> set[tuple[str, str]]:
    """Every ``(writer, table)`` pair under the package directory ``root``."""

    tables = tuple(tables)
    found: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root.parent).with_suffix("")
        module = ".".join(relative.parts)
        if module.endswith(".__init__"):
            module = module.removesuffix(".__init__")
        found |= scan_source(path.read_text(encoding="utf-8"), module, tables)
    return found


def schema_tables(database: Path) -> frozenset[str]:
    """The tables a freshly created store at ``database`` holds."""

    import sqlite3
    from contextlib import closing

    from rcp.storage import AppStore

    AppStore(database).close()
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return frozenset(row[0] for row in rows if not row[0].startswith("sqlite_"))
