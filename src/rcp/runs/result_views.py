from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rcp.artifacts import validate_artifact_bytes
from rcp.limits import CHAT_ARTIFACT_MAX_FILE_BYTES
from rcp.transport.run_stage import RemoteRunStage
from rcp.transport.state import StateUnavailable


@dataclass(frozen=True)
class ResultViewSnapshot:
    name: str
    size: int
    sha256: str
    data: bytes


def result_view_slot_path(stage: Path, view_id: str) -> Path:
    """Reconstruct the stable local path without consulting a turn artifact scope."""
    return stage / "views" / _result_view_id(view_id)


def prepare_local_result_view_slot(stage: Path, view_id: str, *, reuse: bool) -> Path:
    """Create or reopen one exact result-view slot and roll stage retention forward."""
    view_id = _result_view_id(view_id)
    root_fd = _open_stage_root(stage)
    fds = [root_fd]
    try:
        try:
            views_fd = os.open("views", _DIRECTORY_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            with suppress(FileExistsError):
                os.mkdir("views", mode=0o700, dir_fd=root_fd)
            try:
                views_fd = os.open("views", _DIRECTORY_FLAGS, dir_fd=root_fd)
            except OSError as exc:
                raise ValueError("result view parent is unsafe") from exc
        except OSError as exc:
            raise ValueError("result view parent is unsafe") from exc
        fds.append(views_fd)
        if reuse:
            try:
                slot_fd = os.open(view_id, _DIRECTORY_FLAGS, dir_fd=views_fd)
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"result view slot is absent: {view_id}") from exc
            except OSError as exc:
                raise ValueError(f"result view slot is unsafe: {view_id}") from exc
        else:
            try:
                os.mkdir(view_id, mode=0o700, dir_fd=views_fd)
            except FileExistsError as exc:
                raise FileExistsError(f"result view slot already exists: {view_id}") from exc
            except OSError as exc:
                raise ValueError(f"result view slot is unsafe: {view_id}") from exc
            slot_fd = os.open(view_id, _DIRECTORY_FLAGS, dir_fd=views_fd)
        fds.append(slot_fd)
        os.utime(root_fd, None)
    finally:
        for descriptor in reversed(fds):
            os.close(descriptor)
    return result_view_slot_path(stage, view_id)


def touch_local_conversation_stage(stage: Path) -> None:
    """Refresh a reused local conversation stage without changing its cwd."""
    root_fd = _open_stage_root(stage)
    try:
        os.utime(root_fd, None)
    finally:
        os.close(root_fd)


def touch_conversation_stage(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
) -> tuple[str, str]:
    """Touch the current exact stage and return its durable host/root binding."""
    _require_one_stage(local_stage, remote_stage)
    if remote_stage is not None:
        remote_stage.touch()
        assert remote_stage.root is not None
        return remote_stage.host, str(remote_stage.root)
    assert local_stage is not None
    touch_local_conversation_stage(local_stage)
    return "", str(local_stage)


def touch_saved_conversation_stages(
    stage_bindings: Iterable[tuple[str, str]],
    *,
    current_binding: tuple[str, str],
) -> None:
    """Touch every distinct saved stage except the already-touched current stage."""
    for stage_host, stage_root in sorted(set(stage_bindings) - {current_binding}):
        if stage_host:
            RemoteRunStage(stage_host).attach(stage_root).touch()
        else:
            touch_local_conversation_stage(Path(stage_root))


def list_local_result_view_files(stage: Path, view_id: str) -> list[tuple[str, int]]:
    """Inspect at most two direct entries, enough to prove the one-file contract."""
    fds, slot_fd = _open_local_result_view_slot(stage, view_id)
    try:
        return sorted((name, info.st_size) for name, info in _bounded_local_slot_files(slot_fd))
    finally:
        _close_descriptors(fds)


