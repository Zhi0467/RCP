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

_ROLLBACK_EXCHANGE_ATTEMPTS = 8


class ArtifactReplacementConflict(ValueError):
    """The live source is proven missing or structurally unsafe."""


class _ArtifactNotRegular(ValueError):
    pass


def recover_regular_file_replacement_in_open_directory(
    directory_fd: int,
    recovery_directory_fd: int,
    name: str,
) -> None:
    """Settle a conditional replacement journal without starting a new write."""

    _require_separate_owned_recovery_directory(directory_fd, recovery_directory_fd)
    _recover_regular_file_replacements(directory_fd, recovery_directory_fd, name)
    name_hash = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    if any(
        item.startswith(f".rcp-artifact-{name_hash}-")
        or item.startswith(f".rcp-artifact-quarantine-{name_hash}-")
        for item in os.listdir(recovery_directory_fd)
    ):
        raise ArtifactReplacementConflict("artifact replacement journal remains unresolved")


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
        replacement_token = secrets.token_hex(8)
        replacement_name = (
            f".rcp-artifact-{name_hash}-{expected_sha256}-{candidate_sha256}-{replacement_token}"
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

        candidate_written_sha256, candidate_fingerprint, candidate_stable = _regular_file_state(
            recovery_directory_fd, replacement_name
        )
        if not candidate_stable or candidate_written_sha256 != candidate_sha256:
            raise ValueError("artifact replacement candidate did not stabilize")
        pending_name = (
            f".rcp-artifact-{name_hash}-{expected_sha256}-{candidate_sha256}-"
            f"{candidate_fingerprint}-{replacement_token}"
        )
        os.rename(
            replacement_name,
            pending_name,
            src_dir_fd=recovery_directory_fd,
            dst_dir_fd=recovery_directory_fd,
        )
        replacement_name = pending_name
        os.fsync(recovery_directory_fd)
        replacement_holds_candidate = False
        try:
            exchange_regular_files(recovery_directory_fd, replacement_name, directory_fd, name)
        except FileNotFoundError:
            try:
                _source_regular_file_state(directory_fd, name)
            except ArtifactReplacementConflict:
                _quarantine_regular_file_replacement(
                    recovery_directory_fd, replacement_name, name_hash=name_hash
                )
                raise
            raise
        os.fsync(directory_fd)
        os.fsync(recovery_directory_fd)
        displaced_sha256, displaced_fingerprint, displaced_stable = _regular_file_state(
            recovery_directory_fd, replacement_name
        )
        if displaced_stable and displaced_sha256 == expected_sha256:
            os.unlink(replacement_name, dir_fd=recovery_directory_fd)
            os.fsync(recovery_directory_fd)
            return True
        if not displaced_stable:
            raise ValueError("artifact displaced during publication did not stabilize")
        replacement_name = _rename_regular_file_rollback(
            recovery_directory_fd,
            replacement_name,
            name_hash=name_hash,
            expected_live_fingerprint=candidate_fingerprint,
            desired_fingerprint=displaced_fingerprint,
            token=replacement_token,
        )
        try:
            _complete_regular_file_rollback(
                directory_fd,
                recovery_directory_fd,
                name,
                replacement_name,
                name_hash=name_hash,
                expected_live_fingerprint=candidate_fingerprint,
                desired_fingerprint=displaced_fingerprint,
                token=replacement_token,
            )
        except ArtifactReplacementConflict:
            _quarantine_regular_file_replacement(
                recovery_directory_fd, replacement_name, name_hash=name_hash
            )
            raise
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
    pending_pattern = re.compile(
        rf"[.]rcp-artifact-{name_hash}-([0-9a-f]{{64}})-([0-9a-f]{{64}})-"
        rf"([0-9a-f]{{64}})-([0-9a-f]{{16}})"
    )
    staged_pattern = re.compile(
        rf"[.]rcp-artifact-{name_hash}-([0-9a-f]{{64}})-([0-9a-f]{{64}})-([0-9a-f]{{16}})"
    )
    rollback_pattern = re.compile(
        rf"[.]rcp-artifact-{name_hash}-rollback-"
        rf"([0-9a-f]{{64}})-([0-9a-f]{{64}})-([0-9a-f]{{16}})"
    )
    for replacement_name in sorted(os.listdir(recovery_directory_fd)):
        rollback = rollback_pattern.fullmatch(replacement_name)
        if rollback is not None:
            expected_live_fingerprint, desired_fingerprint, token = rollback.groups()
            try:
                _complete_regular_file_rollback(
                    directory_fd,
                    recovery_directory_fd,
                    name,
                    replacement_name,
                    name_hash=name_hash,
                    expected_live_fingerprint=expected_live_fingerprint,
                    desired_fingerprint=desired_fingerprint,
                    token=token,
                )
            except ArtifactReplacementConflict:
                _quarantine_regular_file_replacement(
                    recovery_directory_fd, replacement_name, name_hash=name_hash
                )
                raise
            continue
        matched = pending_pattern.fullmatch(replacement_name)
        if matched is not None:
            expected_sha256, candidate_sha256, candidate_fingerprint, token = matched.groups()
        else:
            staged = staged_pattern.fullmatch(replacement_name)
            if staged is None:
                continue
            _discard_staged_regular_file(recovery_directory_fd, replacement_name)
            continue
        replacement_sha256, replacement_fingerprint, replacement_stable = _regular_file_state(
            recovery_directory_fd, replacement_name
        )
        try:
            current_sha256, current_fingerprint, current_stable = _source_regular_file_state(
                directory_fd, name
            )
        except ArtifactReplacementConflict:
            _quarantine_regular_file_replacement(
                recovery_directory_fd, replacement_name, name_hash=name_hash
            )
            raise
        if not replacement_stable or not current_stable:
            raise ValueError("artifact replacement recovery found changing source bytes")
        if (
            replacement_sha256 not in {expected_sha256, candidate_sha256}
            and current_fingerprint == candidate_fingerprint
        ):
            replacement_name = _rename_regular_file_rollback(
                recovery_directory_fd,
                replacement_name,
                name_hash=name_hash,
                expected_live_fingerprint=current_fingerprint,
                desired_fingerprint=replacement_fingerprint,
                token=token,
            )
            try:
                _complete_regular_file_rollback(
                    directory_fd,
                    recovery_directory_fd,
                    name,
                    replacement_name,
                    name_hash=name_hash,
                    expected_live_fingerprint=current_fingerprint,
                    desired_fingerprint=replacement_fingerprint,
                    token=token,
                )
            except ArtifactReplacementConflict:
                _quarantine_regular_file_replacement(
                    recovery_directory_fd, replacement_name, name_hash=name_hash
                )
                raise
            continue
        if replacement_sha256 not in {
            expected_sha256,
            candidate_sha256,
        }:
            os.unlink(replacement_name, dir_fd=recovery_directory_fd)
            os.fsync(recovery_directory_fd)
            continue
        os.unlink(replacement_name, dir_fd=recovery_directory_fd)
        os.fsync(recovery_directory_fd)


def _discard_staged_regular_file(recovery_directory_fd: int, replacement_name: str) -> None:
    """Remove a pre-publication candidate that cannot have reached the live path."""

    metadata = os.stat(replacement_name, dir_fd=recovery_directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise ArtifactReplacementConflict(
            "artifact replacement staging marker is not a regular file"
        )
    os.unlink(replacement_name, dir_fd=recovery_directory_fd)
    os.fsync(recovery_directory_fd)


def _quarantine_regular_file_replacement(
    recovery_directory_fd: int,
    replacement_name: str,
    *,
    name_hash: str,
) -> None:
    """Preserve ambiguous displaced bytes under a name recovery will ignore."""

    quarantine_name = f".rcp-artifact-quarantine-{name_hash}-{secrets.token_hex(8)}"
    os.rename(
        replacement_name,
        quarantine_name,
        src_dir_fd=recovery_directory_fd,
        dst_dir_fd=recovery_directory_fd,
    )
    os.fsync(recovery_directory_fd)


def _rename_regular_file_rollback(
    recovery_directory_fd: int,
    replacement_name: str,
    *,
    name_hash: str,
    expected_live_fingerprint: str,
    desired_fingerprint: str,
    token: str,
) -> str:
    rollback_name = (
        f".rcp-artifact-{name_hash}-rollback-"
        f"{expected_live_fingerprint}-{desired_fingerprint}-{token}"
    )
    if rollback_name != replacement_name:
        os.rename(
            replacement_name,
            rollback_name,
            src_dir_fd=recovery_directory_fd,
            dst_dir_fd=recovery_directory_fd,
        )
        os.fsync(recovery_directory_fd)
    return rollback_name


def _complete_regular_file_rollback(
    directory_fd: int,
    recovery_directory_fd: int,
    name: str,
    replacement_name: str,
    *,
    name_hash: str,
    expected_live_fingerprint: str,
    desired_fingerprint: str,
    token: str,
) -> None:
    """Restore the newest displaced bytes and retain a resumable exchange state."""

    for _ in range(_ROLLBACK_EXCHANGE_ATTEMPTS):
        _, replacement_fingerprint, replacement_stable = _regular_file_state(
            recovery_directory_fd, replacement_name
        )
        _, current_fingerprint, current_stable = _source_regular_file_state(directory_fd, name)
        if not replacement_stable or not current_stable:
            raise ValueError("artifact replacement rollback found changing source bytes")
        if replacement_fingerprint == expected_live_fingerprint:
            os.unlink(replacement_name, dir_fd=recovery_directory_fd)
            os.fsync(recovery_directory_fd)
            return
        if replacement_fingerprint == desired_fingerprint:
            if current_fingerprint != expected_live_fingerprint:
                os.unlink(replacement_name, dir_fd=recovery_directory_fd)
                os.fsync(recovery_directory_fd)
                return
        elif current_fingerprint == desired_fingerprint:
            expected_live_fingerprint = current_fingerprint
            desired_fingerprint = replacement_fingerprint
            replacement_name = _rename_regular_file_rollback(
                recovery_directory_fd,
                replacement_name,
                name_hash=name_hash,
                expected_live_fingerprint=expected_live_fingerprint,
                desired_fingerprint=desired_fingerprint,
                token=token,
            )
        else:
            os.unlink(replacement_name, dir_fd=recovery_directory_fd)
            os.fsync(recovery_directory_fd)
            return
        exchange_regular_files(recovery_directory_fd, replacement_name, directory_fd, name)
        os.fsync(directory_fd)
        os.fsync(recovery_directory_fd)
    raise ValueError("artifact replacement rollback did not stabilize")


def _regular_file_digest(directory_fd: int, name: str) -> tuple[str, bool]:
    digest, _, stable = _regular_file_state(directory_fd, name)
    return digest, stable


def _regular_file_state(directory_fd: int, name: str) -> tuple[str, str, bool]:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise _ArtifactNotRegular("artifact is not a regular file")
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
        digest_sha256 = digest.hexdigest()
        # st_dev identifies a mount instance and can change across reboot or remount.
        # st_ino still distinguishes same-byte replacement files on this filesystem.
        fingerprint_identity = (
            path.st_ino,
            stat.S_IFMT(path.st_mode),
            path.st_size,
            path.st_mtime_ns,
        )
        fingerprint = hashlib.sha256(
            (":".join(str(value) for value in fingerprint_identity) + ":" + digest_sha256).encode()
        ).hexdigest()
        return digest_sha256, fingerprint, initial_identity == final_identity == path_identity
    finally:
        os.close(descriptor)


def _source_regular_file_state(directory_fd: int, name: str) -> tuple[str, str, bool]:
    try:
        return _regular_file_state(directory_fd, name)
    except FileNotFoundError as exc:
        raise ArtifactReplacementConflict("artifact source is missing") from exc
    except _ArtifactNotRegular as exc:
        raise ArtifactReplacementConflict("artifact source is not a regular file") from exc
    except OSError as exc:
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError as missing:
            raise ArtifactReplacementConflict("artifact source is missing") from missing
        except OSError:
            raise exc from None
        if not stat.S_ISREG(metadata.st_mode):
            raise ArtifactReplacementConflict("artifact source is not a regular file") from exc
        raise


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
