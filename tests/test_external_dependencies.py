"""Keep external program names in the operations spec aligned with Python launch sites.

Scan literal argv heads, including assignments/builders and SSH/runner wrappers.
Literal remote shell commands contribute their first program. ``sys.executable``
and fully dynamic argv (configured providers, shells, interpreters, and user jobs)
are excluded: this is a static name inventory, not an execution or prose check.
"""

from __future__ import annotations

import ast
import re
import shlex
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Concrete subprocess adapters, including injected subprocess runners and the
# account/compute wrappers. Arguments are inspected as argv, never as prose.
RUNNERS = {
    "run",
    "Popen",
    "check_output",
    "create_subprocess_exec",
    "runner",
    "_runner",
    "_run",
    "_run_process",
    "_run_read_only",
    "_require_command",
    "_run_as_account",
    "_run_as_service",
    "_ssh",
    "_ssh_bytes",
    "popen",
    "run_check_process",
    "_version",
    "facility_probe",
    "launch",
    "execv",
    "run_rsync",
    "_probe",
    "_target",
    "_target_result",
}
SSH_BUILDERS = {"ssh_arguments", "_strict_ssh_arguments"}
# `_programs` records names only at calls to these; a file without one has none.
LAUNCH_CALL = re.compile(
    r"\b(?:" + "|".join(sorted(map(re.escape, RUNNERS | SSH_BUILDERS))) + r")\s*\("
)


def _name(node: ast.expr) -> str:
    return (
        node.id
        if isinstance(node, ast.Name)
        else node.attr
        if isinstance(node, ast.Attribute)
        else ""
    )


def _programs(source: str) -> set[str]:
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    found: set[str] = set()

    def scan(scope: ast.AST, inherited: dict[str, list[ast.expr]]) -> None:
        bindings = {key: list(values) for key, values in inherited.items()}
        nodes: list[ast.AST] = []

        def collect(node: ast.AST) -> None:
            nodes.append(node)
            if node is not scope and isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                return
            for child in ast.iter_child_nodes(node):
                collect(child)

        collect(scope)
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = scope.args
            for argument, default in zip(
                arguments.posonlyargs + arguments.args,
                [None]
                * (len(arguments.posonlyargs) + len(arguments.args) - len(arguments.defaults))
                + arguments.defaults,
                strict=True,
            ):
                if default is not None:
                    bindings[argument.arg] = [default]
            for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
                if default is not None:
                    bindings[argument.arg] = [default]
        for node in nodes:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bindings.setdefault(target.id, []).append(node.value)
            elif (
                isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value
            ):
                bindings.setdefault(node.target.id, []).append(node.value)
            elif (
                isinstance(node, ast.For)
                and isinstance(node.target, ast.Name)
                and isinstance(node.iter, (ast.List, ast.Tuple))
            ):
                bindings.setdefault(node.target.id, []).extend(node.iter.elts)

        def resolve(node: ast.expr, seen: frozenset[str] = frozenset()) -> list[ast.expr]:
            if isinstance(node, ast.Name) and node.id not in seen:
                return [
                    item
                    for value in bindings.get(node.id, [])
                    for item in resolve(value, seen | {node.id})
                ]
            if isinstance(node, ast.IfExp):
                return resolve(node.body, seen) + resolve(node.orelse, seen)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                return resolve(node.left, seen)
            if isinstance(node, ast.Call):
                name = _name(node.func)
                if name in {"join", "list", "tuple"} and node.args:
                    return resolve(node.args[0], seen)
                if name in functions and name not in seen:
                    return [
                        item
                        for child in ast.walk(functions[name])
                        if isinstance(child, ast.Return) and child.value
                        for item in resolve(child.value, seen | {name})
                    ]
            return [node]

        def argv(node: ast.expr) -> None:
            for value in resolve(node):
                if isinstance(value, (ast.List, ast.Tuple)) and value.elts:
                    head = value.elts[0]
                    if isinstance(head, ast.Starred):
                        argv(head.value)
                    else:
                        for candidate in resolve(head):
                            if isinstance(candidate, ast.Constant) and isinstance(
                                candidate.value, str
                            ):
                                found.add(Path(candidate.value).name)
                                if Path(candidate.value).name == "ssh":
                                    # Skip options/operands and destination to reach the
                                    # command, without mistaking a literal host for a tool.
                                    operands = iter(value.elts[1:])
                                    for operand in operands:
                                        if isinstance(operand, ast.Starred):
                                            continue
                                        if (
                                            isinstance(operand, ast.Constant)
                                            and isinstance(operand.value, str)
                                            and operand.value.startswith("-")
                                        ):
                                            if operand.value in {
                                                "-o",
                                                "-p",
                                                "-l",
                                                "-i",
                                                "-F",
                                                "-S",
                                                "-J",
                                                "-W",
                                                "-E",
                                                "-L",
                                                "-R",
                                                "-D",
                                                "-b",
                                                "-c",
                                                "-m",
                                                "-w",
                                            }:
                                                next(operands, None)
                                            continue
                                        command = next(operands, None)
                                        if command is not None:
                                            remote(command)
                                        break

        def remote(node: ast.expr) -> None:
            if (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Add)
                and isinstance(node.left, ast.Constant)
                and node.left.value == "exec "
            ):
                remote(node.right)
                return
            argv(node)
            for value in resolve(node):
                text = (
                    value.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                    else None
                )
                if (
                    isinstance(value, ast.JoinedStr)
                    and value.values
                    and isinstance(value.values[0], ast.Constant)
                ):
                    text = value.values[0].value.split(" ")[0]
                if text:
                    words = shlex.split(text)
                    if words and words[0] == "exec":
                        words = words[1:]
                    if words:
                        found.add(Path(words[0]).name)

        def bind(target: ast.expr, value: ast.expr) -> None:
            if isinstance(target, ast.Name):
                bindings.setdefault(target.id, []).append(value)
            elif isinstance(target, (ast.List, ast.Tuple)) and isinstance(
                value, (ast.List, ast.Tuple)
            ):
                for child, item in zip(target.elts, value.elts, strict=False):
                    bind(child, item)

        for node in nodes:
            if isinstance(node, ast.For):
                for sequence in resolve(node.iter):
                    if isinstance(sequence, (ast.List, ast.Tuple)):
                        for value in sequence.elts:
                            bind(node.target, value)

        for node in nodes:
            if isinstance(node, ast.Call):
                name = _name(node.func)
                if name in RUNNERS:
                    for argument in node.args:
                        argv(argument.value if isinstance(argument, ast.Starred) else argument)
                    if (
                        name == "create_subprocess_exec"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                    ):
                        remote(node.args[0])
                    for keyword in node.keywords:
                        if keyword.arg in {"args", "argv"}:
                            argv(keyword.value)
                if name in SSH_BUILDERS and len(node.args) > 1:
                    found.add("ssh")
                    remote(node.args[1])
                if name in {"_ssh", "_ssh_bytes"} and node.args:
                    # Methods own their host; free functions receive host first.
                    index = 0 if isinstance(node.func, ast.Attribute) or len(node.args) == 1 else 1
                    remote(node.args[index])
            if node is not scope and isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                scan(node, bindings)

    scan(tree, {})
    return found


