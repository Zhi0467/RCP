"""Execution-host resolution of selected-secret read policy."""

from __future__ import annotations

import glob
import importlib.resources
import json
import re
import threading
import time
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from rcp.agents.provider_environment import LOGIN_HOMES
from rcp.agents.staged_hidden_read import glob_path_regex, host_facts
from rcp.agents.staged_hidden_read import probe_hidden_read_wrapper as probe_hidden_read_wrapper
from rcp.agents.write_scope import RegisteredRepositoryRoot, installed_server_storage
from rcp.config import MachineConfig, Manifest
from rcp.core.models import (
    HIDDEN_READ_WRAPPER_ROOT,
    HiddenReadKeyEvidence,
    HiddenReadScope,
    HiddenReadStatus,
)
from rcp.limits import (
    HIDDEN_READ_MACHINE_FOLDER_MAX_COUNT,
    HIDDEN_READ_PATH_MAX_COUNT,
    HIDDEN_READ_PATH_MAX_LENGTH,
    HIDDEN_READ_READINESS_TTL_SECONDS,
)
from rcp.providers import PROVIDER_IDS, AgentCapability, ProviderId, profile_for
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
    login_home_credentials: tuple[str, ...] = (),
    thread_owner: str | None = None,
    member_ids: Sequence[str] = (),
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Credential roots name provider stores, never a deploy-key parent.

    `login_home_credentials` are globs relative to the added-login homes roots.
    A provider whose process reads its own home hides only these; for every
    other provider the roots are hidden whole.
    """
    directories = list(credential_roots)
    directories.append(home + "/" + HIDDEN_READ_WRAPPER_ROOT)
    directories.append(home + "/.config/rcp/logins")
    files = [*provider_login_files, home + "/.config/rcp/claude-setup-token"]
    globs = []
    login_homes = [home + "/.rcp/" + LOGIN_HOMES]
    if app_data_dir:
        login_homes.append(app_data_dir + "/" + LOGIN_HOMES)
    if login_home_credentials:
        globs.extend(
            glob.escape(root) + "/" + pattern
            for root in login_homes
            for pattern in login_home_credentials
        )
    else:
        directories.extend(login_homes)
    for path in WEBKIT_READ_DENY_PATHS:
        (files if path.endswith(".binarycookies") else directories).append(home + path[1:])
    if control_socket_dir:
        directories.append(control_socket_dir)
    if app_data_dir:
        # Globs vary only their last component, so a Linux wrapper can hide
        # future matches too; wholly secret trees are plain directories.
        # Each provider's credential namespace and the command-mailbox checkpoints,
        # never the Git identities beside them.
        directories.extend(app_data_dir + "/providers/" + provider for provider in PROVIDER_IDS)
        directories.extend(
            (
                app_data_dir + "/providers/command-mailboxes",
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
    thread_roots = [home + "/.rcp/agent-threads"]
    if app_data_dir:
        thread_roots.append(app_data_dir + "/agent-threads")
    for root in thread_roots:
        if thread_owner is None:
            directories.append(root)
        else:
            from rcp.agents.memory_files import member_memory_root

            # Reuse the member-id validator; identity comes from the thread row.
            member_memory_root(PurePosixPath(root), thread_owner)
            for member in sorted(set(member_ids) - {thread_owner}):
                member_memory_root(PurePosixPath(root), member)
                directories.append(root + "/" + member)
    return tuple(sorted(set(directories))), tuple(sorted(set(files))), tuple(sorted(set(globs)))


class HiddenFolderRejected(ValueError):
    def __init__(self, *, code: str, path: str = "") -> None:
        self.code = code
        self.path = path
        super().__init__(f"{code}: {path}")


def member_hidden_read_entries(
    *,
    memory_root: Path | PurePosixPath,
    state_root: Path | PurePosixPath,
    user_id: str | None,
    member_ids: Sequence[str],
    existing_path_count: int = 0,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Build private-note directories and transcript globs on the execution host.

    Thread launch owners supply the member inventory and the number of already
    resolved hidden entries so private data shares the existing path budget.
    No member identity means that all members' private data stays hidden.
    """
    from rcp.agents.memory_files import member_memory_root

    if existing_path_count < 0:
        raise ValueError("existing hidden path count must not be negative")
    directories = []
    globs = []
    for member_id in sorted(set(member_ids)):
        directory = str(member_memory_root(memory_root, member_id))
        if member_id == user_id:
            continue
        _absolute(directory)
        directories.append(directory)
        prefix = str(state_root / "chat" / f"private-{member_id}-")
        _absolute(prefix)
        globs.append(glob.escape(prefix) + "*.jsonl")
    if existing_path_count + len(directories) + len(globs) > HIDDEN_READ_PATH_MAX_COUNT:
        raise HiddenFolderRejected(code="too_many_hidden_folders")
    return tuple(directories), tuple(globs)


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
    if len(folders) > HIDDEN_READ_MACHINE_FOLDER_MAX_COUNT:
        raise HiddenFolderRejected(code="too_many_hidden_folders")
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
    machine_provider_paths: Sequence[str] = (),
    thread_owner: str | None = None,
    member_ids: Sequence[str] = (),
) -> HiddenReadScope:
    machine = manifest.machine_map[execution_machine]
    return resolve_machine_hidden_read_scope(
        machine=machine,
        execution_machine=execution_machine,
        provider=provider,
        capability=capability,
        stage_root=stage_root,
        workspace_root=workspace_root,
        app_data_dir=app_data_dir,
        remote_stage=remote_stage,
        repository_inventory=repository_inventory,
        machine_hidden_folders=machine_hidden_folders,
        key_evidence=key_evidence,
        browser_enabled=browser_enabled,
        machine_provider_paths=machine_provider_paths,
        thread_owner=thread_owner,
        member_ids=member_ids,
        repository_paths=tuple(
            item.path for item in manifest.repositories if item.machine == execution_machine
        ),
        project_transcript_roots=(str(manifest.research_dir / "chat"),)
        if remote_stage is None
        else (),
    )


