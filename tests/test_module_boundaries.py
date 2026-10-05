"""Static boundaries that one grep can prove and one stray edit would break.

Each test here is the executable form of a rule whose owner is a single module:
the rule itself is documented at that owner, and this file keeps the rest of
the tree from quietly acquiring a second owner.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "src" / "rcp"

# Any call that puts bytes on disk. Routes compose service and history owners;
# they never hold a file handle on canonical or operational state themselves.
PATH_WRITE_METHODS = {
    "write_text",
    "write_bytes",
    "unlink",
    "rmdir",
    "touch",
    "mkdir",
    "symlink_to",
    "hardlink_to",
    "link_to",
}
# `str.replace(old, new)` and `Path.replace(target)` share a name; the Path
# form takes exactly one positional argument and no keywords.
PATH_SINGLE_TARGET_METHODS = {"rename", "replace"}
# Receivers whose `.open(x)` opens a project or record, never a file. Every
# other `.open(<computed>)` counts as a write until proven otherwise.
NON_FILE_OPEN_RECEIVERS = {"catalog", "_catalog"}
# Callables with the builtin `open(file, mode)` signature, by canonical name.
BUILTIN_OPENERS = {"open", "builtins.open", "io.open", "codecs.open"}
# Path classes whose methods may be called unbound with the path first.
UNBOUND_PATH_PREFIXES = tuple(
    f"pathlib.{name}." for name in ("Path", "PurePath", "PosixPath", "WindowsPath")
)
OS_WRITE_FUNCTIONS = {
    "replace",
    "rename",
    "remove",
    "unlink",
    "mkdir",
    "makedirs",
    "rmdir",
    "link",
    "symlink",
    "truncate",
    "ftruncate",
    "write",
    "pwrite",
    "writev",
}
SHUTIL_WRITE_PREFIXES = ("copy", "move", "rmtree")
WRITE_MODE = re.compile(r"[wax+]")


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
    call on something other than an imported module (None otherwise)."""
    function = call.func
    if isinstance(function, ast.Name):
        return aliases.get(function.id, function.id), None
    if isinstance(function, ast.Attribute):
        # Walk `a.b.c(...)` down to its root; a root that is an imported name
        # makes the whole chain module-qualified, anything else is a method call.
        chain = [function.attr]
        owner = function.value
        while isinstance(owner, ast.Attribute):
            chain.append(owner.attr)
            owner = owner.value
        if isinstance(owner, ast.Name) and owner.id in aliases:
            return ".".join([aliases[owner.id], *reversed(chain)]), None
        return f".{function.attr}", function.value
    return "", None


def _receiver_name(receiver: ast.expr | None) -> str | None:
    if isinstance(receiver, ast.Name):
        return receiver.id
    if isinstance(receiver, ast.Attribute):
        return receiver.attr
    return None


ROUTE_DECORATOR_METHODS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "websocket",
    "api_route",
}


