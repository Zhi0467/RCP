"""A Paper written by the SQLite-era writer: saved tree, CRDT tail, history, Coach, cards."""

from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from pathlib import Path

from pycrdt import Doc, Text

from rcp.storage import AgentTaskRecord, AppStore

FIGURE = b"\x89PNG\r\n\x1a\nlegacy figure"
INTRO = "Hello world"
CHAPTER = "Chapter one\n"


@dataclass(frozen=True)
class LegacyPaper:
    project_id: str
    running_coach: str
    finished_coach: str
    pending_group: str
    git_group: str


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_state(prefix: str, suffix: str) -> tuple[bytes, bytes]:
    """A compacted prefix and the separate update that appends the suffix."""
    doc = Doc({"text": Text()}, skip_gc=False)
    doc["text"].insert(0, prefix)
    state = doc.get_update()
    before = doc.get_state()
    doc["text"].insert(len(prefix), suffix)
    return state, doc.get_update(before)


def _coach(store: AppStore, project_id: str, operation_id: str, status: str) -> None:
    now = store.now()
    store.create_agent_task(
        AgentTaskRecord(
            operation_id=operation_id,
            project_id=project_id,
            kind="paper_coach",
            status="running",
            request={"message": f"Coach request {operation_id}", "mode": "review"},
            created_at=now,
            updated_at=now,
            status_message="running",
        )
    )
    if status != "running":
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status=?, result_json=?, finished_at=? WHERE operation_id=?",
                (status, json.dumps({"answer": f"Coach answer {operation_id}"}), now, operation_id),
            )


def seed_legacy_paper(store: AppStore, project_id: str, state_root: Path | None) -> LegacyPaper:
    """Rows exactly as the SQLite-era writer left them, plus its `.research/paper` copy."""
    intro_state, intro_tail = _text_state("Hello", " world")
    chapter_doc = Doc({"text": Text()}, skip_gc=False)
    chapter_doc["text"].insert(0, CHAPTER)
    texts = {INTRO: _sha(INTRO.encode()), CHAPTER: _sha(CHAPTER.encode()), "Hello": _sha(b"Hello")}
    saved = [
        {
            "file_id": "intro",
            "path": "sections/intro.md",
            "kind": "text",
            "text_sha256": texts[INTRO],
        },
        {
            "file_id": "chapter",
            "path": "chapter.tex",
            "kind": "text",
            "text_sha256": texts[CHAPTER],
        },
        {
            "file_id": "figure",
            "path": "figures/plot.png",
            "kind": "binary",
            "blob_sha256": _sha(FIGURE),
        },
    ]
    named = [{**saved[0], "text_sha256": texts["Hello"]}]
    with store.connection() as connection:
        for text, digest in texts.items():
            connection.execute(
                "INSERT OR IGNORE INTO paper_version_texts VALUES (?, ?)",
                (digest, zlib.compress(text.encode())),
            )
        connection.execute(
            "INSERT OR IGNORE INTO paper_blobs VALUES (?, ?, ?)",
            (_sha(FIGURE), FIGURE, len(FIGURE)),
        )
        for entry in saved:
            connection.execute(
                "INSERT INTO paper_files(project_id,file_id,path,kind,created_at,blob_sha256) "
                "VALUES (?,?,?,?,'2026-01-01T00:00:00+00:00',?)",
                (
                    project_id,
                    entry["file_id"],
                    entry["path"],
                    entry["kind"],
                    entry.get("blob_sha256"),
                ),
            )
        connection.execute(
            "INSERT INTO paper_files(project_id,file_id,path,kind,created_at,deleted_at) "
            "VALUES (?, 'gone', 'old.md', 'text', '2026-01-01T00:00:00+00:00', "
            "'2026-01-02T00:00:00+00:00')",
            (project_id,),
        )
        connection.execute(
            "INSERT INTO paper_doc_states(project_id,file_id,state,through_seq,through_revision) "
            "VALUES (?, 'intro', ?, 1, 1)",
            (project_id, intro_state),
        )
        connection.execute(
            "INSERT INTO paper_updates VALUES (?, 'intro', 2, ?, 2, 'human', "
            "'2026-01-01T00:00:01+00:00')",
            (project_id, intro_tail),
        )
        connection.execute(
            "INSERT INTO paper_updates VALUES (?, 'chapter', 1, ?, 1, 'human', "
            "'2026-01-01T00:00:00+00:00')",
            (project_id, chapter_doc.get_update()),
        )
        connection.execute(
            "INSERT INTO paper_revisions VALUES (?, 2, '2026-01-01T00:00:01+00:00', 'human', 'text')",
            (project_id,),
        )
        connection.execute(
            "INSERT INTO paper_publication VALUES (?, 2, 1, '2026-01-01T00:00:01+00:00', NULL, ?, "
            "'2026-01-01T00:00:01+00:00')",
            (project_id, json.dumps({"canonical_version": "named", "canonical_unknown": False})),
        )
        connection.execute(
            "INSERT INTO paper_saved_snapshots VALUES (?, 2, ?)", (project_id, json.dumps(saved))
        )
        connection.execute(
            "INSERT INTO paper_versions VALUES (?, 'named', 1, 'named', 'Legacy canonical', "
            "'migration', '2026-01-01T00:00:00+00:00', ?)",
            (project_id, json.dumps(named)),
        )
    running, finished = f"{project_id}-coach-running", f"{project_id}-coach-finished"
    _coach(store, project_id, finished, "succeeded")
    _coach(store, project_id, running, "running")
    change = {"path": "sections/intro.md", "kind": "edit", "before": "Hello", "after": "Hi"}
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO paper_coach_steers VALUES (?, ?, 'Also fix the title', 1, 't', "
            "'delivered', 'Follow-up', NULL, '2026-01-01T00:00:02+00:00')",
            (f"{project_id}-steer", finished),
        )
        for group, task in ((f"{project_id}-cards", finished), (f"{project_id}-git", "paper-git")):
            connection.execute(
                "INSERT INTO paper_change_groups VALUES (?, ?, ?, 2, 'review', 'pending', NULL, "
                "NULL, '2026-01-01T00:00:03+00:00')",
                (project_id, group, task),
            )
            connection.execute(
                "INSERT INTO paper_changes VALUES (?, ?, ?, ?)",
                (project_id, f"{group}-change", group, json.dumps(change)),
            )
    if state_root is not None:
        projection = state_root / "paper" / "sections"
        projection.mkdir(parents=True, exist_ok=True)
        (projection / "intro.md").write_text("Hello")
    return LegacyPaper(project_id, running, finished, f"{project_id}-cards", f"{project_id}-git")


def legacy_tree() -> dict[str, bytes]:
    return {
        "chapter.tex": CHAPTER.encode(),
        "figures/plot.png": FIGURE,
        "sections/intro.md": INTRO.encode(),
    }
