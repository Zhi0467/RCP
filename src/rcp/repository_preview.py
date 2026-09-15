from __future__ import annotations

import html
import json
import os
import re
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rcp.config import Manifest
from rcp.limits import (
    REPOSITORY_PREVIEW_MAX_BYTES,
    REPOSITORY_PREVIEW_TIMEOUT_SECONDS,
    REPOSITORY_PREVIEW_WINDOW_LINES,
)
from rcp.transport.ssh import ssh_arguments
from rcp.transport.state import StateUnavailable

REPOSITORY_PREVIEW_CSP = (
    "sandbox; default-src 'none'; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

# Reads one bounded window. A file within the byte limit is returned whole so an
# ordinary preview is unchanged; a larger one returns only the lines around the
# cited line, because a cited line in a multi-gigabyte log is still evidence.
_REMOTE_READER = """
import json,os,stat,sys
root,relative=sys.argv[1],sys.argv[2]
limit,line,window=int(sys.argv[3]),int(sys.argv[4]),int(sys.argv[5])
parts=relative.split('/')
if (not relative or relative.startswith('/') or
        any(part in ('','.','..') for part in parts)):
    raise SystemExit(45)
if not hasattr(os,'O_DIRECTORY') or not hasattr(os,'O_NOFOLLOW'):
    raise SystemExit(45)
directory_flags=(os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|
                 getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_CLOEXEC',0))
file_flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_CLOEXEC',0)
fds=[]
try:
    fd=os.open(root,directory_flags); fds.append(fd)
    for part in parts[:-1]:
        fd=os.open(part,directory_flags,dir_fd=fd); fds.append(fd)
    file_fd=os.open(parts[-1],file_flags,dir_fd=fd); fds.append(file_fd)
    info=os.fstat(file_fd)
    if not stat.S_ISREG(info.st_mode): raise SystemExit(45)
    if info.st_size<=limit:
        sys.stdout.write(json.dumps(
            {'start_line':1,'complete':True,'total_bytes':info.st_size})+'\\n')
        sys.stdout.flush()
        while True:
            chunk=os.read(file_fd,1024*1024)
            if not chunk: break
            sys.stdout.buffer.write(chunk)
    else:
        first=max(1,line-window) if line else 1
        last=line+window if line else 2*window
        anchor=line or first
        collected=[];start=first;total=0;current=1;pending=b''
        while current<=last:
            index=pending.find(b'\\n')
            if index<0:
                chunk=os.read(file_fd,1024*1024)
                if chunk:
                    pending+=chunk; continue
                if not pending: break
                value,pending=pending,b''
            else:
                value,pending=pending[:index],pending[index+1:]
            if current>=first:
                collected.append(value); total+=len(value)+1
                while total>limit and len(collected)>1 and start<anchor:
                    total-=len(collected.pop(0))+1; start+=1
                if total>limit and start>=anchor: break
            current+=1
        sys.stdout.write(json.dumps(
            {'start_line':start,'complete':False,'total_bytes':info.st_size})+'\\n')
        sys.stdout.flush()
        sys.stdout.buffer.write(b'\\n'.join(collected)[:limit])
except FileNotFoundError:
    raise SystemExit(44)
except (NotADirectoryError,OSError):
    raise SystemExit(45)
finally:
    for item in reversed(fds): os.close(item)
"""


@dataclass(frozen=True)
class RepositorySource:
    repository_alias: str
    relative_path: str
    text: str
    start_line: int = 1
    complete: bool = True
    total_bytes: int = 0


def load_repository_source(
    manifest: Manifest,
    repository_alias: str,
    relative_path: str,
    *,
    line: int | None = None,
    max_bytes: int = REPOSITORY_PREVIEW_MAX_BYTES,
) -> RepositorySource:
    """Read one bounded repository-relative UTF-8 window without following symlinks."""

    parts = _relative_parts(relative_path)
    repository = manifest.repository_map.get(repository_alias)
    if repository is None:
        raise FileNotFoundError("Repository not found")
    machine = manifest.machine_map[repository.machine]
    if machine.host:
        window = _read_remote_file(
            machine.host,
            repository.path,
            relative_path,
            line=line,
            max_bytes=max_bytes,
        )
    else:
        window = _read_local_file(repository.path, parts, line=line, max_bytes=max_bytes)
    try:
        text = window.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Repository file is not UTF-8 text") from exc
    if any(
        character not in {"\t", "\n", "\r"} and not character.isprintable() for character in text
    ):
        raise ValueError("Repository file contains unsupported control characters")
    return RepositorySource(
        repository_alias=repository_alias,
        relative_path=relative_path,
        text=text,
        start_line=window.start_line,
        complete=window.complete,
        total_bytes=window.total_bytes,
    )


def load_repository_source_for_path(
    manifest: Manifest,
    absolute_path: str,
    *,
    line: int | None = None,
    max_bytes: int = REPOSITORY_PREVIEW_MAX_BYTES,
) -> RepositorySource:
    """Resolve an absolute host path to exactly one configured repository and read it."""

    target = _absolute_posix_path(absolute_path, label="Repository file path")
    matches: list[tuple[str, PurePosixPath]] = []
    for repository in manifest.repositories:
        root = _absolute_posix_path(
            repository.path,
            label=f"Repository {repository.alias!r} root",
            allow_trailing_slash=True,
        )
        try:
            relative = target.relative_to(root)
        except ValueError:
            continue
        matches.append((repository.alias, relative))

    if not matches:
        raise ValueError("Path is outside every configured repository")
    if len(matches) > 1:
        aliases = ", ".join(sorted(alias for alias, _relative in matches))
        raise ValueError(f"Path is ambiguous across configured repositories: {aliases}")

    repository_alias, relative = matches[0]
    return load_repository_source(
        manifest,
        repository_alias,
        relative.as_posix(),
        line=line,
        max_bytes=max_bytes,
    )


def repository_source_document(source: RepositorySource, *, line: int | None = None) -> bytes:
    """Render escaped source as a standalone, script-free HTML document."""

    lines = source.text.split("\n")
    last_line = source.start_line + len(lines) - 1
    if line is not None and (line < source.start_line or line > last_line):
        raise ValueError("Requested line is outside the repository file")
    rendered_lines = []
    for number, value in enumerate(lines, start=source.start_line):
        selected = ' class="line selected"' if number == line else ' class="line"'
        rendered_lines.append(
            f'<span id="L{number}"{selected}>{html.escape(value, quote=True)}</span>'
        )
    title = html.escape(
        f"{source.repository_alias}: {source.relative_path}",
        quote=True,
    )
    window_note = (
        ""
        if source.complete
        else (
            f'<span class="window">lines {source.start_line:,}–{last_line:,} '
            f"of a {source.total_bytes:,}-byte file</span>"
        )
    )
    source_html = "\n".join(rendered_lines)
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ margin: 0; font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }}
header {{ position: sticky; top: 0; padding: 10px 16px; background: Canvas; border-bottom: 1px solid GrayText; }}
.window {{ display: block; color: GrayText; }}
pre {{ margin: 0; padding: 16px 0; overflow: auto; }}
.line {{ display: block; min-height: 1.55em; padding: 0 16px 0 4.5em; white-space: pre-wrap; overflow-wrap: anywhere; }}
.line::before {{ content: attr(id); display: inline-block; width: 3.5em; margin-left: -4em; color: GrayText; user-select: none; }}
.selected {{ background: Mark; color: MarkText; }}
</style>
</head>
<body>
<header>{title}{window_note}</header>
<pre aria-label="Repository source"><code>{source_html}</code></pre>
</body>
</html>
"""
    return document.encode("utf-8")


@dataclass(frozen=True)
class _SourceWindow:
    data: bytes
    start_line: int
    complete: bool
    total_bytes: int


def _window_bounds(line: int | None, window: int) -> tuple[int, int]:
    if line is None:
        return 1, 2 * window
    return max(1, line - window), line + window


def _relative_parts(relative_path: str) -> tuple[str, ...]:
    if not relative_path or PurePosixPath(relative_path).is_absolute():
        raise ValueError("Repository file path must be relative")
    parts = tuple(relative_path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Repository file path contains an unsafe segment")
    return parts


def _absolute_posix_path(
    value: str,
    *,
    label: str,
    allow_trailing_slash: bool = False,
) -> PurePosixPath:
    if not value or not value.startswith("/"):
        raise ValueError(f"{label} must be an absolute POSIX path")
    segments = value.split("/")[1:]
    if allow_trailing_slash and segments and segments[-1] == "":
        segments.pop()
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"{label} contains an unsafe segment")
    return PurePosixPath(value)


def _read_local_file(
    root: str,
    parts: tuple[str, ...],
    *,
    line: int | None,
    max_bytes: int,
) -> _SourceWindow:
    repository_root = Path(root)
    if not repository_root.is_absolute():
        raise ValueError("Local repository root must be absolute")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("This platform cannot preview repository files without following links")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    try:
        directory_fd = os.open(repository_root, directory_flags)
        descriptors.append(directory_fd)
        for part in parts[:-1]:
            directory_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            descriptors.append(directory_fd)
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        descriptors.append(file_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Repository path is not a bounded regular file")
        if metadata.st_size <= max_bytes:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return _SourceWindow(
                data=b"".join(chunks)[:max_bytes],
                start_line=1,
                complete=True,
                total_bytes=metadata.st_size,
            )
        first, last = _window_bounds(line, REPOSITORY_PREVIEW_WINDOW_LINES)
        anchor = line or first
        collected: list[bytes] = []
        start = first
        total = 0
        current = 1
        pending = b""
        while current <= last:
            index = pending.find(b"\n")
            if index < 0:
                chunk = os.read(file_fd, 1024 * 1024)
                if chunk:
                    pending += chunk
                    continue
                if not pending:
                    break
                value, pending = pending, b""
            else:
                value, pending = pending[:index], pending[index + 1 :]
            if current >= first:
                collected.append(value)
                total += len(value) + 1
                # The cited line is the evidence, so leading context is what gives
                # way to the byte bound; the header states the range that survived.
                while total > max_bytes and len(collected) > 1 and start < anchor:
                    total -= len(collected.pop(0)) + 1
                    start += 1
                if total > max_bytes and start >= anchor:
                    break
            current += 1
        return _SourceWindow(
            data=b"\n".join(collected)[:max_bytes],
            start_line=start,
            complete=False,
            total_bytes=metadata.st_size,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError("Repository file not found") from exc
    except NotADirectoryError as exc:
        raise ValueError("Repository path is not a regular file") from exc
    except OSError as exc:
        raise ValueError("Repository file cannot be previewed safely") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_remote_file(
    host: str,
    root: str,
    relative_path: str,
    *,
    line: int | None,
    max_bytes: int,
) -> _SourceWindow:
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host):
        raise ValueError("SSH host contains unsupported characters")
    if not PurePosixPath(root).is_absolute():
        raise ValueError("Remote repository root must be absolute")
    command = shlex.join(
        [
            "python3",
            "-c",
            _REMOTE_READER,
            root,
            relative_path,
            str(max_bytes),
            str(line or 0),
            str(REPOSITORY_PREVIEW_WINDOW_LINES),
        ]
    )
    try:
        result = subprocess.run(
            ssh_arguments(host, command),
            capture_output=True,
            timeout=REPOSITORY_PREVIEW_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StateUnavailable("Repository SSH host is unavailable") from exc
    if result.returncode == 44:
        raise FileNotFoundError("Repository file not found")
    if result.returncode == 45:
        raise ValueError("Remote repository file cannot be previewed safely")
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise StateUnavailable(detail or "Repository SSH host is unavailable")
    header, separator, data = result.stdout.partition(b"\n")
    if not separator:
        raise ValueError("Remote repository file cannot be previewed safely")
    try:
        metadata = json.loads(header)
        window = _SourceWindow(
            data=data,
            start_line=int(metadata["start_line"]),
            complete=bool(metadata["complete"]),
            total_bytes=int(metadata["total_bytes"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Remote repository file cannot be previewed safely") from exc
    if len(window.data) > max_bytes:
        raise ValueError("Repository file exceeds the preview size limit")
    return window
