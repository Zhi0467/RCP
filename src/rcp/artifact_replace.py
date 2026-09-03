from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import platform
import re
import secrets
import stat
import sys
from contextlib import suppress


def replace_regular_file_in_open_directory(
    directory_fd: int,
    recovery_directory_fd: int,
    name: str,
    data: bytes,
    *,
    expected_sha256: str | None,
    mode: int,
) -> bool:
    """Replace while retaining the displaced live inode in an RCP-owned directory."""

    if expected_sha256 is not None:
        _require_separate_owned_recovery_directory(directory_fd, recovery_directory_fd)
        _recover_regular_file_replacements(directory_fd, recovery_directory_fd, name)
    candidate_sha256 = hashlib.sha256(data).hexdigest()
    if expected_sha256 is None:
        replacement_name = f".rcp-artifact-{secrets.token_hex(16)}"
    else:
        name_hash = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
        replacement_name = (
            f".rcp-artifact-{name_hash}-{expected_sha256}-{candidate_sha256}-{secrets.token_hex(8)}"
        )
    descriptor = os.open(
        replacement_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=recovery_directory_fd,
    )
    replacement_holds_candidate = True
    try:
        try:
            remaining = memoryview(data)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short artifact replacement write")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if expected_sha256 is None:
            os.replace(
                replacement_name,
                name,
                src_dir_fd=recovery_directory_fd,
                dst_dir_fd=directory_fd,
            )
            replacement_holds_candidate = False
            os.fsync(directory_fd)
            os.fsync(recovery_directory_fd)
            return True

        replacement_holds_candidate = False
        exchange_regular_files(recovery_directory_fd, replacement_name, directory_fd, name)
        os.fsync(directory_fd)
        os.fsync(recovery_directory_fd)
        try:
            displaced_sha256, displaced_stable = _regular_file_digest(
                recovery_directory_fd, replacement_name
            )
        except (OSError, ValueError):
            displaced_sha256, displaced_stable = "", False
        if displaced_stable and displaced_sha256 == expected_sha256:
            os.unlink(replacement_name, dir_fd=recovery_directory_fd)
            os.fsync(recovery_directory_fd)
            return True

        exchange_regular_files(recovery_directory_fd, replacement_name, directory_fd, name)
        os.fsync(directory_fd)
        os.fsync(recovery_directory_fd)
        try:
            restored_sha256, restored_stable = _regular_file_digest(
                recovery_directory_fd, replacement_name
            )
        except (OSError, ValueError):
            restored_sha256, restored_stable = "", False
        if restored_stable and restored_sha256 == candidate_sha256:
            replacement_holds_candidate = True
            os.unlink(replacement_name, dir_fd=recovery_directory_fd)
            replacement_holds_candidate = False
            os.fsync(recovery_directory_fd)
        return False
    except BaseException:
        if replacement_holds_candidate:
            with suppress(FileNotFoundError):
                os.unlink(replacement_name, dir_fd=recovery_directory_fd)
        raise


def _require_separate_owned_recovery_directory(
    directory_fd: int,
    recovery_directory_fd: int,
) -> None:
    live = os.fstat(directory_fd)
    recovery = os.fstat(recovery_directory_fd)
    if (live.st_dev, live.st_ino) == (recovery.st_dev, recovery.st_ino):
        raise ValueError("artifact recovery directory must be outside agent-writable output")
    if recovery.st_uid != os.geteuid() or stat.S_IMODE(recovery.st_mode) & 0o077:
        raise ValueError("artifact recovery directory is not privately RCP-owned")


def _recover_regular_file_replacements(
    directory_fd: int,
    recovery_directory_fd: int,
    name: str,
) -> None:
    name_hash = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    pattern = re.compile(
        rf"[.]rcp-artifact-{name_hash}-([0-9a-f]{{64}})-([0-9a-f]{{64}})-[0-9a-f]{{16}}"
    )
    for replacement_name in sorted(os.listdir(recovery_directory_fd)):
        matched = pattern.fullmatch(replacement_name)
        if matched is None:
            continue
        expected_sha256, candidate_sha256 = matched.groups()
        replacement_sha256, replacement_stable = _regular_file_digest(
            recovery_directory_fd, replacement_name
        )
        try:
            current_sha256, current_stable = _regular_file_digest(directory_fd, name)
        except FileNotFoundError:
            if replacement_sha256 == candidate_sha256:
                os.unlink(replacement_name, dir_fd=recovery_directory_fd)
            else:
                os.replace(
                    replacement_name,
                    name,
                    src_dir_fd=recovery_directory_fd,
                    dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
            os.fsync(recovery_directory_fd)
            continue
        if not replacement_stable or not current_stable:
            raise ValueError("artifact replacement recovery found changing source bytes")
        if current_sha256 == candidate_sha256 and replacement_sha256 not in {
            expected_sha256,
            candidate_sha256,
        }:
            exchange_regular_files(recovery_directory_fd, replacement_name, directory_fd, name)
            os.fsync(directory_fd)
            os.fsync(recovery_directory_fd)
            restored_sha256, restored_stable = _regular_file_digest(
                recovery_directory_fd, replacement_name
            )
            if not restored_stable or restored_sha256 != candidate_sha256:
                raise ValueError("artifact replacement recovery preserved concurrent edits")
        elif replacement_sha256 not in {expected_sha256, candidate_sha256}:
            raise ValueError("artifact replacement recovery preserved concurrent edits")
        os.unlink(replacement_name, dir_fd=recovery_directory_fd)
        os.fsync(recovery_directory_fd)


def _regular_file_digest(directory_fd: int, name: str) -> tuple[str, bool]:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise ValueError("artifact is not a regular file")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        final = os.fstat(descriptor)
        path = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        initial_identity = (
            initial.st_dev,
            initial.st_ino,
            initial.st_size,
            initial.st_mtime_ns,
            initial.st_ctime_ns,
        )
        final_identity = (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        )
        path_identity = (
            path.st_dev,
            path.st_ino,
            path.st_size,
            path.st_mtime_ns,
            path.st_ctime_ns,
        )
        return digest.hexdigest(), initial_identity == final_identity == path_identity
    finally:
        os.close(descriptor)


def exchange_regular_files(
    first_directory_fd: int,
    first: str,
    second_directory_fd: int,
    second: str,
) -> None:
    """Atomically swap two names on one filesystem without removing the live path."""

    libc = ctypes.CDLL(None, use_errno=True)
    encoded_first = os.fsencode(first)
    encoded_second = os.fsencode(second)
    if sys.platform == "darwin":
        rename = libc.renameatx_np
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            first_directory_fd,
            encoded_first,
            second_directory_fd,
            encoded_second,
            0x00000002,
        )
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        if rename is not None:
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            rename.restype = ctypes.c_int
            result = rename(
                first_directory_fd,
                encoded_first,
                second_directory_fd,
                encoded_second,
                0x2,
            )
        else:
            system_call = libc.syscall
            system_call.restype = ctypes.c_long
            number = {"x86_64": 316, "aarch64": 276}.get(platform.machine())
            if number is None:
                raise OSError(errno.ENOTSUP, "atomic file exchange is unsupported")
            result = system_call(
                number,
                first_directory_fd,
                encoded_first,
                second_directory_fd,
                encoded_second,
                0x2,
            )
    else:
        raise OSError(errno.ENOTSUP, "atomic file exchange is unsupported")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), first, second)