def test_external_dependency_names_match_source() -> None:
    locations: dict[str, list[str]] = defaultdict(list)
    for path in sorted((ROOT / "src/rcp").rglob("*.py")):
        source = path.read_text()
        if not LAUNCH_CALL.search(source):
            continue
        for program in _programs(source):
            locations[program].append(str(path.relative_to(ROOT)))
    spec = (ROOT / "docs/specs/server-and-machine-operations.md").read_text()
    section = spec.split("## External dependencies\n", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE)
    missing = sorted(locations.keys() - set(rows))
    stale = sorted(set(rows) - locations.keys())
    assert len(rows) == len(set(rows)), "Duplicate dependency rows"
    assert not missing and not stale, (
        "Missing external programs:\n"
        + "\n".join(f"  {name}: {', '.join(locations[name])}" for name in missing)
        + "\nTable programs absent from source: "
        + ", ".join(stale)
    )


def test_inventory_follows_literal_launches_and_excludes_dynamic_commands() -> None:
    source = """
import asyncio
import subprocess
import sys

def build():
    return ["built-tool", "--version"]

argv = ["assigned-tool", "--version"]
subprocess.run(argv)
subprocess.Popen(build())
subprocess.check_output(("tuple-tool",))
asyncio.create_subprocess_exec("async-tool", "--version")
asyncio.create_subprocess_exec(*["starred-tool", "--version"])
ssh_arguments(host, "remote-tool --version")
self._ssh(["remote-list-tool", "--version"])
_ssh(host, ("remote-tuple-tool", "--version"))
self._ssh_bytes("remote-string-tool --version")
subprocess.run(["ssh", host, "direct-remote-tool --version"])
ssh_arguments(host, "exec " + shlex.join(["exec-tool", "--version"]))
run_rsync(host, ["rsync", "-a", source, destination])
for command in [["loop-tool", "--version"]]:
    runner(command)
subprocess.run([sys.executable, "-m", "rcp"])
subprocess.run(dynamic_argv)
subprocess.Popen([configured_binary, "--version"])
unrelated(["ordinary-data"])
"""
    assert _programs(source) == {
        "assigned-tool",
        "built-tool",
        "tuple-tool",
        "async-tool",
        "starred-tool",
        "ssh",
        "remote-tool",
        "remote-list-tool",
        "remote-tuple-tool",
        "remote-string-tool",
        "direct-remote-tool",
        "exec-tool",
        "rsync",
        "loop-tool",
    }
