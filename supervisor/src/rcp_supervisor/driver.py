"""Installed operator commands and systemd startup admission for the supervisor."""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Callable
from pathlib import Path

from rcp_supervisor import __version__
from rcp_supervisor.bootstrap import bootstrap
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.events import EventEmitter
from rcp_supervisor.install import install_operator_console, install_supervisor
from rcp_supervisor.launch import read_selected_receipt, validate_selected_receipt
from rcp_supervisor.operations import Coordinator, OperationBusy, OperationStore
from rcp_supervisor.releases import VerifiedRelease, fetch_release
from rcp_supervisor.runtime import (
    DEFAULT_PATHS,
    Paths,
    SystemRuntime,
    _read_file,
    _sync,
    read_config,
    selected_release,
    write_root_json,
)


def store_for(paths: Paths) -> OperationStore:
    return OperationStore(
        paths.operations, paths.releases_root, status_path=paths.supervisor / "status.json"
    )


def selected_pointer(paths: Paths) -> dict:
    return selected_release(paths)


def _require_initialized(runtime: SystemRuntime, selected: dict) -> None:
    inspection = runtime.application(
        selected, "inspect", {"version": 1, "data_dir": str(runtime.paths.data_dir)}
    )
    if inspection.get("status") != "initialized_team":
        raise SupervisorError(
            "Application startup requires a completed team initialization or restore."
        )


def recover(
    *,
    paths: Paths = DEFAULT_PATHS,
    startup: bool = False,
    boundary: Callable[[str], None] | None = None,
) -> dict | None:
    """Finish interrupted deployment locally before systemd starts the application.

    A live coordinator can hold the lock while waiting for systemd's ExecStartPre.
    Only its durable chosen release can pass that guard. Lock contention is never
    a general permission to start, and corrupt journals still fail closed.
    """
    runtime = SystemRuntime(paths, startup=startup, allow_legacy_config=True)
    store = store_for(paths)
    try:
        if os.path.lexists(paths.supervisor / "adoption.json"):
            from rcp_supervisor import migration

            with store.locked():
                adoption = migration.recover(runtime)
                if adoption is not None:
                    return {"kind": "migration", "phase": adoption["phase"]}
        result = Coordinator(store, runtime, boundary=boundary).recover(startup=startup)
    except OperationBusy:
        if not startup:
            raise
        record = store.active()
        if record is None and os.path.lexists(paths.supervisor / "adoption.json"):
            from rcp_supervisor import migration

            if migration.startup_allowed(runtime):
                return {"kind": "migration", "phase": "chosen"}
        selected = selected_pointer(paths)
        if record is not None:
            if record["phase"] not in {"candidate_chosen", "previous_chosen"}:
                raise SupervisorError(
                    "A live deployment has not chosen a safe startup release."
                ) from None
            choice = (
                record["target"] if record["phase"] == "candidate_chosen" else record["previous"]
            )
            if selected != choice:
                raise SupervisorError(
                    "The live deployment choice is not yet selected for startup."
                ) from None
        _require_initialized(runtime, selected)
        return record
    selected = selected_pointer(paths)
    if startup:
        _require_initialized(runtime, selected)
    return result


def _root_directory(path: Path, *, mode: int) -> None:
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        pass
    else:
        # The installed wrapper uses umask 077; newly created shared metadata
        # directories still need their exact service-readable mode.
        path.chmod(mode)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != mode:
        raise SupervisorError("Supervisor storage has unsafe ownership or permissions.")


def followed_release(runtime: SystemRuntime) -> VerifiedRelease:
    destination = runtime.paths.supervisor / "bundles"
    _root_directory(destination, mode=0o755)
    selector = runtime.config.get("release", {}).get("pin") or "stable"
    release = fetch_release(selector, destination / str(uuid.uuid4()))
    # These are public promoted assets. Service-account installation must read
    # them, while only the root coordinator can alter the origin-bound bundle.
    os.chmod(release.directory, 0o755)
    for asset in release.directory.iterdir():
        os.chmod(asset, 0o644)
    return release


