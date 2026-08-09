from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
from concurrent.futures import Future
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter

from rcp.agents import AgentLauncher
from rcp.config import Manifest, load_manifest
from rcp.core.models import GraphState
from rcp.history import HistoryManager
from rcp.limits import PROJECT_DISPLAY_SNAPSHOT_MAX_BYTES
from rcp.paper import PaperService
from rcp.provider_skills import ProviderSkillInventoryManager
from rcp.providers import PROVIDER_IDS, ProviderId
from rcp.service import ProjectService, ProjectSettingsRequest
from rcp.storage import AppStore, ProjectRecord, ProjectStageRecord
from rcp.transport import RemoteRunStage, prepare_state_workspace

_DISPLAY_SNAPSHOT_SCHEMA_VERSION = 1
_DISPLAY_SNAPSHOT_ENVELOPE_ADAPTER = TypeAdapter(dict[str, object])
_DISPLAY_SNAPSHOT_FIELDS = {
    "id",
    "name",
    "revision",
    "state_repository",
    "canonical_state",
    "run_on",
    "project_truth_scope",
    "default_run_truth_scope",
    "repositories",
    "machines",
    "primary_question",
    "last_refresh_at",
    "counts",
    "coverage",
    "graph",
    "paper",
    "paper_coach",
    "agent_profiles",
    "provider_readiness",
    "provider_skill_inventories",
    "providers",
    "cache_metrics",
    "validation_messages",
}


class ProjectDeletionResult(BaseModel):
    project_id: str
    database_records: dict[str, int]
    removed_stages: int
    removed_display_snapshot: bool
    removed_paper_snapshot: bool


