"""Account-local central-checkout path and retained-state inspection.

The server sends this module's source through ``python3 -c``. It performs only
filesystem work on the selected local or SSH account and returns bounded,
nonsecret JSON. Git and durable-state changes remain with the caller.
"""

from __future__ import annotations

import errno
import json
import os
import pwd
import re
import stat
import sys
import uuid
from pathlib import Path

ALIAS = re.compile(r"[a-z][a-z0-9-]{0,47}")
ACCOUNT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,127}")
DIRECTORY_MODE = 0o700
MAX_RESEARCH_ENTRIES = 4096
MAX_PATCH_BYTES = 1024 * 1024
MAX_GIT_CONFIG_BYTES = 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _uuid4(value: str, label: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be a canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{label} must be a lowercase canonical UUID4")
    return value


def _absolute_path(value: str, label: str) -> Path:
    if len(value) > 4096 or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError(f"{label} is invalid")
    path = Path(value)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts or str(path) != value:
        raise ValueError(f"{label} is invalid")
    return path


def _account(expected_account: str, expected_home: str) -> tuple[pwd.struct_passwd, Path]:
    if ACCOUNT.fullmatch(expected_account) is None:
        raise ValueError("expected account is invalid")
    account = pwd.getpwuid(os.getuid())
    if account.pw_name != expected_account:
        raise ValueError("checkout helper is running as the wrong operating-system account")
    home = _absolute_path(expected_home, "execution account home")
    if Path(account.pw_dir) != home:
        raise ValueError("checkout helper resolved a different execution account home")
    with _opened_absolute_directory(home, uid=account.pw_uid, require_owner=True):
        pass
    return account, home


class _Directory:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor

    def __enter__(self) -> int:
        return self.descriptor

    def __exit__(self, *_args: object) -> None:
        os.close(self.descriptor)


def _opened_absolute_directory(
    path: Path,
    *,
    uid: int,
    require_owner: bool,
) -> _Directory:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            _require_safe_ancestor(descriptor)
        _require_directory_descriptor(
            descriptor,
            uid=uid,
            require_owner=require_owner,
            label="directory",
        )
        return _Directory(descriptor)
    except Exception:
        os.close(descriptor)
        raise


def _require_safe_ancestor(descriptor: int) -> None:
    info = os.fstat(descriptor)
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISDIR(info.st_mode) or (mode & 0o022 and not mode & stat.S_ISVTX):
        raise ValueError("directory ancestry has unsafe type or writable mode")


def _require_directory_descriptor(
    descriptor: int,
    *,
    uid: int,
    require_owner: bool,
    label: str,
) -> None:
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or (require_owner and info.st_uid != uid)
        or (require_owner and stat.S_IMODE(info.st_mode) & 0o022)
    ):
        raise ValueError(f"{label} has unsafe type, ownership, or mode")


def _open_or_create_child(
    parent: int,
    name: str,
    *,
    uid: int,
    label: str,
) -> tuple[int, bool]:
    created = False
    try:
        os.mkdir(name, DIRECTORY_MODE, dir_fd=parent)
        os.fsync(parent)
        created = True
    except FileExistsError:
        pass
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except OSError as exc:
        raise ValueError(f"{label} is unavailable or unsafe") from exc
    try:
        _require_directory_descriptor(
            descriptor,
            uid=uid,
            require_owner=True,
            label=label,
        )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, created


def _default_remote_root(home: Path) -> Path:
    return home / ".local" / "share" / "rcp" / "projects"


def _central_root(
    account: pwd.struct_passwd,
    home: Path,
    location: str,
    configured_root: str,
) -> tuple[Path, _Directory]:
    if location not in {"local", "ssh"}:
        raise ValueError("checkout location is invalid")
    if location == "ssh" and configured_root == "-":
        root = _default_remote_root(home)
        descriptor = os.open(home, _DIRECTORY_FLAGS)
        try:
            for name, label in (
                (".local", "remote .local directory"),
                ("share", "remote share directory"),
                ("rcp", "remote RCP state directory"),
                ("projects", "remote central checkout root"),
            ):
                next_descriptor, _created = _open_or_create_child(
                    descriptor,
                    name,
                    uid=account.pw_uid,
                    label=label,
                )
                os.close(descriptor)
                descriptor = next_descriptor
            return root, _Directory(descriptor)
        except Exception:
            os.close(descriptor)
            raise
    if configured_root == "-":
        raise ValueError("server-local central checkout root cannot be defaulted")
    root = _absolute_path(configured_root, "central checkout root")
    return root, _opened_absolute_directory(
        root,
        uid=account.pw_uid,
        require_owner=True,
    )


