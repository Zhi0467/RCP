from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from rcp.agents.context import (
    ContextAssembler,
    RunContext,
    SessionPointer,
    SessionRoutingIndex,
    bounded_session_metadata,
    with_session_routing,
    write_session_routing_index,
)
from rcp.core.models import GraphState
from rcp.sources import ConversationIndex, ConversationSession, ConversationSlice


def _sessions(count: int) -> list[SessionPointer]:
    return [
        SessionPointer(
            key=f"repo-a/laptop/codex/session-{index:04d}",
            provider="codex",
            machine="laptop",
            path=f"/derived/session-slices/{index:04d}/records.jsonl",
            last_timestamp=datetime(2026, 7, 29, tzinfo=UTC),
            last_uuid=f"record-{index:04d}",
            record_count=index + 1,
            slice_record_count=1,
            slice_sha256=f"{index:064x}",
        )
        for index in range(count)
    ]


def test_ingest_sessions_use_complete_immutable_index_and_bounded_inline_rows(
    tmp_path: Path,
) -> None:
    sessions = _sessions(200)
    pointer = write_session_routing_index(sessions, tmp_path / "routing")
    inline, omitted = bounded_session_metadata(sessions)
    path = Path(pointer.path)

    assert pointer.session_count == 200
    assert hashlib.sha256(path.read_bytes()).hexdigest() == pointer.sha256
    index = SessionRoutingIndex.model_validate_json(path.read_text(encoding="utf-8"))
    assert index.sessions == sessions
    assert len(inline) <= 40
    assert omitted == 200 - len(inline)
    assert len(
        json.dumps(
            [item.model_dump(mode="json") for item in inline],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ) <= 12 * 1024
    assert path.stat().st_mode & 0o222 == 0


def test_run_prompt_payload_does_not_inline_the_full_session_table(tmp_path: Path) -> None:
    sessions = _sessions(200)
    pointer = write_session_routing_index(sessions, tmp_path / "routing")
    inline, omitted = bounded_session_metadata(sessions)
    context = RunContext(
        project_name="bounded",
        run_truth_scope=["repo-a"],
        repositories=[],
        sessions=sessions,
        sessions_inline=inline,
        sessions_omitted=omitted,
        session_routing_index=pointer,
        graph_revision=0,
        graph_path="/state/graph.json",
        research_md_path="/state/research.md",
        introduction_path=None,
        glossary_path="/state/glossary.json",
        coverage_path="/state/coverage.json",
        facts_dir="/state/facts",
        state_repository="repo-a",
        source_errors=[],
    )

    payload = context.prompt_payload()

    assert len(payload["sessions"]) == len(inline)
    assert payload["sessions_omitted"] == omitted
    assert payload["session_routing_index"]["session_count"] == 200
    assert "session-0199" not in json.dumps(payload)
    assert "session-0199" in Path(pointer.path).read_text(encoding="utf-8")

    staged = [
        session.model_copy(update={"path": f"/remote/{index:04d}.jsonl"})
        for index, session in enumerate(sessions)
    ]
    rebound = with_session_routing(
        context,
        staged,
        tmp_path / "remote-routing",
        exposed_path="/remote/session-routing.json",
    )
    assert rebound.sessions[-1].path == "/remote/0199.jsonl"
    assert rebound.session_routing_index is not None
    assert rebound.session_routing_index.path == "/remote/session-routing.json"
    assert rebound.sessions_omitted == 200 - len(rebound.sessions_inline)


def test_context_assembly_keeps_prior_slices_active_during_large_run(
    manifest,
    tmp_path: Path,
) -> None:
    class RecordingIndexer:
        def __init__(self) -> None:
            self.cache_root = tmp_path / "source-cache"
            self.cursor_repairs: dict[str, str] = {}
            self.active_counts: list[int] = []

        def materialize_slice(
            self,
            session: ConversationSession,
            *,
            from_uuid: str | None = None,
            active_paths=(),
            pin_artifact=None,
        ) -> ConversationSlice:
            del from_uuid
            self.active_counts.append(len(tuple(active_paths)))
            result = ConversationSlice(
                path=str(tmp_path / "slices" / f"{session.session_id}.jsonl"),
                record_count=1,
                content_sha256="a" * 64,
            )
            if pin_artifact is not None:
                pin_artifact(Path(result.path))
            return result

        def register_session_artifact(self, path, *, active_paths=()) -> None:
            del path, active_paths

    sessions = [
        ConversationSession(
            key=f"repo-a/laptop/codex/session-{index:04d}",
            provider="codex",
            source_machine="laptop",
            truth_repository="repo-a",
            session_id=f"session-{index:04d}",
            cwd=manifest.repository_map["repo-a"].path,
            path=f"/provider/session-{index:04d}.jsonl",
            last_uuid=f"record-{index:04d}",
            record_count=1,
        )
        for index in range(513)
    ]
    indexer = RecordingIndexer()

    context = ContextAssembler(
        manifest,
        indexer,  # type: ignore[arg-type]
        session_routing_root=tmp_path / "routing",
    ).assemble(
        GraphState(project_truth_scope=manifest.project.truth_scope),
        ConversationIndex(
            generated_at=datetime(2026, 7, 29, tzinfo=UTC),
            sessions=sessions,
        ),
    )

    assert len(context.sessions) == 513
    assert indexer.active_counts == list(range(513))
