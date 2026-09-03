"""Remote canonical lock holder and task-artifact replacement entry point.

RCP ships this module with the shared artifact-replacement helper prepended and
runs the result with ``python -c``. Keeping the executable source in real modules
lets ruff, the formatter, and ``tests/test_remote_scripts.py`` see it.

Lock protocol. ``argv[1]`` is the lock path. The holder prints one status word on
stdout — ``legacy-directory``, ``unsafe-entry``, or ``error`` and exits, or
``contended`` followed by ``acquired`` once the wait finishes. It then reads one
JSON command per line from stdin and prints one JSON response per line, holding
the lock for as long as stdin stays open. ``replace-run-artifact`` is instead a
one-shot command for task scratch, which has its own per-artifact mutation owner.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from pathlib import Path

if "replace_regular_file_in_open_directory" not in globals():
    from rcp.artifact_replace import (
        ArtifactReplacementConflict,
        replace_regular_file_in_open_directory,
    )


def relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe relative path: {value}")
    return path


def validate_branch_path(path: Path) -> None:
    if not path.parts or path.parts[0] != "branches":
        return
    if len(path.parts) not in {3, 4}:
        raise ValueError(f"unsafe branch path: {path}")
    try:
        branch_id = uuid.UUID(path.parts[1])
    except ValueError as exc:
        raise ValueError(f"unsafe branch path: {path}") from exc
    if str(branch_id) != path.parts[1] or branch_id.version != 4:
        raise ValueError(f"unsafe branch path: {path}")
    if len(path.parts) == 3 and path.parts[2] not in {
        "branch.json",
        "graph.json",
        "glossary.json",
        "proposals.json",
        "coverage.json",
        "research.md",
    }:
        raise ValueError(f"unsafe branch path: {path}")
    if len(path.parts) == 4:
        if path.parts[2] == "patches" and re.fullmatch(r"[0-9]{6}[.]json", path.parts[3]):
            return
        if path.parts[2] == "merges" and re.fullmatch(r"[a-f0-9]{64}[.]json", path.parts[3]):
            return
        raise ValueError(f"unsafe branch path: {path}")


def require_safe_branch_parents(root: Path, path: Path) -> None:
    validate_branch_path(path)
    if not path.parts or path.parts[0] != "branches":
        return
    for parent in (root / "branches", root / "branches" / path.parts[1]):
        try:
            mode = os.lstat(parent).st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode):
            raise ValueError(f"unsafe branch parent: {parent}")
    if len(path.parts) == 4:
        leaf_parent = root / "branches" / path.parts[1] / path.parts[2]
        try:
            mode = os.lstat(leaf_parent).st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(mode):
            raise ValueError(f"unsafe branch parent: {leaf_parent}")


def apply_staged(command: dict, lock_path: str) -> dict:
    root = Path(command["root"])
    stage = Path(command["stage"])
    if (
        Path(lock_path).name != ".refresh.lock"
        or root != Path(lock_path).parent
        or not root.is_absolute()
        or stage.parent != root / ".publish"
    ):
        raise ValueError("invalid canonical root or staging directory")
    paths = [relative_path(value) for value in command["paths"]]
    commit_value = command.get("commit")
    commit = relative_path(commit_value) if commit_value is not None else None
    for path in paths:
        require_safe_branch_parents(root, path)
    if commit is not None:
        require_safe_branch_parents(root, commit)
    commit_is_directory = bool(command.get("commit_is_directory"))
    ordinary = [
        path
        for path in paths
        if commit is None
        or (path != commit and not (commit_is_directory and commit in path.parents))
    ]
    commit_target = root / commit if commit is not None else None
    try:
        if commit is not None:
            commit_source = stage / commit
            commit_target.parent.mkdir(parents=True, exist_ok=True)
            if commit_target.exists():
                if commit_source.exists():
                    if commit_is_directory:
                        shutil.rmtree(commit_source)
                    else:
                        if not stat.S_ISREG(os.lstat(commit_target).st_mode) or not stat.S_ISREG(
                            os.lstat(commit_source).st_mode
                        ):
                            raise FileExistsError(
                                f"history commit path has an incompatible type: {commit_target}"
                            )
                        if commit_target.read_bytes() != commit_source.read_bytes():
                            raise FileExistsError(
                                f"history commit content disagrees with existing file: "
                                f"{commit_target}"
                            )
                        commit_source.unlink()
            elif commit_source.exists():
                os.replace(commit_source, commit_target)
            else:
                raise FileNotFoundError(f"missing staged history commit: {commit_source}")
        for path in ordinary:
            source = stage / path
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_file():
                os.replace(source, target)
        shutil.rmtree(stage, ignore_errors=True)
        return {"ok": True, "commit_status": "present" if commit is not None else None}
    except Exception as exc:
        if commit_target is None:
            commit_status = None
        elif commit_is_directory:
            commit_status = "present" if commit_target.is_dir() else "absent"
        else:
            commit_status = "present" if commit_target.is_file() else "absent"
        return {"ok": False, "commit_status": commit_status, "error": str(exc)[:1000]}


def _fd_digest(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest(), size


def _open_owned_recovery_directory(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    return os.open(name, flags, dir_fd=parent_fd)


def restore_exact(command: dict, lock_path: str) -> dict:
    root = Path(command["root"])
    stage = Path(command["stage"])
    path = relative_path(command["path"])
    expected_sha256 = command["sha256"]
    expected_size = command["size"]
    external = bool(command.get("external"))
    if (
        Path(lock_path).name != ".refresh.lock"
        or root != Path(lock_path).parent
        or not root.is_absolute()
        or root.name != ".research"
        or stage.parent != root / ".publish"
        or re.fullmatch(r"restore-[0-9]+-[0-9]+", stage.name) is None
        or not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or not isinstance(expected_size, int)
        or expected_size < 0
    ):
        raise ValueError("invalid exact-restore request")
    if external:
        if len(path.parts) != 2 or path.parts[0] not in {"artifacts", "views"}:
            raise ValueError("invalid external exact-restore path")
        if path.parts[0] == "artifacts":
            pattern = r"[a-z0-9](?:[a-z0-9-]{0,220})[.](?:html?|png|jpe?g|gif|webp|svg)"
        else:
            pattern = r"[a-z0-9](?:[a-z0-9-]{0,238})[.]html"
        if re.fullmatch(pattern, path.name) is None or expected_size > 16 * 1024 * 1024:
            raise ValueError("invalid external exact-restore file")
    elif ".publish" in path.parts:
        raise ValueError("invalid canonical exact-restore path")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("safe exact-restore file operations are unavailable")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    stage_fd = os.open(stage, directory_flags)
    try:
        if os.listdir(stage_fd) != ["content.bin"]:
            raise ValueError("exact-restore stage does not contain exactly content.bin")
        source_fd = os.open("content.bin", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=stage_fd)
        try:
            source_info = os.fstat(source_fd)
            digest, size = _fd_digest(source_fd)
            if (
                not stat.S_ISREG(source_info.st_mode)
                or size != expected_size
                or digest != expected_sha256
            ):
                raise ValueError("staged exact-restore bytes differ from their proof")
            base = root.parent if external else root
            base_fd = os.open(base, directory_flags)
            parent_fd = base_fd
            opened: list[int] = []
            try:
                for part in path.parent.parts:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, 0o755, dir_fd=parent_fd)
                    next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                    opened.append(next_fd)
                    parent_fd = next_fd
                try:
                    target_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
                except FileNotFoundError:
                    target_fd = -1
                if target_fd >= 0:
                    try:
                        target_info = os.fstat(target_fd)
                        target_digest, target_size = _fd_digest(target_fd)
                        if (
                            not stat.S_ISREG(target_info.st_mode)
                            or target_size != expected_size
                            or target_digest != expected_sha256
                        ):
                            raise FileExistsError(
                                f"restored project file conflicts with existing bytes: {path}"
                            )
                        return {"ok": True}
                    finally:
                        os.close(target_fd)
                temporary = f".{path.name}.restore-{uuid.uuid4().hex}"
                target_fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o644,
                    dir_fd=parent_fd,
                )
                try:
                    while True:
                        chunk = os.read(source_fd, 1024 * 1024)
                        if not chunk:
                            break
                        remaining = memoryview(chunk)
                        while remaining:
                            written = os.write(target_fd, remaining)
                            if written <= 0:
                                raise OSError("short exact-restore write")
                            remaining = remaining[written:]
                    os.fsync(target_fd)
                finally:
                    os.close(target_fd)
                try:
                    os.link(
                        temporary,
                        path.name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    target_fd = os.open(
                        path.name,
                        os.O_RDONLY | os.O_NOFOLLOW,
                        dir_fd=parent_fd,
                    )
                    try:
                        target_digest, target_size = _fd_digest(target_fd)
                        if target_size != expected_size or target_digest != expected_sha256:
                            raise FileExistsError(
                                f"restored project file raced with conflicting bytes: {path}"
                            ) from None
                    finally:
                        os.close(target_fd)
                finally:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=parent_fd)
                os.fsync(parent_fd)
                return {"ok": True}
            finally:
                for descriptor in reversed(opened):
                    os.close(descriptor)
                os.close(base_fd)
        finally:
            os.close(source_fd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink("content.bin", dir_fd=stage_fd)
        os.close(stage_fd)
        with contextlib.suppress(OSError):
            os.rmdir(stage)


def kept_view_candidate(base_name: str, index: int) -> str:
    if index == 1:
        return base_name
    return f"{base_name[:-5]}-{index}.html"


def kept_artifact_candidate(base_name: str, index: int) -> str:
    if index == 1:
        return base_name
    path = Path(base_name)
    return f"{path.stem}-{index}{path.suffix}"


def keep_staged_view(command: dict, lock_path: str) -> dict:
    root = Path(command["root"])
    stage = Path(command["stage"])
    base_name = command["base_name"]
    artifact = command.get("op") == "keep-artifact"
    directory_name = "artifacts" if artifact else "views"
    content_name = "content.bin" if artifact else "content.html"
    stage_pattern = r"artifact-[0-9]+-[0-9]+" if artifact else r"view-[0-9]+-[0-9]+"
    name_pattern = (
        r"[a-z0-9](?:[a-z0-9-]{0,220})[.](?:html?|png|jpe?g|gif|webp|svg)"
        if artifact
        else r"[a-z0-9](?:[a-z0-9-]{0,238})[.]html"
    )
    if (
        Path(lock_path).name != ".refresh.lock"
        or root != Path(lock_path).parent
        or not root.is_absolute()
        or root.name != ".research"
        or stage.parent != root / ".publish"
        or not re.fullmatch(stage_pattern, stage.name)
        or not isinstance(base_name, str)
        or not re.fullmatch(name_pattern, base_name)
    ):
        raise ValueError("invalid result-view root, stage, or base name")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("safe result-view file operations are unavailable")
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise ValueError("canonical root is not a regular directory")
    if not stat.S_ISDIR(os.lstat(stage.parent).st_mode):
        raise ValueError("result-view staging parent is not a regular directory")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    stage_fd = os.open(stage, directory_flags)
    try:
        if os.listdir(stage_fd) != [content_name]:
            raise ValueError("artifact stage does not contain exactly one content file")
        source_fd = os.open(content_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=stage_fd)
        try:
            source_info = os.fstat(source_fd)
            if not stat.S_ISREG(source_info.st_mode):
                raise ValueError("staged result view is not a regular file")
            if source_info.st_size > 16 * 1024 * 1024:
                raise ValueError("staged result view exceeds the per-file limit")

            repository_fd = os.open(root.parent, directory_flags)
            try:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(directory_name, 0o755, dir_fd=repository_fd)
                try:
                    views_fd = os.open(directory_name, directory_flags, dir_fd=repository_fd)
                except OSError as exc:
                    raise ValueError(
                        f"repository {directory_name} path is not a regular directory"
                    ) from exc
                try:
                    for index in range(1, 10000):
                        candidate = (
                            kept_artifact_candidate(base_name, index)
                            if artifact
                            else kept_view_candidate(base_name, index)
                        )
                        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                        try:
                            target_fd = os.open(candidate, flags, 0o644, dir_fd=views_fd)
                        except FileExistsError:
                            continue
                        try:
                            bytes_left = 16 * 1024 * 1024
                            while True:
                                chunk = os.read(source_fd, min(1024 * 1024, bytes_left + 1))
                                if not chunk:
                                    break
                                if len(chunk) > bytes_left:
                                    raise ValueError(
                                        "staged result view exceeds the per-file limit"
                                    )
                                bytes_left -= len(chunk)
                                remaining = memoryview(chunk)
                                while remaining:
                                    written = os.write(target_fd, remaining)
                                    if written <= 0:
                                        raise OSError("short result-view write")
                                    remaining = remaining[written:]
                            os.fsync(target_fd)
                        except BaseException:
                            os.close(target_fd)
                            target_fd = -1
                            try:
                                os.unlink(candidate, dir_fd=views_fd)
                                os.fsync(views_fd)
                            except OSError:
                                pass
                            raise
                        finally:
                            if target_fd >= 0:
                                os.close(target_fd)
                        os.fsync(views_fd)
                        os.fsync(repository_fd)
                        return {"ok": True, "name": candidate}
                    raise FileExistsError("too many repository result-view name collisions")
                finally:
                    os.close(views_fd)
            finally:
                os.close(repository_fd)
        finally:
            os.close(source_fd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(content_name, dir_fd=stage_fd)
        os.close(stage_fd)
        with contextlib.suppress(OSError):
            os.rmdir(stage)


def replace_staged_artifact(command: dict, lock_path: str) -> dict:
    root = Path(command["root"])
    stage = Path(command["stage"])
    name = command["name"]
    expected_sha256 = command.get("expected_sha256")
    if (
        Path(lock_path).name != ".refresh.lock"
        or root != Path(lock_path).parent
        or not root.is_absolute()
        or root.name != ".research"
        or stage.parent != root / ".publish"
        or not re.fullmatch(r"artifact-[0-9]+-[0-9]+", stage.name)
        or not isinstance(name, str)
        or (
            expected_sha256 is not None
            and (
                not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            )
        )
        or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,220})[.](?:html?|png|jpe?g|gif|webp|svg)", name
        )
    ):
        raise ValueError("invalid artifact replacement root, stage, or name")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    stage_fd = os.open(stage, directory_flags)
    try:
        if os.listdir(stage_fd) != ["content.bin"]:
            raise ValueError("artifact stage does not contain exactly content.bin")
        source_fd = os.open("content.bin", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=stage_fd)
        try:
            source_info = os.fstat(source_fd)
            if not stat.S_ISREG(source_info.st_mode) or source_info.st_size > 16 * 1024 * 1024:
                raise ValueError("staged artifact is invalid or too large")
            chunks: list[bytes] = []
            remaining = 16 * 1024 * 1024 + 1
            while remaining > 0:
                chunk = os.read(source_fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) != source_info.st_size:
                raise ValueError("staged artifact changed while being read")
            repository_fd = os.open(root.parent, directory_flags)
            try:
                artifacts_fd = os.open("artifacts", directory_flags, dir_fd=repository_fd)
                try:
                    research_fd = os.open(root, directory_flags)
                    try:
                        publish_fd = os.open(".publish", directory_flags, dir_fd=research_fd)
                        try:
                            recovery_fd = _open_owned_recovery_directory(
                                publish_fd, "artifact-replacements"
                            )
                            try:
                                try:
                                    replaced = replace_regular_file_in_open_directory(
                                        artifacts_fd,
                                        recovery_fd,
                                        name,
                                        data,
                                        expected_sha256=expected_sha256,
                                        mode=0o644,
                                    )
                                except ArtifactReplacementConflict as exc:
                                    return {"ok": False, "conflict": True, "error": str(exc)}
                            finally:
                                os.close(recovery_fd)
                        finally:
                            os.close(publish_fd)
                    finally:
                        os.close(research_fd)
                    if not replaced:
                        return {
                            "ok": False,
                            "conflict": True,
                            "error": "kept artifact digest changed",
                        }
                    os.fsync(repository_fd)
                    return {"ok": True, "name": name}
                finally:
                    os.close(artifacts_fd)
            finally:
                os.close(repository_fd)
        finally:
            os.close(source_fd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink("content.bin", dir_fd=stage_fd)
        os.close(stage_fd)
        with contextlib.suppress(OSError):
            os.rmdir(stage)


def replace_run_artifact() -> None:
    if len(sys.argv) != 6:
        raise ValueError("invalid run artifact replacement arguments")
    root = Path(sys.argv[2])
    scope, name, expected_sha256 = sys.argv[3:6]
    expected = expected_sha256 or None
    if (
        not root.is_absolute()
        or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9_-])?", scope) is None
        or Path(name).name != name
        or name in {"", ".", ".."}
        or (expected is not None and re.fullmatch(r"[0-9a-f]{64}", expected) is None)
    ):
        raise ValueError("invalid run artifact replacement path or digest")
    data = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
    if not 1 <= len(data) <= 16 * 1024 * 1024:
        raise ValueError("run artifact replacement bytes are invalid or too large")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        descriptor = os.open(root, flags)
        descriptors.append(descriptor)
        inputs_fd = os.open("inputs", flags, dir_fd=descriptor)
        descriptors.append(inputs_fd)
        recovery_root_fd = _open_owned_recovery_directory(inputs_fd, ".artifact-replacements")
        descriptors.append(recovery_root_fd)
        recovery_fd = _open_owned_recovery_directory(recovery_root_fd, scope)
        descriptors.append(recovery_fd)
        for part in ("workspace", "turns", scope, "artifacts"):
            descriptor = os.open(part, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        if not replace_regular_file_in_open_directory(
            descriptor,
            recovery_fd,
            name,
            data,
            expected_sha256=expected,
            mode=0o600,
        ):
            raise SystemExit(46)
    except SystemExit:
        raise
    except ArtifactReplacementConflict as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(47) from exc
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(44) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def main() -> None:
    if sys.argv[1:2] == ["replace-run-artifact"]:
        try:
            replace_run_artifact()
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(44) from exc
        return
    lock_path = sys.argv[1]
    try:
        mode = os.lstat(lock_path).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        if stat.S_ISDIR(mode):
            # A crashed mkdir-era run leaves its lock directory behind empty, and
            # that artifact is RCP's to clear. rmdir reclaims exactly that case:
            # anything with contents is somebody's state and still refuses.
            try:
                os.rmdir(lock_path)
            except OSError:
                print("legacy-directory", flush=True)
                raise SystemExit(0) from None
        elif not stat.S_ISREG(mode):
            print("unsafe-entry", flush=True)
            raise SystemExit(0)
    try:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
    except IsADirectoryError:
        print("legacy-directory", flush=True)
        raise SystemExit(0) from None
    except OSError as exc:
        if os.path.lexists(lock_path) and os.path.islink(lock_path):
            print("unsafe-entry", flush=True)
            raise SystemExit(0) from None
        print("error", flush=True)
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        print("unsafe-entry", flush=True)
        raise SystemExit(0)
    with os.fdopen(descriptor, "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("contended", flush=True)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        print("acquired", flush=True)
        for line in sys.stdin:
            try:
                command = json.loads(line)
                if command.get("op") == "apply":
                    response = apply_staged(command, lock_path)
                elif command.get("op") == "restore-exact":
                    response = restore_exact(command, lock_path)
                elif command.get("op") in {"keep-view", "keep-artifact"}:
                    response = keep_staged_view(command, lock_path)
                elif command.get("op") == "replace-artifact":
                    response = replace_staged_artifact(command, lock_path)
                else:
                    raise ValueError("unsupported lock-holder command")
            except Exception as exc:
                response = {"ok": False, "commit_status": None, "error": str(exc)[:1000]}
            print(json.dumps(response, separators=(",", ":")), flush=True)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