class ProjectCatalog:
    def __init__(
        self,
        data_dir: Path,
        store: AppStore,
        launcher: AgentLauncher,
        provider_skills: ProviderSkillInventoryManager | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.store = store
        self.launcher = launcher
        self.provider_skills = provider_skills
        self._services: dict[str, ProjectService] = {}
        self._services_lock = threading.Lock()
        self._opening: dict[str, Future[tuple[ProjectService, GraphState]]] = {}
        self._deleting: set[str] = set()
        self._snapshot_locks: dict[str, threading.Lock] = {}
        self._snapshot_generations: dict[str, int] = {}
        self._committed_snapshot_generations: dict[str, int] = {}

    def register(self, locator: str) -> ProjectRecord:
        manifest = load_manifest(locator)
        canonical_locator = str(manifest.path)
        existing = self.store.project_by_locator(canonical_locator)
        project_id = existing.project_id if existing else _project_id(manifest)
        state_repository = manifest.repository_map[manifest.state.repository]
        state_machine = manifest.machine_map[state_repository.machine]
        state_location = (
            f"{state_machine.host}:{state_repository.path}/.research"
            if state_machine.host
            else str(manifest.research_dir)
        )
        record = ProjectRecord(
            project_id=project_id,
            locator=canonical_locator,
            name=manifest.name,
            state_location=state_location,
            state_remote=bool(state_machine.host),
            added_at=existing.added_at if existing else self.store.now(),
            last_opened_at=existing.last_opened_at if existing else None,
            revision=existing.revision if existing else None,
            primary_question=existing.primary_question if existing else None,
            attention_count=existing.attention_count if existing else 0,
            last_refresh_at=existing.last_refresh_at if existing else None,
            reachable=existing.reachable if existing else None,
            error=existing.error if existing else None,
        )
        return self.store.upsert_project(record)

    def cards(self) -> list[dict[str, object]]:
        return [self._card(record) for record in self.store.projects()]

    def card(self, project_id: str) -> dict[str, object]:
        record = self.store.project(project_id)
        if record is None:
            raise KeyError(project_id)
        return self._card(record)

    def state_host(self, project_id: str) -> str:
        """Read the registered state host without opening canonical history."""

        record = self.store.project(project_id)
        if record is None:
            raise KeyError(project_id)
        manifest = load_manifest(record.locator)
        repository = manifest.repository_map[manifest.state.repository]
        return manifest.machine_map[repository.machine].host

    def open(self, project_id: str) -> ProjectService:
        service, _ = self._service_or_open(project_id)
        return service

    def open_snapshot(self, project_id: str) -> tuple[ProjectService, dict[str, object]]:
        service, initialized_state = self._service_or_open(project_id)
        return service, service.project_snapshot(state=initialized_state)

    def _service_or_open(
        self,
        project_id: str,
    ) -> tuple[ProjectService, GraphState | None]:
        """Open once per project while leaving snapshot work outside the lock."""

        with self._services_lock:
            if project_id in self._deleting:
                raise KeyError(project_id)
            cached = self._services.get(project_id)
            if cached is not None:
                return cached, None
            opening = self._opening.get(project_id)
            owner = opening is None
            if opening is None:
                opening = Future()
                self._opening[project_id] = opening

        if not owner:
            return opening.result()

        try:
            result = self._open_service(project_id)
        except BaseException as exc:
            with self._services_lock:
                self._opening.pop(project_id, None)
            opening.set_exception(exc)
            raise

        with self._services_lock:
            deleting = project_id in self._deleting
            if not deleting:
                self._services[project_id] = result[0]
            self._opening.pop(project_id, None)
            if not deleting:
                opening.set_result(result)
        if deleting:
            error = KeyError(project_id)
            opening.set_exception(error)
            raise error
        return result

    def readiness_snapshot(
        self,
        project_id: str,
        *,
        refresh: bool = False,
    ) -> dict[str, object]:
        with self._services_lock:
            cached = self._services.get(project_id)
        if cached is not None:
            return cached.readiness_snapshot(refresh=refresh)
        record = self.store.project(project_id)
        if record is None:
            raise KeyError(project_id)
        manifest = load_manifest(record.locator)
        snapshot = ProjectService.readiness_for(manifest, self.launcher, refresh=refresh)
        ProjectService.wait_for_provider_skill_inventories_for(manifest, self.provider_skills)
        snapshot["provider_skill_inventories"] = ProjectService.provider_skill_inventories_for(
            manifest,
            self.provider_skills,
        )
        return snapshot

    def provider_targets(self) -> list[tuple[ProviderId, str, str | None]]:
        """Unique configured provider capabilities known to this app process."""

        targets: set[tuple[ProviderId, str, str | None]] = set()
        for record in self.store.projects():
            try:
                manifest = load_manifest(record.locator)
            except (FileNotFoundError, OSError, ValueError):
                continue
            for machine in manifest.machines:
                for provider in PROVIDER_IDS:
                    targets.add((provider, machine.host, machine.provider_paths.get(provider)))
        return sorted(targets, key=lambda item: (item[1], item[0], item[2] or ""))

    def delete(self, project_id: str) -> ProjectDeletionResult:
        """Forget one RCP registration without touching any research source."""
        with self._services_lock:
            if self.store.project(project_id) is None or project_id in self._deleting:
                raise KeyError(project_id)
            self._deleting.add(project_id)
            self._services.pop(project_id, None)
            opening = self._opening.get(project_id)
        try:
            if opening is not None:
                with suppress(Exception):
                    opening.result()
            with self._snapshot_lock(project_id):
                stages = self.store.project_deletion_stages(project_id)
                for stage in stages:
                    self._remove_stage(stage)

                display_snapshot = self._cached_snapshot_path(project_id)
                paper_snapshot = self._paper_snapshot_path(project_id)
                removed_display = _unlink_regular_app_file(display_snapshot)
                removed_paper = _unlink_regular_app_file(paper_snapshot)
                self._snapshot_generations.pop(project_id, None)
                self._committed_snapshot_generations.pop(project_id, None)
                database_records = self.store.delete_project_records(project_id)
            return ProjectDeletionResult(
                project_id=project_id,
                database_records=database_records,
                removed_stages=len(stages),
                removed_display_snapshot=removed_display,
                removed_paper_snapshot=removed_paper,
            )
        finally:
            with self._services_lock:
                self._deleting.discard(project_id)

    def _snapshot_lock(self, project_id: str) -> threading.Lock:
        with self._services_lock:
            return self._snapshot_locks.setdefault(project_id, threading.Lock())

    def _is_deleting(self, project_id: str) -> bool:
        with self._services_lock:
            return project_id in self._deleting

    def _remove_stage(self, stage: ProjectStageRecord) -> None:
        if stage.host:
            remote = RemoteRunStage(stage.host).attach_artifact_source(stage.root)
            if not remote.close():
                raise RuntimeError(
                    f"Could not remove saved run stage {stage.root!r} on {stage.host!r}; "
                    "the project was not deleted."
                )
            return

        boundary = (self.data_dir / "run-stage").resolve()
        target = Path(stage.root)
        if not target.is_absolute() or target.parent.resolve() != boundary:
            raise ValueError("Saved local run stage is outside the RCP staging boundary")
        _remove_local_stage(target)

    def write_cached_snapshot(
        self,
        project_id: str,
        snapshot: dict[str, object],
    ) -> None:
        with self._snapshot_lock(project_id):
            if self._is_deleting(project_id) or self.store.project(project_id) is None:
                raise KeyError(project_id)
            self._write_cached_snapshot_locked(project_id, snapshot)

    def commit_cached_snapshot(
        self,
        project_id: str,
        snapshot: dict[str, object],
        *,
        generation: int,
    ) -> bool:
        """Commit a display snapshot unless a newer project view already won."""

        with self._snapshot_lock(project_id):
            if self._is_deleting(project_id):
                raise KeyError(project_id)
            record = self.store.project(project_id)
            if record is None:
                raise KeyError(project_id)
            if not _valid_display_snapshot(project_id, snapshot):
                raise ValueError("Project display snapshot is invalid")
            if generation < 1 or generation > self._snapshot_generations.get(project_id, 0):
                raise ValueError("Project display snapshot generation is invalid")
            cached = self._cached_snapshot_locked(project_id)
            persisted_revisions = [
                revision
                for revision in (
                    record.revision,
                    int(cached["revision"]) if cached is not None else None,
                )
                if revision is not None
            ]
            candidate_revision = int(snapshot["revision"])
            if persisted_revisions:
                persisted_revision = max(persisted_revisions)
                if candidate_revision < persisted_revision:
                    return False
                if (
                    candidate_revision == persisted_revision
                    and generation < self._committed_snapshot_generations.get(project_id, 0)
                ):
                    return False
            self._write_cached_snapshot_locked(project_id, snapshot)
            self._committed_snapshot_generations[project_id] = max(
                generation,
                self._committed_snapshot_generations.get(project_id, 0),
            )
            self.update_summary(project_id, snapshot)
            return True

    def reserve_cached_snapshot_generation(self, project_id: str) -> int:
        """Reserve construction order for one future display snapshot candidate."""

        with self._snapshot_lock(project_id):
            if self._is_deleting(project_id) or self.store.project(project_id) is None:
                raise KeyError(project_id)
            generation = self._snapshot_generations.get(project_id, 0) + 1
            self._snapshot_generations[project_id] = generation
            return generation

    def _write_cached_snapshot_locked(
        self,
        project_id: str,
        snapshot: dict[str, object],
    ) -> None:
        if not _valid_display_snapshot(project_id, snapshot):
            raise ValueError("Project display snapshot is invalid")
        envelope = {
            "schema_version": _DISPLAY_SNAPSHOT_SCHEMA_VERSION,
            "project_id": project_id,
            "snapshot": snapshot,
        }
        content = (
            json.dumps(
                _DISPLAY_SNAPSHOT_ENVELOPE_ADAPTER.dump_python(envelope, mode="json"),
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()
        if len(content) > PROJECT_DISPLAY_SNAPSHOT_MAX_BYTES:
            raise ValueError("Project display snapshot exceeds its size limit")

        target = self._cached_snapshot_path(project_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def cached_snapshot(self, project_id: str) -> dict[str, object] | None:
        _status, snapshot = self.cached_snapshot_status(project_id)
        return snapshot

    def cached_snapshot_status(
        self,
        project_id: str,
    ) -> tuple[Literal["missing", "invalid", "valid"], dict[str, object] | None]:
        with self._snapshot_lock(project_id):
            if self._is_deleting(project_id) or self.store.project(project_id) is None:
                return "missing", None
            return self._cached_snapshot_status_locked(project_id)

    def _cached_snapshot_locked(self, project_id: str) -> dict[str, object] | None:
        _status, snapshot = self._cached_snapshot_status_locked(project_id)
        return snapshot

    def _cached_snapshot_status_locked(
        self,
        project_id: str,
    ) -> tuple[Literal["missing", "invalid", "valid"], dict[str, object] | None]:
        path = self._cached_snapshot_path(project_id)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return "missing", None
        except OSError:
            return "invalid", None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > PROJECT_DISPLAY_SNAPSHOT_MAX_BYTES
        ):
            return "invalid", None
        try:
            content = path.read_bytes()
        except OSError:
            return "invalid", None
        if not content or len(content) > PROJECT_DISPLAY_SNAPSHOT_MAX_BYTES:
            return "invalid", None
        try:
            envelope = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return "invalid", None
        if not isinstance(envelope, dict) or set(envelope) != {
            "schema_version",
            "project_id",
            "snapshot",
        }:
            return "invalid", None
        if (
            type(envelope["schema_version"]) is not int
            or envelope["schema_version"] != _DISPLAY_SNAPSHOT_SCHEMA_VERSION
            or envelope["project_id"] != project_id
        ):
            return "invalid", None
        snapshot = envelope["snapshot"]
        if not isinstance(snapshot, dict) or not _valid_display_snapshot(project_id, snapshot):
            return "invalid", None
        return "valid", snapshot

    def loaded_service(self, project_id: str) -> ProjectService | None:
        """Return an already-open service without opening or refreshing it."""

        with self._services_lock:
            return self._services.get(project_id)

    def _cached_snapshot_path(self, project_id: str) -> Path:
        digest = hashlib.sha256(project_id.encode()).hexdigest()
        return self.data_dir / "project-snapshots" / f"{digest}.json"

    def _paper_snapshot_path(self, project_id: str) -> Path:
        safe_project_id = re.sub(r"[^A-Za-z0-9._-]+", "_", project_id).strip("._")
        return (
            self.data_dir
            / "paper-snapshots"
            / (f"{(safe_project_id or 'project')[:80]}-introduction.md")
        )

    def _open_service(self, project_id: str) -> tuple[ProjectService, GraphState]:
        record = self.store.project(project_id)
        if record is None:
            raise KeyError(project_id)
        bootstrap = load_manifest(record.locator)
        manifest, workspace = prepare_state_workspace(bootstrap, self.data_dir)
        history = HistoryManager(manifest, workspace)
        initialized_state = history.initialize().state
        self.store.migrate_legacy_project_data(history.manifest.name, project_id)
        paper = PaperService(
            history.manifest,
            self.store,
            workspace,
            project_id=project_id,
        )
        service = ProjectService(
            history.manifest,
            history,
            paper,
            self.launcher,
            data_dir=self.data_dir,
            provider_skills=self.provider_skills,
        )
        return service, initialized_state

    def update_summary(
        self,
        project_id: str,
        snapshot: dict[str, object],
    ) -> ProjectRecord:
        with self._services_lock:
            if project_id in self._deleting or self.store.project(project_id) is None:
                raise KeyError(project_id)
        primary = snapshot.get("primary_question")
        primary_question = None
        if isinstance(primary, dict):
            primary_question = str(primary.get("question") or primary.get("title") or "") or None
        counts = snapshot["counts"]
        assert isinstance(counts, dict)
        canonical = snapshot["canonical_state"]
        assert isinstance(canonical, dict)
        last_refresh = snapshot.get("last_refresh_at")
        return self.store.update_project_summary(
            project_id,
            revision=int(snapshot["revision"]),
            primary_question=primary_question,
            attention_count=sum(
                int(counts[key])
                for key in (
                    "pending_proposals",
                    "decisions_awaiting_choice",
                    "open_blockers",
                )
            ),
            last_refresh_at=_timestamp(last_refresh),
            reachable=bool(canonical["reachable"]),
            error=str(canonical["error"]) if canonical.get("error") else None,
        )

    def update_settings(
        self,
        project_id: str,
        request: ProjectSettingsRequest,
    ) -> dict[str, object]:
        service = self.open(project_id)
        service.update_settings(request)
        self._persist_bootstrap_locator(project_id, service)
        snapshot = service.project_snapshot()
        self.update_summary(project_id, snapshot)
        return snapshot

    def resolve_provider_path(
        self,
        project_id: str,
        machine_alias: str,
        provider: ProviderId,
    ) -> dict[str, object]:
        service = self.open(project_id)
        readiness = service.resolve_provider_path(machine_alias, provider)
        self._persist_bootstrap_locator(project_id, service)
        snapshot = service.project_snapshot()
        self.update_summary(project_id, snapshot)
        return {
            "machine": machine_alias,
            "provider": provider,
            "binary_path": readiness.binary_path,
            "readiness": readiness.model_dump(mode="json"),
            "project": snapshot,
        }

    def _persist_bootstrap_locator(
        self,
        project_id: str,
        service: ProjectService,
    ) -> None:
        record = self.store.project(project_id)
        assert record is not None
        locator = Path(record.locator)
        if locator.resolve() != service.manifest.path.resolve():
            temp = locator.with_name(f".{locator.name}.{os.getpid()}.tmp")
            temp.write_text(service.manifest.path.read_text(encoding="utf-8"), encoding="utf-8")
            os.replace(temp, locator)

    @staticmethod
    def _card(record: ProjectRecord) -> dict[str, object]:
        return {
            "id": record.project_id,
            "name": record.name,
            "locator": record.locator,
            "state_location": record.state_location,
            "remote": record.state_remote,
            "last_opened_at": record.last_opened_at,
            "revision": record.revision,
            "primary_question": record.primary_question,
            "attention_count": record.attention_count,
            "last_refresh_at": record.last_refresh_at,
            "reachable": record.reachable,
            "error": record.error,
        }


def _project_id(manifest: Manifest) -> str:
    repository = manifest.repository_map[manifest.state.repository]
    machine = manifest.machine_map[repository.machine]
    identity = f"{manifest.name}\0{machine.host}\0{repository.path}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", manifest.name.lower()).strip("-") or "project"
    return f"{slug[:42]}-{digest}"


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _remove_local_stage(target: Path) -> None:
    if not os.path.lexists(target):
        return
    if target.is_symlink() or not target.is_dir():
        target.unlink()
    else:
        _make_tree_writable(target)
        shutil.rmtree(target)
    if os.path.lexists(target):
        raise OSError(f"Local cleanup left {target} behind")


def _make_tree_writable(target: Path) -> None:
    if target.is_symlink():
        return
    target.chmod(0o700 if target.is_dir() else 0o600)
    if target.is_dir():
        for child in target.iterdir():
            _make_tree_writable(child)


def _unlink_regular_app_file(target: Path) -> bool:
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Refusing to remove non-file app snapshot: {target}")
    target.unlink()
    return True


def _valid_display_snapshot(project_id: str, snapshot: dict[str, object]) -> bool:
    if not _DISPLAY_SNAPSHOT_FIELDS.issubset(snapshot):
        return False
    if snapshot.get("id") != project_id or not isinstance(snapshot.get("name"), str):
        return False
    revision = snapshot.get("revision")
    if type(revision) is not int or revision < 0:
        return False
    if not all(
        isinstance(snapshot.get(key), dict)
        for key in (
            "canonical_state",
            "counts",
            "coverage",
            "graph",
            "paper",
            "paper_coach",
            "agent_profiles",
            "provider_readiness",
            "provider_skill_inventories",
            "providers",
            "cache_metrics",
        )
    ):
        return False
    if not all(
        isinstance(snapshot.get(key), list)
        for key in (
            "project_truth_scope",
            "default_run_truth_scope",
            "repositories",
            "machines",
            "validation_messages",
        )
    ):
        return False
    graph = snapshot["graph"]
    assert isinstance(graph, dict)
    graph_revision = graph.get("revision")
    return type(graph_revision) is int and graph_revision == revision


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