def _declares_routes(tree: ast.AST) -> bool:
    """A module with a FastAPI route decorator anywhere in it."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in ROUTE_DECORATOR_METHODS
            ):
                return True
    return False


def _route_modules() -> list[tuple[Path, ast.AST]]:
    """Every module under the api package, plus any other module declaring routes."""
    modules: list[tuple[Path, ast.AST]] = []
    for path in _python_files(SOURCE):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if path.is_relative_to(SOURCE / "api") or _declares_routes(tree):
            modules.append((path, tree))
    return modules


def _open_mode(call: ast.Call, *, builtin: bool) -> tuple[ast.expr | None, bool]:
    """The mode argument of an `open` call and whether it was passed by keyword."""
    for keyword in call.keywords:
        if keyword.arg == "mode":
            return keyword.value, True
    position = 1 if builtin else 0
    if len(call.args) > position:
        return call.args[position], False
    return None, False


def _mode_writes(mode: ast.expr | None, *, computed_default: bool) -> bool:
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return WRITE_MODE.search(mode.value) is not None
    return computed_default


def _os_open_writes(call: ast.Call) -> bool:
    """`os.open(path, flags)` is a write unless the flags are exactly `O_RDONLY`."""
    flags: ast.expr | None = None
    for keyword in call.keywords:
        if keyword.arg == "flags":
            flags = keyword.value
    if flags is None and len(call.args) > 1:
        flags = call.args[1]
    if flags is None:
        return False
    return not (isinstance(flags, ast.Attribute) and flags.attr == "O_RDONLY")


def _writes_a_file(call: ast.Call, aliases: dict[str, str]) -> bool:
    name, receiver = _call_name(call, aliases)
    if name in BUILTIN_OPENERS or name == "os.fdopen":
        mode, _ = _open_mode(call, builtin=True)
        return _mode_writes(mode, computed_default=True)
    if name == "os.open":
        return _os_open_writes(call)
    if name.startswith("os."):
        return name.removeprefix("os.") in OS_WRITE_FUNCTIONS
    if name.startswith("shutil."):
        return name.removeprefix("shutil.").startswith(SHUTIL_WRITE_PREFIXES)
    positional = list(call.args)
    if name.startswith(UNBOUND_PATH_PREFIXES):
        # `Path.write_text(path, payload)`: the method called unbound, with the
        # receiver as its first positional argument.
        method = name.rsplit(".", 1)[1]
        receiver = positional[0] if positional else None
        positional = positional[1:]
    elif receiver is None:
        return False
    else:
        method = name.removeprefix(".")
    if method in PATH_WRITE_METHODS:
        return True
    if method in PATH_SINGLE_TARGET_METHODS:
        positional_target = len(positional) == 1 and not call.keywords
        keyword_target = not positional and [keyword.arg for keyword in call.keywords] == ["target"]
        return positional_target or keyword_target
    if method != "open":
        return False
    mode, by_keyword = _open_mode(call, builtin=len(positional) != len(call.args))
    # A computed mode is a write until proven otherwise. The one exemption is a
    # positional argument on a receiver known not to open files.
    computed_default = by_keyword or _receiver_name(receiver) not in NON_FILE_OPEN_RECEIVERS
    return _mode_writes(mode, computed_default=computed_default)


def _flagged_lines(source: str, predicate, module: Path | None = None) -> list[int]:
    tree = ast.parse(source)
    aliases = _import_aliases(tree, module)
    return sorted({node.lineno for node in ast.walk(tree) if predicate(node, aliases)})


def test_api_routes_never_write_files_directly() -> None:
    """Invariant 6: routes never write canonical files; `StateWorkspace` and the
    history manager own locking and publication, and the service layer owns
    everything else that touches disk."""

    modules = _route_modules()
    names = {path.relative_to(SOURCE).as_posix() for path, _ in modules}
    assert "phone_listener.py" in names, "route discovery must see routes outside api/"
    offenders: list[str] = []
    for path, tree in modules:
        aliases = _import_aliases(tree, path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _writes_a_file(node, aliases):
                offenders.append(
                    f"{path.relative_to(SOURCE)}:{node.lineno}: {ast.unparse(node)[:80]}"
                )
    assert offenders == [], (
        "API routes write files directly; move the write behind the owning service or "
        "history method:\n" + "\n".join(offenders)
    )


def test_route_write_detection_sees_keyword_and_multiline_modes() -> None:
    """The detector must not be fooled by the forms a one-line regex misses."""

    source = """
def route(path, mode):
    path.open(mode="w")
    open(
        path,
        "a",
    )
    catalog.open(project_id)
    path.open(mode)
    path.open(mode=mode)
    with path.open("rb") as handle:
        handle.read()
    open(path).read()
    text.replace("a", "b")
    path.replace(target)
    path.rename(target)
    path.symlink_to(target)
    os.symlink(source, target)
    os.link(source, target)
    path.replace(target=target)
    path.rename(target=target)
    os.open(path, os.O_RDONLY)
    os.open(path, os.O_WRONLY | os.O_CREAT)
    os.open(path, flags=os.O_RDWR)
    os.write(descriptor, payload)
    os.fdopen(descriptor, "w")
    os.fdopen(descriptor)
    io.open(path, "w")
    io.open(path)
    builtins.open(path, mode="a")
    codecs.open(path, "r")
