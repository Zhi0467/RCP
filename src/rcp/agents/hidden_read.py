"""Execution-host resolution of selected-secret read policy."""

from __future__ import annotations

import glob
import importlib.resources
import json
import re
import threading
import time
from pathlib import Path, PurePosixPath

from rcp.agents.staged_hidden_read import glob_path_regex, host_facts
from rcp.agents.staged_hidden_read import probe_hidden_read_wrapper as probe_hidden_read_wrapper
from rcp.agents.write_scope import RegisteredRepositoryRoot, installed_server_storage
from rcp.config import Manifest
from rcp.core.models import (
    HIDDEN_READ_WRAPPER_ROOT,
    HiddenReadKeyEvidence,
    HiddenReadScope,
    HiddenReadStatus,
)
from rcp.limits import (
    HIDDEN_READ_PATH_MAX_COUNT,
    HIDDEN_READ_PATH_MAX_LENGTH,
    HIDDEN_READ_READINESS_TTL_SECONDS,
)
from rcp.providers import PROVIDER_IDS, AgentCapability, ProviderId
from rcp.rcp_home import command_socket_directory
from rcp.transport.run_stage import RemoteRunStage
from rcp.transport.ssh import control_directory_candidate

# Case-insensitive globs over variable names. Tool calls keep every other
# variable, so a lab's own settings, proxies, schedulers, GPUs and toolchains
# work unchanged; names that commonly carry credentials are dropped.
HIDDEN_READ_ENV_DENY_LIST = (
    "*_TOKEN*",
    "TOKEN",
    "*SECRET*",
    "*PASSWORD*",
    "*PASSWD*",
    "*PASSPHRASE*",
    "*CREDENTIAL*",
    "*API_KEY*",
    "*APIKEY*",
    "*ACCESS_KEY*",
    "*PRIVATE_KEY*",
    "*_KEY",
    "*COOKIE*",
    "*DATABASE_URL*",
    "*INDEX_URL*",
    "*_DSN",
    "AWS_*",
    "AZURE_*",
    # Shell startup hooks would run before the wrapped command.
    "BASH_ENV",
    "ENV",
)
# Command-mailbox credentials are explicit command arguments, not environment variables.
WEBKIT_READ_DENY_PATHS = tuple(
    f"~/Library/{directory}/{bundle}{suffix}"
    for bundle in ("app.researchcontrolpanel.rcp", "app.researchcontrolpanel.rcp.dev")
    for directory, suffix in (
        ("WebKit", ""),
        ("Application Support", ""),
        ("HTTPStorages", ""),
        ("HTTPStorages", ".binarycookies"),
        ("Caches", ""),
        ("Cookies", ".binarycookies"),
    )
)


_readiness_cache: tuple[float, HiddenReadStatus] | None = None
_readiness_lock = threading.Lock()


def cached_hidden_read_readiness() -> HiddenReadStatus:
    """Settings checks wrapper availability, never a launch's effective scope."""
    global _readiness_cache
    with _readiness_lock:
        if _readiness_cache is not None and time.monotonic() < _readiness_cache[0]:
            return _readiness_cache[1]
        result = probe_hidden_read_wrapper()
        status = HiddenReadStatus(
            status="enforced" if result["ready"] else "unhidden",
            reasons=() if result["ready"] else (result["reason"] or "wrapper_unavailable",),
        )
        _readiness_cache = (time.monotonic() + HIDDEN_READ_READINESS_TTL_SECONDS, status)
        return status


def staged_hidden_read_source() -> str:
    return (
        importlib.resources.files("rcp.agents")
        .joinpath("staged_hidden_read.py")
        .read_text(encoding="utf-8")
    )


