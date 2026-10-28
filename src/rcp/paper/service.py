from __future__ import annotations

import codecs
import os
import stat
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, model_validator

from rcp.config import Manifest
from rcp.providers import (
    ProviderId,
    legacy_runtime_id,
    profile_for,
    require_runtime_id,
    runtime_label,
)
from rcp.storage import AppStore
from rcp.transport import LocalStateWorkspace, StateWorkspace


def canonical_introduction_backup_source(root: Path) -> Path | None:
    """Return the only canonical Paper file without refreshing or locking it."""

    paper_root = root / "paper"
    try:
        directory = paper_root.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("The canonical Paper directory is unavailable.") from exc
    if not stat.S_ISDIR(directory.st_mode):
        raise ValueError("The canonical Paper path is not a safe directory.")
    try:
        entries = sorted(paper_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ValueError("The canonical Paper directory cannot be enumerated.") from exc
    if not entries:
        return None
    introduction = paper_root / "introduction.md"
    if entries != [introduction]:
        raise ValueError("The canonical Paper directory contains an unclassified entry.")
    try:
        metadata = introduction.lstat()
    except OSError as exc:
        raise ValueError("The canonical Paper introduction cannot be inspected.") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("The canonical Paper introduction is not a safe regular file.")
    return introduction


def validate_canonical_introduction_backup(path: Path) -> None:
    """Validate one copied introduction as UTF-8 Markdown without loading it whole."""

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("The copied Paper introduction is not a regular file.")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            if "\x00" in decoder.decode(chunk):
                raise ValueError("The copied Paper introduction contains NUL text.")
        if "\x00" in decoder.decode(b"", final=True):
            raise ValueError("The copied Paper introduction contains NUL text.")
    except UnicodeDecodeError as exc:
        raise ValueError("The copied Paper introduction is not UTF-8 Markdown.") from exc
    finally:
        os.close(descriptor)


class WritingSession(BaseModel):
    provider: ProviderId
    runtime_id: str = ""
    #: The human-facing name for `runtime_id`, filled from the provider registry.
    runtime_label: str = ""
    #: The umbrella provider name the session list shows.
    provider_label: str = ""
    native_session_id: str
    execution_machine: str
    project_id: str
    title: str | None = None
    model: str
    reasoning: str | None = None
    created_at: datetime
    last_resumed_at: datetime
    introduction_hash_examined: str
    graph_revision_examined: int
    research_md_hash_examined: str

    @model_validator(mode="after")
    def validate_runtime(self) -> WritingSession:
        if not self.runtime_id:
            self.runtime_id = legacy_runtime_id(self.provider)
        require_runtime_id(self.provider, self.runtime_id)
        self.runtime_label = runtime_label(self.provider, self.runtime_id)
        self.provider_label = profile_for(self.provider).label
        return self


class PaperService:
    def __init__(
        self,
        manifest: Manifest,
        store: AppStore,
        workspace: StateWorkspace | None = None,
        *,
        project_id: str | None = None,
    ) -> None:
        self.manifest = manifest
        self.store = store
        self.workspace = workspace or LocalStateWorkspace(
            manifest.research_dir, str(manifest.research_dir)
        )
        self.project_id = project_id or manifest.name
        self.canonical_path = self.workspace.root / "paper" / "introduction.md"

    def restore_canonical(
        self,
        source: Path,
        *,
        expected_sha256: str,
        expected_size: int,
    ) -> None:
        """Validate and publish the archived introduction without changing its bytes."""

        validate_canonical_introduction_backup(source)
        self.workspace.restore_exact_file(
            Path("paper/introduction.md"),
            source,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )

    def sessions(self) -> list[WritingSession]:
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM writing_sessions WHERE project_id = ? ORDER BY last_resumed_at DESC",
                (self.project_id,),
            ).fetchall()
        return [WritingSession.model_validate(dict(row)) for row in rows]

    def record_session(self, session: WritingSession) -> None:
        data = session.model_dump(mode="json")
        with self.store.connection() as connection:
            connection.execute(
                """
                INSERT INTO writing_sessions (
                    native_session_id, provider, runtime_id, execution_machine, project_id,
                    title, model, reasoning, created_at, last_resumed_at,
                    introduction_hash_examined, graph_revision_examined,
                    research_md_hash_examined
                ) VALUES (
                    :native_session_id, :provider, :runtime_id, :execution_machine, :project_id,
                    :title, :model, :reasoning, :created_at, :last_resumed_at,
                    :introduction_hash_examined, :graph_revision_examined,
                    :research_md_hash_examined
                )
                ON CONFLICT(native_session_id) DO UPDATE SET
                    runtime_id = excluded.runtime_id,
                    title = excluded.title,
                    last_resumed_at = excluded.last_resumed_at,
                    introduction_hash_examined = excluded.introduction_hash_examined,
                    graph_revision_examined = excluded.graph_revision_examined,
                    research_md_hash_examined = excluded.research_md_hash_examined
                """,
                data,
            )
