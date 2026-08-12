from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
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


_ROLLBACK_SNAPSHOT_DIRECTORY = ".rcp-result-view-snapshots"
_ROLLBACK_SNAPSHOT_MAGIC = b"RCP-RV-SNAPSHOT\x01"
_ROLLBACK_SNAPSHOT_HEADER_MAX_BYTES = 1024


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


def restore_local_result_view_bytes(
    stage: Path,
    view_id: str,
    name: str,
    data: bytes,
    *,
    max_bytes: int,
) -> bool:
    """Atomically restore the exact prior one-file slot without traversing agent output.

    A displaced or interrupted slot remains as a hidden sibling beneath this
    conversation stage. The existing stage-retention sweep removes the whole
    stage later; rollback never recursively deletes an agent-controlled tree.
    """
    name = _plain_name(name)
    if max_bytes < 0 or len(data) > max_bytes:
        raise ValueError("result view restore exceeds its byte limit")
    view_id = _result_view_id(view_id)
    root_fd = _open_stage_root(stage)
    fds = [root_fd]
    views_fd: int | None = None
    temporary_slot: str | None = None
    quarantine: str | None = None
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
        if _local_slot_matches(views_fd, view_id, name, data, max_bytes=max_bytes):
            return False

        temporary_slot = f".rcp-result-view-slot-{uuid.uuid4().hex}"
        os.mkdir(temporary_slot, mode=0o700, dir_fd=views_fd)
        temporary_fd = os.open(temporary_slot, _DIRECTORY_FLAGS, dir_fd=views_fd)
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=temporary_fd,
            )
            try:
                _write_all(descriptor, data)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)

        try:
            os.stat(view_id, dir_fd=views_fd, follow_symlinks=False)
        except FileNotFoundError:
            previous_exists = False
        else:
            previous_exists = True
        if previous_exists:
            quarantine = f".rcp-result-view-old-{uuid.uuid4().hex}"
            os.rename(view_id, quarantine, src_dir_fd=views_fd, dst_dir_fd=views_fd)
        try:
            os.rename(temporary_slot, view_id, src_dir_fd=views_fd, dst_dir_fd=views_fd)
            temporary_slot = None
        except BaseException:
            if quarantine is not None:
                os.rename(quarantine, view_id, src_dir_fd=views_fd, dst_dir_fd=views_fd)
                quarantine = None
            raise
        os.fsync(views_fd)
        return True
    finally:
        # Hidden temporary and quarantine slots are intentionally retained under
        # the conversation stage for its whole-stage retention sweep. Even error
        # cleanup must not traverse output that an agent could have replaced.
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


def restore_result_view(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    view_id: str,
    snapshot: ResultViewSnapshot,
    *,
    max_bytes: int = CHAT_ARTIFACT_MAX_FILE_BYTES,
) -> bool:
    """Restore a rejected revision while avoiding a write when bytes are unchanged."""
    _require_one_stage(local_stage, remote_stage)
    if remote_stage is not None:
        return remote_stage.restore_result_view_bytes(
            view_id,
            snapshot.name,
            snapshot.data,
            max_bytes=max_bytes,
        )
    return restore_local_result_view_bytes(
        _required_local_stage(local_stage),
        view_id,
        snapshot.name,
        snapshot.data,
        max_bytes=max_bytes,
    )


def persist_result_view_rollback_snapshot(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    view_id: str,
    snapshot: ResultViewSnapshot,
    *,
    max_bytes: int = CHAT_ARTIFACT_MAX_FILE_BYTES,
) -> None:
    """Atomically persist one verified pre-launch snapshot in the conversation stage."""
    _require_one_stage(local_stage, remote_stage)
    payload = _encode_rollback_snapshot(view_id, snapshot, max_bytes=max_bytes)
    limit = _rollback_snapshot_payload_limit(max_bytes)
    if remote_stage is not None:
        remote_stage.write_result_view_rollback_snapshot(
            view_id,
            payload,
            max_bytes=limit,
        )
        return
    _write_local_result_view_rollback_snapshot(
        _required_local_stage(local_stage),
        view_id,
        payload,
        max_bytes=limit,
    )


def read_result_view_rollback_snapshot(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    view_id: str,
    *,
    expected_name: str,
    expected_size: int,
    expected_sha256: str,
    max_bytes: int = CHAT_ARTIFACT_MAX_FILE_BYTES,
) -> ResultViewSnapshot:
    """Read and verify one same-stage snapshot against trusted receipt metadata."""
    _require_one_stage(local_stage, remote_stage)
    limit = _rollback_snapshot_payload_limit(max_bytes)
    payload = (
        remote_stage.read_result_view_rollback_snapshot(view_id, max_bytes=limit)
        if remote_stage is not None
        else _read_local_result_view_rollback_snapshot(
            _required_local_stage(local_stage),
            view_id,
            max_bytes=limit,
        )
    )
    return _decode_rollback_snapshot(
        payload,
        expected_view_id=view_id,
        expected_name=expected_name,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        max_bytes=max_bytes,
    )