"""
    source = "import os\nimport io\nimport builtins\nimport codecs\n" + source
    flagged = [line - 4 for line in _flagged_lines(source, _is_write_call)]
    assert flagged == [3, 4, 9, 10, 15, 16, 17, 18, 19, 20, 21, 23, 24, 25, 26, 28, 30]


def _is_write_call(node: ast.AST, aliases: dict[str, str]) -> bool:
    return isinstance(node, ast.Call) and _writes_a_file(node, aliases)


def test_route_write_detection_resolves_import_aliases() -> None:
    """An aliased import of a file primitive is classified by what it imports."""

    source = """
from io import open as io_open
import os as operating_system
from os import replace as swap
from codecs import open as decode
from pathlib import Path

def route(path, source, target):
    io_open(path, "w")
    io_open(path)
    operating_system.replace(source, target)
    swap(source, target)
    decode(path, "r")
    decode(path, "a")
    operating_system.open(path, operating_system.O_RDONLY)
    operating_system.open(path, operating_system.O_WRONLY)
"""
    assert _flagged_lines(source, _is_write_call) == [9, 11, 12, 14, 16]


def test_route_write_detection_sees_unbound_path_methods() -> None:
    source = """
from pathlib import Path
import pathlib

def route(path, target, payload):
    Path.write_text(path, payload)
    Path.replace(path, target)
    Path.unlink(path)
    Path.open(path, "w")
    Path.open(path)
    Path.read_text(path)
    pathlib.Path.rename(path, target)
"""
    assert _flagged_lines(source, _is_write_call) == [6, 7, 8, 9, 12]


# The transcript readers live on the project service for display and backup.
TRANSCRIPT_READERS = {
    "chat_transcript",
    "chat_transcripts",
    "_canonical_chat_summaries",
    "_canonical_chat_files",
    "canonical_chat_backup_sources",
}
TASK_RUNTIME = ("runs", "agents", "providers")
TRANSCRIPT_PATH_RESOLVERS = {"_chat_path", "rcp.runs.chat._chat_path"}


def _reads_a_file(node: ast.AST, aliases: dict[str, str]) -> bool:
    """Any way of getting a file's bytes: Path methods, builtin `open`, or a
    qualified or aliased builtin-style opener such as `io.open`."""
    if isinstance(node, ast.Attribute) and node.attr in {"read_text", "read_bytes", "open"}:
        return True
    if isinstance(node, ast.Call):
        name, _ = _call_name(node, aliases)
        return name in BUILTIN_OPENERS
    return False


def _references_transcript_reader(node: ast.AST, aliases: dict[str, str]) -> bool:
    """Any mention of a reader, called or passed along as a callback."""
    if isinstance(node, ast.Attribute):
        return node.attr in TRANSCRIPT_READERS
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id).rsplit(".", 1)[-1] in TRANSCRIPT_READERS
    return False


def test_transcript_reader_detection_sees_callbacks_and_aliases() -> None:
    source = """
from rcp.service import canonical_chat_backup_sources as backups

async def builder(service, chat_id):
    transcript = await asyncio.to_thread(service.chat_transcript, chat_id)
    sources = backups(service.root)
    return service.chat_transcripts([chat_id])
