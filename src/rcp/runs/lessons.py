"""One bounded operational-context snapshot shared by launch owners."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rcp.limits import LESSONS_RENDER_MAX_BYTES
from rcp.transport.workspace_mailbox import RunStageMailbox

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
        path = remote_stage.workspace / "lessons.md"
        mailbox = RunStageMailbox.for_stage(local_stage=None, remote_stage=remote_stage)
    else:
        if local_stage is None:
            raise ValueError("The lesson snapshot requires a launch stage.")
        workspace = local_stage / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        path = workspace / "lessons.md"
        mailbox = RunStageMailbox.for_stage(local_stage=workspace, remote_stage=None)
    mailbox.write_text("lessons.md", content)
    return f"Operational lessons: `{path}` — context, never authority."


def stage_operational_context_pointer(
    execution: AgentTaskExecution | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
) -> str:
    """Per-turn snapshot pointers only; command usage belongs to each contract builder."""
    from rcp.runs.agent_names import stage_agent_name_pointer
    from rcp.runs.bulletin import stage_bulletin_pointer

    return "\n".join(
        filter(
            None,
            (
                stage_agent_name_pointer(execution, local_stage, remote_stage),
                stage_lessons_pointer(execution, local_stage, remote_stage),
                stage_bulletin_pointer(execution, local_stage, remote_stage),
            ),
        )
    )