def clear_result_view_rollback_snapshot(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    view_id: str,
    snapshot: ResultViewSnapshot,
    *,
    max_bytes: int = CHAT_ARTIFACT_MAX_FILE_BYTES,
) -> bool:
    """Clear only the exact verified snapshot, never traversing a poisoned entry."""
    _require_one_stage(local_stage, remote_stage)
    payload = _encode_rollback_snapshot(view_id, snapshot, max_bytes=max_bytes)
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    limit = _rollback_snapshot_payload_limit(max_bytes)
    if remote_stage is not None:
        return remote_stage.clear_result_view_rollback_snapshot(
            view_id,
            expected_size=len(payload),
            expected_sha256=expected_sha256,
            max_bytes=limit,
        )
    return _clear_local_result_view_rollback_snapshot(
        _required_local_stage(local_stage),
        view_id,
        expected_size=len(payload),
        expected_sha256=expected_sha256,
        max_bytes=limit,
    )


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


def _read_bounded_descriptor(descriptor: int, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise ValueError("result view file exceeds its byte limit")
    return data


def _local_slot_matches(
    views_fd: int,
    view_id: str,
    name: str,
    data: bytes,
    *,
    max_bytes: int,
) -> bool:
    try:
        slot_fd = os.open(view_id, _DIRECTORY_FLAGS, dir_fd=views_fd)
    except OSError:
        return False
    try:
        try:
            files = _bounded_local_slot_files(slot_fd)
        except (OSError, ValueError):
            return False
        if len(files) != 1 or files[0][0] != name:
            return False
        try:
            info = files[0][1]
            if not stat.S_ISREG(info.st_mode) or info.st_size != len(data):
                return False
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=slot_fd,
            )
        except OSError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return False
            return _read_bounded_descriptor(descriptor, max_bytes=max_bytes) == data
        finally:
            os.close(descriptor)
    finally:
        os.close(slot_fd)


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


def _open_local_rollback_snapshot_parent(
    stage: Path,
    *,
    create: bool,
) -> tuple[list[int], int]:
    root_fd = _open_stage_root(stage)
    fds = [root_fd]
    try:
        try:
            views_fd = os.open("views", _DIRECTORY_FLAGS, dir_fd=root_fd)
        except OSError as exc:
            raise ValueError("result view snapshot parent is unsafe") from exc
        fds.append(views_fd)
        try:
            snapshots_fd = os.open(
                _ROLLBACK_SNAPSHOT_DIRECTORY,
                _DIRECTORY_FLAGS,
                dir_fd=views_fd,
            )
        except FileNotFoundError:
            if not create:
                raise FileNotFoundError("result view rollback snapshot is absent") from None
            with suppress(FileExistsError):
                os.mkdir(
                    _ROLLBACK_SNAPSHOT_DIRECTORY,
                    mode=0o700,
                    dir_fd=views_fd,
                )
            try:
                snapshots_fd = os.open(
                    _ROLLBACK_SNAPSHOT_DIRECTORY,
                    _DIRECTORY_FLAGS,
                    dir_fd=views_fd,
                )
            except OSError as exc:
                raise ValueError("result view snapshot parent is unsafe") from exc
            os.fsync(views_fd)
        except OSError as exc:
            raise ValueError("result view snapshot parent is unsafe") from exc
        fds.append(snapshots_fd)
        return fds, snapshots_fd
    except BaseException:
        _close_descriptors(fds)
        raise


def _write_local_result_view_rollback_snapshot(
    stage: Path,
    view_id: str,
    payload: bytes,
    *,
    max_bytes: int,
) -> None:
    view_id = _result_view_id(view_id)
    if max_bytes < 0 or len(payload) > max_bytes:
        raise ValueError("result view rollback snapshot exceeds its byte limit")
    fds, snapshots_fd = _open_local_rollback_snapshot_parent(stage, create=True)
    temporary = f".rcp-result-view-snapshot-{uuid.uuid4().hex}"
    try:
        try:
            existing = os.stat(view_id, dir_fd=snapshots_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(existing.st_mode):
                raise ValueError("result view rollback snapshot target is unsafe")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=snapshots_fd,
        )
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, view_id, src_dir_fd=snapshots_fd, dst_dir_fd=snapshots_fd)
        temporary = ""
        os.fsync(snapshots_fd)
    finally:
        # An interrupted temporary remains hidden inside the conversation stage.
        # Its whole-stage retention sweep owns cleanup; do not inspect agent-writable output.
        _close_descriptors(fds)