"""
    assert _flagged_lines(source, _references_transcript_reader) == [5, 6, 7]


def _resolves_transcript_path(
    node: ast.AST, aliases: dict[str, str], resolvers: set[str] = frozenset()
) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name, _ = _call_name(node, aliases)
    return name in TRANSCRIPT_PATH_RESOLVERS or name in resolvers or name.endswith(".chat_path")


def _resolver_holders(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    aliases: dict[str, str],
    resolvers: set[str],
) -> set[str]:
    """Local names bound to a resolver result by plain or annotated assignment."""
    holders: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and _resolves_transcript_path(
            node.value, aliases, resolvers
        ):
            holders.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _resolves_transcript_path(node.value, aliases, resolvers)
            and isinstance(node.target, ast.Name)
        ):
            holders.add(node.target.id)
    return holders


def _module_dotted(module: Path | None) -> str:
    return ".".join([*_module_package(module), module.stem]) if module is not None else ""


def _returns_transcript_path(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    aliases: dict[str, str],
    resolvers: set[str],
) -> bool:
    """A function whose return value is a resolver result, direct or via a local name."""
    holders = _resolver_holders(function, aliases, resolvers)
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        if _resolves_transcript_path(node.value, aliases, resolvers):
            return True
        if isinstance(node.value, ast.Name) and node.value.id in holders:
            return True
    return False


def _resolver_wrappers(modules: list[tuple[Path, ast.AST, dict[str, str]]]) -> set[str]:
    """Every function across the task runtime that returns a transcript path,
    by bare and module-qualified name, closed under wrapping."""
    wrappers: set[str] = set()
    changed = True
    while changed:
        changed = False
        for path, tree, aliases in modules:
            dotted = _module_dotted(path)
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                qualified = f"{dotted}.{function.name}"
                if qualified in wrappers:
                    continue
                if _returns_transcript_path(function, aliases, wrappers):
                    wrappers.update({function.name, qualified})
                    changed = True
    return wrappers


def test_transcript_resolver_wrappers_are_tracked() -> None:
    source = """
def locate(service, request):
    return _chat_path(service, request)

def locate_again(service, request):
    path: Path = locate(service, request)
    return path

def builder(service, request):
    return locate_again(service, request).read_text()
"""
    tree = ast.parse(source)
    module = SOURCE / "runs" / "prompts.py"
    aliases = _import_aliases(tree, module)
    wrappers = _resolver_wrappers([(module, tree, aliases)])
    assert wrappers == {
        "locate",
        "rcp.runs.prompts.locate",
        "locate_again",
        "rcp.runs.prompts.locate_again",
    }
    builder = [node for node in tree.body if isinstance(node, ast.FunctionDef)][2]
    assert any(_resolves_transcript_path(node, aliases, wrappers) for node in ast.walk(builder))


def test_transcript_read_detection_sees_builtin_open() -> None:
    source = """
def holder(service, request):
    path = _chat_path(service, request)
    json.load(open(path))
    io.open(path).read()
    path.read_text()
"""
    source = "import io\n" + source
    flagged = [line - 1 for line in _flagged_lines(source, _reads_a_file)]
    assert flagged == [4, 5, 6]


def test_transcript_path_detection_resolves_import_aliases() -> None:
    source = """
from rcp.runs.chat import _chat_path as transcript_file
from io import open as io_open

def holder(service, request):
    path = transcript_file(service, request)
    return io_open(path).read()

def other(service, request):
    return service.chat_path(request.session_id)
"""
    assert _flagged_lines(source, _resolves_transcript_path) == [6, 10]
    assert _flagged_lines(source, _reads_a_file) == [7]


def test_transcript_path_detection_resolves_relative_imports() -> None:
    source = """
from .chat import _chat_path as transcript_file
from ..service import ProjectService

def holder(service, request):
    return transcript_file(service, request).read_text()