def resolve_machine_hidden_read_scope(
    *,
    machine: MachineConfig,
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
    machine_provider_paths: Sequence[str] = (),
    thread_owner: str | None = None,
    member_ids: Sequence[str] = (),
    repository_paths: tuple[str, ...] = (),
    project_transcript_roots: tuple[str, ...] = (),
) -> HiddenReadScope:
    """Resolve account policy independently of whether a project admits writes."""
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
        thread_owner=thread_owner,
        member_ids=member_ids,
        credential_roots=(),
        provider_login_files=tuple(facts["provider_login_files"]),
        control_socket_dir=str(control_directory_candidate()) if remote_stage is None else None,
        # A wildcard keeps a home added after this launch covered. Codex expands
        # deny globs with ripgrep, which anchors `*/name` to its own working
        # directory; `**/name` matches wherever the run starts.
        login_home_credentials=tuple(
            f"{provider}/**/{glob.escape(name)}"
            for name in profile_for(provider).login_home_credentials
        ),
    )
    # Private history is display data. Fresh thread sessions receive an explicit
    # bounded prompt seed, so even their own transcript need not be tool-readable.
    transcript_roots = {
        str(PurePosixPath(item.path) / ".research" / "chat")
        for item in repository_inventory
        if item.execution_host == machine.host
    }
    transcript_roots.update(project_transcript_roots)
    transcript_roots.update(
        str(PurePosixPath(path) / ".research" / "chat") for path in repository_paths
    )
    if remote_stage is None and data_dir:
        transcript_roots.update(
            str(path / ".research" / "chat")
            for path in (Path(data_dir) / "state-cache").glob("*")
            if path.is_dir()
        )
    globs += tuple(glob.escape(root) + "/private-*.jsonl" for root in sorted(transcript_roots))
    if len(set((*directories, *files, *globs))) > HIDDEN_READ_PATH_MAX_COUNT:
        raise HiddenFolderRejected(code="too_many_hidden_folders")
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
        *repository_paths,
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
        *(str(PurePosixPath(path).parent) for path in machine_provider_paths),
    ]
    # A saved folder can come to cover a required path later (a provider moved
    # in, a checkout appeared). Drop only that folder; the defaults stay hidden.
    requested = list(dict.fromkeys(canonical[path] for path in machine_hidden_folders))
    folders = _validated_folders(
        [folder for folder in requested if not any(_overlap(folder, r) for r in protected)], ()
    )
    reasons = set()
    if len(folders) != len(requested):
        reasons.add("hidden_folder_conflict")
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
    absent = {canonical[path] for path in resolved["absent"]}
    glob_folders = {
        glob.escape(canonical[prefix]) + suffix: canonical[prefix]
        for pattern in globs
        for prefix, suffix in [_glob_prefix(pattern)]
    }
    globs = tuple(sorted(glob_folders))
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
        absent_entries=tuple(
            sorted(
                {
                    *(path for path in (*filtered_dirs, *filtered_files) if path in absent),
                    *(pattern for pattern in filtered_globs if glob_folders[pattern] in absent),
                }
            )
        ),
        enforcement=HiddenReadStatus(
            status="unhidden" if reasons else "enforced", reasons=tuple(sorted(reasons))
        ),
    )
