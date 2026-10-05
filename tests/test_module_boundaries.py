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
# Modules whose `open(file, mode)` has the builtin signature, mode second.
BUILTIN_SIGNATURE_OPENERS = {"io", "builtins", "codecs"}
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


def _os_open_writes(call: ast.Call) -> bool:
    """`os.open(path, flags)` is a write unless the flags are exactly `os.O_RDONLY`."""
    flags: ast.expr | None = None
    for keyword in call.keywords:
        if keyword.arg == "flags":
            flags = keyword.value
    if flags is None and len(call.args) > 1:
        flags = call.args[1]
    if flags is None:
        return False
    read_only = (
        isinstance(flags, ast.Attribute)
        and flags.attr == "O_RDONLY"
        and isinstance(flags.value, ast.Name)
        and flags.value.id == "os"
    )
    return not read_only


SHUTIL_WRITE_PREFIXES = ("copy", "move", "rmtree")
WRITE_MODE = re.compile(r"[wax+]")


def _python_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py") if path.is_file())


def _open_mode(call: ast.Call, *, builtin: bool) -> tuple[ast.expr | None, bool]:
    """The mode argument of an `open` call and whether it was passed by keyword."""
    for keyword in call.keywords:
        if keyword.arg == "mode":
            return keyword.value, True
    position = 1 if builtin else 0
    if len(call.args) > position:
        return call.args[position], False
    return None, False


def _writes_a_file(call: ast.Call) -> bool:
    function = call.func
    if isinstance(function, ast.Name):
        if function.id != "open":
            return False
        builtin = True
        mode, by_keyword = _open_mode(call, builtin=True)
    elif isinstance(function, ast.Attribute):
        owner = function.value
        if (
            isinstance(owner, ast.Name)
            and owner.id in BUILTIN_SIGNATURE_OPENERS
            and function.attr == "open"
        ):
            builtin = True
            mode, by_keyword = _open_mode(call, builtin=True)
        elif isinstance(owner, ast.Name) and owner.id == "os":
            if function.attr == "open":
                return _os_open_writes(call)
            if function.attr == "fdopen":
                builtin = True
                mode, by_keyword = _open_mode(call, builtin=True)
                return mode is not None and (
                    WRITE_MODE.search(mode.value) is not None
                    if isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                    else True
                )
            return function.attr in OS_WRITE_FUNCTIONS
        elif isinstance(owner, ast.Name) and owner.id == "shutil":
            return function.attr.startswith(SHUTIL_WRITE_PREFIXES)
        elif function.attr in PATH_WRITE_METHODS:
            return True
        elif function.attr in PATH_SINGLE_TARGET_METHODS:
            positional_target = len(call.args) == 1 and not call.keywords
            keyword_target = not call.args and [keyword.arg for keyword in call.keywords] == [
                "target"
            ]
            return positional_target or keyword_target
        elif function.attr != "open":
            return False
        else:
            builtin = False
            mode, by_keyword = _open_mode(call, builtin=False)
    else:
        return False
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return WRITE_MODE.search(mode.value) is not None
    # A computed mode is a write until proven otherwise. The one exemption is a
    # positional argument on a receiver known not to open files.
    if builtin or by_keyword:
        return True
    owner = call.func.value if isinstance(call.func, ast.Attribute) else None
    receiver = (
        owner.id
        if isinstance(owner, ast.Name)
        else owner.attr
        if isinstance(owner, ast.Attribute)
        else None
    )
    return receiver not in NON_FILE_OPEN_RECEIVERS


def test_api_routes_never_write_files_directly() -> None:
    """Invariant 6: routes never write canonical files; `StateWorkspace` and the
    history manager own locking and publication, and the service layer owns
    everything else that touches disk."""

    offenders: list[str] = []
    for path in _python_files(SOURCE / "api"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _writes_a_file(node):
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
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]
    flagged = sorted(node.lineno for node in calls if _writes_a_file(node))
    assert flagged == [3, 4, 9, 10, 15, 16, 17, 18, 19, 20, 21, 23, 24, 25, 26, 28, 30]


# The transcript readers live on the project service for display and backup.
TRANSCRIPT_READERS = (
    "chat_transcript(",
    "chat_transcripts(",
    "_canonical_chat_summaries",
    "_canonical_chat_files",
    "canonical_chat_backup_sources",
)
TASK_RUNTIME = ("runs", "agents", "providers")


def _reads_a_file(node: ast.AST) -> bool:
    """Any way of getting a file's bytes: Path methods, builtin `open`, or a
    qualified builtin-style opener such as `io.open`."""
    if isinstance(node, ast.Attribute):
        if node.attr in {"read_text", "read_bytes", "open"}:
            return True
    if isinstance(node, ast.Call):
        function = node.func
        if isinstance(function, ast.Name) and function.id == "open":
            return True
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "open"
            and isinstance(function.value, ast.Name)
            and function.value.id in BUILTIN_SIGNATURE_OPENERS
        ):
            return True
    return False


def test_transcript_read_detection_sees_builtin_open() -> None:
    source = """
def holder(service, request):
    path = _chat_path(service, request)
    json.load(open(path))
    io.open(path).read()
    path.read_text()
"""
    flagged = sorted(
        node.lineno for node in ast.walk(ast.parse(source)) if _reads_a_file(node)
    )
    assert flagged == [4, 5, 6]


def test_task_runtime_never_reads_chat_transcripts() -> None:
    """Discuss and Work do not consume prior RCP chat transcripts.

    Canonical chat history exists for display; continuity comes from the
    provider's native session. No prompt builder, launcher, or provider
    profile may call a transcript reader, and the one module that appends to
    the transcript reads it back only inside that append, for identity.
    """

    offenders: list[str] = []
    for package in TASK_RUNTIME:
        for path in _python_files(SOURCE / package):
            text = path.read_text(encoding="utf-8")
            for reader in TRANSCRIPT_READERS:
                if reader in text:
                    offenders.append(f"{path.relative_to(SOURCE)} calls {reader.rstrip('(')}")
    assert offenders == [], "\n".join(offenders)

    # Inside runs/chat.py the transcript path is obtained only to append to it.
    # A function that resolves that path must not read any file; the append
    # itself may read back identity, and nothing else holds the path.
    chat = SOURCE / "runs" / "chat.py"
    tree = ast.parse(chat.read_text(encoding="utf-8"))
    readers_in_path_holders: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef) or function.name == "_append_chat_records":
            continue
        resolves_transcript_path = any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "_chat_path")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "chat_path")
            )
            for node in ast.walk(function)
        )
        if not resolves_transcript_path:
            continue
        for node in ast.walk(function):
            if _reads_a_file(node):
                readers_in_path_holders.append(f"{function.name}:{node.lineno}")
    assert readers_in_path_holders == [], (
        "runs/chat.py reads a file in a function that holds the transcript path: "
        f"{readers_in_path_holders}"
    )
