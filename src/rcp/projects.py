from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
from concurrent.futures import Future
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter

from rcp.agents import AgentLauncher
from rcp.attachments import ChatAttachmentStore
from rcp.config import Manifest, load_manifest
from rcp.core.models import GraphState
from rcp.history import HistoryManager, ProjectIdentityConflict
from rcp.limits import PROJECT_DISPLAY_SNAPSHOT_MAX_BYTES
from rcp.paper import PaperService
from rcp.provider_skills import ProviderSkillInventoryManager
from rcp.providers import PROVIDER_IDS, ProviderId
from rcp.service import ProjectService, ProjectSettingsRequest
from rcp.storage import AppStore, ProjectRecord, ProjectStageRecord
from rcp.transport import (
    RemoteRunStage,
    StateUnavailable,
    StateWorkspace,
    prepare_state_workspace,
)
from rcp.transport.state import SSHStateWorkspace, state_workspace_for_probe

_DISPLAY_SNAPSHOT_SCHEMA_VERSION = 2
_DISPLAY_SNAPSHOT_ENVELOPE_ADAPTER = TypeAdapter(dict[str, object])
_PATCH_LOG_HEAD_UNSET = object()
_DISPLAY_SNAPSHOT_FIELDS = {
    "id",
    "home_space_id",
    "name",
    "revision",
    "snapshot_freshness",
    "last_remote_sync_at",
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
        self._cached_snapshot_patch_heads: dict[str, int | None] = {}
        self._candidate_snapshot_patch_heads: dict[str, int | None] = {}
        self._registration_lock = threading.Lock()
        self._project_aliases = self.store.project_aliases()

    def register(
        self,
        locator: str,
        *,
        identity_action: Literal["created", "adopted"] | None = None,
    ) -> ProjectRecord:
        """Register one canonical project after its durable nameplate is settled."""

        with self._registration_lock:
            bootstrap = load_manifest(locator)
            canonical_locator = str(bootstrap.path)
            existing = self.store.project_by_locator(canonical_locator)
            manifest, workspace = prepare_state_workspace(bootstrap, self.data_dir)
            history = self._history_for_manifest(manifest, workspace)
            identity = history.project_identity()
            if identity is None:
                if existing is not None:
                    claim_action: Literal["created", "adopted"] = "adopted"
                elif identity_action is not None:
                    claim_action = identity_action
                else:
                    raise ValueError(
                        "This existing project has no durable identity. Connect it through "
                        "project setup and confirm that this space becomes its sole writable home."
                    )
                identity = history.claim_project_identity(claim_action)
            else:
                # The idempotent claim path also enforces the expected home-space boundary.
                identity = history.claim_project_identity(identity.action)

            if existing is None:
                existing = self.store.project(identity.project_id)

            if existing is not None:
                old_project_id = existing.project_id
            elif identity.action == "adopted":
                old_project_id = _project_id(manifest)
            else:
                old_project_id = identity.project_id

            record = self._record_for_identity(
                bootstrap,
                existing,
                project_id=old_project_id,
                home_space_id=(
                    self.store.space_id if old_project_id == identity.project_id else None
                ),
            )
            if old_project_id == identity.project_id:
                stored = self.store.upsert_project(record)
            else:
                resolved_old = self.store.resolve_project_id(old_project_id)
                if resolved_old != old_project_id:
                    if resolved_old != identity.project_id:
                        raise ValueError(
                            f"Legacy project alias {old_project_id!r} already belongs to "
                            f"{resolved_old!r}."
                        )
                    self._refresh_project_aliases()
                    canonical_record = self._record_for_identity(
                        bootstrap,
                        self.store.project(identity.project_id),
                        project_id=identity.project_id,
                        home_space_id=self.store.space_id,
                    )
                    stored = self.store.upsert_project(canonical_record)
                else:
                    migration = self._prepare_app_file_migration(
                        old_project_id,
                        identity.project_id,
                    )
                    attachment_store = ChatAttachmentStore(self.data_dir / "chat-attachments")
                    attachment_migration = attachment_store.prepare_project_identity_migration(
                        old_project_id,
                        identity.project_id,
                    )
                    self.store.upsert_project(record)
                    stored = self.store.migrate_project_identity(
                        old_project_id,
                        identity.project_id,
                        self.store.space_id,
                    )
                    self._refresh_project_aliases()
                    self._apply_app_file_migration(migration)
                    attachment_store.apply_project_identity_migration(attachment_migration)
                    self._migrate_runtime_keys(old_project_id, identity.project_id)
                stored = self.store.upsert_project(
                    self._record_for_identity(
                        bootstrap,
                        stored,
                        project_id=identity.project_id,
                        home_space_id=self.store.space_id,
                    )
                )
            self._finish_alias_file_migrations(identity.project_id)
            return stored

    def _record_for_identity(
        self,
        manifest: Manifest,
        existing: ProjectRecord | None,
        *,
        project_id: str,
        home_space_id: str | None,
    ) -> ProjectRecord:
        state_repository = manifest.repository_map[manifest.state.repository]
        state_machine = manifest.machine_map[state_repository.machine]
        state_location = (
            f"{state_machine.host}:{state_repository.path}/.research"
            if state_machine.host
            else str(manifest.research_dir)
        )
        return ProjectRecord(
            project_id=project_id,
            home_space_id=home_space_id,
            locator=str(manifest.path),
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

    def resolve_project_id(self, project_id: str) -> str:
        """Resolve a project URL without opening SQLite on the request path."""

        return self._project_aliases.get(project_id, project_id)

    def _canonical_project_id(self, project_id: str) -> str:
        return self.resolve_project_id(project_id)

    def _refresh_project_aliases(self) -> None:
        # Replace the snapshot as one object so concurrent request reads never
        # observe a partially refreshed mapping.
        self._project_aliases = self.store.project_aliases()

    def _history_for_manifest(
        self,
        manifest: Manifest,
        workspace: StateWorkspace,
    ) -> HistoryManager:
        return HistoryManager(
            manifest,
            workspace,
            expected_space_id=self.store.space_id,
            require_attribution=True,
            agent_authorizer_resolver=self.store.agent_task_authorizer,
        )

    def _ensure_registered_identity(self, project_id: str) -> str:
        project_id = self._canonical_project_id(project_id)
        record = self.store.project(project_id)
        if record is None:
            raise KeyError(project_id)
        if record.home_space_id is not None:
            if record.home_space_id != self.store.space_id:
                raise ProjectIdentityConflict(
                    f"Project {project_id} belongs to space {record.home_space_id}; "
                    f"this space is {self.store.space_id}. Canonical writes are refused."
                )
            return record.project_id
        return self.register(record.locator).project_id

    def _stamp_snapshot_identity(
        self,
        snapshot: dict[str, object],
        project_id: str,
    ) -> None:
        project_id = self._canonical_project_id(project_id)
        record = self.store.project(project_id)
        if record is None or record.home_space_id is None:
            raise KeyError(project_id)
        snapshot["id"] = project_id
        snapshot["home_space_id"] = record.home_space_id

    def _finish_alias_file_migrations(self, canonical_project_id: str) -> None:
        for alias_id, destination in self._project_aliases.items():
            if destination != canonical_project_id:
                continue
            migration = self._prepare_app_file_migration(alias_id, canonical_project_id)
            attachment_store = ChatAttachmentStore(self.data_dir / "chat-attachments")
            attachment_migration = attachment_store.prepare_project_identity_migration(
                alias_id,
                canonical_project_id,
            )
            self._apply_app_file_migration(migration)
            attachment_store.apply_project_identity_migration(attachment_migration)
            self._migrate_runtime_keys(alias_id, canonical_project_id)

    def _prepare_app_file_migration(
        self,
        old_project_id: str,
        canonical_project_id: str,
    ) -> list[tuple[Literal["display", "paper"], Path, Path, bytes | None]]:
        migrations: list[tuple[Literal["display", "paper"], Path, Path, bytes | None]] = []
        display_source = self._cached_snapshot_path_for_id(old_project_id)
        display_target = self._cached_snapshot_path_for_id(canonical_project_id)
        if display_source != display_target and display_source.exists():
            _require_regular_app_file(display_source, "legacy display snapshot")
            try:
                envelope = json.loads(display_source.read_bytes())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Legacy project display snapshot is invalid.") from exc
            if not isinstance(envelope, dict) or not isinstance(envelope.get("snapshot"), dict):
                raise ValueError("Legacy project display snapshot is invalid.")
            if envelope.get("project_id") != old_project_id:
                raise ValueError("Legacy project display snapshot names a different project.")
            snapshot = envelope["snapshot"]
            assert isinstance(snapshot, dict)
            if snapshot.get("id") != old_project_id:
                raise ValueError("Legacy project display snapshot names a different project.")
            envelope["project_id"] = canonical_project_id
            snapshot["id"] = canonical_project_id
            snapshot["home_space_id"] = self.store.space_id
            content = (
                json.dumps(envelope, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
            ).encode()
            if len(content) > PROJECT_DISPLAY_SNAPSHOT_MAX_BYTES:
                raise ValueError("Migrated project display snapshot exceeds its size limit.")
            if display_target.exists():
                _require_regular_app_file(display_target, "project display snapshot destination")
                if display_target.read_bytes() != content:
                    raise ValueError(
                        "Project display snapshot migration destination already exists; "
                        "nothing was overwritten."
                    )
            migrations.append(("display", display_source, display_target, content))

        paper_source = self._paper_snapshot_path_for_id(old_project_id)
        paper_target = self._paper_snapshot_path_for_id(canonical_project_id)
        if paper_source != paper_target and paper_source.exists():
            _require_regular_app_file(paper_source, "legacy paper snapshot")
            if paper_target.exists():
                raise ValueError(
                    "Project paper snapshot migration destination already exists; "
                    "nothing was overwritten."
                )
            migrations.append(("paper", paper_source, paper_target, None))
        return migrations

    def _apply_app_file_migration(
        self,
        migrations: list[tuple[Literal["display", "paper"], Path, Path, bytes | None]],
    ) -> None:
        for kind, source, target, content in migrations:
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind == "paper":
                os.replace(source, target)
                _fsync_directory(target.parent)
                continue
            assert content is not None
            if target.exists():
                _require_regular_app_file(target, "project display snapshot destination")
                if target.read_bytes() != content:
                    raise ValueError(
                        "Project display snapshot migration destination already exists; "
                        "nothing was overwritten."
                    )
                source.unlink(missing_ok=True)
                _fsync_directory(source.parent)
                continue
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
                source.unlink()
                _fsync_directory(target.parent)
                if source.parent != target.parent:
                    _fsync_directory(source.parent)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def _migrate_runtime_keys(self, old_project_id: str, canonical_project_id: str) -> None:
        if old_project_id == canonical_project_id:
            return
        with self._services_lock:
            old_service = self._services.pop(old_project_id, None)
            current_service = self._services.get(canonical_project_id)
            if old_service is not None:
                if current_service is not None and current_service is not old_service:
                    raise RuntimeError("Project identity migration found duplicate open services.")
                self._services[canonical_project_id] = old_service
            old_opening = self._opening.pop(old_project_id, None)
            if old_opening is not None:
                if canonical_project_id in self._opening:
                    raise RuntimeError("Project identity migration found duplicate open attempts.")
                self._opening[canonical_project_id] = old_opening
            if old_project_id in self._deleting:
                self._deleting.remove(old_project_id)
                self._deleting.add(canonical_project_id)
            for mapping in (
                self._snapshot_locks,
                self._snapshot_generations,
                self._committed_snapshot_generations,
                self._cached_snapshot_patch_heads,
                self._candidate_snapshot_patch_heads,
            ):
                if old_project_id not in mapping:
                    continue
                old_value = mapping.pop(old_project_id)
                if canonical_project_id in mapping and mapping[canonical_project_id] != old_value:
                    raise RuntimeError(
                        "Project identity migration found conflicting in-memory cache state."
                    )
                mapping[canonical_project_id] = old_value

    def cards(self) -> list[dict[str, object]]:
        return [self._card(record) for record in self.store.projects()]

    def card(self, project_id: str) -> dict[str, object]:
        project_id = self._canonical_project_id(project_id)
        record = self.store.project(project_id)
        if record is None:
            raise KeyError(project_id)
        return self._card(record)

    def state_host(self, project_id: str) -> str:
        """Read the registered state host without opening canonical history."""

        project_id = self._canonical_project_id(project_id)
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
        project_id = self._canonical_project_id(project_id)
        service, initialized_state = self._service_or_open(project_id)
        project_id = self._canonical_project_id(project_id)
        snapshot = service.project_snapshot(state=initialized_state)
        self._stamp_snapshot_identity(snapshot, project_id)
        self.mark_snapshot_fresh(snapshot)
        return service, snapshot

    def reconcile_snapshot(self, project_id: str) -> tuple[ProjectService, dict[str, object]]:
        """Refresh canonical state and build one fresh display-snapshot candidate."""

        project_id = self._canonical_project_id(project_id)
        service, initialized_state = self._service_or_open(project_id)
        project_id = self._canonical_project_id(project_id)
        if initialized_state is None:
            refreshed = service.history.workspace.refresh()
            if service.history.workspace.remote and not refreshed:
                raise StateUnavailable("Remote canonical state has no readable manifest.")
            initialized_state = service.history.materialize(write_outputs=False).state
        snapshot = service.project_snapshot(state=initialized_state)
        self._stamp_snapshot_identity(snapshot, project_id)
        self.mark_snapshot_fresh(snapshot)
        return service, snapshot

    def probe_remote_patch_log_head(
        self,
        project_id: str,
    ) -> Literal["moved", "unchanged", "unavailable"]:
        """Compare canonical and cached patch heads without opening the project."""

        project_id = self._canonical_project_id(project_id)
        with self._services_lock:
            if project_id in self._deleting:
                raise KeyError(project_id)
            service = self._services.get(project_id)
        if service is not None:
            workspace = service.history.workspace
        else:
            record = self.store.project(project_id)
            if record is None:
                raise KeyError(project_id)
            workspace = state_workspace_for_probe(load_manifest(record.locator), self.data_dir)
        if isinstance(workspace, SSHStateWorkspace):
            available, canonical_head = workspace.probe_remote_patch_log_head()
            if not available:
                return "unavailable"
        else:
            canonical_head = workspace.cached_patch_log_head()
        snapshot = self.cached_snapshot(project_id)
        if snapshot is None:
            raise KeyError(project_id)
        return (
            "moved"
            if canonical_head != self._cached_snapshot_patch_heads.get(project_id)
            else "unchanged"
        )

    @staticmethod
    def mark_snapshot_fresh(snapshot: dict[str, object]) -> None:
        canonical = snapshot.get("canonical_state")
        unreachable_remote = (
            isinstance(canonical, dict)
            and canonical.get("remote") is True
            and canonical.get("reachable") is False
        )
        _ensure_snapshot_freshness(
            snapshot,
            freshness="stale" if unreachable_remote else "fresh",
        )

    def _service_or_open(
        self,
        project_id: str,
    ) -> tuple[ProjectService, GraphState | None]:
        """Open once per project while leaving snapshot work outside the lock."""

        project_id = self._ensure_registered_identity(project_id)
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
        project_id = self._canonical_project_id(project_id)
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
        project_id = self._canonical_project_id(project_id)
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
                self._cached_snapshot_patch_heads.pop(project_id, None)
                self._candidate_snapshot_patch_heads.pop(project_id, None)
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
        project_id = self._canonical_project_id(project_id)
        with self._services_lock:
            return self._snapshot_locks.setdefault(project_id, threading.Lock())

    def _is_deleting(self, project_id: str) -> bool:
        project_id = self._canonical_project_id(project_id)
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
        project_id = self._canonical_project_id(project_id)
        self._stamp_snapshot_identity(snapshot, project_id)
        _ensure_snapshot_freshness(snapshot)
        with self._snapshot_lock(project_id):
            if self._is_deleting(project_id) or self.store.project(project_id) is None:
                raise KeyError(project_id)
            self._candidate_snapshot_patch_heads[project_id] = _display_patch_log_head(snapshot)
            try:
                self._write_cached_snapshot_locked(project_id, snapshot)
            finally:
                self._candidate_snapshot_patch_heads.pop(project_id, None)

    def commit_cached_snapshot(
        self,
        project_id: str,
        snapshot: dict[str, object],
        *,
        generation: int,
        patch_log_head: int | None | object = _PATCH_LOG_HEAD_UNSET,
    ) -> bool:
        """Commit a display snapshot unless a newer project view already won."""

        project_id = self._canonical_project_id(project_id)
        self._stamp_snapshot_identity(snapshot, project_id)
        _ensure_snapshot_freshness(snapshot)
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
            if patch_log_head is _PATCH_LOG_HEAD_UNSET:
                patch_log_head = (
                    self._cached_snapshot_patch_heads.get(project_id)
                    if cached is not None and int(cached["revision"]) == candidate_revision
                    else _display_patch_log_head(snapshot)
                )
            if not _valid_patch_log_head(patch_log_head):
                raise ValueError("Project display snapshot patch-log head is invalid")
            if persisted_revisions:
                persisted_revision = max(persisted_revisions)
                if candidate_revision < persisted_revision:
                    return False
                if (
                    candidate_revision == persisted_revision
                    and generation < self._committed_snapshot_generations.get(project_id, 0)
                ):
                    return False
            assert patch_log_head is None or isinstance(patch_log_head, int)
            self._candidate_snapshot_patch_heads[project_id] = patch_log_head
            try:
                self._write_cached_snapshot_locked(project_id, snapshot)
            finally:
                self._candidate_snapshot_patch_heads.pop(project_id, None)
            self._committed_snapshot_generations[project_id] = max(
                generation,
                self._committed_snapshot_generations.get(project_id, 0),
            )
            self.update_summary(project_id, snapshot)
            return True

    def update_cached_snapshot_freshness(
        self,
        project_id: str,
        freshness: Literal["fresh", "reconciling", "stale"],
    ) -> bool:
        """Version one freshness-only cache update through the normal guards."""

        project_id = self._canonical_project_id(project_id)
        current = self.cached_snapshot(project_id)
        if current is None:
            return False
        if current.get("snapshot_freshness") == freshness:
            return True
        generation = self.reserve_cached_snapshot_generation(project_id)
        snapshot = self.cached_snapshot(project_id)
        if snapshot is None:
            return False
        snapshot["snapshot_freshness"] = freshness
        return self.commit_cached_snapshot(project_id, snapshot, generation=generation)

    def reserve_cached_snapshot_generation(self, project_id: str) -> int:
        """Reserve construction order for one future display snapshot candidate."""

        project_id = self._canonical_project_id(project_id)
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
        project_id = self._canonical_project_id(project_id)
        if not _valid_display_snapshot(project_id, snapshot):
            raise ValueError("Project display snapshot is invalid")
        patch_log_head = self._candidate_snapshot_patch_heads.get(
            project_id,
            _display_patch_log_head(snapshot),
        )
        envelope = {
            "schema_version": _DISPLAY_SNAPSHOT_SCHEMA_VERSION,
            "project_id": project_id,
            "canonical_patch_head": patch_log_head,
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
            self._cached_snapshot_patch_heads[project_id] = patch_log_head
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def cached_snapshot(self, project_id: str) -> dict[str, object] | None:
        project_id = self._canonical_project_id(project_id)
        _status, snapshot = self.cached_snapshot_status(project_id)
        return snapshot

    def cached_snapshot_status(
        self,
        project_id: str,
    ) -> tuple[Literal["missing", "invalid", "valid"], dict[str, object] | None]:
        project_id = self._canonical_project_id(project_id)
        with self._snapshot_lock(project_id):
            if self._is_deleting(project_id) or self.store.project(project_id) is None:
                return "missing", None
            return self._cached_snapshot_status_locked(project_id)

    def _cached_snapshot_locked(self, project_id: str) -> dict[str, object] | None:
        project_id = self._canonical_project_id(project_id)
        _status, snapshot = self._cached_snapshot_status_locked(project_id)
        return snapshot

    def _cached_snapshot_status_locked(
        self,
        project_id: str,
    ) -> tuple[Literal["missing", "invalid", "valid"], dict[str, object] | None]:
        project_id = self._canonical_project_id(project_id)
        self._cached_snapshot_patch_heads.pop(project_id, None)
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
        if not isinstance(envelope, dict):
            return "invalid", None
        schema_version = envelope.get("schema_version")
        if schema_version == 1 and set(envelope) == {
            "schema_version",
            "project_id",
            "snapshot",
        }:
            patch_log_head = None
        elif schema_version == _DISPLAY_SNAPSHOT_SCHEMA_VERSION and set(envelope) == {
            "schema_version",
            "project_id",
            "canonical_patch_head",
            "snapshot",
        }:
            patch_log_head = envelope["canonical_patch_head"]
            if not _valid_patch_log_head(patch_log_head):
                return "invalid", None
        else:
            return "invalid", None
        if envelope["project_id"] != project_id:
            return "invalid", None
        snapshot = envelope["snapshot"]
        if not isinstance(snapshot, dict):
            return "invalid", None
        _ensure_snapshot_freshness(snapshot)
        if not _valid_display_snapshot(project_id, snapshot):
            return "invalid", None
        if schema_version == 1:
            patch_log_head = _display_patch_log_head(snapshot)
        assert patch_log_head is None or isinstance(patch_log_head, int)
        self._cached_snapshot_patch_heads[project_id] = patch_log_head
        return "valid", snapshot

    def loaded_service(self, project_id: str) -> ProjectService | None:
        """Return an already-open service without opening or refreshing it."""

        project_id = self._canonical_project_id(project_id)
        with self._services_lock:
            return self._services.get(project_id)

    def _cached_snapshot_path(self, project_id: str) -> Path:
        project_id = self._canonical_project_id(project_id)
        return self._cached_snapshot_path_for_id(project_id)

    def _cached_snapshot_path_for_id(self, project_id: str) -> Path:
        digest = hashlib.sha256(project_id.encode()).hexdigest()
        return self.data_dir / "project-snapshots" / f"{digest}.json"

    def _paper_snapshot_path(self, project_id: str) -> Path:
        project_id = self._canonical_project_id(project_id)
        return self._paper_snapshot_path_for_id(project_id)

    def _paper_snapshot_path_for_id(self, project_id: str) -> Path:
        safe_project_id = re.sub(r"[^A-Za-z0-9._-]+", "_", project_id).strip("._")
        return (
            self.data_dir
            / "paper-snapshots"
            / (f"{(safe_project_id or 'project')[:80]}-introduction.md")
        )

    def _open_service(self, project_id: str) -> tuple[ProjectService, GraphState]:
        project_id = self._canonical_project_id(project_id)
        record = self.store.project(project_id)
        if record is None:
            raise KeyError(project_id)
        bootstrap = load_manifest(record.locator)
        manifest, workspace = prepare_state_workspace(bootstrap, self.data_dir)
        history = self._history_for_manifest(manifest, workspace)
        initialized = history.initialize()
        identity = history.project_identity(initialized)
        if identity is None:
            raise RuntimeError(
                "Registered project identity disappeared before the project could open."
            )
        if identity.project_id != project_id:
            raise RuntimeError(
                f"Registered project id {project_id!r} does not match canonical history "
                f"{identity.project_id!r}."
            )
        initialized_state = initialized.state
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
        project_id = self._canonical_project_id(project_id)
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
        project_id = self._canonical_project_id(project_id)
        generation = self.reserve_cached_snapshot_generation(project_id)
        service = self.open(project_id)
        project_id = self._canonical_project_id(project_id)
        service.update_settings(request)
        self._persist_bootstrap_locator(project_id, service)
        snapshot = service.project_snapshot()
        self._stamp_snapshot_identity(snapshot, project_id)
        self.mark_snapshot_fresh(snapshot)
        self.commit_cached_snapshot(
            project_id,
            snapshot,
            generation=generation,
            patch_log_head=service.history.workspace.cached_patch_log_head(),
        )
        return snapshot

    def resolve_provider_path(
        self,
        project_id: str,
        machine_alias: str,
        provider: ProviderId,
    ) -> dict[str, object]:
        project_id = self._canonical_project_id(project_id)
        generation = self.reserve_cached_snapshot_generation(project_id)
        service = self.open(project_id)
        project_id = self._canonical_project_id(project_id)
        readiness = service.resolve_provider_path(machine_alias, provider)
        self._persist_bootstrap_locator(project_id, service)
        snapshot = service.project_snapshot()
        self._stamp_snapshot_identity(snapshot, project_id)
        self.mark_snapshot_fresh(snapshot)
        self.commit_cached_snapshot(
            project_id,
            snapshot,
            generation=generation,
            patch_log_head=service.history.workspace.cached_patch_log_head(),
        )
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
        project_id = self._canonical_project_id(project_id)
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
            "home_space_id": record.home_space_id,
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


def _require_regular_app_file(target: Path, label: str) -> None:
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise ValueError(f"Could not inspect {label}: {target}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Refusing to migrate non-file {label}: {target}")


def _valid_display_snapshot(project_id: str, snapshot: dict[str, object]) -> bool:
    if not _DISPLAY_SNAPSHOT_FIELDS.issubset(snapshot):
        return False
    if snapshot.get("id") != project_id or not isinstance(snapshot.get("name"), str):
        return False
    home_space_id = snapshot.get("home_space_id")
    if not isinstance(home_space_id, str):
        return False
    try:
        parsed_home = uuid.UUID(home_space_id)
    except ValueError:
        return False
    if str(parsed_home) != home_space_id or parsed_home.version != 4:
        return False
    revision = snapshot.get("revision")
    if type(revision) is not int or revision < 0:
        return False
    if snapshot.get("snapshot_freshness") not in {"fresh", "reconciling", "stale"}:
        return False
    last_remote_sync_at = snapshot.get("last_remote_sync_at")
    if last_remote_sync_at is not None and not isinstance(last_remote_sync_at, str):
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


def _ensure_snapshot_freshness(
    snapshot: dict[str, object],
    *,
    freshness: Literal["fresh", "reconciling", "stale"] | None = None,
) -> None:
    canonical = snapshot.get("canonical_state")
    remote = isinstance(canonical, dict) and canonical.get("remote") is True
    if freshness is not None:
        snapshot["snapshot_freshness"] = freshness
    elif snapshot.get("snapshot_freshness") not in {"fresh", "reconciling", "stale"}:
        snapshot["snapshot_freshness"] = "stale" if remote else "fresh"
    if "last_remote_sync_at" not in snapshot:
        last_synced_at = canonical.get("last_synced_at") if isinstance(canonical, dict) else None
        snapshot["last_remote_sync_at"] = str(last_synced_at) if remote and last_synced_at else None


def _display_patch_log_head(snapshot: dict[str, object]) -> int | None:
    revisions = [snapshot.get("revision")]
    graph = snapshot.get("graph")
    replay_failure = graph.get("replay_failure") if isinstance(graph, dict) else None
    if isinstance(replay_failure, dict):
        revisions.append(replay_failure.get("revision"))
    return max(
        (revision for revision in revisions if type(revision) is int and revision > 0),
        default=None,
    )


def _valid_patch_log_head(value: object) -> bool:
    return value is None or (type(value) is int and value > 0)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
