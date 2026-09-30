from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from rcp.limits import ARTIFACT_MAX_VERSION_BYTES, ARTIFACT_RECENT_VERSIONS
from rcp.live_artifacts import ResolvedLiveVersion
from rcp.server_ops._local_primitives import fsync_directory
from rcp.storage.artifact_models import (
    Artifact,
    ArtifactFile,
    ArtifactOperationConflict,
    ArtifactVersion,
    ArtifactVersionConflict,
    artifact_version_files,
)
from rcp.storage.models import ACTIVE_AGENT_TASK_STATUSES

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_CAPTURE_GUARDS: dict[str, _CaptureGuard] = {}


class _CaptureGuard:
    """Allow overlapping captures; exclude deletion until every capture exits."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.captures = 0

    @contextmanager
    def capture(self) -> Iterator[None]:
        with self.condition:
            self.captures += 1
        try:
            yield
        finally:
            with self.condition:
                self.captures -= 1
                self.condition.notify_all()

    @contextmanager
    def deletion(self) -> Iterator[None]:
        with self.condition:
            self.condition.wait_for(lambda: self.captures == 0)
            yield


def _capture_guard(root: Path) -> _CaptureGuard:
    with _LOCKS_GUARD:
        return _CAPTURE_GUARDS.setdefault(str(root.resolve()), _CaptureGuard())


def _lock(root: Path, artifact_id: str) -> threading.RLock:
    key = str(root.resolve()) + "/" + artifact_id
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def write_artifact_file(root: Path, entry: ArtifactFile, data: bytes) -> Path:
    if len(data) != entry.size_bytes or hashlib.sha256(data).hexdigest() != entry.sha256:
        raise ValueError("artifact bytes do not match their inventory")
    path = root / "artifacts" / entry.artifact_id / entry.file_id
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("immutable artifact file differs from its digest")
        return path
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".artifact-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def insert_artifact(
    connection: sqlite3.Connection, artifact: Artifact, versions: list[ArtifactVersion]
) -> None:
    connection.execute(
        "INSERT INTO artifacts VALUES (?, ?, ?)",
        (artifact.artifact_id, artifact.project_id, artifact.model_dump_json()),
    )
    for version in versions:
        connection.execute(
            "INSERT INTO artifact_versions VALUES (?, ?, ?, ?, ?)",
            (
                artifact.artifact_id,
                version.version_id,
                version.sequence,
                version.operation_id,
                version.model_dump_json(),
            ),
        )


class ArtifactStoreMixin:
    @contextmanager
    def artifact_capture(self) -> Iterator[None]:
        with _capture_guard(self.path.parent).capture():
            yield

    @contextmanager
    def artifact_lock(self, artifact_id: str) -> Iterator[None]:
        with _lock(self.path.parent, artifact_id):
            yield

    def artifact(self, artifact_id: str) -> Artifact | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT metadata FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return Artifact.model_validate_json(row[0]) if row else None

    def artifacts(self, project_id: str) -> list[Artifact]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT metadata FROM artifacts WHERE project_id = ? ORDER BY artifact_id",
                (project_id,),
            ).fetchall()
        return [Artifact.model_validate_json(row[0]) for row in rows]

    def artifact_versions(self, artifact_id: str) -> list[ArtifactVersion]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT metadata FROM artifact_versions WHERE artifact_id = ? ORDER BY sequence",
                (artifact_id,),
            ).fetchall()
        return [ArtifactVersion.model_validate_json(row[0]) for row in rows]

    def artifact_inventory(self, project_id: str | None = None) -> list[ArtifactFile]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT v.metadata FROM artifact_versions v JOIN artifacts a USING(artifact_id)"
                + (" WHERE a.project_id = ?" if project_id else ""),
                (project_id,) if project_id else (),
            ).fetchall()
        entries = [ArtifactVersion.model_validate_json(row[0]) for row in rows]
        return artifact_version_files(entries)

    def artifact_file_path(self, entry: ArtifactFile) -> Path:
        entry = ArtifactFile.model_validate(
            entry.model_dump(include=set(ArtifactFile.model_fields))
        )
        return self.path.parent / "artifacts" / entry.artifact_id / entry.file_id

    def create_artifact(self, artifact: Artifact, *, data: bytes) -> Artifact:
        with self.artifact_lock(artifact.artifact_id):
            existing = self.artifact(artifact.artifact_id)
            if existing is not None:
                if (
                    self.read_artifact_bytes(
                        existing.artifact_id,
                        self.artifact_versions(existing.artifact_id)[0].version_id,
                    )
                    != data
                ):
                    raise ArtifactVersionConflict(
                        "artifact identity already has different original bytes"
                    )
                return existing
            digest = hashlib.sha256(data).hexdigest()
            if len(data) > ARTIFACT_MAX_VERSION_BYTES:
                raise ValueError("artifact exceeds version storage byte limit")
            version = ArtifactVersion(
                artifact_id=artifact.artifact_id,
                file_id=digest,
                sha256=digest,
                size_bytes=len(data),
                version_id=digest,
                operation_id=artifact.supplier_id,
                created_at=artifact.created_at,
                sequence=0,
            )
            artifact = artifact.model_copy(update={"current_version": version.version_id})
            write_artifact_file(self.path.parent, version, data)
            with self.connection() as connection:
                insert_artifact(connection, artifact, [version])
            return artifact

    def read_artifact_bytes(self, artifact_id: str, version_id: str | None = None) -> bytes:
        artifact = self.artifact(artifact_id)
        if artifact is None:
            raise KeyError(artifact_id)
        version = next(
            (
                v
                for v in self.artifact_versions(artifact_id)
                if v.version_id == (version_id or artifact.current_version)
            ),
            None,
        )
        if version is None:
            raise KeyError(version_id)
        data = self.artifact_file_path(version).read_bytes()
        if len(data) != version.size_bytes or hashlib.sha256(data).hexdigest() != version.sha256:
            raise ValueError("stored artifact bytes differ from inventory")
        return data

    def set_artifact_version_live(
        self, artifact_id: str, version_id: str, live: ResolvedLiveVersion
    ) -> None:
        with self.artifact_lock(artifact_id):
            version = self._artifact_version(artifact_id, version_id)
            version = version.model_copy(update={"live": ResolvedLiveVersion.model_validate(live)})
            self._save_artifact_version_metadata(version)

    def save_artifact_live_snapshot(
        self, artifact_id: str, version_id: str, data: bytes
    ) -> ArtifactFile:
        """Publish immutable captured bytes before their version's reference."""
        with self.artifact_lock(artifact_id):
            version = self._artifact_version(artifact_id, version_id)
            if version.live_snapshot is not None:
                if self.read_artifact_live_snapshot(artifact_id, version_id) != data:
                    raise ArtifactVersionConflict("final live snapshot is already saved")
                return version.live_snapshot
            digest = hashlib.sha256(data).hexdigest()
            entry = ArtifactFile(
                artifact_id=artifact_id, file_id=digest, sha256=digest, size_bytes=len(data)
            )
            inventory = artifact_version_files(self.artifact_versions(artifact_id))
            additional = 0 if entry in inventory else entry.size_bytes
            if sum(item.size_bytes for item in inventory) + additional > ARTIFACT_MAX_VERSION_BYTES:
                raise ValueError("live snapshot exceeds artifact storage byte limit")
            write_artifact_file(self.path.parent, entry, data)
            self._save_artifact_version_metadata(
                version.model_copy(update={"live_snapshot": entry})
            )
            return entry

    def read_artifact_live_snapshot(self, artifact_id: str, version_id: str) -> bytes | None:
        with self.artifact_lock(artifact_id):
            entry = self._artifact_version(artifact_id, version_id).live_snapshot
            if entry is None:
                return None
            data = self.artifact_file_path(entry).read_bytes()
            if len(data) != entry.size_bytes or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise ValueError("stored live snapshot bytes differ from inventory")
            return data

    def keep_artifact(self, artifact_id: str) -> Artifact:
        with self.artifact_lock(artifact_id):
            artifact = self.artifact(artifact_id)
            if artifact is None:
                raise KeyError(artifact_id)
            artifact = artifact.model_copy(
                update={"kept_at": artifact.kept_at or self.now(), "expires_at": None}
            )
            with self.connection() as connection:
                connection.execute(
                    "UPDATE artifacts SET metadata = ? WHERE artifact_id = ?",
                    (artifact.model_dump_json(), artifact_id),
                )
            return artifact

    def artifact_edit_operation_id(self, project_id: str, artifact_id: str) -> str | None:
        """The latest admitted, nonterminal edit, without loading task history."""
        statuses = sorted(ACTIVE_AGENT_TASK_STATUSES)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT operation_id FROM graph_runs WHERE project_id = ? "
                f"AND status IN ({','.join('?' for _ in statuses)}) "
                "AND json_extract(request_json, '$.artifact_edit.artifact_id') = ? "
                "ORDER BY created_at DESC, operation_id DESC LIMIT 1",
                (project_id, *statuses, artifact_id),
            ).fetchone()
        return row[0] if row else None

    def protected_edit_artifact_ids(self) -> frozenset[str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT json_extract(request_json, '$.artifact_context.artifact_id') "
                "FROM graph_runs WHERE status IN "
                "('queued', 'running', 'pausing', 'paused', 'interrupted')"
            ).fetchall()
        return frozenset(row[0] for row in rows if isinstance(row[0], str))

    @staticmethod
    def _artifact_ancestors(
        current: ArtifactVersion, versions: list[ArtifactVersion]
    ) -> list[ArtifactVersion]:
        # Older versions formed one linear history. New versions carry the
        # bounded retained ancestry, so pinned abandoned edits never become Undo targets.
        return [
            v
            for v in versions
            if (
                v.version_id in current.ancestors
                if current.ancestors is not None
                else v.sequence < current.sequence
            )
        ]

    def undo_artifact(self, artifact_id: str) -> Artifact:
        """Move to the preceding retained version without ever dropping the original."""
        with self.artifact_lock(artifact_id):
            artifact = self.artifact(artifact_id)
            if artifact is None:
                raise KeyError(artifact_id)
            versions = self.artifact_versions(artifact_id)
            current = next(v for v in versions if v.version_id == artifact.current_version)
            previous = self._artifact_ancestors(current, versions)
            if not previous:
                raise ArtifactVersionConflict("artifact is already at its original version")
            artifact = artifact.model_copy(update={"current_version": previous[-1].version_id})
            with self.connection() as connection:
                connection.execute(
                    "UPDATE artifacts SET metadata = ? WHERE artifact_id = ?",
                    (artifact.model_dump_json(), artifact_id),
                )
            return artifact

    def publish_artifact_version(
        self, artifact_id: str, *, base_version: str, operation_id: str, data: bytes
    ) -> ArtifactVersion:
        with self.artifact_lock(artifact_id):
            artifact = self.artifact(artifact_id)
            if artifact is None:
                raise KeyError(artifact_id)
            versions = self.artifact_versions(artifact_id)
            with self.connection() as connection:
                receipt = connection.execute(
                    "SELECT metadata FROM artifact_operations WHERE artifact_id = ? AND operation_id = ?",
                    (artifact_id, operation_id),
                ).fetchone()
            if receipt:
                outcome = json.loads(receipt[0])
                if outcome["sha256"] != hashlib.sha256(data).hexdigest():
                    raise ArtifactOperationConflict(
                        "operation was already published with different bytes"
                    )
                return ArtifactVersion.model_validate({**outcome, "file_id": outcome["sha256"]})
            if artifact.current_version != base_version:
                raise ArtifactVersionConflict("artifact changed since the operation started")
            digest = hashlib.sha256(data).hexdigest()
            version = ArtifactVersion(
                artifact_id=artifact_id,
                file_id=digest,
                sha256=digest,
                size_bytes=len(data),
                version_id="edit:" + hashlib.sha256(operation_id.encode()).hexdigest(),
                operation_id=operation_id,
                created_at=self.now(),
                sequence=max(v.sequence for v in versions) + 1,
            )
            current = next(v for v in versions if v.version_id == base_version)
            lineage = self._artifact_ancestors(current, versions) + [current]
            version = version.model_copy(update={"ancestors": [v.version_id for v in lineage]})
            ancestors = [v for v in lineage if v.sequence > 0]
            statuses = sorted(ACTIVE_AGENT_TASK_STATUSES)
            with self.connection() as connection:
                pending = connection.execute(
                    "SELECT json_extract(request_json, '$.artifact_edit.base_version') FROM graph_runs g "
                    "WHERE json_extract(request_json, '$.artifact_edit.artifact_id') = ? "
                    f"AND g.status IN ({','.join('?' for _ in statuses)}) "
                    "AND NOT EXISTS (SELECT 1 FROM graph_run_receipts r "
                    "JOIN graph_runs attempt ON attempt.operation_id = r.operation_id WHERE "
                    "json_extract(attempt.request_json, '$.artifact_edit.operation_id') = "
                    "json_extract(g.request_json, '$.artifact_edit.operation_id') "
                    "AND r.category = 'artifact_edit_staged')",
                    (artifact_id, *statuses),
                ).fetchall()
            pinned = {row[0] for row in pending}

            retained = [
                versions[0],
                *(
                    ancestors[-(ARTIFACT_RECENT_VERSIONS - 1) :]
                    if ARTIFACT_RECENT_VERSIONS > 1
                    else []
                ),
                version,
            ]
            retained = sorted(
                {
                    v.version_id: v
                    for v in [*retained, *(v for v in versions if v.version_id in pinned)]
                }.values(),
                key=lambda v: v.sequence,
            )
            while (
                len(retained) > 2
                and sum(v.size_bytes for v in artifact_version_files(retained))
                > ARTIFACT_MAX_VERSION_BYTES
            ):
                removable = next((v for v in retained[1:-1] if v.version_id not in pinned), None)
                if removable is None:
                    break
                retained.remove(removable)
            if (
                sum(v.size_bytes for v in artifact_version_files(retained))
                > ARTIFACT_MAX_VERSION_BYTES
            ):
                raise ValueError("original and new artifact version exceed storage byte limit")
            write_artifact_file(self.path.parent, version, data)
            artifact = artifact.model_copy(update={"current_version": version.version_id})
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE artifacts SET metadata = ? WHERE artifact_id = ?",
                    (artifact.model_dump_json(), artifact_id),
                )
                connection.execute(
                    "DELETE FROM artifact_versions WHERE artifact_id = ?", (artifact_id,)
                )
                for item in retained:
                    connection.execute(
                        "INSERT INTO artifact_versions VALUES (?, ?, ?, ?, ?)",
                        (
                            artifact_id,
                            item.version_id,
                            item.sequence,
                            item.operation_id,
                            item.model_dump_json(),
                        ),
                    )
                connection.execute(
                    "INSERT INTO artifact_operations VALUES (?, ?, ?)",
                    (artifact_id, operation_id, version.model_dump_json(exclude={"file_id"})),
                )
            kept_files = {v.file_id for v in artifact_version_files(retained)}
            folder = self.path.parent / "artifacts" / artifact_id
            obsolete = [
                path
                for path in folder.iterdir()
                if (
                    re.fullmatch(r"[a-f0-9]{64}", path.name)
                    and path.name not in kept_files
                    and path.is_file()
                    and not path.is_symlink()
                )
            ]
            if obsolete:
                with _capture_guard(self.path.parent).deletion():
                    for path in obsolete:
                        path.unlink(missing_ok=True)
            return version

    def expire_artifacts(
        self, *, as_of: datetime | None = None, protected_artifact_ids: frozenset[str] = frozenset()
    ) -> int:
        """Serialize expiry with Keep and hold off deletion during captures."""
        removed = 0
        with self.connection() as connection:
            rows = connection.execute("SELECT artifact_id FROM artifacts").fetchall()
        now = as_of or datetime.fromisoformat(self.now())
        for row in rows:
            artifact_id = row[0]
            with self.artifact_lock(artifact_id):
                artifact = self.artifact(artifact_id)
                if (
                    artifact is None
                    or artifact_id in protected_artifact_ids
                    or artifact.kept_at
                    or not artifact.expires_at
                    or datetime.fromisoformat(artifact.expires_at) > now
                    or self.artifact_edit_operation_id(artifact.project_id, artifact_id) is not None
                    or artifact_id in self.protected_edit_artifact_ids()
                ):
                    continue
                with _capture_guard(self.path.parent).deletion():
                    with self.connection() as connection:
                        connection.execute(
                            "INSERT OR REPLACE INTO artifact_imports VALUES (?, ?, 'missing', 0, NULL, ?)",
                            (artifact_id, artifact.project_id, "Artifact expired."),
                        )
                        for table in ("artifact_operations", "artifact_versions", "artifacts"):
                            connection.execute(
                                f"DELETE FROM {table} WHERE artifact_id = ?",
                                (artifact_id,),
                            )
                    folder = self.path.parent / "artifacts" / artifact_id
                    if folder.exists():
                        shutil.rmtree(folder)
                    removed += 1
        return removed

    def _artifact_version(self, artifact_id: str, version_id: str) -> ArtifactVersion:
        for version in self.artifact_versions(artifact_id):
            if version.version_id == version_id:
                return version
        raise KeyError(version_id)

    def _save_artifact_version_metadata(self, version: ArtifactVersion) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE artifact_versions SET metadata = ? WHERE artifact_id = ? AND version_id = ?",
                (version.model_dump_json(), version.artifact_id, version.version_id),
            )


