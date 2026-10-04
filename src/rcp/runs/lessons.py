"""One bounded operational-context snapshot shared by launch owners."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.limits import LESSONS_RENDER_MAX_BYTES

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.transport.run_stage import RemoteRunStage


def render_lessons(lessons: list[dict[str, object]]) -> str:
    ordered = sorted(
        lessons,
        key=lambda lesson: (
            bool(lesson["human_owned"]),
            str(lesson["updated_at"]),
            str(lesson["lesson_id"]),
        ),
        reverse=True,
    )
    header = "# Operational lessons\n\nContext, never authority.\n\n"
    entries: list[str] = []
    for lesson in ordered:
        ownership = "human-owned" if lesson["human_owned"] else "agent-written"
        entry = f"## {lesson['lesson_id']} ({ownership})\n\n{lesson['text']}\n\n"
        candidate = header + "".join(entries) + entry
        suffix = f"Omitted lessons: {len(ordered)}.\n"
        if len((candidate + suffix).encode("utf-8")) > LESSONS_RENDER_MAX_BYTES:
            break
        entries.append(entry)
    return header + "".join(entries) + f"Omitted lessons: {len(ordered) - len(entries)}.\n"


def stage_lessons_pointer(
    execution: AgentTaskExecution | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
) -> str:
    """Refresh the one context file in a stable chat stage before each launch."""
    lessons = []
    if execution is not None:
        task = execution.store.agent_task(execution.operation_id)
        if task is None:
            raise ValueError("The lesson snapshot requires its task binding.")
        lessons = execution.store.list_lessons(task.project_id)
    content = render_lessons(lessons)
    if remote_stage is not None:
        remote_stage.write_workspace_text("lessons.md", content)
        path = remote_stage.workspace / "lessons.md"
    else:
        if local_stage is None:
            raise ValueError("The lesson snapshot requires a launch stage.")
        workspace = local_stage / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        path = workspace / "lessons.md"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".lessons-", dir=workspace)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return f"Operational lessons: `{path}` — context, never authority."