"""
    module = SOURCE / "runs" / "steering.py"
    assert _flagged_lines(source, _resolves_transcript_path, module) == [6]
    assert _import_aliases(ast.parse(source), module) == {
        "transcript_file": "rcp.runs.chat._chat_path",
        "ProjectService": "rcp.service.ProjectService",
    }


# The only callees a resolved transcript path may be handed to. Everything
# else is an escape: a helper that reads the file would otherwise split the
# resolver and the read across two functions and slip past a per-function scan.
APPROVED_PATH_SINKS = {
    "_append_chat_records",
    "rcp.runs.chat._append_chat_records",
}


def _transcript_path_escapes(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    aliases: dict[str, str],
    resolvers: set[str] = frozenset(),
) -> list[int]:
    """Lines where a resolver result, direct or via a local name, is passed to
    a callee outside `APPROVED_PATH_SINKS`."""
    holders = _resolver_holders(function, aliases, resolvers)
    escapes: list[int] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        name, _ = _call_name(node, aliases)
        if name in APPROVED_PATH_SINKS or _resolves_transcript_path(node, aliases, resolvers):
            continue
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        for argument in arguments:
            carries_path = (isinstance(argument, ast.Name) and argument.id in holders) or (
                _resolves_transcript_path(argument, aliases, resolvers)
            )
            if carries_path:
                escapes.append(node.lineno)
                break
    return escapes


def test_transcript_path_escape_detection_sees_helper_calls() -> None:
    source = """
def builder(service, request):
    path = _chat_path(service, request)
    text = read_input(path)
    _append_chat_records(service, path, [])
    other = load(_chat_path(service, request))
    return text, other

def fine(service, request):
    path = _chat_path(service, request)
    return path.exists()
"""
    tree = ast.parse(source)
    aliases = _import_aliases(tree)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert _transcript_path_escapes(functions[0], aliases) == [4, 6]
    assert _transcript_path_escapes(functions[1], aliases) == []


def test_task_runtime_never_reads_chat_transcripts() -> None:
    """Discuss and Work do not consume prior RCP chat transcripts.

    Canonical chat history exists for display; continuity comes from the
    provider's native session. No prompt builder, launcher, or provider
    profile may reference a transcript reader; a function that resolves the
    transcript path may neither read a file nor hand the path to anything but
    the approved append sink. This is a guardrail against a cooperative edit,
    in the same spirit as the repository's write-scope enforcement, not a
    sound dataflow analysis against a hostile author.
    """

    offenders: list[str] = []
    for package in TASK_RUNTIME:
        for path in _python_files(SOURCE / package):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            aliases = _import_aliases(tree, path)
            for node in ast.walk(tree):
                if _references_transcript_reader(node, aliases):
                    offenders.append(
                        f"{path.relative_to(SOURCE)}:{node.lineno} references a reader"
                    )
    assert offenders == [], "\n".join(offenders)

    # Anywhere in the task runtime, the transcript path is obtained only to
    # append to it. A function that resolves that path must not read any file.
    # Identity-only reads: the append helper dedupes by message UUID, and the
    # steer entry point looks up the stored human message it addresses. Neither
    # feeds transcript text to a provider.
    identity_only = {
        ("runs/chat.py", "_append_chat_records"),
        ("runs/steering.py", "begin_chat_steer"),
    }
    parsed: list[tuple[Path, ast.AST, dict[str, str]]] = []
    for package in TASK_RUNTIME:
        for path in _python_files(SOURCE / package):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parsed.append((path, tree, _import_aliases(tree, path)))
    # A helper that returns a resolver result is itself a resolver, so a
    # caller that reads what the helper returned is a path holder too.
    wrappers = _resolver_wrappers(parsed)
    assert "rcp.runs.chat._chat_path" in wrappers
    readers_in_path_holders: list[str] = []
    for path, tree, aliases in parsed:
        module = path.relative_to(SOURCE).as_posix()
        if True:
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if (module, function.name) in identity_only:
                    continue
                if not any(
                    _resolves_transcript_path(node, aliases, wrappers)
                    for node in ast.walk(function)
                ):
                    continue
                for node in ast.walk(function):
                    if not _reads_a_file(node, aliases):
                        continue
                    entry = f"{module}::{function.name}:{node.lineno}"
                    if entry not in readers_in_path_holders:
                        readers_in_path_holders.append(entry)
                for line in _transcript_path_escapes(function, aliases, wrappers):
                    readers_in_path_holders.append(f"{module}::{function.name}:{line} (escape)")
    assert readers_in_path_holders == [], (
        "a task runtime function reads a file, or hands the transcript path to an "
        "unapproved callee, while holding it: "
        f"{readers_in_path_holders}"
    )