def read_local_result_view_bytes(
    stage: Path,
    view_id: str,
    name: str,
    *,
    max_bytes: int,
) -> bytes:
    """Read one bounded direct child without following any component symlink."""
    name = _plain_name(name)
    if max_bytes < 0:
        raise ValueError("result view byte limit must be non-negative")
    fds, slot_fd = _open_local_result_view_slot(stage, view_id)
    try:
        try:
            file_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=slot_fd,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"result view file is absent: {view_id}/{name}") from exc
        except OSError as exc:
            raise ValueError(f"result view file is unsafe: {view_id}/{name}") from exc
        fds.append(file_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"result view file is unsafe: {view_id}/{name}")
        if info.st_size > max_bytes:
            raise ValueError(f"result view file exceeds its byte limit: {view_id}/{name}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(file_fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise ValueError(f"result view file exceeds its byte limit: {view_id}/{name}")
        return data
    finally:
        _close_descriptors(fds)


def prepare_result_view_slot(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    view_id: str,
    *,
    reuse: bool,
) -> Path | PurePosixPath:
    """Prepare the stable slot on exactly one execution host."""
    _require_one_stage(local_stage, remote_stage)
    if remote_stage is not None:
        return remote_stage.prepare_result_view_slot(view_id, reuse=reuse)
    assert local_stage is not None
    return prepare_local_result_view_slot(local_stage, view_id, reuse=reuse)


def discover_result_view(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    view_id: str,
    *,
    expected_name: str | None = None,
    max_bytes: int = CHAT_ARTIFACT_MAX_FILE_BYTES,
) -> ResultViewSnapshot:
    """Validate and snapshot the one self-contained HTML file in a view slot."""
    _require_one_stage(local_stage, remote_stage)
    files = (
        remote_stage.list_result_view_files(view_id)
        if remote_stage is not None
        else list_local_result_view_files(_required_local_stage(local_stage), view_id)
    )
    if len(files) != 1:
        raise ValueError("result view slot must contain exactly one direct regular HTML file")
    name, advertised_size = files[0]
    if expected_name is not None and name != expected_name:
        raise ValueError("result view revision must update its existing exact file")
    if len(name) > 255 or Path(name).suffix.casefold() != ".html":
        raise ValueError("result view must be one descriptively named .html file")
    if advertised_size > max_bytes:
        raise ValueError("result view file exceeds its byte limit")
    data = (
        remote_stage.read_result_view_bytes(view_id, name, max_bytes=max_bytes)
        if remote_stage is not None
        else read_local_result_view_bytes(
            _required_local_stage(local_stage),
            view_id,
            name,
            max_bytes=max_bytes,
        )
    )
    if validate_artifact_bytes(name, data) != "text/html":
        raise ValueError("result view must be HTML")
    return ResultViewSnapshot(
        name=name,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def require_result_view_changed(before: ResultViewSnapshot, after: ResultViewSnapshot) -> None:
    """Accept atomic replacement at the same path, but reject a no-op revision."""
    if after.name != before.name:
        raise ValueError("result view revision must update its existing exact file")
    if after.sha256 == before.sha256:
        raise ValueError("result view revision did not change the existing file")


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _result_view_id(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{24}", value) is None:
        raise ValueError("result view id must be exactly 24 lowercase hexadecimal characters")
    return value


def _plain_name(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise ValueError("result view file name must be a plain base name")
    return name


def _open_stage_root(stage: Path) -> int:
    if not stage.is_absolute():
        raise ValueError("result view stage must be absolute")
    try:
        return os.open(stage, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise StateUnavailable(f"conversation stage is unavailable: {stage}") from exc


def _open_local_result_view_slot(stage: Path, view_id: str) -> tuple[list[int], int]:
    view_id = _result_view_id(view_id)
    root_fd = _open_stage_root(stage)
    fds = [root_fd]
    try:
        try:
            views_fd = os.open("views", _DIRECTORY_FLAGS, dir_fd=root_fd)
            fds.append(views_fd)
            slot_fd = os.open(view_id, _DIRECTORY_FLAGS, dir_fd=views_fd)
            fds.append(slot_fd)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"result view slot is absent: {view_id}") from exc
        except OSError as exc:
            raise ValueError(f"result view slot is unsafe: {view_id}") from exc
        return fds, slot_fd
    except BaseException:
        _close_descriptors(fds)
        raise


def _close_descriptors(descriptors: list[int]) -> None:
    for descriptor in reversed(descriptors):
        os.close(descriptor)


def _bounded_local_slot_files(slot_fd: int) -> list[tuple[str, os.stat_result]]:
    """Inspect no more than the two entries needed to disprove exact-one."""
    files: list[tuple[str, os.stat_result]] = []
    with os.scandir(slot_fd) as entries:
        for entry in entries:
            try:
                info = os.stat(entry.name, dir_fd=slot_fd, follow_symlinks=False)
            except OSError as exc:
                raise ValueError("result view slot contains an unsafe entry") from exc
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("result view slot contains an unsafe entry")
            files.append((entry.name, info))
            if len(files) == 2:
                break
    return files


def _require_one_stage(local: Path | None, remote: RemoteRunStage | None) -> None:
    if (local is None) == (remote is None):
        raise ValueError("exactly one result view stage must be selected")


def _required_local_stage(stage: Path | None) -> Path:
    if stage is None:
        raise ValueError("local result view stage is missing")
    return stage
