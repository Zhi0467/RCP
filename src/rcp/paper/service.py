from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from rcp.config import Manifest
from rcp.providers import ProviderId
from rcp.storage import AppStore
from rcp.transport import LocalStateWorkspace, StateUnavailable, StateWorkspace

INTRODUCTION_TEMPLATE = """# Introduction

## What question we study

## What adjacent questions there are

## Literature review

## High-level methods

## Main results

## Why this deserves publication and communication to the community
"""


class PaperSnapshot(BaseModel):
    content: str
    sync_state: Literal["not_created", "synced", "unsynced", "conflict"]
    base_hash: str | None = None
    canonical_hash: str | None = None
    updated_at: datetime | None = None
    canonical_available: bool


class WritingSession(BaseModel):
    provider: ProviderId
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

    def snapshot(self) -> PaperSnapshot:
        draft = self._draft()
        canonical_content, canonical_available = self._read_canonical()
        canonical_hash = _hash(canonical_content) if canonical_content is not None else None
        if draft is None and canonical_content is None:
            state = "not_created"
            content = ""
            base_hash = None
            updated_at = None
        elif draft is None:
            state = "synced"
            content = canonical_content or ""
            base_hash = canonical_hash
            updated_at = None
        else:
            content = draft["content"]
            base_hash = draft["base_hash"]
            updated_at = datetime.fromisoformat(draft["updated_at"])
            draft_hash = _hash(content)
            if not canonical_available:
                state = "unsynced"
            elif canonical_hash == draft_hash:
                state = "synced"
            elif canonical_hash != base_hash:
                state = "conflict"
            else:
                state = "unsynced"
        if not canonical_available and state != "not_created":
            state = "unsynced"
        return PaperSnapshot(
            content=content,
            sync_state=state,
            base_hash=base_hash,
            canonical_hash=canonical_hash,
            updated_at=updated_at,
            canonical_available=canonical_available,
        )

    def create(self) -> PaperSnapshot:
        draft = self._draft()
        if draft is None:
            canonical_content = self._read_cached_canonical()
            content = canonical_content if canonical_content is not None else INTRODUCTION_TEMPLATE
            base_hash = _hash(canonical_content) if canonical_content is not None else None
            self._save_draft(content, base_hash)
            draft = self._draft()
        return self._local_draft_snapshot(draft)

    def save(self, content: str, base_hash: str | None) -> PaperSnapshot:
        self._save_draft(content, base_hash)
        try:
            with self.workspace.transaction():
                canonical_content = self._read_cached_canonical()
                canonical_hash = _hash(canonical_content) if canonical_content is not None else None
                if canonical_hash == _hash(content):
                    self._save_draft(content, canonical_hash)
                    return self.snapshot()
                if canonical_hash != base_hash:
                    return self.snapshot()
                self._write_canonical(content)
                new_hash = _hash(content)
                self._save_draft(content, new_hash)
                return self.snapshot()
        except StateUnavailable:
            return self.snapshot()

    def resolve_conflict(
        self, strategy: Literal["use_canonical", "overwrite_canonical"]
    ) -> PaperSnapshot:
        snapshot = self.snapshot()
        if snapshot.sync_state != "conflict":
            return snapshot
        try:
            with self.workspace.transaction():
                canonical_content = self._read_cached_canonical()
                if canonical_content is None:
                    return self.snapshot()
                if strategy == "use_canonical":
                    self._save_draft(canonical_content, _hash(canonical_content))
                else:
                    self._write_canonical(snapshot.content)
                    self._save_draft(snapshot.content, _hash(snapshot.content))
                return self.snapshot()
        except StateUnavailable:
            return snapshot

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
                    native_session_id, provider, execution_machine, project_id,
                    title, model, reasoning, created_at, last_resumed_at,
                    introduction_hash_examined, graph_revision_examined,
                    research_md_hash_examined
                ) VALUES (
                    :native_session_id, :provider, :execution_machine, :project_id,
                    :title, :model, :reasoning, :created_at, :last_resumed_at,
                    :introduction_hash_examined, :graph_revision_examined,
                    :research_md_hash_examined
                )
                ON CONFLICT(native_session_id) DO UPDATE SET
                    title = excluded.title,
                    last_resumed_at = excluded.last_resumed_at,
                    introduction_hash_examined = excluded.introduction_hash_examined,
                    graph_revision_examined = excluded.graph_revision_examined,
                    research_md_hash_examined = excluded.research_md_hash_examined
                """,
                data,
            )

    def _draft(self):
        with self.store.connection() as connection:
            return connection.execute(
                "SELECT * FROM paper_drafts WHERE project_id = ?", (self.project_id,)
            ).fetchone()

    def _save_draft(self, content: str, base_hash: str | None) -> None:
        with self.store.connection() as connection:
            connection.execute(
                """
                INSERT INTO paper_drafts(project_id, content, base_hash, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    content = excluded.content,
                    base_hash = excluded.base_hash,
                    updated_at = excluded.updated_at
                """,
                (self.project_id, content, base_hash, self.store.now()),
            )

    def _local_draft_snapshot(self, draft) -> PaperSnapshot:
        content = draft["content"]
        base_hash = draft["base_hash"]
        canonical_content = self._read_cached_canonical()
        canonical_hash = _hash(canonical_content) if canonical_content is not None else None
        synchronized = (
            self.workspace.reachable
            and canonical_hash is not None
            and canonical_hash == _hash(content)
            and canonical_hash == base_hash
        )
        return PaperSnapshot(
            content=content,
            sync_state="synced" if synchronized else "unsynced",
            base_hash=base_hash,
            canonical_hash=canonical_hash,
            updated_at=datetime.fromisoformat(draft["updated_at"]),
            canonical_available=self.workspace.reachable,
        )

    def _read_canonical(self) -> tuple[str | None, bool]:
        available = True
        try:
            self.workspace.refresh_if_stale()
        except StateUnavailable:
            available = False
        return self._read_cached_canonical(), available

    def _read_cached_canonical(self) -> str | None:
        try:
            if not self.canonical_path.exists():
                return None
            return self.canonical_path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _write_canonical(self, content: str) -> None:
        self.canonical_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.canonical_path.with_name(f".{self.canonical_path.name}.{os.getpid()}.tmp")
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, self.canonical_path)
        self.workspace.publish([Path("paper/introduction.md")])


def _hash(content: str | None) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()