def _prove_writable(descriptor: int) -> None:
    name = f".rcp-write-probe-{uuid.uuid4().hex}"
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=descriptor,
        )
        os.fsync(file_descriptor)
    except OSError as exc:
        raise ValueError("central checkout root is not writable by the execution account") from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)
        except FileNotFoundError:
            pass


def _prepare(
    expected_account: str,
    expected_home: str,
    location: str,
    configured_root: str,
    project_id: str,
    alias: str,
) -> dict[str, object]:
    _uuid4(project_id, "project id")
    if ALIAS.fullmatch(alias) is None:
        raise ValueError("repository alias is invalid")
    account, home = _account(expected_account, expected_home)
    root, opened_root = _central_root(account, home, location, configured_root)
    with opened_root as root_descriptor:
        _prove_writable(root_descriptor)
        project_descriptor, _project_created = _open_or_create_child(
            root_descriptor,
            project_id,
            uid=account.pw_uid,
            label="project checkout directory",
        )
        with _Directory(project_descriptor) as opened_project:
            repositories_descriptor, _repositories_created = _open_or_create_child(
                opened_project,
                "repositories",
                uid=account.pw_uid,
                label="repository checkout directory",
            )
            with _Directory(repositories_descriptor) as opened_repositories:
                checkout_descriptor, created = _open_or_create_child(
                    opened_repositories,
                    alias,
                    uid=account.pw_uid,
                    label="central repository checkout",
                )
                with _Directory(checkout_descriptor) as opened_checkout:
                    entries = os.listdir(opened_checkout)
                    if len(entries) > MAX_RESEARCH_ENTRIES:
                        raise ValueError("central repository checkout contains too many entries")
                    empty = not entries
    checkout = root / project_id / "repositories" / alias
    return {
        "account": account.pw_name,
        "home": str(home),
        "central_root": str(root),
        "repository_path": str(checkout),
        "disposition": "request_created" if created else "reused_existing",
        "empty": empty,
    }


def _patch_names(patches_descriptor: int) -> list[str]:
    names: list[str] = []
    entries = os.listdir(patches_descriptor)
    if len(entries) > MAX_RESEARCH_ENTRIES:
        return ["too-many"]
    scanned_entries = len(entries)
    for name in entries:
        if re.fullmatch(r"[0-9]{6}\.json", name):
            names.append(name)
            continue
        if not name.startswith("batch-"):
            continue
        try:
            batch_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=patches_descriptor)
        except OSError:
            return ["unsafe-batch"]
        with _Directory(batch_descriptor) as opened_batch:
            batch_entries = os.listdir(opened_batch)
            scanned_entries += len(batch_entries)
            if scanned_entries > MAX_RESEARCH_ENTRIES:
                return ["too-many"]
            names.extend(
                f"{name}/{entry}"
                for entry in batch_entries
                if re.fullmatch(r"[0-9]{6}\.json", entry)
            )
    return sorted(names, key=lambda value: value.rsplit("/", 1)[-1])


def _git_directory(
    expected_account: str,
    expected_home: str,
    repository_path: str,
) -> dict[str, object]:
    account, home = _account(expected_account, expected_home)
    path = _absolute_path(repository_path, "repository checkout path")
    with _opened_absolute_directory(
        path,
        uid=account.pw_uid,
        require_owner=True,
    ) as repository_descriptor:
        try:
            git_descriptor = os.open(".git", _DIRECTORY_FLAGS, dir_fd=repository_descriptor)
        except OSError as exc:
            raise ValueError("central checkout Git directory is unavailable or unsafe") from exc
        with _Directory(git_descriptor) as opened_git:
            _require_directory_descriptor(
                opened_git,
                uid=account.pw_uid,
                require_owner=True,
                label="central checkout Git directory",
            )
            try:
                config_descriptor = os.open(
                    "config",
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=opened_git,
                )
            except OSError as exc:
                raise ValueError("central checkout Git config is unavailable or unsafe") from exc
            try:
                config = os.fstat(config_descriptor)
                if (
                    not stat.S_ISREG(config.st_mode)
                    or config.st_uid != account.pw_uid
                    or stat.S_IMODE(config.st_mode) & 0o022
                    or config.st_size > MAX_GIT_CONFIG_BYTES
                ):
                    raise ValueError("central checkout Git config is unavailable or unsafe")
            finally:
                os.close(config_descriptor)
    return {"repository_path": str(path), "safe": True}


