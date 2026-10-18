from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from types import UnionType
from typing import Annotated, Any, Literal, TypeAlias, Union, get_args, get_origin

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined, to_jsonable_python

from rcp.core.transition_models import GraphHeadRef
from rcp.limits import (
    HIDDEN_READ_ENV_MAX_COUNT,
    HIDDEN_READ_ENV_NAME_MAX_LENGTH,
    HIDDEN_READ_IDENTITY_MAX_LENGTH,
    HIDDEN_READ_KEY_MAX_COUNT,
    HIDDEN_READ_PATH_MAX_COUNT,
    HIDDEN_READ_PATH_MAX_LENGTH,
    HIDDEN_READ_REASON_MAX_COUNT,
)
from rcp.providers import PROVIDER_IDS


@dataclass(frozen=True)
class GraphField:
    """Semantic comparison role plus the concrete owner's independent write policy.

    These annotation metadata never enter persisted documents or agent schemas.
    Content includes judgments and retained historical values even when only a
    dedicated operation or the engine can write them. Identity names an entity;
    engine fields are recomputed bookkeeping, excluded from semantic comparison.
    """

    role: Literal["content", "engine", "identity"]
    node_update: Literal["immutable", "replay_only"] | None = None
    proposal_owner: Literal["filing", "resolution"] | None = None


def graph_field(field: FieldInfo) -> GraphField:
    """Read exactly one explicit role; unclassified or multiply classified fails."""

    roles = [item for item in field.metadata if isinstance(item, GraphField)]
    if len(roles) != 1:
        raise ValueError("a graph field requires exactly one GraphField annotation")
    return roles[0]


def graph_fields(
    model: type[BaseModel],
    *,
    role: Literal["content", "engine", "identity"] | None = None,
    node_update: Literal["immutable", "replay_only"] | None = None,
    proposal_owner: Literal["filing", "resolution"] | None = None,
) -> frozenset[str]:
    """Select declared field metadata, including fields inherited by this model."""

    result: set[str] = set()
    for name, field in model.model_fields.items():
        metadata = graph_field(field)
        if (
            (role is None or metadata.role == role)
            and (node_update is None or metadata.node_update == node_update)
            and (proposal_owner is None or metadata.proposal_owner == proposal_owner)
        ):
            result.add(name)
    return frozenset(result)


DISPLAY_NAME_MAX_LENGTH = 120
EVIDENCE_ASSESSMENT_SCOPE_MAX_LENGTH = 500
EVIDENCE_ASSESSMENT_QUALIFICATION_MAX_LENGTH = 300
EVIDENCE_ASSESSMENT_MAX_QUALIFICATIONS = 12


def _absolute_hidden_read_path(value: str) -> str:
    # Lexical validation only: the resolver must canonicalize on the execution
    # host and check protected overlaps. Never resolve remote paths locally.
    if (
        not PurePosixPath(value).is_absolute()
        or ".." in value.split("/")
        or str(PurePosixPath(value)) != value
        or value.startswith("//")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("hidden-read paths must be canonical absolute POSIX paths")
    return value


# Outside every write root; tool calls can neither read nor rewrite the policy.
HIDDEN_READ_WRAPPER_ROOT = ".rcp/hidden-read"
HiddenReadPath = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=HIDDEN_READ_PATH_MAX_LENGTH),
    AfterValidator(_absolute_hidden_read_path),
]
HiddenReadReason = Literal[
    "wrapper_unavailable",
    "userns_blocked",
    "provider_native_tools_uncovered",
    "browser_unwrapped_macos",
    "deploy_key_agent_unconfirmed",
    "ssh_key_agent_unconfirmed",
    "credential_compatibility_exception",
    "hidden_folder_conflict",
]


class HiddenReadStatus(BaseModel):
    """Conservative launch status; any uncovered surface requires reasons."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: Literal["enforced", "unhidden"]
    reasons: tuple[HiddenReadReason, ...] = Field(
        default=(), max_length=HIDDEN_READ_REASON_MAX_COUNT
    )

    @model_validator(mode="after")
    def validate_status(self) -> HiddenReadStatus:
        if (self.status == "unhidden") != bool(self.reasons):
            raise ValueError("unhidden requires reasons; enforced must have none")
        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError("hidden-read reasons must be unique")
        object.__setattr__(self, "reasons", tuple(sorted(self.reasons)))
        return self


class MissingProgram(BaseModel):
    """One program a machine lacks, described from `rcp.dependencies`."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str
    purpose: str
    required: bool
    # Optional programs only: what stops working, and what the user sees instead.
    feature: str = ""
    fallback: str = ""


class DependencyStatus(BaseModel):
    """One machine checked against `rcp.dependencies`.

    Only a definite ``missing`` or ``unsupported`` refuses an agent run.
    ``not_checked`` (unreachable, timed out, or an unreadable answer) never does.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    outcome: Literal["ready", "missing", "unsupported", "not_checked"]
    # OS family and `/etc/os-release` ID, when the check got that far.
    platform: str | None = None
    distribution: str | None = None
    # False only for a Linux distribution RCP has not been tested on.
    tested: bool = True
    missing: tuple[MissingProgram, ...] = ()
    # An apt line on Debian and Ubuntu; otherwise None and `install_notes` lists steps.
    install_command: str | None = None
    install_notes: tuple[str, ...] = ()
    # Why the check is unsupported or not checked.
    reason: str | None = None
    checked_at: str

    @model_validator(mode="after")
    def validate_outcome(self) -> DependencyStatus:
        missing_required = any(program.required for program in self.missing)
        if (self.outcome == "missing") != missing_required:
            raise ValueError("missing is the outcome exactly when a required program is absent")
        if self.outcome in {"unsupported", "not_checked"} and not self.reason:
            raise ValueError(f"{self.outcome} requires a reason")
        return self


class HiddenReadKeyEvidence(BaseModel):
    """Launch-host evidence, never private key bytes.

    agent_confirmed means the decoded public-key SHA256 identity was listed
    by the appropriate agent AND a bounded signing check passed on this host.
    None preserves missing/unverified public identities without inventing one.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    path: HiddenReadPath
    kind: Literal["deploy_key", "ssh_identity"]
    public_key_fingerprint: str | None = Field(
        default=None, pattern=r"^SHA256:[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]$"
    )
    agent_confirmed: bool
    visibility: Literal["hidden", "readable"]

    @model_validator(mode="after")
    def validate_evidence(self) -> HiddenReadKeyEvidence:
        if self.path.endswith(".pub"):
            raise ValueError("public key files are not private-key candidates")
        if self.agent_confirmed and self.public_key_fingerprint is None:
            raise ValueError("confirmed keys require a public-key fingerprint")
        if self.visibility == "hidden" and not self.agent_confirmed:
            raise ValueError("a private key can be hidden only after agent confirmation")
        return self


