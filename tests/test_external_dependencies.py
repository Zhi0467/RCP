"""Hold `rcp.dependencies` to the Python launch sites and the server guide.

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

from rcp.dependencies import BY_NAME, UBUNTU_BASE_PACKAGES, required

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
    "execvp",
    "run_rsync",
    "_probe",
    "_target",
    "_target_result",
    # A compute job's argv is launched by its backend profile.
    "ComputeLaunchRequest",
}
SSH_BUILDERS = {"ssh_arguments", "_strict_ssh_arguments"}
# A literal `shutil.which("tool")` names a program RCP depends on, even when the
# launch itself receives the resolved path.
LOOKUPS = {"which"}
# An `sh -c` script contributes the first word of each simple command, after
# leading `NAME=value` assignments, and the program after find's `-exec`. Lines
# are commands; a trailing backslash continues one. Builtins and keywords are not
# programs. This is a bounded reading, not a shell parser.
SHELL_BUILTINS = {"cd", "echo", "exec", "exit", "export", "set", "trap", "wait"}
SHELL_KEYWORDS = {"if", "then", "else", "elif", "fi", "for", "do", "done", "while"}
SHELL_KEYWORDS |= {"until", "case", "esac", "in", "!", "[", "[[", "{", "}"}
SHELL_SEPARATOR = re.compile(r"&&|\|\||[;|&]")
# Stands in for an interpolated value; a command word containing it is dynamic.
DYNAMIC = "\0"
# `_programs` records names only at calls to these; a file without one has none.
LAUNCH_CALL = re.compile(
    r"\b(?:" + "|".join(sorted(map(re.escape, RUNNERS | SSH_BUILDERS | LOOKUPS))) + r")\s*\("
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
                                if Path(candidate.value).name in {"sh", "bash"}:
                                    shell(value.elts[1:])
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

        def shell(operands: list[ast.expr]) -> None:
            # Only the operand after a literal `-c`-style flag is a script.
            for flag, script in zip(operands, operands[1:], strict=False):
                if not (
                    isinstance(flag, ast.Constant)
                    and isinstance(flag.value, str)
                    and re.fullmatch(r"-[a-z]*c", flag.value)
                ):
                    continue
                for value in resolve(script):
                    if isinstance(value, (ast.List, ast.Tuple)):
                        argv(value)
                        continue
                    text = ""
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        text = value.value
                    elif isinstance(value, ast.JoinedStr):
                        text = "".join(
                            part.value
                            if isinstance(part, ast.Constant) and isinstance(part.value, str)
                            else DYNAMIC
                            for part in value.values
                        )
                    text = text.replace("\\\n", " ").replace("\n", ";")
                    for command in SHELL_SEPARATOR.split(text):
                        words = command.split()
                        while words and (words[0] in SHELL_KEYWORDS or "=" in words[0]):
                            words = words[1:]
                        candidates = words[:1] + [
                            word
                            for flag, word in zip(words, words[1:], strict=False)
                            if flag == "-exec"
                        ]
                        for word in candidates:
                            if word not in SHELL_BUILTINS and re.fullmatch(
                                r"[A-Za-z0-9_./][A-Za-z0-9_./-]*", word
                            ):
                                found.add(Path(word).name)
                return

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
                        program = words[0]
                        # A configured shell is dynamic, but its literal fallback
                        # still belongs in the external-program inventory.
                        default = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z_0-9]*:-([^${}]+)\}", program)
                        if default:
                            program = default[1]
                        found.add(Path(program).name)

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
                if (
                    name == "join"
                    and isinstance(node.func, ast.Attribute)
                    and _name(node.func.value) == "shlex"
                    and node.args
                ):
                    # A literal argv joined into a shell command string.
                    argv(node.args[0])
                if (
                    name in LOOKUPS
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    found.add(Path(node.args[0].value).name)
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
    missing = sorted(locations.keys() - BY_NAME.keys())
    stale = sorted(BY_NAME.keys() - locations.keys())
    assert not missing and not stale, (
        "Programs missing from rcp.dependencies:\n"
        + "\n".join(f"  {name}: {', '.join(locations[name])}" for name in missing)
        + "\nRegistry programs absent from source: "
        + ", ".join(stale)
    )


def test_server_guide_installs_every_required_linux_package() -> None:
    guide = (ROOT / "docs/server.md").read_text()
    lines = re.findall(r"^sudo apt-get install --yes (.+)$", guide, re.MULTILINE)
    assert len(lines) == 1
    listed = set(lines[0].split())
    needed = {
        dependency.apt
        for role in ("server_install", "server", "local")
        for dependency in required(role, "linux")
        if dependency.apt is not None
    }
    assert not needed - UBUNTU_BASE_PACKAGES - listed


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
ssh_arguments(host, 'exec "${SHELL:-/bin/sh}" -lc command')
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
shutil.which("looked-up-tool")
shutil.which(configured_tool)
wrapper = shlex.join(["joined-tool", "--wait", "sh", "-c", child])
ComputeLaunchRequest(argv=["sh", "-c", f"echo ready; script-tool {seconds} && {dynamic}"])
subprocess.run(["bash", "-lc", "if [ ! -d x ]; then\\n  exit 0\\nfi\\nline-tool x -exec exec-arg-tool {} + | LC_ALL=C piped-tool"])
os.execvp("execvp-tool", ["execvp-tool", "-D"])
"""
    assert _programs(source) == {
        "assigned-tool",
        "built-tool",
        "tuple-tool",
        "async-tool",
        "starred-tool",
        "ssh",
        "sh",
        "remote-tool",
        "remote-list-tool",
        "remote-tuple-tool",
        "remote-string-tool",
        "direct-remote-tool",
        "exec-tool",
        "rsync",
        "loop-tool",
        "looked-up-tool",
        "joined-tool",
        "script-tool",
        "bash",
        "line-tool",
        "exec-arg-tool",
        "piped-tool",
        "execvp-tool",
    }