def _read_local_result_view_rollback_snapshot(
    stage: Path,
    view_id: str,
    *,
    max_bytes: int,
) -> bytes:
    view_id = _result_view_id(view_id)
    if max_bytes < 0:
        raise ValueError("result view rollback snapshot byte limit must be non-negative")
    fds, snapshots_fd = _open_local_rollback_snapshot_parent(stage, create=False)
    try:
        try:
            descriptor = os.open(
                view_id,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=snapshots_fd,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError("result view rollback snapshot is absent") from exc
        except OSError as exc:
            raise ValueError("result view rollback snapshot is unsafe") from exc
        fds.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("result view rollback snapshot is unsafe")
        if info.st_size > max_bytes:
            raise ValueError("result view rollback snapshot exceeds its byte limit")
        return _read_bounded_descriptor(descriptor, max_bytes=max_bytes)
    finally:
        _close_descriptors(fds)


def _clear_local_result_view_rollback_snapshot(
    stage: Path,
    view_id: str,
    *,
    expected_size: int,
    expected_sha256: str,
    max_bytes: int,
) -> bool:
    view_id = _result_view_id(view_id)
    fds, snapshots_fd = _open_local_rollback_snapshot_parent(stage, create=False)
    try:
        try:
            descriptor = os.open(
                view_id,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=snapshots_fd,
            )
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ValueError("result view rollback snapshot is unsafe") from exc
        fds.append(descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size != expected_size:
            raise ValueError("result view rollback snapshot changed before it could be cleared")
        data = _read_bounded_descriptor(descriptor, max_bytes=max_bytes)
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ValueError("result view rollback snapshot changed before it could be cleared")
        current = os.stat(view_id, dir_fd=snapshots_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise ValueError("result view rollback snapshot changed before it could be cleared")
        os.unlink(view_id, dir_fd=snapshots_fd)
        os.fsync(snapshots_fd)
        return True
    finally:
        _close_descriptors(fds)


def _encode_rollback_snapshot(
    view_id: str,
    snapshot: ResultViewSnapshot,
    *,
    max_bytes: int,
) -> bytes:
    view_id = _result_view_id(view_id)
    name = _plain_name(snapshot.name)
    actual_size = len(snapshot.data)
    actual_sha256 = hashlib.sha256(snapshot.data).hexdigest()
    if (
        max_bytes < 0
        or actual_size > max_bytes
        or snapshot.size != actual_size
        or snapshot.sha256 != actual_sha256
    ):
        raise ValueError("result view rollback snapshot metadata does not match its bytes")
    header = json.dumps(
        {
            "content_sha256": actual_sha256,
            "name": name,
            "size": actual_size,
            "version": 1,
            "view_id": view_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(header) > _ROLLBACK_SNAPSHOT_HEADER_MAX_BYTES:
        raise ValueError("result view rollback snapshot header exceeds its byte limit")
    return _ROLLBACK_SNAPSHOT_MAGIC + len(header).to_bytes(4, "big") + header + snapshot.data


def _decode_rollback_snapshot(
    payload: bytes,
    *,
    expected_view_id: str,
    expected_name: str,
    expected_size: int,
    expected_sha256: str,
    max_bytes: int,
) -> ResultViewSnapshot:
    expected_view_id = _result_view_id(expected_view_id)
    expected_name = _plain_name(expected_name)
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("result view rollback snapshot digest is invalid")
    prefix_size = len(_ROLLBACK_SNAPSHOT_MAGIC) + 4
    if not payload.startswith(_ROLLBACK_SNAPSHOT_MAGIC) or len(payload) < prefix_size:
        raise ValueError("result view rollback snapshot envelope is invalid")
    header_size = int.from_bytes(
        payload[len(_ROLLBACK_SNAPSHOT_MAGIC) : prefix_size],
        "big",
    )
    if not 0 < header_size <= _ROLLBACK_SNAPSHOT_HEADER_MAX_BYTES:
        raise ValueError("result view rollback snapshot envelope is invalid")
    body_start = prefix_size + header_size
    if body_start > len(payload):
        raise ValueError("result view rollback snapshot envelope is truncated")
    try:
        header = json.loads(payload[prefix_size:body_start].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("result view rollback snapshot header is invalid") from exc
    expected_header = {
        "content_sha256": expected_sha256,
        "name": expected_name,
        "size": expected_size,
        "version": 1,
        "view_id": expected_view_id,
    }
    if header != expected_header:
        raise ValueError("result view rollback snapshot binding does not match its receipt")
    data = payload[body_start:]
    if expected_size < 0 or expected_size > max_bytes or len(data) != expected_size:
        raise ValueError("result view rollback snapshot size does not match its receipt")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("result view rollback snapshot digest does not match its receipt")
    return ResultViewSnapshot(
        name=expected_name,
        size=expected_size,
        sha256=expected_sha256,
        data=data,
    )


def _rollback_snapshot_payload_limit(max_bytes: int) -> int:
    if max_bytes < 0:
        raise ValueError("result view rollback snapshot byte limit must be non-negative")
    return len(_ROLLBACK_SNAPSHOT_MAGIC) + 4 + _ROLLBACK_SNAPSHOT_HEADER_MAX_BYTES + max_bytes


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]


def _require_one_stage(local: Path | None, remote: RemoteRunStage | None) -> None:
    if (local is None) == (remote is None):
        raise ValueError("exactly one result view stage must be selected")


def _required_local_stage(stage: Path | None) -> Path:
    if stage is None:
        raise ValueError("local result view stage is missing")
    return stage