def migrate_artifacts(connection: sqlite3.Connection, file_root: Path) -> None:
    connection.execute(
        "CREATE TABLE artifacts (artifact_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, metadata TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE artifact_versions (artifact_id TEXT NOT NULL, version_id TEXT NOT NULL, sequence INTEGER NOT NULL, operation_id TEXT NOT NULL, metadata TEXT NOT NULL, PRIMARY KEY (artifact_id, version_id), UNIQUE(artifact_id, operation_id))"
    )
    connection.execute(
        "CREATE TABLE artifact_operations (artifact_id TEXT NOT NULL, operation_id TEXT NOT NULL, metadata TEXT NOT NULL, PRIMARY KEY(artifact_id, operation_id))"
    )
    connection.execute("ALTER TABLE episode_reports ADD COLUMN artifact_id TEXT")
    connection.execute("ALTER TABLE episode_reports ADD COLUMN artifact_version_id TEXT")
    for row in connection.execute(
        "SELECT r.*, e.project_id FROM episode_reports r JOIN episodes e USING(episode_id)"
    ).fetchall():
        data = row["html"].encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        artifact = Artifact(
            artifact_id=hashlib.sha256(row["report_id"].encode()).hexdigest()[:24],
            project_id=row["project_id"],
            supplier="episode_ending",
            live_data_allowed=False,
            supplier_id=row["episode_id"],
            source_name="episode-report.html",
            media_type="text/html",
            created_at=row["created_at"],
            current_version=digest,
            episode_id=row["episode_id"],
            origin_operation_id=row["allocation_operation_id"],
            display_title=row["display_title"],
        )
        version = ArtifactVersion(
            artifact_id=artifact.artifact_id,
            file_id=digest,
            sha256=digest,
            size_bytes=len(data),
            version_id=digest,
            sequence=0,
            operation_id=artifact.supplier_id,
            created_at=artifact.created_at,
        )
        write_artifact_file(file_root, version, data)
        insert_artifact(connection, artifact, [version])
        connection.execute(
            "UPDATE episode_reports SET artifact_id = ?, artifact_version_id = ? WHERE report_id = ?",
            (artifact.artifact_id, digest, row["report_id"]),
        )
    if connection.execute(
        "SELECT 1 FROM episode_reports WHERE artifact_id IS NULL LIMIT 1"
    ).fetchone():
        raise ValueError("Artifact migration refused: an episode report has no owning episode.")
    connection.execute("ALTER TABLE episode_reports DROP COLUMN html")
    for row in connection.execute("SELECT * FROM result_views").fetchall():
        data = row["html"].encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        artifact = Artifact(
            artifact_id=row["view_id"],
            project_id=row["project_id"],
            supplier="turn",
            supplier_id=row["origin_operation_id"],
            origin_operation_id=row["origin_operation_id"],
            chat_id=row["chat_id"],
            source_name=row["source_name"],
            media_type="text/html",
            created_at=row["created_at"],
            expires_at=None if row["kept_at"] else row["expires_at"],
            kept_at=row["kept_at"],
            current_version=digest,
        )
        version = ArtifactVersion(
            artifact_id=artifact.artifact_id,
            file_id=digest,
            sha256=digest,
            size_bytes=len(data),
            version_id=digest,
            sequence=0,
            operation_id=artifact.supplier_id,
            created_at=artifact.created_at,
        )
        write_artifact_file(file_root, version, data)
        insert_artifact(connection, artifact, [version])
    connection.execute("DROP TABLE result_views")
    connection.execute(
        "UPDATE graph_runs SET request_json = json_remove(request_json, '$.result_view') WHERE json_valid(request_json) AND json_type(request_json, '$.result_view') = 'null'"
    )
