"""Strict, channel-neutral approval action contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.providers import ProviderId


class MergeEpisodeOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_branch: str | None = None
    history_mode: Literal["merge", "squash"] = "merge"
    remove_worktree: bool = True
    delete_code_branch: bool = True
    archive_graph_branch: bool = True
    keep_branch_open: bool = False
    confirm_unfinished_jobs: bool = False

    @model_validator(mode="after")
    def coherent_cleanup(self):
        if self.history_mode == "squash" and self.keep_branch_open:
            raise ValueError("squash_keep_branch_open")
        if self.delete_code_branch and not self.remove_worktree:
            raise ValueError("delete_branch_requires_remove_worktree")
        return self


class MergeEpisodeBody(MergeEpisodeOptions):
    request_id: str | None = Field(default=None, min_length=1, max_length=120)


class ProjectAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: str = Field(min_length=1)


class ProposalAction(ProjectAction):
    kind: Literal["proposal"]
    proposal_id: str = Field(min_length=1)
    decision: Literal["approved", "rejected"]
    reason: str | None = None


class TruthScopeAction(ProjectAction):
    kind: Literal["truth_scope"]
    alias: str = Field(min_length=1)
    writable: bool


class AutoResearchAction(ProjectAction):
    kind: Literal["auto_research"]
    invocation_ceiling: int = Field(ge=1)
    starting_instruction: str | None = None
    code_worktree: bool | None = None
    worktree_repository: str | None = None
    branch_name: str | None = None
    provider: ProviderId | None = None
    model: str | None = None
    reasoning: str | None = None


class MergeBranchAction(ProjectAction):
    kind: Literal["merge_branch"]
    branch_id: str = Field(min_length=1)


class MergeEpisodeAction(ProjectAction, MergeEpisodeOptions):
    kind: Literal["merge_episode"]
    episode_id: str = Field(min_length=1)


ApprovalAction = Annotated[
    ProposalAction | TruthScopeAction | AutoResearchAction | MergeBranchAction | MergeEpisodeAction,
    Field(discriminator="kind"),
]
