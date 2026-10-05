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
FILE_WRITE = re.compile(
    r"\.(write_text|write_bytes|unlink|rmdir|touch|mkdir)\("
    r"|\bos\.(replace|rename|remove|unlink|mkdir|makedirs|rmdir)\("
    r"|\bshutil\.(copy\w*|move|rmtree)\("
    r"|\.open\(\s*[\"\'][waxWAX]"
    r"|\bopen\([^)]*,\s*[\"\'][waxWAX]"
)


def _python_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py") if path.is_file())


def test_api_routes_never_write_files_directly() -> None:
    """Invariant 6: routes never write canonical files; `StateWorkspace` and the
    history manager own locking and publication, and the service layer owns
    everything else that touches disk."""

    offenders: list[str] = []
    for path in _python_files(SOURCE / "api"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FILE_WRITE.search(line):
                offenders.append(f"{path.relative_to(SOURCE)}:{number}: {line.strip()}")
    assert offenders == [], (
        "API routes write files directly; move the write behind the owning service or "
        "history method:\n" + "\n".join(offenders)
    )


# The transcript readers live on the project service for display and backup.
TRANSCRIPT_READERS = (
    "chat_transcript(",
    "chat_transcripts(",
    "_canonical_chat_summaries",
    "_canonical_chat_files",
    "canonical_chat_backup_sources",
)
TASK_RUNTIME = ("runs", "agents", "providers")


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
            if isinstance(node, ast.Attribute) and node.attr in {"read_text", "read_bytes", "open"}:
                readers_in_path_holders.append(f"{function.name}:{node.lineno}")
    assert readers_in_path_holders == [], (
        "runs/chat.py reads a file in a function that holds the transcript path: "
        f"{readers_in_path_holders}"
    )
