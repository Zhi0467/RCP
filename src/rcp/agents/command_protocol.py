from __future__ import annotations

import importlib.resources
import json
import re
from functools import lru_cache
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from rcp.compute_jobs.models import ComputeLaunchRequest
from rcp.history.queries import parse_head
from rcp.limits import (
    ASK_ANSWER_MAX_LENGTH,
    ASK_CHOICE_MAX_COUNT,
    ASK_CHOICE_MAX_LENGTH,
    ASK_QUESTION_MAX_LENGTH,
    BULLETIN_CURSOR_MAX_CHARS,
    BULLETIN_KEY_MAX_CHARS,
    BULLETIN_REPLY_MAX_CHARS,
    BULLETIN_TEXT_MAX_CHARS,
    COMMAND_ASK_POLL_SECONDS,
    EVIDENCE_FLAG_FINDING_MAX_CHARS,
    EVIDENCE_FLAG_SOURCE_MAX_CHARS,
    GRAPH_HISTORY_COMMAND_CURSOR_MAX_LENGTH,
    GRAPH_HISTORY_COMMAND_IDS_MAX_COUNT,
    LESSON_TEXT_MAX_CHARS,
)
from rcp.providers import ProviderId
from rcp.storage import GraphCondition

COMMAND_PROTOCOL_VERSION = 1
COMMAND_OK = 0
COMMAND_INVALID = 1
COMMAND_UNAVAILABLE = 2

CommandVerb = Literal[
    "projects",
    "handoff",
    "validate",
    "merge_answers",
    "graph_diff",
    "graph_log",
    "apply",
    "status",
    "spawn",
    "merge",
    "branch",
    "pause",
    "resume",
    "stop",
    "message",
    "watch_graph",
    "episode",
    "inbox",
    "finish",
    "launch",
    "ask",
    "lesson",
    "bulletin",
    "name",
    "flag",
]
CommandStatus = Literal["ok", "invalid", "unavailable"]
MutatingCommandVerb = Literal[
    "handoff",
    "apply",
    "spawn",
    "merge",
    "branch",
    "pause",
    "resume",
    "stop",
    "message",
    "watch_graph",
    "episode",
    "inbox",
    "finish",
    "launch",
    "ask",
    "lesson",
    "bulletin",
    "name",
    "flag",
]

MUTATING_COMMAND_VERBS: frozenset[CommandVerb] = frozenset(
    {
        "handoff",
        "apply",
        "spawn",
        "merge",
        "branch",
        "pause",
        "resume",
        "stop",
        "message",
        "watch_graph",
        "episode",
        "inbox",
        "finish",
        "launch",
        "ask",
        "lesson",
        "bulletin",
        "name",
        "flag",
    }
)

_MAILBOX_ID = re.compile(r"^[a-f0-9]{32}$")
_REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
_CREDENTIAL = re.compile(r"^[a-f0-9]{64}$")