def release_receipt(release: VerifiedRelease, paths: Paths) -> dict:
    if release.release_tag is None or release.full_commit is None:
        raise SupervisorError(
            "Installed releases require verified promoted origin and full commit identity."
        )
    return validate_selected_receipt(
        {
            "version": 1,
            "release_tag": release.release_tag,
            "version_string": release.version,
            "build": release.build,
            "commit": release.full_commit,
            "manifest_sha256": release.manifest_sha256,
            "release_directory": str(paths.releases_root / str(release.build)),
            "supervisor_version": release.supervisor_version,
        },
        releases_root=paths.releases_root,
    )


def prepare_release(runtime: SystemRuntime, release: VerifiedRelease) -> dict:
    receipt = release_receipt(release, runtime.paths)
    install_operator_console(release.directory, runtime.paths.supervisor)
    inventory = runtime.paths.supervisor / "release-receipts"
    _root_directory(inventory, mode=0o755)
    sealed = inventory / f"{release.build}.json"
    target = Path(receipt["release_directory"])
    if os.path.lexists(sealed):
        existing = read_selected_receipt(sealed, releases_root=runtime.paths.releases_root)
        if existing != receipt:
            raise SupervisorError("This installed build already names another verified release.")
    else:
        if os.path.lexists(target):
            raise SupervisorError(
                "An unsealed build directory already exists; inspect retained installation diagnostics before retrying."
            )
        installed = runtime.filesystem(
            "install",
            {"bundle": str(release.directory), "releases_root": str(runtime.paths.releases_root)},
        )
        if installed.get("release_directory") != str(target):
            raise SupervisorError("The service account installed a different release directory.")
        runtime.require_capability(receipt)
        write_root_json(sealed, receipt)
    runtime.require_capability(receipt)
    return receipt


def _version_tuple(version: str) -> tuple[int, ...]:
    try:
        values = tuple(int(part) for part in version.split("."))
    except (ValueError, AttributeError) as exc:
        raise SupervisorError("Supervisor version is invalid.") from exc
    if len(values) != 3:
        raise SupervisorError("Supervisor version is invalid.")
    return values


def _require_supervisor(release: VerifiedRelease) -> None:
    if _version_tuple(release.supervisor_version) > _version_tuple(__version__):
        raise SupervisorError(
            "Update the independent supervisor from the followed release before updating RCP."
        )


def update(arguments, emitter: EventEmitter, *, paths: Paths = DEFAULT_PATHS) -> int:
    recover(paths=paths)
    runtime = SystemRuntime(paths)
    previous = selected_pointer(paths)
    release = followed_release(runtime)
    _require_supervisor(release)
    target = release_receipt(release, paths)
    if target == previous:
        emitter.emit(
            "succeeded", "The selected application already matches the followed promoted release."
        )
        return 0
    confirmation = f"{target['release_tag']}:{target['manifest_sha256']}"
    if arguments.confirm_target != confirmation:
        emitter.emit(
            "operator_action_needed",
            "Confirm this exact promoted release before closing admission.",
            actions=[
                {
                    "kind": "external",
                    "instruction": "Review the displayed release identity and rerun the exact confirmation command to authorize deployment.",
                }
            ],
            fields=[
                {"name": "release", "value": target["release_tag"]},
                {"name": "build", "value": target["build"]},
                {"name": "commit", "value": target["commit"]},
                {"name": "manifest_sha256", "value": target["manifest_sha256"]},
            ],
            resume_argv=["sudo", "rcp", "server", "update", "--confirm-target", confirmation],
        )
        return 3
    runtime.require_capability(previous)
    target = prepare_release(runtime, release)
    result = Coordinator(store_for(paths), runtime).deploy(previous, target)
    emitter.emit(
        "succeeded",
        "The verified release is serving after protected backup and fenced validation.",
        fields=[
            {"name": "build", "value": target["build"]},
            {"name": "phase", "value": result["phase"]},
        ],
    )
    return 0


def _emit_adoption(adoption: dict, emitter: EventEmitter) -> int:
    committed = adoption["phase"] == "committed"
    fields = [
        {"name": "phase", "value": adoption["phase"]},
        {"name": "legacy_backup", "value": adoption["legacy_backup"]},
    ]
    if adoption["protected_backup"] is not None:
        fields.extend(
            {"name": name, "value": value} for name, value in adoption["protected_backup"].items()
        )
    emitter.emit(
        "succeeded" if committed else "failed",
        "The source installation was adopted with verified checkpoints and supervised startup."
        if committed
        else "The interrupted adoption recovered the previous source installation.",
        fields=fields,
    )
    return 0 if committed else 1


