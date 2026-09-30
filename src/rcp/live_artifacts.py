"""The validated declaration, bindings, and data protocol for live HTML artifacts."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rcp.limits import LIVE_ARTIFACT_MAX_NEEDS


class LiveModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobNeed(LiveModel):
    kind: Literal["job"] = Field(default="job", description="Watch a helper-launched job.")
    key: str = Field(min_length=1, description="The key passed to launch --key in this lineage.")


class NodeNeed(LiveModel):
    kind: Literal["node"] = Field(
        default="node", description="Watch a node in this artifact's graph."
    )
    id: str = Field(min_length=1, description="The node id in the artifact's main or branch graph.")


class EpisodeNeed(LiveModel):
    kind: Literal["episode"] = Field(
        default="episode", description="Watch this artifact's own episode."
    )


class FileNeed(LiveModel):
    kind: Literal["file"] = Field(
        default="file", description="Read a regular file on the execution host."
    )
    path: str = Field(
        description="Absolute path inside a registered project repository or this artifact's own ready conversation/episode worktree, on its execution host."
    )
    read: Literal["tail", "whole"] = Field(
        description="Read the bounded tail or bounded whole file."
    )
    format: Literal["jsonl", "csv", "text"] = Field(
        description="Decode JSON lines, CSV records, or text lines."
    )

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if (
            not PurePosixPath(value).is_absolute()
            or "\x00" in value
            or ".." in PurePosixPath(value).parts
        ):
            raise ValueError("file path must be absolute without parent traversal")
        return value


LiveNeed = Annotated[JobNeed | NodeNeed | EpisodeNeed | FileNeed, Field(discriminator="kind")]


class LiveTag(LiveModel):
    version: Literal[1] = Field(default=1, description="Declaration protocol version.")
    needs: list[LiveNeed] = Field(
        min_length=1,
        max_length=LIVE_ARTIFACT_MAX_NEEDS,
        description="Sources to watch in snapshot order.",
    )


class ResolvedLiveNeed(LiveModel):
    need: LiveNeed = Field(description="The validated source declaration.")
    job_id: str | None = Field(
        default=None, description="Stable job id resolved from its launch receipt."
    )
    graph_branch_id: str | None = Field(
        default=None, description="Bound graph branch; null means main."
    )
    episode_id: str | None = Field(default=None, description="The artifact's bound episode id.")
    host: str | None = Field(default=None, description="Execution host; null means local.")
    root: str | None = Field(default=None, description="Readable root that admitted this file.")


class ResolvedLiveVersion(LiveModel):
    needs: list[ResolvedLiveNeed] = Field(
        default_factory=list, description="Resolved sources for this immutable version."
    )
    graph_branch_id: str | None = Field(
        default=None, description="Artifact graph target; null means main."
    )
    execution_host: str | None = Field(
        default=None, description="Artifact execution host; null means local."
    )
    invalid_reason: str | None = Field(
        default=None, description="Why the declaration remains static."
    )
    capture_attempts: int = Field(default=0, description="Number of final capture attempts.")
    next_capture_at: str | None = Field(
        default=None, description="Next allowed final capture retry time."
    )
    capture_error: str | None = Field(
        default=None, description="Last incomplete final capture diagnostic."
    )


class LiveSnapshot(LiveModel):
    error: str | None = Field(
        default=None, description="Read failure; missing data is never a completed capture."
    )


class JobSnapshot(LiveSnapshot):
    kind: Literal["job"] = Field(default="job", description="Job snapshot.")
    key: str = Field(description="The declared launch key.")
    state: str | None = Field(default=None, description="Current job lifecycle state.")
    exit_code: int | None = Field(default=None, description="Process exit code when known.")
    started_at: str | None = Field(default=None, description="Job start timestamp when known.")
    ended_at: str | None = Field(default=None, description="Job end timestamp when known.")
    log_tail: str = Field(default="", description="Bounded trailing job log lines.")


class LiveEvidence(LiveModel):
    id: str = Field(description="Evidence node id.")
    title: str = Field(description="Evidence title.")
    stance: str = Field(description="Evidence stance toward the watched node.")


class NodeSnapshot(LiveSnapshot):
    kind: Literal["node"] = Field(default="node", description="Graph node snapshot.")
    id: str = Field(description="The declared node id.")
    title: str | None = Field(default=None, description="Current node title.")
    type: str | None = Field(default=None, description="Node type.")
    status: str | None = Field(default=None, description="Node status when available.")
    evidence: list[LiveEvidence] = Field(
        default_factory=list, description="Connected Evidence, each with id, title, and stance."
    )


class EpisodeSnapshot(LiveSnapshot):
    kind: Literal["episode"] = Field(default="episode", description="Own episode snapshot.")
    turn: int | None = Field(default=None, description="Episode turn count.")
    turn_limit: int | None = Field(default=None, description="Authorized turn ceiling.")
    budget_used: float | None = Field(default=None, description="Episode invocations consumed.")
    budget_limit: float | None = Field(
        default=None, description="Authorized episode invocation ceiling."
    )
    state: str | None = Field(default=None, description="Episode lifecycle state.")


class FileSnapshot(LiveSnapshot):
    kind: Literal["file"] = Field(default="file", description="File snapshot.")
    path: str = Field(description="The declared absolute file path.")
    rows: list[Any] = Field(
        default_factory=list,
        description="JSON values, CSV records as arrays, or text lines, in file order.",
    )
    truncated: bool = Field(
        default=False, description="True when a row or byte cap omitted content."
    )


Snapshot = Annotated[
    JobSnapshot | NodeSnapshot | EpisodeSnapshot | FileSnapshot, Field(discriminator="kind")
]


class LiveDataMessage(LiveModel):
    kind: Literal["rcp-live-data"] = Field(
        default="rcp-live-data", description="Viewer-relayed message discriminator."
    )
    version: Literal[1] = Field(default=1, description="Snapshot protocol version.")
    snapshots: list[Snapshot] = Field(
        description="Snapshots in declaration order, identified by kind and key, id, or path."
    )
    static: bool = Field(
        default=False,
        description="True when no valid live declaration is available; stop refreshing.",
    )
    reason: str | None = Field(default=None, description="Visible reason this page remains static.")
    final: bool = Field(default=False, description="True only for a saved complete final snapshot.")
    complete: bool = Field(
        default=True, description="False if any declared source could not be read."
    )
    refresh_seconds: float = Field(description="Visible viewer refresh interval in seconds.")


NEED_SNAPSHOT_MODELS = (
    (JobNeed, JobSnapshot),
    (NodeNeed, NodeSnapshot),
    (EpisodeNeed, EpisodeSnapshot),
    (FileNeed, FileSnapshot),
)


class _LiveTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: list[str] = []
        self.active = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id") != "rcp-live":
            return
        if tag != "script" or values.get("type") != "application/json":
            raise ValueError("rcp-live must be an application/json script tag")
        if self.active or self.tags:
            raise ValueError("only one rcp-live tag is allowed")
        self.active = True

    def handle_data(self, data: str) -> None:
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.active and tag == "script":
            self.tags.append("".join(self.parts))
            self.active = False


def parse_live_tag(html: str, *, allow_episode: bool = False) -> LiveTag | None:
    """Return no declaration for static HTML; reject malformed or unauthorized tags."""
    parser = _LiveTagParser()
    parser.feed(html)
    parser.close()
    if parser.active:
        raise ValueError("rcp-live script tag is not closed")
    if not parser.tags:
        return None
    tag = LiveTag.model_validate_json(parser.tags[0])
    if not allow_episode and any(isinstance(need, EpisodeNeed) for need in tag.needs):
        raise ValueError("episode needs are available only for episode artifacts")
    return tag
