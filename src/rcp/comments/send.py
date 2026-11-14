"""Comment threads sent to an agent, on the send path their target already uses.

Each caller picks its shape: a steer or a Paper coach turn takes every thread as
text, a chat turn takes one artifact's threads as that artifact's selections, and
the viewer sends one artifact's threads as its selections.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rcp.limits import ARTIFACT_CONTEXT_MAX_SELECTIONS, STEERING_MESSAGE_MAX_CHARS
from rcp.runs.artifact_edit_admission import artifact_comment_message
from rcp.service import ArtifactContextRequest
from rcp.storage.comments import CommentError, CommentTargetRoute, comment_target_not_found

if TYPE_CHECKING:
    from rcp.storage import AppStore
    from rcp.storage.models import AgentTaskRecord

_Routed = list[tuple[dict[str, Any], CommentTargetRoute]]


@dataclass(frozen=True)
class CommentSend:
    text: str
    context: ArtifactContextRequest | None
    rendered_through: dict[str, str]

    def require_replay_of(self, record: AgentTaskRecord) -> None:
        """Refuse a repeated key whose turn carried a different send."""
        context = self.context
        expected = artifact_comment_message(self.text, context) if context else self.text
        same_context = True
        if context is not None:
            stored = record.request.get("artifact_context")
            incoming = context.model_dump(mode="json")
            same_context = isinstance(stored, dict) and all(
                stored.get(field) == incoming[field]
                for field in ("artifact_id", "operation_id", "selections")
            )
        if record.request.get("message") != expected or not same_context:
            raise CommentError(
                "comment_send_key_reused", "This send key belongs to another comment send.", 409
            )


def thread_send_text(
    store: AppStore,
    project_id: str,
    user_id: str,
    thread_ids: list[str],
    key: str,
    *,
    message: str,
    chat_id: str | None,
) -> CommentSend:
    """Every thread rendered after `message`, for a steer or for a Paper coach turn.

    `chat_id` names the chat whose reply and artifact threads may ride; None is the
    Paper coach, which takes Paper threads only.
    """
    routed = _sendable(store, project_id, user_id, thread_ids, key)
    for thread, route in routed:
        if chat_id is None:
            if thread["target_kind"] != "paper":
                raise _wrong_destination()
        else:
            _require_chat(thread, route, user_id, chat_id)
    return CommentSend(_text(message, routed, routed), None, _through(routed))


def chat_turn_send(
    store: AppStore,
    project_id: str,
    user_id: str,
    thread_ids: list[str],
    key: str,
    *,
    message: str,
    chat_id: str,
) -> CommentSend:
    """A chat's new turn: reply threads as text, one artifact's threads as its selections."""
    if not chat_id:
        raise CommentError("comment_send_needs_chat", "Open the chat to send these threads.")
    routed = _sendable(store, project_id, user_id, thread_ids, key)
    for thread, route in routed:
        _require_chat(thread, route, user_id, chat_id)
    artifact_threads = [item for item in routed if item[0]["target_kind"] == "artifact"]
    replies = [item for item in routed if item[0]["target_kind"] != "artifact"]
    text = _text(message, replies, routed)
    context = _artifact_context(store, artifact_threads) if artifact_threads else None
    return CommentSend(text, context, _through(routed))


def artifact_thread_context(
    store: AppStore,
    project_id: str,
    user_id: str,
    thread_ids: list[str],
    key: str,
    *,
    artifact_id: str,
) -> CommentSend:
    """The viewer's send: one artifact's threads as that artifact's selections."""
    routed = _sendable(store, project_id, user_id, thread_ids, key)
    for thread, route in routed:
        if thread["target_kind"] != "artifact" or thread["target_id"] != artifact_id:
            raise CommentError(
                "comment_thread_elsewhere", "A thread belongs to another artifact.", 409
            )
        _require_chat(thread, route, user_id, route.chat_id or "")
    _text("", [], routed)
    return CommentSend("", _artifact_context(store, routed), _through(routed))


def _artifact_context(store: AppStore, routed: _Routed) -> ArtifactContextRequest:
    artifact = store.artifact(routed[0][0]["target_id"])
    if artifact is None:
        raise comment_target_not_found()
    return ArtifactContextRequest.model_validate(
        {
            "source": "task",
            "operation_id": artifact.origin_operation_id,
            "artifact_id": artifact.artifact_id,
            "selections": [
                {
                    **thread["anchor"],
                    "comment": thread["rendered_posts"],
                    "drawn_version": thread["drawn_version"],
                }
                for thread, _ in routed
            ],
        }
    )


def _sendable(
    store: AppStore, project_id: str, user_id: str, thread_ids: list[str], key: str
) -> _Routed:
    if not key or not thread_ids or len(thread_ids) > ARTIFACT_CONTEXT_MAX_SELECTIONS:
        raise CommentError(
            "comment_send_invalid",
            f"Send between 1 and {ARTIFACT_CONTEXT_MAX_SELECTIONS} threads at a time.",
        )
    routed = store.sendable_threads(project_id, user_id, thread_ids, key)
    if (
        len({thread["target_id"] for thread, _ in routed if thread["target_kind"] == "artifact"})
        > 1
    ):
        raise CommentError(
            "comment_send_two_artifacts", "Send comments on one artifact at a time.", 409
        )
    return routed


def _require_chat(
    thread: dict[str, Any], route: CommentTargetRoute, user_id: str, chat_id: str
) -> None:
    if thread["target_kind"] == "paper":
        raise CommentError("comment_send_needs_coach", "Paper comments go to the Paper coach.", 409)
    if route.chat_owner not in {None, user_id}:
        raise comment_target_not_found()
    if not route.chat_id or route.chat_id != chat_id:
        raise _wrong_destination()


def _wrong_destination() -> CommentError:
    return CommentError("comment_thread_elsewhere", "A thread belongs to another chat.", 409)


def _text(message: str, shown: _Routed, routed: _Routed) -> str:
    """`message` and the `shown` threads; the whole send, `routed`, must fit one turn."""
    total = "\n\n".join(part for part in [message, *(t["rendered"] for t, _ in routed)] if part)
    if len(total) > STEERING_MESSAGE_MAX_CHARS:
        raise CommentError(
            "comment_send_too_long",
            f"The send is over {STEERING_MESSAGE_MAX_CHARS:,} characters. Send fewer threads.",
        )
    return "\n\n".join(part for part in [message, *(t["rendered"] for t, _ in shown)] if part)


def _through(routed: _Routed) -> dict[str, str]:
    return {thread["thread_id"]: thread["rendered_through"] for thread, _ in routed}