def install(arguments, emitter: EventEmitter, *, paths: Paths = DEFAULT_PATHS) -> int:
    runtime = SystemRuntime(paths, allow_legacy_config=True)
    _root_directory(paths.operations, mode=0o700)
    with store_for(paths).locked():
        if os.path.lexists(paths.supervisor / "adoption.json"):
            from rcp_supervisor import migration

            adoption = migration.recover(runtime, for_install=True)
            runtime = SystemRuntime(paths, allow_legacy_config=True)
            if adoption is not None:
                return _emit_adoption(adoption, emitter)
        if store_for(paths).active() is not None:
            raise SupervisorError(
                "Recover the unfinished deployment before converging installation."
            )
        if os.path.lexists(paths.selected):
            selected = selected_pointer(paths)
            bundle = Path(selected["release_directory"]) / "assets"
        else:
            release = followed_release(runtime)
            _require_supervisor(release)
            selected = prepare_release(runtime, release)
            bundle = release.directory
            if os.path.lexists(paths.current):
                if runtime.config["schema_version"] != 3:
                    from rcp_supervisor import migration

                    adoption = migration.adopt(runtime, selected)
                    return _emit_adoption(adoption, emitter)
                pointer = paths.current.lstat()
                if (
                    not stat.S_ISLNK(pointer.st_mode)
                    or pointer.st_uid != 0
                    or os.readlink(paths.current) != selected["release_directory"]
                ):
                    raise SupervisorError(
                        "An existing source installation requires explicit supervised adoption."
                    )
            else:
                temporary = paths.current.with_name(f".current-{uuid.uuid4().hex}")
                try:
                    temporary.symlink_to(selected["release_directory"])
                    os.replace(temporary, paths.current)
                    _sync(paths.current.parent)
                finally:
                    temporary.unlink(missing_ok=True)
            runtime.select(selected)
        result = bootstrap(bundle, team_name=arguments.team_name, release=selected, paths=paths)
        write_root_json(
            paths.supervisor / "status.json",
            {
                "version": 1,
                "operation_id": None,
                "kind": "install",
                "phase": "ready" if result["status"] == "ready" else "team_space_init",
                "previous_build": None,
                "target_build": selected["build"],
                "error": None,
                "active": False,
            },
        )
        if result["status"] != "ready":
            emitter.emit(
                "operator_action_needed",
                "Initialize the team space from the operator terminal, then rerun install.",
                actions=result["actions"],
                resume_argv=result["resume_argv"],
            )
            return 3
        runtime._systemctl("enable")
        runtime.start_service()
    emitter.emit(
        "succeeded",
        "The selected application is enabled and serving under supervisor startup recovery. "
        "Optional: to let members connect phones and other devices, put the server on a "
        "tailnet and set [team] access_url in /etc/rcp/server.toml; docs/device-pairing.md "
        "walks through it.",
    )
    return 0