def _read_patch_identity(patches_descriptor: int, name: str) -> tuple[str | None, str | None]:
    parent = patches_descriptor
    opened_batch: _Directory | None = None
    filename = name
    if "/" in name:
        batch, filename = name.split("/", 1)
        try:
            descriptor = os.open(batch, _DIRECTORY_FLAGS, dir_fd=patches_descriptor)
        except OSError:
            return None, None
        opened_batch = _Directory(descriptor)
        parent = descriptor
    try:
        try:
            descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        except OSError:
            return None, None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PATCH_BYTES:
                return None, None
            chunks: list[bytes] = []
            remaining = MAX_PATCH_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > MAX_PATCH_BYTES:
                return None, None
        finally:
            os.close(descriptor)
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, None
        identity = document.get("project_identity") if isinstance(document, dict) else None
        if not isinstance(identity, dict):
            return None, None
        project_id = identity.get("project_id")
        home_space_id = identity.get("home_space_id")
        if not isinstance(project_id, str) or not isinstance(home_space_id, str):
            return None, None
        try:
            return _uuid4(project_id, "retained project id"), _uuid4(
                home_space_id,
                "retained home space id",
            )
        except ValueError:
            return None, None
    finally:
        if opened_batch is not None:
            opened_batch.__exit__()


def _retained(
    expected_account: str,
    expected_home: str,
    repository_path: str,
) -> dict[str, object]:
    account, home = _account(expected_account, expected_home)
    path = _absolute_path(repository_path, "repository checkout path")
    with _opened_absolute_directory(
        path,
        uid=account.pw_uid,
        require_owner=True,
    ) as repository_descriptor:
        try:
            research_descriptor = os.open(
                ".research",
                _DIRECTORY_FLAGS,
                dir_fd=repository_descriptor,
            )
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return {
                    "retained": False,
                    "patch_history": False,
                    "project_id": None,
                    "home_space_id": None,
                }
            raise ValueError("retained research path is unavailable or unsafe") from exc
        with _Directory(research_descriptor) as opened_research:
            _require_directory_descriptor(
                opened_research,
                uid=account.pw_uid,
                require_owner=True,
                label="retained research directory",
            )
            entries = os.listdir(opened_research)
            if len(entries) > MAX_RESEARCH_ENTRIES:
                return {
                    "retained": True,
                    "patch_history": True,
                    "project_id": None,
                    "home_space_id": None,
                }
            patch_names: list[str] = []
            if "patches" in entries:
                try:
                    patches_descriptor = os.open(
                        "patches",
                        _DIRECTORY_FLAGS,
                        dir_fd=opened_research,
                    )
                except OSError as exc:
                    raise ValueError("retained Patch path is unavailable or unsafe") from exc
                with _Directory(patches_descriptor) as opened_patches:
                    patch_names = _patch_names(opened_patches)
                    project_id, home_space_id = (
                        _read_patch_identity(opened_patches, patch_names[0])
                        if patch_names
                        else (None, None)
                    )
            else:
                project_id, home_space_id = None, None
            return {
                "retained": bool(entries),
                "patch_history": bool(patch_names),
                "project_id": project_id,
                "home_space_id": home_space_id,
            }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        operation = arguments.pop(0)
        if operation == "prepare" and len(arguments) == 6:
            payload = _prepare(*arguments)
        elif operation == "git-directory" and len(arguments) == 3:
            payload = _git_directory(*arguments)
        elif operation == "retained" and len(arguments) == 3:
            payload = _retained(*arguments)
        else:
            raise ValueError("checkout helper operation is invalid")
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