def hidden_read_defaults(
    *,
    home: str,
    app_data_dir: str | None,
    credential_roots: tuple[str, ...],
    provider_login_files: tuple[str, ...],
    control_socket_dir: str | None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Credential roots name provider stores, never a deploy-key parent."""
    directories = list(credential_roots)
    directories.append(home + "/" + HIDDEN_READ_WRAPPER_ROOT)
    files = [*provider_login_files, home + "/.config/rcp/claude-setup-token"]
    globs = []
    for path in WEBKIT_READ_DENY_PATHS:
        (files if path.endswith(".binarycookies") else directories).append(home + path[1:])
    if control_socket_dir:
        directories.append(control_socket_dir)
    if app_data_dir:
        # Globs vary only their last component, so a Linux wrapper can hide
        # future matches too; wholly secret trees are plain directories.
        # Each provider's credential namespace, never the Git identities beside them.
        directories.extend(app_data_dir + "/providers/" + provider for provider in PROVIDER_IDS)
        directories.extend(
            (
                app_data_dir + "/service-connections",
                app_data_dir + "/run-stage/project-transfer-activation",
            )
        )
        globs.extend(
            (
                glob.escape(app_data_dir) + "/rcp.sqlite3*",
                glob.escape(app_data_dir) + "/run-stage/backup-*",
            )
        )
    return tuple(sorted(set(directories))), tuple(sorted(set(files))), tuple(sorted(set(globs)))


class HiddenFolderRejected(ValueError):
    def __init__(self, *, code: str, path: str = "") -> None:
        self.code = code
        self.path = path
        super().__init__(f"{code}: {path}")


def _absolute(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path
        or len(path) > HIDDEN_READ_PATH_MAX_LENGTH
        or not path.startswith("/")
        or path.startswith("//")
        or str(PurePosixPath(path)) != path
        or ".." in path.split("/")
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise HiddenFolderRejected(code="invalid_hidden_folder", path=str(path))


def _overlap(left: str, right: str) -> bool:
    a, b = PurePosixPath(left), PurePosixPath(right)
    return a == b or a in b.parents or b in a.parents


# Executable and system roots: a hidden folder covering one breaks every tool call.
SYSTEM_RUNTIME_ROOTS = (
    "/bin",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/nix",
    "/opt/homebrew",
    "/private/etc",
    "/proc",
    "/sbin",
    "/sys",
    "/System",
    "/Library/Developer",
    "/usr",
)


def _validated_folders(folders: list[str], protected_roots: tuple[str, ...]) -> list[str]:
    if len(folders) > HIDDEN_READ_PATH_MAX_COUNT:
        raise HiddenFolderRejected(code="too_many_hidden_folders")
    for folder in folders:
        _absolute(folder)
        if any(_overlap(folder, root) for root in protected_roots):
            raise HiddenFolderRejected(code="protected_root_overlap", path=folder)
    if len(folders) != len(set(folders)):
        raise HiddenFolderRejected(code="duplicate_hidden_folder")
    return sorted(folders)


def validate_machine_hidden_folders(
    folders: list[str], *, protected_roots: tuple[str, ...]
) -> list[str]:
    # Lexical only: callers canonicalize on the execution host first, so a
    # path is never resolved through this backend's symlinks.
    return _validated_folders(folders, protected_roots)


def _facts(remote_stage: RemoteRunStage | None, paths: list[str]) -> dict:
    if remote_stage is None:
        return host_facts(paths)
    result = remote_stage._ssh(
        [
            "python3",
            "-c",
            staged_hidden_read_source(),
            "--host-facts",
            json.dumps(paths),
        ]
    )
    if result.returncode:
        raise ValueError("hidden-read execution host inspection failed")
    return json.loads(result.stdout)


def _glob_prefix(pattern: str) -> tuple[str, str]:
    """Separate the canonicalizable literal parent from the wildcard suffix."""
    parts = pattern.split("/")
    literal = []
    for part in parts:
        unescaped = part.replace("[[]", "\x00").replace("[*]", "\x01").replace("[?]", "\x02")
        if any(char in unescaped for char in "*?["):
            break
        literal.append(unescaped.replace("\x00", "[").replace("\x01", "*").replace("\x02", "?"))
    prefix = "/".join(literal) or "/"
    suffix = "/" + "/".join(parts[len(literal) :])
    return prefix, suffix


def resolve_hidden_read_scope(
    *,
    manifest: Manifest,
    execution_machine: str,
    provider: ProviderId,
    capability: AgentCapability,
    stage_root: str,
    workspace_root: str,
    app_data_dir: Path | None,
    remote_stage: RemoteRunStage | None,
    repository_inventory: list[RegisteredRepositoryRoot],
    machine_hidden_folders: list[str],
    key_evidence: tuple[HiddenReadKeyEvidence, ...],
    browser_enabled: bool,
) -> HiddenReadScope:
    machine = manifest.machine_map[execution_machine]
    if bool(machine.host) != (remote_stage is not None) or (
        remote_stage is not None and remote_stage.host != machine.host
    ):
        raise ValueError("hidden-read execution host does not match the stage")
    _validated_folders(machine_hidden_folders, ())
    facts = _facts(remote_stage, [])
    home = facts["home"]
    if machine.os_account and machine.os_account != facts["os_account"]:
        raise ValueError("hidden-read execution account does not match the machine")
    data_dir = (
        str(app_data_dir.expanduser().resolve())
        if app_data_dir is not None and remote_stage is None
        else home + "/.local/share/rcp"
        if remote_stage is not None
        else None
    )
    directories, files, globs = hidden_read_defaults(
        home=home,
        app_data_dir=data_dir,
        credential_roots=(),
        provider_login_files=tuple(facts["provider_login_files"]),
        control_socket_dir=str(control_directory_candidate()) if remote_stage is None else None,
    )
    if data_dir and remote_stage is None:
        storage = installed_server_storage(Path(data_dir))
        if storage:
            directories += (storage.update_checkpoints_root, storage.restore_operations_root)
    protected = [
        stage_root,
        workspace_root,
        home + "/.rcp/stages",
        home + "/.rcp/tools",
        command_socket_directory(home),
        home + "/.ssh/known_hosts",
        *[item.path for item in repository_inventory if item.execution_host == machine.host],
        *[item.path for item in manifest.repositories if item.machine == execution_machine],
    ]
    if data_dir:
        protected.extend((data_dir + "/tools", data_dir + "/run-stage"))
    # Only canonicalize literal prefixes: glob syntax must remain syntax.
    prefixes = {_glob_prefix(pattern)[0] for pattern in globs}
    declared = list(
        dict.fromkeys(
            [
                *directories,
                *files,
                *protected,
                *machine_hidden_folders,
                *prefixes,
                *[key.path for key in key_evidence],
            ]
        )
    )
    resolved = _facts(remote_stage, declared)
    if (resolved["home"], resolved["os_account"]) != (home, facts["os_account"]):
        raise ValueError("hidden-read execution account changed during resolution")
    canonical = resolved["paths"]
    exempt = [canonical[key.path] for key in key_evidence if key.visibility == "readable"]
    protected = [
        *(canonical[path] for path in protected),
        *(str(PurePosixPath(path).parent) for path in exempt),
        *SYSTEM_RUNTIME_ROOTS,
        *resolved["runtime_paths"],
        *(str(PurePosixPath(path).parent) for path in machine.provider_paths.values()),
    ]
    folders = _validated_folders(
        [canonical[path] for path in machine_hidden_folders], tuple(protected)
    )
    reasons = set()
    readiness = resolved["readiness"]
    if not readiness["ready"]:
        reasons.add(readiness["reason"] or "wrapper_unavailable")
    if browser_enabled and readiness["platform"] == "darwin":
        reasons.add("browser_unwrapped_macos")
    for key in key_evidence:
        if key.visibility == "readable":
            reasons.add(
                "deploy_key_agent_unconfirmed"
                if key.kind == "deploy_key"
                else "ssh_key_agent_unconfirmed"
            )
    directories = tuple(sorted({canonical[path] for path in directories} | set(folders)))
    files = tuple(
        sorted(
            {canonical[path] for path in files}
            | {canonical[key.path] for key in key_evidence if key.visibility == "hidden"}
        )
    )
    globs = tuple(
        sorted(
            {
                glob.escape(canonical[prefix]) + suffix
                for pattern in globs
                for prefix, suffix in [_glob_prefix(pattern)]
            }
        )
    )
    # Defaults also yield to a readable key: never mask its containing folder.
    filtered_dirs = tuple(
        path
        for path in directories
        if not any(
            path == key or PurePosixPath(path) in PurePosixPath(key).parents for key in exempt
        )
    )
    filtered_files = tuple(path for path in files if path not in exempt)
    filtered_globs = tuple(
        pattern
        for pattern in globs
        if not any(re.fullmatch(glob_path_regex(pattern), key) for key in exempt)
    )
    if (directories, files, globs) != (filtered_dirs, filtered_files, filtered_globs):
        reasons.add("credential_compatibility_exception")
    # OpenCode's path permission grammar cannot denote a literal glob character.
    if provider == "opencode" and any(
        char in path for path in (*filtered_dirs, *filtered_files) for char in "*?["
    ):
        reasons.add("provider_native_tools_uncovered")
    return HiddenReadScope(
        execution_machine=execution_machine,
        execution_host=machine.host,
        os_account=facts["os_account"],
        account_home=home,
        hidden_directories=filtered_dirs,
        hidden_files=filtered_files,
        hidden_globs=filtered_globs,
        env_deny_list=HIDDEN_READ_ENV_DENY_LIST,
        key_evidence=tuple(
            HiddenReadKeyEvidence.model_validate({**key.model_dump(), "path": canonical[key.path]})
            for key in key_evidence
        ),
        enforcement=HiddenReadStatus(
            status="unhidden" if reasons else "enforced", reasons=tuple(sorted(reasons))
        ),
    )