def restore(arguments, emitter: EventEmitter, *, paths: Paths = DEFAULT_PATHS) -> int:
    recover(paths=paths)
    from rcp_supervisor.restore import RestoreOperatorAction

    request = {
        "archive_path": str(arguments.archive_path),
        "identity_file": str(
            arguments.identity_file or paths.config.parent / "backup-recovery.agekey"
        ),
        "confirmed_data_dir": arguments.confirm_data_dir,
        "confirmed_by": os.environ.get("SUDO_USER") or "root",
        "old_authority_disposition": arguments.old_authority_disposition,
        "confirm_old_authority": arguments.confirm_old_authority,
        "confirm_member_roster": arguments.confirm_member_roster,
        "remove_stale_member": arguments.remove_stale_member,
    }
    if request["confirmed_data_dir"] != str(paths.data_dir):
        emitter.emit(
            "operator_action_needed",
            "Restore replaces the installed application data; confirm the exact data directory.",
            actions=[
                {
                    "kind": "external",
                    "instruction": "Review the installed data directory and authorize its replacement with this archive by rerunning the confirmation command.",
                }
            ],
            fields=[{"name": "data_dir", "value": str(paths.data_dir)}],
            resume_argv=[
                "sudo",
                "rcp",
                "server",
                "restore",
                str(arguments.archive_path),
                "--confirm-data-dir",
                str(paths.data_dir),
                *(
                    ["--identity-file", str(arguments.identity_file)]
                    if arguments.identity_file is not None
                    else []
                ),
            ],
        )
        return 3
    runtime = SystemRuntime(paths, restore_request=request)
    selected = selected_pointer(paths)
    runtime.require_capability(selected)
    inspection = runtime.application(
        selected, "inspect", {"version": 1, "data_dir": str(paths.data_dir)}
    )
    if inspection.get("status") not in {"initialized_team", "uninitialized"}:
        raise SupervisorError("The installed application did not prove its restore starting state.")
    runtime._systemctl("enable")
    try:
        result = Coordinator(store_for(paths), runtime).deploy(
            selected,
            selected,
            kind="restore",
            previous_uninitialized=inspection["status"] == "uninitialized",
        )
    except RestoreOperatorAction as exc:
        emitter.emit("operator_action_needed", str(exc), **exc.details)
        return 3
    emitter.emit(
        "succeeded",
        "The restored application passed fenced verification and is serving.",
        fields=[{"name": "phase", "value": result["phase"]}],
    )
    return 0


def supervisor_update(arguments, emitter: EventEmitter, *, paths: Paths = DEFAULT_PATHS) -> int:
    del arguments
    recover(paths=paths)
    runtime = SystemRuntime(paths)
    with store_for(paths).locked():
        if store_for(paths).active() is not None:
            raise SupervisorError(
                "Recover the application operation before updating its supervisor."
            )
        release = followed_release(runtime)
        minimum = max(
            _version_tuple(__version__),
            _version_tuple(selected_pointer(paths)["supervisor_version"]),
        )
        if _version_tuple(release.supervisor_version) < minimum:
            raise SupervisorError(
                "The followed release would downgrade the installed recovery supervisor; keep the current supervisor when pinning an older application."
            )
        target = install_supervisor(release.directory, paths.supervisor)
        pointer = paths.supervisor / "current"
        info = pointer.lstat()
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            raise SupervisorError("The installed supervisor pointer is not root-owned.")
        temporary = pointer.with_name(f".supervisor-{uuid.uuid4().hex}")
        try:
            temporary.symlink_to(target)
            os.replace(temporary, pointer)
            _sync(pointer.parent)
        finally:
            temporary.unlink(missing_ok=True)
    emitter.emit(
        "succeeded",
        f"Supervisor {release.supervisor_version} is selected independently of the application.",
    )
    return 0


def doctor(arguments, emitter: EventEmitter, *, paths: Paths = DEFAULT_PATHS) -> int:
    del arguments
    read_config(paths)
    selected = selected_pointer(paths)
    fields = [
        {"name": "build", "value": selected["build"]},
        {"name": "commit", "value": selected["commit"]},
        {"name": "supervisor_version", "value": __version__},
    ]
    fields.extend(_status_fields(_status_projection(paths)))
    emitter.emit(
        "succeeded",
        "Installed release authority and the supervisor status projection are readable.",
        fields=fields,
    )
    return 0


def _status_projection(paths: Paths) -> dict | None:
    projection = paths.supervisor / "status.json"
    if not projection.exists():
        return None
    status = json.loads(_read_file(projection, uid=0, max_bytes=65536))
    if (
        not isinstance(status, dict)
        or status.get("version") != 1
        or type(status.get("active")) is not bool
    ):
        raise SupervisorError("The supervisor status projection is invalid.")
    return status


def _status_fields(status: dict | None) -> list[dict]:
    if status is None:
        return []
    return [
        {"name": "deployment_phase", "value": str(status.get("phase", "unknown"))},
        {"name": "deployment_active", "value": status["active"]},
    ]


def safe_state_fields(paths: Paths = DEFAULT_PATHS) -> list[dict]:
    """Name the retained deployment state at a failure breakpoint; never raise there."""
    try:
        return _status_fields(_status_projection(paths))
    except (SupervisorError, OSError, ValueError):
        return []
