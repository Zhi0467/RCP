"""Lesson commands shared by concrete owners of served mutating mailboxes."""

from __future__ import annotations

from rcp.agents.command_protocol import CommandResponse, LessonCommandRequest
from rcp.storage import AppStore
from rcp.storage.lessons import LessonError


def handle_lesson(
    store: AppStore, operation_id: str, request: LessonCommandRequest
) -> CommandResponse:
    task = store.agent_task(operation_id)
    if task is None:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message="The lesson command task is unavailable.",
            result={"code": "lesson_task_unavailable"},
        )
    try:
        result = store.execute_lesson_command(
            task.project_id,
            operation_id,
            request.arguments.action,
            key=request.idempotency_key,
            lesson_id=request.arguments.lesson_id,
            text=request.arguments.text,
            cursor=request.arguments.cursor,
        )
    except LessonError as exc:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message=str(exc),
            result={"code": exc.code},
        )
    except ValueError as exc:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message=str(exc),
            result={"code": "lesson_write_unavailable"},
        )
    return CommandResponse(request_id=request.request_id, status="ok", result=result)


def lesson_command_usage(*, edit_authorized: bool, client: str) -> str:
    usages = ["lesson add --key <key> --text <text>"]
    if edit_authorized:
        usages.extend(
            (
                "lesson list [--cursor <cursor>]",
                "lesson update --key <key> --id <lesson-id> --text <text>",
                "lesson delete --key <key> --id <lesson-id>",
            )
        )
    return "\n".join(f"- `{client} {usage}`" for usage in usages)