class CommandCredential(BaseModel):
    """One staged secret whose server-side binding expires with a provider turn."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = COMMAND_PROTOCOL_VERSION
    mailbox_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    token: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProjectsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["list", "show"]
    project_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def project_matches_action(self) -> ProjectsArguments:
        if (self.project_id is not None) != (self.action == "show"):
            raise ValueError("Only projects show requires project_id")
        if self.project_id is not None and not self.project_id.strip():
            raise ValueError("project_id must not be blank")
        return self


class HandoffArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    project_id: str = Field(min_length=1, max_length=200)
    message_file: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._-]+$")

    @model_validator(mode="after")
    def valid_target(self) -> HandoffArguments:
        if not self.project_id.strip() or self.message_file in {".", ".."}:
            raise ValueError("handoff requires a project and a safe workspace filename")
        return self


class ValidateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    patch: str


class MergeAnswersArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["check"]
    answers: str


class ApplyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    patch_file: Literal["patch.json"]


class BranchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    branch_id: str = Field(min_length=1, max_length=200)

    @field_validator("branch_id")
    @classmethod
    def nonblank_branch(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("branch id must not be blank")
        return value


class BranchArchiveArguments(BranchArguments):
    action: Literal["archive"]


class StatusArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    worker_id: str | None = Field(default=None, min_length=1, max_length=200)
    episode_id: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("worker_id", "episode_id")
    @classmethod
    def normalize_target_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("status target id must not be blank")
        return stripped

    @model_validator(mode="after")
    def target_is_unambiguous(self) -> StatusArguments:
        if self.worker_id is not None and self.episode_id is not None:
            raise ValueError("status accepts either a worker id or an episode id, not both")
        return self


_SAFE_WORKSPACE_FILENAME = r"^[A-Za-z0-9._-]+$"


class GraphDiffArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, serialize_by_alias=True)

    project: str | None = Field(default=None, min_length=1)
    from_head: str | None = Field(default=None, alias="from")
    to_head: str | None = Field(default=None, alias="to")
    branch_changes: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,200}$")
    ids: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list, max_length=GRAPH_HISTORY_COMMAND_IDS_MAX_COUNT
    )
    cursor: str | None = Field(
        default=None, min_length=1, max_length=GRAPH_HISTORY_COMMAND_CURSOR_MAX_LENGTH
    )

    @field_validator("from_head", "to_head")
    @classmethod
    def valid_head(cls, value: str | None) -> str | None:
        if value is not None:
            parse_head(value)
        return value

    @model_validator(mode="after")
    def valid_range(self) -> GraphDiffArguments:
        if self.branch_changes is not None:
            if self.from_head is not None or self.to_head is not None:
                raise ValueError("branch_changes cannot be combined with from or to")
        elif self.from_head is None or self.to_head is None:
            raise ValueError("graph_diff requires from and to, or branch_changes")
        return self


class GraphLogArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    project: str | None = Field(default=None, min_length=1)
    id: str = Field(min_length=1)
    at: str = "@head"
    cursor: str | None = Field(
        default=None, min_length=1, max_length=GRAPH_HISTORY_COMMAND_CURSOR_MAX_LENGTH
    )

    @field_validator("at")
    @classmethod
    def valid_head(cls, value: str) -> str:
        parse_head(value)
        return value


class _ChildTargetArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    at: str | None = Field(default=None, exclude_if=lambda value: value is None)
    target: str | None = Field(
        default=None, min_length=1, max_length=200, exclude_if=lambda value: value is None
    )
    seed_file: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=_SAFE_WORKSPACE_FILENAME,
        exclude_if=lambda value: value is None,
    )

    @field_validator("at")
    @classmethod
    def exact_fork_head(cls, value: str | None) -> str | None:
        if value is not None:
            head = parse_head(value)
            if head.target is None or head.revision == "head":
                raise ValueError("at requires an explicit graph ref and numeric revision")
        return value

    @field_validator("target")
    @classmethod
    def nonblank_target(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("target must not be blank")
        return value

    @model_validator(mode="after")
    def valid_target_options(self) -> _ChildTargetArguments:
        if self.at is not None and self.target is not None:
            raise ValueError("at and target are mutually exclusive")
        if self.seed_file is not None and self.at is None:
            raise ValueError("seed_file requires at")
        return self


class SpawnArguments(_ChildTargetArguments):
    seat_node_id: str | None = Field(
        default=None, min_length=1, max_length=200, exclude_if=lambda value: value is None
    )
    project: bool = Field(default=False, exclude_if=lambda value: not value)
    instruction_file: str = Field(
        min_length=1,
        max_length=255,
        pattern=_SAFE_WORKSPACE_FILENAME,
    )
    # An optional orchestrator pick of the child's provider profile. Omitted fields
    # resolve from the Settings node Work profile; absent ones stay out of the dump
    # so a record admitted before these fields existed replays unchanged.
    provider: ProviderId | None = Field(default=None, exclude_if=lambda value: value is None)
    model: str | None = Field(
        default=None, min_length=1, max_length=200, exclude_if=lambda value: value is None
    )
    reasoning: str | None = Field(
        default=None, min_length=1, max_length=100, exclude_if=lambda value: value is None
    )

    @field_validator("seat_node_id")
    @classmethod
    def strip_nonblank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("seat node id must not be blank")
        return stripped

    @model_validator(mode="after")
    def exactly_one_seat(self) -> SpawnArguments:
        if (self.seat_node_id is not None) == self.project:
            raise ValueError("spawn requires exactly one of seat_node_id and project")
        return self


class WorkerControlArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    worker_id: str = Field(min_length=1, max_length=200)


class MessageArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    recipient_task_id: str | None = Field(default=None, min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=16_000)

    @field_validator("body")
    @classmethod
    def strip_nonblank_body(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message body must not be blank")
        return stripped


class WatchGraphArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    condition: GraphCondition
    reason: str = Field(min_length=1, max_length=2_000)

    @field_validator("reason")
    @classmethod
    def strip_nonblank_reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("watch reason must not be blank")
        return stripped


class ExperimentKickoffArguments(_ChildTargetArguments):
    action: Literal["kick_off_experiment"]
    node_id: str = Field(min_length=1, max_length=200)
    goal_file: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=_SAFE_WORKSPACE_FILENAME,
    )
    invocation_limit: int | None = Field(default=None, gt=0)
    # The same optional provider-profile pick as Spawn.
    provider: ProviderId | None = Field(default=None, exclude_if=lambda value: value is None)
    model: str | None = Field(
        default=None, min_length=1, max_length=200, exclude_if=lambda value: value is None
    )
    reasoning: str | None = Field(
        default=None, min_length=1, max_length=100, exclude_if=lambda value: value is None
    )

    @field_validator("node_id")
    @classmethod
    def strip_nonblank_node_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Experiment node id must not be blank")
        return stripped


class EpisodeControlArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["stop", "resume"]
    episode_id: str = Field(min_length=1, max_length=200)

    @field_validator("episode_id")
    @classmethod
    def strip_nonblank_episode_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("episode id must not be blank")
        return stripped


EpisodeArguments: TypeAlias = Annotated[
    ExperimentKickoffArguments | EpisodeControlArguments,
    Field(discriminator="action"),
]


class InboxHarvestArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["harvest"]


class InboxClearArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["clear"]


InboxArguments: TypeAlias = Annotated[
    InboxHarvestArguments | InboxClearArguments,
    Field(discriminator="action"),
]


class FinishArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LaunchArguments(ComputeLaunchRequest):
    model_config = ConfigDict(extra="forbid", strict=True)


class AskArguments(BaseModel):
    """One keyed human question; omitted options have one canonical identity."""

    model_config = ConfigDict(extra="forbid", strict=True)

    question: str = Field(min_length=1, max_length=ASK_QUESTION_MAX_LENGTH)
    choices: list[Annotated[str, Field(min_length=1, max_length=ASK_CHOICE_MAX_LENGTH)]] = Field(
        default_factory=list, max_length=ASK_CHOICE_MAX_COUNT
    )
    multiple: bool = False

    @model_validator(mode="after")
    def valid_choices(self) -> AskArguments:
        if not self.question.strip() or any(not choice.strip() for choice in self.choices):
            raise ValueError("question and choices must not be blank")
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("choices must be distinct")
        if self.multiple and not self.choices:
            raise ValueError("multiple requires choices")
        return self


class FlagArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    evidence_id: str = Field(min_length=1, max_length=200)
    kind: Literal["overclaim", "underclaim", "no_source", "unverified"]
    finding: str = Field(min_length=1, max_length=EVIDENCE_FLAG_FINDING_MAX_CHARS)
    source: str | None = Field(default=None, max_length=EVIDENCE_FLAG_SOURCE_MAX_CHARS)

    @field_validator("finding")
    @classmethod
    def single_line_finding(cls, value: str) -> str:
        if not value.strip() or value.splitlines() != [value]:
            raise ValueError("finding must be non-blank and single line")
        return value


class NameArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str


class BulletinArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["post", "read", "artifact", "link", "edit", "delete"]
    text: str | None = Field(default=None, min_length=1, max_length=BULLETIN_TEXT_MAX_CHARS)
    post_id: str | None = Field(default=None, min_length=1, max_length=BULLETIN_REPLY_MAX_CHARS)
    reply_to: str | None = Field(default=None, min_length=1, max_length=BULLETIN_REPLY_MAX_CHARS)
    cursor: str | None = Field(default=None, min_length=1, max_length=BULLETIN_CURSOR_MAX_CHARS)

    channel: str | None = Field(default=None, min_length=1, max_length=BULLETIN_REPLY_MAX_CHARS)
    artifact_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    # One direct entry of this turn's artifact directory, never a path.
    file_name: str | None = Field(
        default=None, min_length=1, max_length=255, pattern=r"^[^/\\\x00]+$"
    )

    @model_validator(mode="after")
    def valid_action(self) -> BulletinArguments:
        if (self.artifact_id is not None) != (self.action == "artifact"):
            raise ValueError("Only artifact reads require an artifact id")
        if self.channel is not None and (
            self.action not in {"read", "post"}
            or self.reply_to is not None
            or not self.channel.strip()
        ):
            raise ValueError("Only Bulletin reads and new posts accept a channel")
        if (self.file_name is not None) != (self.action == "link"):
            raise ValueError("Only artifact links require an artifact file name")
        if self.action == "link":
            if self.file_name in {".", ".."} or any(
                value is not None for value in (self.text, self.post_id, self.reply_to, self.cursor)
            ):
                raise ValueError("Artifact links accept only an artifact file name")
        elif self.action == "artifact":
            if any(
                value is not None for value in (self.text, self.post_id, self.reply_to, self.cursor)
            ):
                raise ValueError("Artifact reads accept only an artifact id")
        elif self.action == "read":
            if any(value is not None for value in (self.text, self.post_id, self.reply_to)):
                raise ValueError("Bulletin reads accept only a cursor")
        else:
            if self.cursor is not None:
                raise ValueError("Bulletin mutations accept no cursor")
            if self.action == "delete":
                if self.text is not None:
                    raise ValueError("Bulletin deletes accept no text")
            elif self.text is None or not self.text.strip():
                raise ValueError("Bulletin posts and edits require nonblank text")
            if self.action in {"edit", "delete"}:
                if self.post_id is None or self.reply_to is not None:
                    raise ValueError(
                        "Bulletin edits and deletes require a post id and no reply target"
                    )
            elif self.post_id is not None:
                raise ValueError("Bulletin posts accept no post id")
        return self


class LessonArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["add", "list", "update", "delete"]
    lesson_id: str | None = Field(default=None, min_length=1, max_length=200)
    text: str | None = Field(default=None, min_length=1, max_length=LESSON_TEXT_MAX_CHARS)
    cursor: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_action(self) -> LessonArguments:
        if (self.text is not None) != (self.action in {"add", "update"}):
            raise ValueError("lesson text is required only for add and update")
        if self.text is not None and not self.text.strip():
            raise ValueError("lesson text must not be blank")
        if (self.lesson_id is not None) != (self.action in {"update", "delete"}):
            raise ValueError("lesson id is required only for update and delete")
        if self.cursor is not None and self.action != "list":
            raise ValueError("only lesson list accepts a cursor")
        return self


class AskResult(BaseModel):
    """Question state is independent of the command envelope and transport delivery."""

    model_config = ConfigDict(extra="forbid", strict=True)

    receipt_token: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    attempt: int | None = Field(default=None, ge=1)
    max_attempts: int | None = Field(default=None, ge=1)
    state: Literal["pending", "answered", "dismissed", "parked"]
    question_id: str = Field(min_length=1)
    answer: str | None = Field(default=None, max_length=ASK_ANSWER_MAX_LENGTH)
    choices: list[Annotated[str, Field(min_length=1, max_length=ASK_CHOICE_MAX_LENGTH)]] = Field(
        default_factory=list, max_length=ASK_CHOICE_MAX_COUNT
    )

    @model_validator(mode="after")
    def resolution_matches_state(self) -> AskResult:
        if self.state == "answered":
            if self.answer is None:
                raise ValueError("answered requires answer text")
        elif self.answer is not None or self.choices:
            raise ValueError("only answered questions carry a resolution")
        return self


class _CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = COMMAND_PROTOCOL_VERSION
    mailbox_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    credential: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("idempotency key must not be blank")
        return stripped


class ProjectsCommandRequest(_CommandRequest):
    verb: Literal["projects"]
    arguments: ProjectsArguments


class HandoffCommandRequest(_CommandRequest):
    verb: Literal["handoff"]
    arguments: HandoffArguments

    @model_validator(mode="after")
    def mutation_is_keyed(self) -> HandoffCommandRequest:
        if self.idempotency_key is None:
            raise ValueError("handoff requires an idempotency key")
        return self


class ValidateCommandRequest(_CommandRequest):
    verb: Literal["validate"]
    arguments: ValidateArguments


class MergeAnswersCommandRequest(_CommandRequest):
    verb: Literal["merge_answers"]
    arguments: MergeAnswersArguments


class ApplyCommandRequest(_CommandRequest):
    verb: Literal["apply"]
    arguments: ApplyArguments


class StatusCommandRequest(_CommandRequest):
    verb: Literal["status"]
    arguments: StatusArguments = Field(default_factory=StatusArguments)


class GraphDiffCommandRequest(_CommandRequest):
    verb: Literal["graph_diff"]
    arguments: GraphDiffArguments


class GraphLogCommandRequest(_CommandRequest):
    verb: Literal["graph_log"]
    arguments: GraphLogArguments


class MergeCommandRequest(_CommandRequest):
    verb: Literal["merge"]
    arguments: BranchArguments


class BranchCommandRequest(_CommandRequest):
    verb: Literal["branch"]
    arguments: BranchArchiveArguments


class SpawnCommandRequest(_CommandRequest):
    verb: Literal["spawn"]
    arguments: SpawnArguments


class PauseCommandRequest(_CommandRequest):
    verb: Literal["pause"]
    arguments: WorkerControlArguments


class ResumeCommandRequest(_CommandRequest):
    verb: Literal["resume"]
    arguments: WorkerControlArguments


class StopCommandRequest(_CommandRequest):
    verb: Literal["stop"]
    arguments: WorkerControlArguments


class MessageCommandRequest(_CommandRequest):
    verb: Literal["message"]
    arguments: MessageArguments


class WatchGraphCommandRequest(_CommandRequest):
    verb: Literal["watch_graph"]
    arguments: WatchGraphArguments


class EpisodeCommandRequest(_CommandRequest):
    verb: Literal["episode"]
    arguments: EpisodeArguments


class InboxCommandRequest(_CommandRequest):
    verb: Literal["inbox"]
    arguments: InboxArguments


class FinishCommandRequest(_CommandRequest):
    verb: Literal["finish"]
    arguments: FinishArguments = Field(default_factory=FinishArguments)


class LaunchCommandRequest(_CommandRequest):
    idempotency_key: str = Field(min_length=1, max_length=200)
    verb: Literal["launch"]
    arguments: LaunchArguments


class AskCommandRequest(_CommandRequest):
    call_id: str | None = Field(default=None, min_length=1, max_length=200)
    receipt_token: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=200)
    verb: Literal["ask"]
    arguments: AskArguments


class FlagCommandRequest(_CommandRequest):
    verb: Literal["flag"]
    arguments: FlagArguments

    @model_validator(mode="after")
    def mutation_is_keyed(self) -> FlagCommandRequest:
        if self.idempotency_key is None:
            raise ValueError("flag requires an idempotency key")
        return self


class NameCommandRequest(_CommandRequest):
    verb: Literal["name"]
    arguments: NameArguments


class BulletinCommandRequest(_CommandRequest):
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=BULLETIN_KEY_MAX_CHARS
    )

    verb: Literal["bulletin"]
    arguments: BulletinArguments

    @model_validator(mode="after")
    def mutation_is_keyed(self) -> BulletinCommandRequest:
        if (
            self.arguments.action not in {"read", "artifact", "link"}
            and self.idempotency_key is None
        ):
            raise ValueError("Bulletin mutations require an idempotency key")
        return self


class LessonCommandRequest(_CommandRequest):
    verb: Literal["lesson"]
    arguments: LessonArguments

    @model_validator(mode="after")
    def mutation_is_keyed(self) -> LessonCommandRequest:
        if self.arguments.action != "list" and self.idempotency_key is None:
            raise ValueError("lesson mutations require an idempotency key")
        return self


CommandRequest: TypeAlias = Annotated[
    ValidateCommandRequest
    | ProjectsCommandRequest
    | HandoffCommandRequest
    | MergeAnswersCommandRequest
    | ApplyCommandRequest
    | StatusCommandRequest
    | GraphDiffCommandRequest
    | GraphLogCommandRequest
    | MergeCommandRequest
    | BranchCommandRequest
    | SpawnCommandRequest
    | PauseCommandRequest
    | ResumeCommandRequest
    | StopCommandRequest
    | MessageCommandRequest
    | WatchGraphCommandRequest
    | EpisodeCommandRequest
    | InboxCommandRequest
    | FinishCommandRequest
    | LaunchCommandRequest
    | AskCommandRequest
    | NameCommandRequest
    | BulletinCommandRequest
    | LessonCommandRequest
    | FlagCommandRequest,
    Field(discriminator="verb"),
]
COMMAND_REQUEST_ADAPTER = TypeAdapter(CommandRequest)


class CommandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = COMMAND_PROTOCOL_VERSION
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    status: CommandStatus
    message: str | None = Field(default=None, max_length=2_000)
    result: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def outcome_has_a_diagnostic(self) -> CommandResponse:
        if self.status != "ok" and not (self.message or "").strip():
            raise ValueError("an unsuccessful command response requires a diagnostic")
        return self

    @property
    def exit_code(self) -> int:
        return {
            "ok": COMMAND_OK,
            "invalid": COMMAND_INVALID,
            "unavailable": COMMAND_UNAVAILABLE,
        }[self.status]


def validate_command_request(value: str | bytes) -> CommandRequest:
    """Validate one exact request envelope, including verb-specific arguments."""

    return COMMAND_REQUEST_ADAPTER.validate_json(value)


def command_requires_idempotency_key(verb: CommandVerb) -> bool:
    return verb in MUTATING_COMMAND_VERBS


@lru_cache(maxsize=1)
def staged_command_client_source() -> str:
    """Load the one tested stdlib client source shipped to local or SSH stages."""

    source = (
        importlib.resources.files("rcp.agents")
        .joinpath("staged_command_client.py")
        .read_text(encoding="utf-8")
    )
    marker = "COMMAND_ASK_POLL_SECONDS = None"
    if source.count(marker) != 1:
        raise RuntimeError("staged command client lost its poll-limit marker")
    return source.replace(marker, f"COMMAND_ASK_POLL_SECONDS = {COMMAND_ASK_POLL_SECONDS!r}")


@lru_cache(maxsize=1)
def staged_command_broker_source() -> str:
    """Load the stdlib broker that authenticates one live provider process tree."""

    return (
        importlib.resources.files("rcp.agents")
        .joinpath("staged_command_broker.py")
        .read_text(encoding="utf-8")
    )


def command_authentication_payload(document: str) -> bytes:
    """Canonical bytes covered by a turn broker's per-request HMAC.

    The broker signs the request exactly as the client wrote it, so verification
    has to canonicalize that same text. Rebuilding the payload from the validated
    model instead would re-serialize whatever validation normalized — a sorted
    ``status_in``, a stripped string, a filled default — and the two sides would
    stop agreeing for reasons that have nothing to do with authenticity.
    """

    value = json.loads(document)
    if not isinstance(value, dict):
        raise ValueError("command request must be one JSON object")
    value.pop("credential", None)
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def request_identity_is_well_formed(request: CommandRequest) -> bool:
    return (
        _MAILBOX_ID.fullmatch(request.mailbox_id) is not None
        and _REQUEST_ID.fullmatch(request.request_id) is not None
        and _CREDENTIAL.fullmatch(request.credential) is not None
    )