class HiddenReadScope(BaseModel):
    """One immutable effective policy for any capability, on one execution host.

    Empty execution_host denotes this backend's host, as in ProjectWriteScope;
    execution_machine and os_account identify the machine account. Paths have
    already been resolved there. Directories, literal files and glob patterns
    remain distinct for provider rendering. Status includes browser/native-tool
    gaps; hidden entries describe the effective denies on covered surfaces.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_generation: Literal[1] = 1
    execution_machine: str = Field(min_length=1, max_length=HIDDEN_READ_IDENTITY_MAX_LENGTH)
    execution_host: str = Field(max_length=HIDDEN_READ_IDENTITY_MAX_LENGTH)
    os_account: str = Field(min_length=1, max_length=HIDDEN_READ_IDENTITY_MAX_LENGTH)
    # Canonical execution-account home; it locates the staged tool wrapper.
    account_home: HiddenReadPath | None = None
    hidden_directories: tuple[HiddenReadPath, ...] = Field(
        default=(), max_length=HIDDEN_READ_PATH_MAX_COUNT
    )
    # Confirmed keys join the default files, so both budgets fit.
    hidden_files: tuple[HiddenReadPath, ...] = Field(
        default=(), max_length=HIDDEN_READ_PATH_MAX_COUNT + HIDDEN_READ_KEY_MAX_COUNT
    )
    hidden_globs: tuple[HiddenReadPath, ...] = Field(
        default=(), max_length=HIDDEN_READ_PATH_MAX_COUNT
    )
    env_deny_list: tuple[
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=HIDDEN_READ_ENV_NAME_MAX_LENGTH,
                # Case-insensitive name globs, as Codex's environment policy reads them.
                pattern=r"^[A-Za-z0-9_*]+$",
            ),
        ],
        ...,
    ] = Field(default=(), max_length=HIDDEN_READ_ENV_MAX_COUNT)
    enforcement: HiddenReadStatus
    key_evidence: tuple[HiddenReadKeyEvidence, ...] = Field(
        default=(), max_length=HIDDEN_READ_KEY_MAX_COUNT
    )
    fingerprint: str = Field(default="", pattern=r"^[0-9a-f]{64}$")

    @field_validator("hidden_directories", "hidden_files", "hidden_globs", "env_deny_list")
    @classmethod
    def canonical_entries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("hidden-read entries must be unique")
        return tuple(sorted(values))

    @field_validator("hidden_globs")
    @classmethod
    def last_component_globs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        # A Linux wrapper hides future matches by masking the literal parent.
        for value in values:
            parent = re.sub(r"\[[*?[]\]", "", value.rsplit("/", 1)[0])
            if any(char in parent for char in "*?["):
                raise ValueError("hidden glob wildcards must be in the last path component")
        return values

    @field_validator("key_evidence")
    @classmethod
    def canonical_keys(
        cls, values: tuple[HiddenReadKeyEvidence, ...]
    ) -> tuple[HiddenReadKeyEvidence, ...]:
        if len(values) != len({item.path for item in values}):
            raise ValueError("hidden-read key paths must be unique")
        return tuple(sorted(values, key=lambda item: item.path))

    @model_validator(mode="after")
    def validate_fingerprint(self) -> HiddenReadScope:
        # A readable private key is a visible gap: it can never ride under
        # an enforced status or an unrelated reason.
        required = {
            "deploy_key": "deploy_key_agent_unconfirmed",
            "ssh_identity": "ssh_key_agent_unconfirmed",
        }
        for item in self.key_evidence:
            if item.visibility == "readable" and required[item.kind] not in (
                self.enforcement.reasons
            ):
                raise ValueError("a readable private key requires its unconfirmed-key reason")
        paths = (*self.hidden_directories, *self.hidden_files, *self.hidden_globs)
        if len(paths) != len(set(paths)):
            raise ValueError("a hidden path must occur in only one path category")
        payload = self.model_dump(mode="json", exclude={"fingerprint"})
        expected = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
                "utf-8"
            )
        ).hexdigest()
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("hidden-read fingerprint does not match the effective policy")
        object.__setattr__(self, "fingerprint", expected)
        return self

    def wrapper_path(self) -> str:
        """The RCP-owned wrapper copy; its directory is hidden from tool calls."""
        if self.account_home is None:
            raise ValueError("hidden-read scope has no execution-account home")
        return (
            f"{self.account_home}/{HIDDEN_READ_WRAPPER_ROOT}/{self.fingerprint}/rcp-hidden-read.py"
        )


class MachineHiddenReadProjection(BaseModel):
    """Computed Settings output, never editable defaults or manifest policy."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    default_paths: tuple[
        Annotated[str, Field(min_length=1, max_length=HIDDEN_READ_PATH_MAX_LENGTH)], ...
    ] = Field(default=(), max_length=HIDDEN_READ_PATH_MAX_COUNT)
    user_folders: tuple[HiddenReadPath, ...] = Field(
        default=(), max_length=HIDDEN_READ_PATH_MAX_COUNT
    )
    readiness: HiddenReadStatus | None = None

    @field_validator("default_paths")
    @classmethod
    def display_defaults(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _absolute_hidden_read_path("/" + value[2:] if value.startswith("~/") else value)
        if len(values) != len(set(values)):
            raise ValueError("hidden-read defaults must be unique")
        return tuple(sorted(values))


class WorktreeBinding(BaseModel):
    """Immutable repository identity shared by chat and episode owners."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    repository_alias: str = Field(min_length=1)
    machine: str = Field(min_length=1)
    execution_host: str
    shared_path: str = Field(min_length=1)
    worktree_path: str = Field(min_length=1)
    git_common_dir: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    starting_branch: str = Field(min_length=1)
    starting_commit: str = Field(min_length=1)

    @field_validator("git_common_dir")
    @classmethod
    def absolute_git_common_dir(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or str(path) != value:
            raise ValueError("Git metadata directory must be absolute and normalized")
        return value


class EpisodeWorktreeBinding(WorktreeBinding):
    owner_episode_id: str = Field(min_length=1)


class EpisodeIsolation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    owner_episode_id: str = Field(min_length=1)
    graph_branch_id: str | None = None
    worktree: EpisodeWorktreeBinding | None = None

    @model_validator(mode="after")
    def validate_owner(self) -> EpisodeIsolation:
        if self.worktree and self.worktree.owner_episode_id != self.owner_episode_id:
            raise ValueError("episode worktree must belong to its isolation owner")
        return self


class ConversationWorktreeBinding(WorktreeBinding):
    """Durable repository identity and legacy chat operation-state projection."""

    project_id: str = Field(min_length=1)
    chat_id: str
    chat_scope: Literal["node", "project"]
    node_id: str | None = None
    status: Literal["creating", "ready", "removing", "removed"] = "creating"

    @field_validator("chat_id")
    @classmethod
    def canonical_chat_id(cls, value: str) -> str:
        if str(uuid.UUID(value)) != value:
            raise ValueError("conversation worktree chat id must be a canonical UUID")
        return value

    @model_validator(mode="after")
    def validate_chat_scope(self) -> ConversationWorktreeBinding:
        if (self.chat_scope == "node") != bool(self.node_id):
            raise ValueError("conversation worktree node id must match its chat scope")
        return self


def normalize_display_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("display name must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("display name must not be blank")
    if len(normalized) > DISPLAY_NAME_MAX_LENGTH:
        raise ValueError(f"display name must be at most {DISPLAY_NAME_MAX_LENGTH} characters")
    if any(unicodedata.category(character) in {"Cc", "Zl", "Zp"} for character in normalized):
        raise ValueError("display name must be a single line without control characters")
    return normalized


def utc_now() -> datetime:
    return datetime.now(UTC)


class Standing(StrEnum):
    ASSERTED = "asserted"
    ACCEPTED = "accepted"
    CONTESTED = "contested"


#: A native session's origin: a registered provider, or RCP's own chat records.
#: Built from the registry so adding a provider needs no edit here; the schema
#: still renders a closed enum.
SessionSourceId: TypeAlias = Literal[(*PROVIDER_IDS, "app_chat")]  # type: ignore[valid-type]


class SourceRef(BaseModel):
    machine: Annotated[str, GraphField("content")]
    truth_repository: Annotated[str, GraphField("content")]
    source: Annotated[SessionSourceId, GraphField("content")]
    session_id: Annotated[str, GraphField("content")]
    record_uuid: Annotated[str, GraphField("content")]
    timestamp: Annotated[datetime, GraphField("content")]
    excerpt: Annotated[str, GraphField("content")] = Field(max_length=800)


class ExperimentDecisionPin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: Annotated[str, GraphField("content")]
    decision_revision: Annotated[int, GraphField("content")] = Field(ge=0)
    selected_option: Annotated[str, GraphField("content")] = Field(min_length=1)


class ExperimentAttemptDebug(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mechanical_fault: Annotated[str, GraphField("content")] = Field(min_length=1)
    change: Annotated[str, GraphField("content")] = Field(min_length=1)
    predicted_effect: Annotated[str, GraphField("content")] = Field(min_length=1)


ACTIVE_EXPERIMENT_ATTEMPT_STATUSES = frozenset({"planned", "submitted", "running"})


class ExperimentAttempt(BaseModel):
    id: Annotated[str, GraphField("identity")]
    sequence: Annotated[int, GraphField("content")] = Field(ge=1)
    purpose: Annotated[str, GraphField("content")]
    attempt_kind: Annotated[Literal["external_run", "proposal_only"], GraphField("content")] = (
        "external_run"
    )
    decision_bundle: Annotated[list[ExperimentDecisionPin], GraphField("content")] = Field(
        default_factory=list
    )
    debug: Annotated[ExperimentAttemptDebug | None, GraphField("content")] = None
    configuration: Annotated[str, GraphField("content")] = ""
    status: Annotated[
        Literal[
            "planned", "submitted", "running", "failed", "completed", "cancelled", "superseded"
        ],
        GraphField("content"),
    ] = "planned"
    job_refs: Annotated[list[str], GraphField("content")] = Field(default_factory=list)
    source_refs: Annotated[list[SourceRef], GraphField("content")] = Field(default_factory=list)
    outcome: Annotated[str | None, GraphField("content")] = None
    failure_reason: Annotated[str | None, GraphField("content")] = None
    started_at: Annotated[datetime | None, GraphField("content")] = None
    finished_at: Annotated[datetime | None, GraphField("content")] = None


class BaseNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, GraphField("identity", node_update="immutable")] = Field(
        description="Stable identity. Reuse an existing node's id rather than recreating it."
    )
    title: Annotated[str, GraphField("content")] = Field(
        description="A short reader-facing name, in ordinary language."
    )
    extension_type: Annotated[str | None, GraphField("identity", node_update="immutable")] = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
        description="The active custom type this node specializes, or null for a base node.",
    )
    extension_fields: Annotated[
        dict[str, str | float | bool | list[str]], GraphField("content")
    ] = Field(
        default_factory=dict,
        description="Values for the custom type's own fields; base fields never go here.",
    )
    standing: Annotated[Standing, GraphField("content", node_update="immutable")] = Field(
        default=Standing.ASSERTED,
        description=(
            "Review standing: `asserted` (not yet reviewed), `accepted`, or `contested`. "
            "Editing accepted ordinary content returns it to `asserted`."
        ),
    )
    created_rev: Annotated[int, GraphField("engine", node_update="immutable")] = Field(
        default=0, description="Revision that created the node."
    )
    updated_rev: Annotated[int, GraphField("engine", node_update="immutable")] = Field(
        default=0, description="Revision that last changed the node."
    )
    source_refs: Annotated[list[SourceRef], GraphField("content")] = Field(
        default_factory=list,
        description=(
            "Exact provider records that support this node. Each `excerpt` must itself contain "
            "the claim, not merely come from the same conversation."
        ),
    )


class ResearchQuestion(BaseNode):
    type: Annotated[Literal["research_question"], GraphField("identity", node_update="immutable")]
    question: Annotated[str, GraphField("content")] = Field(
        description="The question the project is trying to answer."
    )
    motivation: Annotated[str, GraphField("content")] = Field(
        default="", description="Why answering it matters to the project."
    )
    scope: Annotated[str, GraphField("content")] = Field(
        default="",
        description=(
            "What the question covers and what it leaves out, such as the population, setting, "
            "or time range. Hypotheses and Experiments under it stay inside this boundary."
        ),
    )
    status: Annotated[
        Literal["open", "answered", "abandoned", "superseded"], GraphField("content")
    ] = Field(default="open", description="Whether the question is still being pursued.")


class Hypothesis(BaseNode):
    type: Annotated[Literal["hypothesis"], GraphField("identity", node_update="immutable")]
    statement: Annotated[str, GraphField("content")] = Field(
        description=(
            "A claim that some observation could show to be false, stated specifically. Name the "
            "observation that would show it false."
        )
    )
    rationale: Annotated[str, GraphField("content")] = Field(
        default="",
        description=(
            "Why the claim is worth testing, written before evidence bears on it. Reasoning, "
            "not evidence."
        ),
    )
    predictions: Annotated[list[str], GraphField("content")] = Field(
        default_factory=list,
        description=(
            "What should be true in the world if the claim holds, independent of how any one "
            "Experiment measures it; each one is an observation that would count against the "
            "claim if it failed. Written before results exist."
        ),
    )
    scope: Annotated[str, GraphField("content")] = Field(
        default="",
        description=(
            "The boundary within which the claim is meant to hold. Write it only when a cited "
            "`source_refs[].excerpt` states that boundary; otherwise leave it empty and say so "
            "in the final answer. Never infer it, and never create a Blocker or Decision for a "
            "missing boundary."
        ),
    )
    status: Annotated[
        Literal["proposed", "active", "supported", "weakened", "rejected", "superseded"],
        GraphField("content"),
    ] = Field(
        default="proposed",
        description=(
            "Where the claim stands against its evidence. A change names the "
            "Evidence-to-Hypothesis edge that caused it."
        ),
    )


class Decision(BaseNode):
    type: Annotated[Literal["decision"], GraphField("identity", node_update="immutable")]
    question: Annotated[str, GraphField("content")] = Field(
        description="The choice that governs how research proceeds."
    )
    options: Annotated[list[str], GraphField("content")] = Field(
        default_factory=list,
        description=(
            "The alternatives the chooser selects among. Investigate each with the same care and "
            "write each as a complete choice a reader could act on alone, at the same level of "
            'detail. Never pad with an underspecified or "leave it open" option, and never '
            "signal a preference through order, length, or wording."
        ),
    )
    selected_option: Annotated[str | None, GraphField("content")] = Field(
        default=None, description="The chosen option, exactly as written in `options`."
    )
    rationale: Annotated[str | None, GraphField("content")] = Field(
        default=None,
        description=(
            "What the choice turns on and any leaning. If only one option survives "
            "investigation, say so here instead of inventing alternatives."
        ),
    )
    consequences: Annotated[list[str], GraphField("content")] = Field(
        default_factory=list,
        description="What changes in the research once the choice is made.",
    )
    status: Annotated[
        Literal["open", "ready", "decided", "revisit", "superseded"], GraphField("content")
    ] = Field(
        default="open",
        description=(
            "`open` while facts are missing; `ready` once the choice is makeable from facts "
            "checked with the task's inputs, including operational state when that decides it, "
            "and without waiting for Experiments it governs; `decided` once chosen; `revisit` "
            "only when new evidence undermines a settled choice."
        ),
    )


class ExperimentProxy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stands_for: Annotated[str, GraphField("content")] = Field(
        min_length=1, description="The quantity the research cares about, such as caffeine intake."
    )
    measure: Annotated[str, GraphField("content")] = Field(
        min_length=1,
        description=(
            "What is actually measured in its place, such as self-reported cups of coffee per week."
        ),
    )

    @field_validator("stands_for", "measure", mode="before")
    @classmethod
    def normalize_side(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("each side of a proxy must not be blank")
        return normalized


class Experiment(BaseNode):
    type: Annotated[Literal["experiment"], GraphField("identity", node_update="immutable")]
    objective: Annotated[str, GraphField("content")] = Field(
        description=(
            "What this bounded test is meant to establish and why, in plain sentences a reader "
            "who knows the field but not this project understands."
        )
    )
    design: Annotated[str, GraphField("content")] = Field(
        default="",
        description=(
            "The operational plan, so a reader knows exactly what will be done: what is run (the "
            "model, method, or system), under which conditions, the run matrix (each factor and "
            "its levels, seeds or repeats, and the total number of runs), what is measured and "
            "logged, and what the results are compared against. Enough detail to repeat. Say what "
            "a script or config does rather than only naming it. Written before results exist."
        ),
    )
    proxies: Annotated[list[ExperimentProxy], GraphField("content")] = Field(
        default_factory=list,
        description=(
            "Each measurable stand-in the design uses, as what it stands for and what is "
            "measured. List every stand-in the design measures in place of the quantity the claim "
            "is about; leave it empty only when the design measures that quantity directly. "
            "Written with the design."
        ),
    )
    limitations: Annotated[list[str], GraphField("content")] = Field(
        default_factory=list,
        description=(
            "What the proxies and the protocol miss or exclude, such as a source not counted or "
            "a group left out. Written with the design."
        ),
    )
    expected_outcomes: Annotated[list[str], GraphField("content")] = Field(
        default_factory=list,
        description=(
            "What this measurement should show if the tested claim holds, in the proxies' terms. "
            "What would count against the claim belongs in `interpretation_rules`. Written before "
            "results exist."
        ),
    )
    interpretation_rules: Annotated[list[str], GraphField("content")] = Field(
        default_factory=list,
        description=(
            "Pre-committed rules for reading results: if this is observed, it means that. "
            "Written before results exist; reading an actual result belongs in "
            "`Evidence.interpretation`."
        ),
    )
    completion_criteria: Annotated[list[str], GraphField("content")] = Field(
        default_factory=list,
        description="What must be recorded for the Experiment to count as finished.",
    )
    invocation_ceiling: Annotated[int, GraphField("content")] = Field(
        default=5,
        ge=1,
        description=(
            "The default number of agent invocations one episode of this Experiment may spend. "
            "A human Run may pin a different count for that episode."
        ),
    )
    status: Annotated[
        Literal[
            "proposed",
            "designing",
            "implementing",
            "debugging",
            "running",
            "analyzing",
            "completed",
            "unspecified",
            "abandoned",
            "superseded",
        ],
        GraphField("content"),
    ] = Field(
        default="proposed",
        description=(
            "The Experiment's current phase. When new work reopens a completed Experiment, change "
            "`status`, `current_summary`, and `next_action` together; a clarification alone does "
            "not reopen it. `unspecified` is a compatibility reading of old history and is never "
            "written."
        ),
    )
    attempts: Annotated[list[ExperimentAttempt], GraphField("content")] = Field(
        default_factory=list,
        description=(
            "Ordered ledger of this Experiment's attempts: purpose, configuration, job "
            "references, status, and outcome."
        ),
    )
    current_summary: Annotated[str, GraphField("content")] = Field(
        default="",
        description=(
            "Short orientation on where the Experiment stands now. Not a substitute for the "
            "attempt ledger or for Evidence."
        ),
    )
    next_action: Annotated[str | None, GraphField("content")] = Field(
        default=None,
        description="The concrete next step, or null when nothing remains.",
    )
    current_summary_stale: Annotated[bool, GraphField("engine", node_update="replay_only")] = Field(
        default=False,
        description=(
            "Set by RCP, never in a Patch: true once something `current_summary` rests on "
            "changes, such as a governing Decision or Blocker, a tested Hypothesis, or Evidence "
            "bearing on it. Rewriting `current_summary` in a later Patch clears it; a rewrite in "
            "the same Patch as that change stays stale."
        ),
    )
    next_action_stale: Annotated[bool, GraphField("engine", node_update="replay_only")] = Field(
        default=False,
        description=(
            "Set by RCP, never in a Patch: true once something `next_action` rests on changes. "
            "Rewriting `next_action` in a later Patch clears it; a rewrite in the same Patch as "
            "that change stays stale."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_attempt_ceiling(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "attempt_ceiling" not in value:
            return value
        migrated = dict(value)
        migrated["invocation_ceiling"] = migrated.pop("attempt_ceiling")
        return migrated


EXPERIMENT_COMPATIBILITY_STATUSES = frozenset({"unspecified"})


# The human's own verdict that an Experiment is finished with, which outranks how
# any one bounded episode inside it happened to end.
CLOSED_EXPERIMENT_STATUSES = frozenset({"completed", "abandoned", "superseded"})


class Evidence(BaseNode):
    type: Annotated[Literal["evidence"], GraphField("identity", node_update="immutable")]
    observation: Annotated[str, GraphField("content")] = Field(
        description=(
            "What the artifact or record directly states: run, step, value, absence, and time "
            "boundary. Written after the result exists."
        )
    )
    interpretation: Annotated[str, GraphField("content")] = Field(
        default="",
        description=(
            "What the observation licenses here and, when useful, what it does not. Written "
            "after the result exists."
        ),
    )
    role: Annotated[Literal["result", "diagnostic"], GraphField("content")] = Field(
        default="result",
        description=(
            "`result` for an ordinary observation; `diagnostic` when it mainly localizes, "
            "disambiguates, or debugs a phenomenon. Role is not evidential weight."
        ),
    )
    legacy_strength: Annotated[
        Literal["diagnostic", "preliminary", "supporting", "confirmatory"] | None,
        GraphField("content", node_update="replay_only"),
    ] = Field(
        default=None,
        description=(
            "A retired node-wide strength label kept only for old history. Never write it, "
            "and never read it as an edge weight."
        ),
    )
    validity: Annotated[
        Literal["valid", "qualified", "invalid", "superseded"], GraphField("content")
    ] = Field(
        default="valid",
        description=(
            "Methodological soundness within the observation's own limits. An incomplete run may "
            "justify only a `qualified` snapshot."
        ),
    )
    origin: Annotated[
        Literal["internal_run", "external_publication", "external_instance", "analytic", "unknown"],
        GraphField("content"),
    ] = Field(
        default="unknown",
        description=(
            "Where the observation came from: `internal_run` for a project run, "
            "`external_publication` for a publication, `external_instance` for another RCP "
            "instance, `analytic` for a derivation, `unknown` only when it cannot be classified. "
            "Set it explicitly. `internal_run` Evidence connects to its producing Experiment; "
            "external or analytic Evidence needs no invented Experiment or conversation source."
        ),
    )
    artifact_refs: Annotated[list[str], GraphField("content")] = Field(
        default_factory=list,
        description="Paths to the primary artifacts the observation comes from.",
    )


class Blocker(BaseNode):
    type: Annotated[Literal["blocker"], GraphField("identity", node_update="immutable")]
    description: Annotated[str, GraphField("content")] = Field(
        description="The concrete impediment and what it stops."
    )
    blocker_type: Annotated[
        Literal["scientific", "design", "data", "implementation", "infrastructure", "unknown"],
        GraphField("content"),
    ] = Field(default="unknown", description="What kind of impediment it is.")
    status: Annotated[Literal["open", "resolved", "superseded"], GraphField("content")] = Field(
        default="open", description="Only an `open` Blocker gates anything."
    )
    resolution_condition: Annotated[str, GraphField("content")] = Field(
        default="",
        description=(
            "The observable condition that clears it. It never amounts to running the "
            "Experiment the Blocker stops."
        ),
    )
    recommended_action: Annotated[str | None, GraphField("content")] = Field(
        default=None, description="The step most likely to meet the resolution condition."
    )


ProjectNode = Annotated[
    ResearchQuestion | Hypothesis | Decision | Experiment | Evidence | Blocker,
    Field(discriminator="type"),
]


HUMAN_EDITABLE_NODE_FIELDS: dict[str, frozenset[str]] = {
    "research_question": frozenset({"title", "question", "motivation", "scope"}),
    "hypothesis": frozenset({"title", "statement", "rationale", "predictions", "scope"}),
    "decision": frozenset({"title", "question", "options", "rationale", "consequences", "status"}),
    "experiment": frozenset(
        {
            "title",
            "objective",
            "design",
            "expected_outcomes",
            "interpretation_rules",
            "completion_criteria",
            # Replay compatibility for approval patches written before the
            # invocation-budget rename. New output uses invocation_ceiling.
            "attempt_ceiling",
            "invocation_ceiling",
            "proxies",
            "limitations",
            "current_summary",
            "next_action",
        }
    ),
    "evidence": frozenset({"title", "observation", "interpretation"}),
    "blocker": frozenset(
        {"title", "description", "status", "resolution_condition", "recommended_action"}
    ),
}


NodeType = Literal[
    "research_question", "hypothesis", "decision", "experiment", "evidence", "blocker"
]
BaseRelation = Literal[
    "has_subquestion",
    "has_hypothesis",
    "has_decision",
    "tests",
    "governed_by",
    "produces",
    "informs",
    "addresses",
    "blocked_by",
    "supports",
    "weakens",
    "contradicts",
    "refutes",
    "inconclusive",
    "requires_decision",
    "supersedes",
    "duplicate_of",
]
EdgeExpectation = Literal["matched", "diverged", "no_expectation"]
RelationLayer = Literal["epistemic", "action", "seam", "meta"]
ALL_NODE_TYPES: frozenset[str] = frozenset(
    {"research_question", "hypothesis", "decision", "experiment", "evidence", "blocker"}
)


class OntologyTypeDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, GraphField("identity")] = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
    definition: Annotated[str, GraphField("content")] = Field(min_length=1)
    base_type: Annotated[NodeType, GraphField("content")]
    layer: Annotated[Literal["epistemic", "action"], GraphField("content")]
    deprecated: Annotated[bool, GraphField("content")] = False


class OntologyFieldDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_type: Annotated[str, GraphField("identity")] = Field(
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
    )
    name: Annotated[str, GraphField("identity")] = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
    definition: Annotated[str, GraphField("content")] = Field(min_length=1)
    kind: Annotated[Literal["text", "number", "boolean", "text_list"], GraphField("content")]
    required: Annotated[bool, GraphField("content")] = False
    agent_writable: Annotated[bool, GraphField("content")] = True
    deprecated: Annotated[bool, GraphField("content")] = False


class OntologyRelationDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, GraphField("identity")] = Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
    definition: Annotated[str, GraphField("content")] = Field(min_length=1)
    source_types: Annotated[list[str], GraphField("content")] = Field(min_length=1)
    target_types: Annotated[list[str], GraphField("content")] = Field(min_length=1)
    layer: Annotated[Literal["epistemic", "action"], GraphField("content")]
    deprecated: Annotated[bool, GraphField("content")] = False


class OntologyState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    types: Annotated[list[OntologyTypeDefinition], GraphField("content")] = Field(
        default_factory=list
    )
    fields: Annotated[list[OntologyFieldDefinition], GraphField("content")] = Field(
        default_factory=list
    )
    relations: Annotated[list[OntologyRelationDefinition], GraphField("content")] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def require_unique_names(self) -> OntologyState:
        for label, names in (
            ("type", [item.name for item in self.types]),
            ("field", [f"{item.owner_type}.{item.name}" for item in self.fields]),
            ("relation", [item.name for item in self.relations]),
        ):
            if len(names) != len(set(names)):
                raise ValueError(f"ontology contains duplicate {label} definitions")
        return self


@dataclass(frozen=True)
class RelationSpec:
    source_types: frozenset[str]
    target_types: frozenset[str]
    layer: RelationLayer
    same_type: bool = False
    description: str = ""


RELATION_SPEC: dict[BaseRelation, RelationSpec] = {
    "has_subquestion": RelationSpec(
        frozenset({"research_question"}),
        frozenset({"research_question"}),
        "epistemic",
        description="The target question is part of answering the source question. Split a question when its parts can be pursued separately; Hypotheses and Decisions then hang off the narrowest question they serve.",
    ),
    "has_hypothesis": RelationSpec(
        frozenset({"research_question"}),
        frozenset({"hypothesis"}),
        "epistemic",
        description="The Hypothesis is a candidate answer to the question. Every Hypothesis serves at least one question, and rival Hypotheses may share one. Create this edge in the Patch that creates the Hypothesis, because adding it to an existing Hypothesis is a protected change.",
    ),
    "supports": RelationSpec(
        frozenset({"evidence"}),
        frozenset({"hypothesis"}),
        "epistemic",
        description="The Evidence makes the claim more likely. One Evidence node often bears on several Hypotheses, each through its own edge.",
    ),
    "weakens": RelationSpec(
        frozenset({"evidence"}),
        frozenset({"hypothesis"}),
        "epistemic",
        description="The Evidence makes the claim less likely without ruling it out.",
    ),
    "refutes": RelationSpec(
        frozenset({"evidence"}),
        frozenset({"hypothesis"}),
        "epistemic",
        description="The Evidence rules the claim out within its scope.",
    ),
    "inconclusive": RelationSpec(
        frozenset({"evidence"}),
        frozenset({"hypothesis"}),
        "epistemic",
        description="The Evidence bears on the claim but settles nothing either way. Use it only when the result really concerns the claim; it is never a placeholder for Evidence that does not.",
    ),
    "contradicts": RelationSpec(
        frozenset({"evidence", "hypothesis"}),
        frozenset({"hypothesis"}),
        "epistemic",
        description="The source, Evidence or another Hypothesis, is incompatible with the claim. Between Hypotheses it marks rival answers that cannot both hold.",
    ),
    "tests": RelationSpec(
        frozenset({"experiment"}),
        frozenset({"hypothesis"}),
        "seam",
        description="The Experiment can tell whether the claim holds: its expected outcomes differ depending on whether the claim is true. Evidence it produces usually connects back to a Hypothesis it tests with `supports`, `weakens`, `refutes`, or `inconclusive`, but only when the result really bears on that claim; a side finding or readiness check needs no such edge. An Experiment that serves only a Decision or Blocker needs no `tests` edge.",
    ),
    "produces": RelationSpec(
        frozenset({"experiment"}),
        frozenset({"evidence"}),
        "seam",
        description="The Experiment generated this Evidence. Create it only once the observation exists; before that, the intended observation belongs in the Experiment's design.",
    ),
    "informs": RelationSpec(
        frozenset({"evidence"}),
        frozenset({"decision"}),
        "action",
        description="The Evidence bears on the choice, for example by ruling an option out or making a tradeoff concrete. It neither selects an option nor closes the Decision.",
    ),
    "addresses": RelationSpec(
        frozenset({"evidence"}),
        frozenset({"blocker"}),
        "action",
        description="The Evidence bears on whether the Blocker is cleared, kept, or narrowed. It does not change the Blocker's status.",
    ),
    "has_decision": RelationSpec(
        frozenset({"research_question"}),
        frozenset({"decision"}),
        "action",
        description="The choice belongs to the question: answering the question depends on making it. A Decision that matters only as an Experiment's input may connect through `governed_by` alone.",
    ),
    "governed_by": RelationSpec(
        frozenset({"experiment"}),
        frozenset({"decision"}),
        "action",
        description="The Decision is a genuine input to the Experiment. A Decision the Experiment's results are meant to settle is downstream, not an input.",
    ),
    "blocked_by": RelationSpec(
        frozenset({"experiment", "decision", "research_question"}),
        frozenset({"blocker"}),
        "action",
        description="The open Blocker keeps the source from proceeding; a blocked Experiment cannot start. Connect the Blocker to each question, Decision, or Experiment it actually stops; a Blocker that stops nothing is a note, not a Blocker.",
    ),
    "requires_decision": RelationSpec(
        frozenset({"blocker"}),
        frozenset({"decision"}),
        "action",
        description="Clearing the Blocker needs this choice rather than an observation. A Blocker that an observation can clear receives that Evidence through `addresses` instead.",
    ),
    "supersedes": RelationSpec(
        ALL_NODE_TYPES,
        ALL_NODE_TYPES,
        "meta",
        same_type=True,
        description="The source replaces the target, which stays in history.",
    ),
    "duplicate_of": RelationSpec(
        ALL_NODE_TYPES,
        ALL_NODE_TYPES,
        "meta",
        same_type=True,
        description="The source is the same entity as the target.",
    ),
}


class EvidenceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relevance: Annotated[Literal["direct", "indirect", "contextual"], GraphField("content")] = (
        Field(description="How directly the observation bears on this claim.")
    )
    weight: Annotated[Literal["limited", "moderate", "strong"], GraphField("content")] = Field(
        description="How much the observation should move belief in this claim."
    )
    scope: Annotated[str | None, GraphField("content")] = Field(
        default=None,
        max_length=EVIDENCE_ASSESSMENT_SCOPE_MAX_LENGTH,
        description="The bounded population, regime, condition, subclaim, or setting covered.",
    )
    qualifications: Annotated[
        list[
            Annotated[
                str,
                Field(min_length=1, max_length=EVIDENCE_ASSESSMENT_QUALIFICATION_MAX_LENGTH),
            ]
        ],
        GraphField("content"),
    ] = Field(
        default_factory=list,
        max_length=EVIDENCE_ASSESSMENT_MAX_QUALIFICATIONS,
        description="Concrete limitations or caveats; empty only when none apply.",
    )

    @field_validator("scope", mode="before")
    @classmethod
    def normalize_scope(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("assessment scope must not be blank")
        return normalized

    @field_validator("qualifications", mode="before")
    @classmethod
    def normalize_qualifications(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized: list[Any] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            qualification = item.strip()
            if not qualification:
                raise ValueError("assessment qualifications must not be blank")
            if qualification in seen:
                raise ValueError("assessment qualifications must not contain duplicates")
            seen.add(qualification)
            normalized.append(qualification)
        return normalized


class Edge(BaseModel):
    # Layer is backend-owned; a supplied one is always discarded. What lands here
    # is the relation's *declared* layer. Materialization then narrows it to the
    # edge's real layer via `ontology.edge_layer`, which needs the endpoint types
    # and so cannot run inside this validator.
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, GraphField("identity")] = Field(
        description="Stable edge identity, by default `<source>::<relation>::<target>`."
    )
    source: Annotated[str, GraphField("content")] = Field(
        description="Id of the node the relation starts from."
    )
    target: Annotated[str, GraphField("content")] = Field(
        description="Id of the node the relation points to."
    )
    relation: Annotated[str, GraphField("content")] = Field(
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
        description="The relation name; its legal endpoints are listed with the relations.",
    )
    layer: Annotated[RelationLayer, GraphField("content")] = Field(
        description="Derived by RCP from the relation and the endpoint types; never written."
    )
    explanation: Annotated[str, GraphField("content")] = Field(
        default="", description="Why the relation holds, in a sentence a reader can check."
    )
    assessment: Annotated[EvidenceAssessment | None, GraphField("content")] = Field(
        default=None,
        description=(
            "How this Evidence bears on this particular Hypothesis. The relation already states "
            "direction; the same Evidence may bear differently on another Hypothesis."
        ),
    )
    expectation: Annotated[EdgeExpectation | None, GraphField("content")] = Field(
        default=None,
        description=(
            "Whether the result `matched` or `diverged` from the producing Experiment's "
            "`expected_outcomes`, or `no_expectation` when none applied. Name the outcome in "
            "`explanation`. A diverged result is either a protocol defect or the finding."
        ),
    )
    created_rev: Annotated[int, GraphField("engine")] = Field(
        default=0, description="Revision that created the edge."
    )

    @model_validator(mode="before")
    @classmethod
    def derive_base_relation_layer(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        relation = value.get("relation")
        spec = RELATION_SPEC.get(relation) if isinstance(relation, str) else None
        if spec is None:
            return value
        return {**value, "layer": spec.layer}


class GatedCard(BaseModel):
    situation_cold: Annotated[str, GraphField("content")] = ""
    why_human_now: Annotated[str, GraphField("content")] = ""
    consequences: Annotated[str, GraphField("content")] = ""
    decision_needed: Annotated[str, GraphField("content")] = ""


# Ordinary new filings use rule 2; materialize selects rule 3 for new merge groups.
CURRENT_PROPOSAL_DEPENDENCY_RULE: Literal[2] = 2


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, GraphField("identity")]
    title: Annotated[str, GraphField("content")]
    card: Annotated[GatedCard, GraphField("content")]
    ops: Annotated[list[ProposalOperation], GraphField("content")]
    related_node_ids: Annotated[list[str], GraphField("engine", proposal_owner="filing")] = Field(
        default_factory=list
    )
    related_edge_ids: Annotated[list[str], GraphField("engine", proposal_owner="filing")] = Field(
        default_factory=list
    )
    related_config_keys: Annotated[list[str], GraphField("engine", proposal_owner="filing")] = (
        Field(default_factory=list)
    )
    # Under rule 2 a Proposal is also stale once the Evidence its cause edge starts
    # from changes (validation/proposals.py). Rule 3 scopes incident snapshots
    # within connected reviews and permits atomic edge replacement; approval
    # accepts only written subjects. Older records retain their recorded rule.
    dependency_rule: Annotated[Literal[1, 2, 3], GraphField("engine", proposal_owner="filing")] = 1
    base_rev: Annotated[int, GraphField("engine", proposal_owner="filing")] = 0
    status: Annotated[
        Literal["pending", "approved", "rejected", "withdrawn"],
        GraphField("content", proposal_owner="resolution"),
    ] = "pending"
    created_by: Annotated[
        Literal["agent", "human"], GraphField("engine", proposal_owner="filing")
    ] = "agent"
    created_by_operation_id: Annotated[
        str | None, GraphField("engine", proposal_owner="filing")
    ] = None
    raised_rev: Annotated[int, GraphField("engine", proposal_owner="filing")] = 0
    resolved_rev: Annotated[int | None, GraphField("engine", proposal_owner="resolution")] = None
    resolved_by: Annotated[
        Literal["agent", "human"] | None, GraphField("content", proposal_owner="resolution")
    ] = None
    resolved_by_operation_id: Annotated[
        str | None, GraphField("engine", proposal_owner="resolution")
    ] = None
    resolution_reason: Annotated[str | None, GraphField("content", proposal_owner="resolution")] = (
        None
    )
    rejection_reason: Annotated[str | None, GraphField("content", proposal_owner="resolution")] = (
        None
    )

    @field_serializer("ops")
    def serialize_ops(
        self,
        operations: list[ProposalOperation] | list[dict[str, Any]],
        info: SerializationInfo,
    ) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for operation in operations:
            item = (
                operation.model_dump(mode=info.mode, exclude_unset=True)
                if isinstance(operation, BaseModel)
                else dict(operation)
            )
            if str(item.get("intent", "")).startswith("legacy_"):
                item.pop("intent")
            serialized.append(item)
        return serialized


class Ambiguity(BaseModel):
    id: Annotated[str, GraphField("identity")]
    question: Annotated[str, GraphField("content")]
    why_it_matters: Annotated[str, GraphField("content")]
    candidates: Annotated[list[str], GraphField("content")] = Field(default_factory=list)
    related_node_ids: Annotated[list[str], GraphField("content")] = Field(default_factory=list)
    artifact_refs: Annotated[list[str], GraphField("content")] = Field(default_factory=list)
    status: Annotated[Literal["open", "resolved", "dismissed"], GraphField("content")] = "open"
    raised_rev: Annotated[int, GraphField("engine")] = 0


class GlossaryTerm(BaseModel):
    term: Annotated[str, GraphField("identity")]
    plain_definition: Annotated[str, GraphField("content")]
    where_defined: Annotated[str | None, GraphField("content")] = None
    updated_rev: Annotated[int, GraphField("engine")] = 0


class ValidationMessage(BaseModel):
    level: Annotated[Literal["flag", "reject"], GraphField("engine")]
    code: Annotated[str, GraphField("engine")]
    message: Annotated[str, GraphField("engine")]
    patch_revision: Annotated[int | None, GraphField("engine")] = None
    related_node_ids: Annotated[list[str], GraphField("engine")] = Field(default_factory=list)
    related_edge_ids: Annotated[list[str], GraphField("engine")] = Field(default_factory=list)
    operation_index: Annotated[int | None, GraphField("engine")] = Field(default=None, ge=0)
    rule_id: Annotated[str | None, GraphField("engine")] = None
    cause_chain: Annotated[list[dict[str, Any]], GraphField("engine")] = Field(default_factory=list)
    failed_invariant: Annotated[str | None, GraphField("engine")] = None


class BeliefTransition(BaseModel):
    hypothesis_id: Annotated[str, GraphField("engine")]
    from_status: Annotated[str, GraphField("engine")]
    to_status: Annotated[str, GraphField("engine")]
    revision: Annotated[int, GraphField("engine")]
    cause: Annotated[dict[str, Any], GraphField("engine")]


class ReplayFailure(BaseModel):
    revision: Annotated[int, GraphField("engine")]
    created_at: Annotated[datetime, GraphField("engine")]
    code: Annotated[str, GraphField("engine")]
    message: Annotated[str, GraphField("engine")]


class GraphState(BaseModel):
    revision: Annotated[int, GraphField("engine")] = 0
    project_truth_scope: Annotated[list[str], GraphField("content")] = Field(default_factory=list)
    config_revisions: Annotated[dict[str, int], GraphField("engine")] = Field(default_factory=dict)
    nodes: Annotated[dict[str, ProjectNode], GraphField("content")] = Field(default_factory=dict)
    edges: Annotated[dict[str, Edge], GraphField("content")] = Field(default_factory=dict)
    proposals: Annotated[dict[str, Proposal], GraphField("content")] = Field(default_factory=dict)
    ambiguities: Annotated[dict[str, Ambiguity], GraphField("content")] = Field(
        default_factory=dict
    )
    glossary: Annotated[dict[str, GlossaryTerm], GraphField("content")] = Field(
        default_factory=dict
    )
    ontology: Annotated[OntologyState, GraphField("content")] = Field(default_factory=OntologyState)
    validation_messages: Annotated[list[ValidationMessage], GraphField("engine")] = Field(
        default_factory=list
    )
    belief_transitions: Annotated[list[BeliefTransition], GraphField("engine")] = Field(
        default_factory=list
    )
    replay_status: Annotated[Literal["complete", "degraded"], GraphField("engine")] = "complete"
    replay_failure: Annotated[ReplayFailure | None, GraphField("engine")] = None
    last_refresh_at: Annotated[datetime | None, GraphField("engine")] = None

    @model_validator(mode="before")
    @classmethod
    def adapt_persisted_snapshot(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        # Local import keeps the domain-model/operation dependency acyclic.
        from rcp.core.operations import adapt_persisted_graph_state_document

        return adapt_persisted_graph_state_document(value)


def _projection_default(field: FieldInfo) -> Any:
    """Return a JSON default only when its construction is known to be deterministic."""
    factory = field.default_factory
    if factory is None:
        if field.is_required():
            return PydanticUndefined
        return to_jsonable_python(field.default)
    if factory in (list, dict, tuple, set, frozenset):
        return to_jsonable_python(factory())
    if isinstance(factory, type) and issubclass(factory, BaseModel):
        defaults = {
            name: _projection_default(child) for name, child in factory.model_fields.items()
        }
        if all(value is not PydanticUndefined for value in defaults.values()):
            return defaults
    # Never execute an arbitrary factory to guess whether it is deterministic.
    # The recursive schema invariant names these fields for an explicit migration.
    return PydanticUndefined


def _upgrade_projection_value(value: Any, annotation: Any, discriminator: Any = None) -> None:
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        for metadata in arguments[1:]:
            if isinstance(metadata, FieldInfo) and metadata.discriminator is not None:
                discriminator = metadata.discriminator
        _upgrade_projection_value(value, arguments[0], discriminator)
    elif origin in (Union, UnionType):
        variants = [variant for variant in arguments if variant is not type(None)]
        if len(variants) == 1:
            _upgrade_projection_value(value, variants[0])
        elif isinstance(discriminator, str) and isinstance(value, dict):
            discriminator_value = value.get(discriminator)
            for variant in variants:
                if isinstance(variant, type) and issubclass(variant, BaseModel):
                    tag = variant.model_fields.get(discriminator)
                    if tag and discriminator_value in get_args(tag.annotation):
                        _upgrade_projection_value(value, variant)
                        break
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if not isinstance(value, dict):
            return
        # A field with its own serializer is stored as serialized: Proposal and Patch
        # operations keep only what their author set, so no default belongs inside them.
        serialized = {
            name
            for decorator in annotation.__pydantic_decorators__.field_serializers.values()
            for name in decorator.info.fields
        }
        for name, field in annotation.model_fields.items():
            if name not in value:
                default = _projection_default(field)
                if default is not PydanticUndefined:
                    value[name] = default
            if name in value and name not in serialized:
                _upgrade_projection_value(value[name], field.annotation, field.discriminator)
    elif origin is dict and isinstance(value, dict):
        for item in value.values():
            _upgrade_projection_value(item, arguments[1])
    elif origin is list and isinstance(value, list):
        for item in value:
            _upgrade_projection_value(item, arguments[0])


def upgrade_graph_projection(graph: dict[str, object]) -> None:
    """Fill absent deterministic defaults from the graph model, in place.

    Walk discriminated variants and nested records without validation or
    normalization: explicit values, unknown keys and stored edge layers survive.
    Fields with their own serializer, such as stored operations, stay as stored.
    Constant defaults, empty container factories and model factories composed of
    these defaults are safe; other factories require an explicit migration and
    fail the recursive schema invariant. Retired coverage is explicitly removed.
    """
    graph.pop("coverage", None)
    _upgrade_projection_value(graph, GraphState)


def _canonical_uuid4(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError("must be a canonical UUIDv4") from exc
    if str(parsed) != value or parsed.version != 4:
        raise ValueError("must be a canonical UUIDv4")
    return value


class ProjectIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    home_space_id: str
    action: Literal["created", "adopted"]

    _validate_uuid4 = field_validator("project_id", "home_space_id")(_canonical_uuid4)


class AuthorizedHuman(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str
    user_id: str
    display_name: str = Field(min_length=1, max_length=DISPLAY_NAME_MAX_LENGTH)

    _validate_uuid4 = field_validator("space_id", "user_id")(_canonical_uuid4)
    _normalize_display_name = field_validator("display_name", mode="before")(normalize_display_name)

    def is_same_member(self, other: AuthorizedHuman | None) -> bool:
        """The same member, whatever display name either snapshot carries."""

        return other is not None and (self.space_id, self.user_id) == (
            other.space_id,
            other.user_id,
        )


class EpisodeUnfinishedJob(BaseModel):
    """A job that may still write an episode worktree, as the human confirmed it at Merge."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["compute_job", "watcher"]
    id: str = Field(min_length=1)
    status: str
    execution_host: str = ""
    command: str | None = None
    log_path: str | None = None


class EpisodeMergeAttempt(BaseModel):
    """Durable intent used to reconcile an interrupted human-dispatched merge."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    attempt_id: str = Field(min_length=1)
    authorized_by: AuthorizedHuman
    phase: Literal[
        "pre_merge", "landing", "agent_merging", "verified", "graph_committed", "cleanup", "done"
    ] = "pre_merge"
    source_commit: str | None = None
    target_branch: str | None = None
    target_commit: str | None = None
    expected_shared_branch: str | None = None
    tree: str | None = None
    landed_commit: str | None = None
    squash_commit: str | None = None
    commit_timestamp: str | None = None
    graph_head: GraphHeadRef | None = None
    history_mode: Literal["merge", "squash"] = "merge"
    remove_worktree: bool = True
    delete_code_branch: bool = True
    archive_graph_branch: bool = True
    keep_branch_open: bool = False
    confirm_discard: bool = False
    cleanup_completed: list[str] = Field(default_factory=list)
    error: str | None = None
    graph_task_id: str | None = None
    # The merge task's agent lands the code; RCP verifies before the graph commit.
    code_by_agent: bool = False
    conflict_files: list[str] = Field(default_factory=list)
    merge_tree_output: str | None = None
    # Jobs the human merged over; the code merge agent stops them before it merges.
    unfinished_jobs: list[EpisodeUnfinishedJob] = Field(default_factory=list)
    # Merge finished but kept the worktree, its branch, and the graph branch listed,
    # because a job was still unfinished; Merge again removes them once it ends.
    worktree_kept: bool = False

    @model_validator(mode="after")
    def validate_cleanup(self) -> EpisodeMergeAttempt:
        if self.history_mode == "squash" and self.keep_branch_open:
            raise ValueError("squash_cannot_keep_branch_open")
        if self.delete_code_branch and not self.remove_worktree:
            raise ValueError("delete_branch_requires_remove_worktree")
        return self


class EpisodeIsolationState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    owner_episode_id: str = Field(min_length=1)
    status: Literal["creating", "ready", "merging", "removing", "removed"] = "creating"
    merge_reservation: str | None = None
    merge_attempt: EpisodeMergeAttempt | None = None
    delivered_source_commit: str | None = None
    delivered_target_branch: str | None = None
    # The target tip the delivery merged into; later cleanup requires it to stay reachable.
    delivered_target_commit: str | None = None
    squash_commit: str | None = None
    graph_archived: bool = False


class ProjectHomeTransfer(BaseModel):
    """One ordered, human-authorized change to a project's writable home."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    previous_home_space_id: str
    new_home_space_id: str
    source_released_by: AuthorizedHuman
    target_admitted_by: AuthorizedHuman

    _validate_uuid4 = field_validator(
        "project_id",
        "previous_home_space_id",
        "new_home_space_id",
    )(_canonical_uuid4)

    @model_validator(mode="after")
    def authority_matches_both_space_scoped_boundaries(self) -> ProjectHomeTransfer:
        if self.previous_home_space_id == self.new_home_space_id:
            raise ValueError("a project home transfer must change spaces")
        if self.source_released_by.space_id != self.previous_home_space_id:
            raise ValueError("the source-release actor must belong to the previous home space")
        if self.target_admitted_by.space_id != self.new_home_space_id:
            raise ValueError("the target-admission actor must belong to the new home space")
        return self


class GraphBranchMetadata(BaseModel):
    """Canonical branch identity, immutable ancestry, and current head.

    Generation-one records without a parent retain their main base and episode
    identity. New records name an exact parent and a separate branch UUID.
    """

    model_config = ConfigDict(extra="forbid")

    schema_generation: Literal[1] = 1
    branch_id: str
    episode_id: str | None = None
    project_id: str = Field(min_length=1)
    kind: Literal["auto_research", "experiment_loop", "fork"] = "auto_research"
    base_head: GraphHeadRef
    parent_head: GraphHeadRef | None = None
    created_by: str | None = Field(default=None, min_length=1)
    head: GraphHeadRef
    created_at: datetime = Field(default_factory=utc_now)
    authorized_by: AuthorizedHuman

    _validate_uuid4 = field_validator("branch_id")(_canonical_uuid4)

    @property
    def effective_parent_head(self) -> GraphHeadRef:
        return self.parent_head or self.base_head

    @model_validator(mode="after")
    def identity_and_heads_are_coherent(self) -> GraphBranchMetadata:
        if self.episode_id is not None:
            _canonical_uuid4(self.episode_id)
        if self.parent_head is None and self.branch_id != self.episode_id:
            raise ValueError("a legacy graph branch must use its owner episode UUID")
        if self.parent_head is not None and self.created_by is None:
            raise ValueError("a new graph branch requires its creating operation identity")
        if self.base_head.target.kind != "main":
            raise ValueError("a graph branch base must name a main head")
        parent = self.effective_parent_head
        if parent.target.branch_id == self.branch_id:
            raise ValueError("a graph branch cannot parent itself")
        if parent.revision < self.base_head.revision:
            raise ValueError("a graph branch parent cannot precede its root main base")
        if parent.target.kind == "main" and parent != self.base_head:
            raise ValueError("a main parent must be the exact root base")
        if self.head.target.kind != "branch" or self.head.target.branch_id != self.branch_id:
            raise ValueError("a graph branch head must name its exact branch")
        if self.head.revision < parent.revision:
            raise ValueError("a graph branch head cannot precede its immutable parent")
        if (
            self.head.revision == parent.revision
            and self.head.transition_id != parent.transition_id
        ):
            raise ValueError("a new graph branch head must preserve its parent transition identity")
        return self


class BranchMergeProvenance(BaseModel):
    """Strict source/rebase identity stamped on one destination merge Patch."""

    model_config = ConfigDict(extra="forbid")

    schema_generation: Literal[1] = 1
    merge_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    branch_id: str
    episode_id: str | None
    branch_base_head: GraphHeadRef
    branch_head: GraphHeadRef
    rebased_main_head: GraphHeadRef
    merge_task_id: str = Field(min_length=1)

    _validate_uuid4 = field_validator("branch_id")(_canonical_uuid4)

    @model_validator(mode="after")
    def source_and_rebase_heads_are_coherent(self) -> BranchMergeProvenance:
        if self.episode_id is not None:
            _canonical_uuid4(self.episode_id)
        if (
            self.branch_head.target.kind != "branch"
            or self.branch_head.target.branch_id != self.branch_id
        ):
            raise ValueError("branch merge provenance names a different branch head")
        if self.branch_head.revision < self.branch_base_head.revision:
            raise ValueError("branch merge provenance has a head before its base")
        if self.rebased_main_head.target != self.branch_base_head.target:
            raise ValueError("branch merge provenance must rebase onto its source parent")
        if self.rebased_main_head.target == self.branch_head.target:
            raise ValueError("a branch cannot merge into itself")
        if self.rebased_main_head.revision < self.branch_base_head.revision:
            raise ValueError("branch merge provenance cannot rebase before its branch base")
        return self


class BranchMergeReceipt(BaseModel):
    """Append-only acknowledgement that one branch head reached its destination exactly once."""

    model_config = ConfigDict(extra="forbid")

    schema_generation: Literal[1] = 1
    outcome: Literal["committed", "no_change"]
    provenance: BranchMergeProvenance
    result_main_head: GraphHeadRef
    authorized_by: AuthorizedHuman
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def committed_head_is_coherent(self) -> BranchMergeReceipt:
        if self.result_main_head.target != self.provenance.rebased_main_head.target:
            raise ValueError("a branch merge receipt must retain its destination target")
        if self.outcome == "committed":
            if self.result_main_head.transition_id is None:
                raise ValueError("a committed branch merge requires its transition identity")
            if self.result_main_head.revision != self.provenance.rebased_main_head.revision + 1:
                raise ValueError(
                    "a committed branch merge must advance its destination exactly once"
                )
        elif self.result_main_head != self.provenance.rebased_main_head:
            raise ValueError(
                "a no-change branch merge must retain the exact rebased destination head"
            )
        return self


class GraphBranchSummary(BaseModel):
    """Strict episode/API projection; the browser never reconstructs branch state.

    ``episode_id`` is the optional owning chain root; ``current_episode_id`` is
    its newest continuation. Human forks have neither episode identity.
    """

    model_config = ConfigDict(extra="forbid")

    branch_id: str
    episode_id: str | None
    current_episode_id: str | None
    base_head: GraphHeadRef
    parent_head: GraphHeadRef | None = None
    head: GraphHeadRef
    merge_eligible: bool
    merge_blocked_reason: str | None = None
    merge_state: Literal["unmerged", "running", "merged", "needs_action", "failed"]
    latest_successful_merge: BranchMergeReceipt | None = None
    active_merge_task_id: str | None = Field(default=None, min_length=1)
    merge_diagnostic: str | None = None

    _validate_uuid4 = field_validator("branch_id")(_canonical_uuid4)

    @model_validator(mode="after")
    def merge_projection_is_coherent(self) -> GraphBranchSummary:
        for value in (self.episode_id, self.current_episode_id):
            if value is not None:
                _canonical_uuid4(value)
        if self.base_head.target.kind != "main":
            raise ValueError("branch summary base must name main")
        if self.head.target.kind != "branch" or self.head.target.branch_id != self.branch_id:
            raise ValueError("branch summary head must name its exact branch")
        if (self.merge_state == "running") != (self.active_merge_task_id is not None):
            raise ValueError("only a running branch merge may name an active merge task")
        if self.merge_state == "merged" and self.latest_successful_merge is None:
            raise ValueError("a merged branch summary requires its successful receipt")
        if self.merge_eligible and self.merge_state == "running":
            raise ValueError("a branch with an active merge task is not merge eligible")
        return self


class Patch(BaseModel):
    schema_generation: Literal[1, 2] = 2
    revision: int = 0
    kind: Literal["seed", "refresh", "chat", "work", "experiment_loop", "approval", "identity"]
    # ``author`` retains its historical human/agent role semantics. Identity
    # revisions have no such author; their separate producer is RCP itself.
    author: Literal["agent", "human"] | None
    producer: Literal["agent", "human", "system"]
    created_at: datetime = Field(default_factory=utc_now)
    summary: str
    ops: list[GraphOperation]
    run_truth_scope: list[str] = Field(default_factory=list)
    repositories_read: list[str] = Field(default_factory=list)
    processed_cursors: dict[str, str] = Field(default_factory=dict)
    change_summary: list[str] = Field(default_factory=list)
    source_operation_id: str | None = None
    # One task may perform several in-turn effects. RCP stamps this separate
    # identity when an effect needs crash-safe canonical deduplication while
    # ``source_operation_id`` remains the direct authorized task.
    source_effect_id: str | None = Field(default=None, min_length=1)
    source_effect_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    # Which human authority action produced this patch, when its operations
    # alone cannot say. A direct Decision choice and an ordinary node edit are
    # both one `update_nodes` on one node, but they carry different authority
    # and are validated by different rules, so the producer names the action
    # rather than leaving the validator to infer it from shape.
    human_action: Literal["decision_choice"] | None = None
    # The same explicit action boundary for an elevated agent producer. RCP
    # supplies the profile; episode lineage remains in operational storage.
    agent_action: Literal["decision_choice"] | None = None
    admission: Literal["accepted", "rejected"] = "accepted"
    admission_messages: list[ValidationMessage] = Field(default_factory=list)
    # RCP stamps these after reading an experiment-loop deliverable. They are
    # persisted so canonical replay enforces the same control boundary.
    experiment_control_node_id: str | None = None
    experiment_decision_bundle: list[ExperimentDecisionPin] = Field(default_factory=list)
    project_identity: ProjectIdentity | None = None
    project_home_transfer: ProjectHomeTransfer | None = None
    authorized_by: AuthorizedHuman | None = None
    profile: Literal["ordinary", "orchestrator"] | None = None
    task_id: str | None = Field(default=None, min_length=1)
    episode_id: str | None = Field(default=None, min_length=1)
    branch_merge: BranchMergeProvenance | None = None
    transition: TransitionTrace | None = None

    @field_serializer("ops")
    def serialize_ops(
        self,
        operations: list[GraphOperation] | list[dict[str, Any]],
        info: SerializationInfo,
    ) -> list[dict[str, Any]]:
        return [
            operation.model_dump(mode=info.mode, exclude_unset=True)
            if isinstance(operation, BaseModel)
            else operation
            for operation in operations
        ]

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_producer(cls, value: Any) -> Any:
        """Reject live legacy lineage and fill producer on older lineage-free payloads."""

        if not isinstance(value, dict):
            return value
        if "campaign_id" in value:
            raise ValueError("campaign_id is not accepted; use episode_id")
        if "producer" in value or "author" not in value:
            return value
        migrated = dict(value)
        migrated["producer"] = migrated["author"]
        return migrated

    @model_validator(mode="after")
    def reject_legacy_proposal_intents_in_current_generation(self) -> Patch:
        if self.schema_generation == 1:
            return self
        for operation in self.ops:
            if operation.op != "create_proposals":
                continue
            for proposal in operation.proposals:
                if any(item.intent.startswith("legacy_") for item in proposal.ops):
                    raise ValueError(
                        "legacy Proposal operation shapes are accepted only through persisted "
                        "schema-generation 1 decoding"
                    )
        return self

    @model_validator(mode="after")
    def branch_merge_attribution_is_complete(self) -> Patch:
        provenance = self.branch_merge
        if provenance is None:
            return self
        if (
            self.kind != "work"
            or self.author != "agent"
            or self.producer != "agent"
            or self.profile != "orchestrator"
            or self.authorized_by is None
        ):
            raise ValueError("a branch merge must be an attributed orchestrator Work Patch")
        if self.task_id != provenance.merge_task_id:
            raise ValueError("branch merge provenance does not match its direct task")
        if self.transition is not None and self.transition.pre_head != provenance.rebased_main_head:
            raise ValueError("branch merge transition does not start from its rebased main head")
        return self


# The operation contract depends on the domain payloads above, while Patch and
# Proposal expose the operation unions. Importing after the models are declared
# keeps that dependency one-way at runtime and lets Pydantic resolve the two
# forward references explicitly.
from rcp.core.operations import GraphOperation, ProposalOperation  # noqa: E402
from rcp.core.transition_models import TransitionTrace  # noqa: E402

Proposal.model_rebuild(_types_namespace={"ProposalOperation": ProposalOperation})
Patch.model_rebuild(
    _types_namespace={
        "GraphOperation": GraphOperation,
        "TransitionTrace": TransitionTrace,
        "GraphHeadRef": GraphHeadRef,
    }
)
GraphBranchMetadata.model_rebuild(_types_namespace={"GraphHeadRef": GraphHeadRef})
BranchMergeProvenance.model_rebuild(_types_namespace={"GraphHeadRef": GraphHeadRef})
BranchMergeReceipt.model_rebuild(_types_namespace={"GraphHeadRef": GraphHeadRef})
GraphBranchSummary.model_rebuild(_types_namespace={"GraphHeadRef": GraphHeadRef})
GraphState.model_rebuild(
    _types_namespace={"EvidenceAssessment": EvidenceAssessment, "Proposal": Proposal}
)

# RCP owns every Proposal field but id, title, card, and ops; an agent writes only
# those four. Filing (`prepare_patch_bookkeeping`) stamps these from the graph and
# the filer: they say where a Proposal was filed, not what it proposes. A merge that
# files a branch Proposal again on main restamps them and carries the same Proposal.
PROPOSAL_FILING_STAMPS = graph_fields(Proposal, proposal_owner="filing")
# Resolution sets these; filing resets them to their defaults.
PROPOSAL_RESOLUTION_FIELDS = graph_fields(Proposal, proposal_owner="resolution")
