from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.service import RunRequest


class MergeBinding(BaseModel):
    """The immutable source, destination and dispatcher of a merge admission."""

    model_config = ConfigDict(extra="forbid")
    source: GraphTargetRef
    destination: GraphTargetRef
    source_head: GraphHeadRef
    dispatcher_episode_id: str | None = None
    dispatcher_operation_id: str | None = None
    command_key: str | None = None
    requires_agent: bool = True

    @model_validator(mode="after")
    def require_bound_source(self) -> MergeBinding:
        if self.source.kind != "branch" or self.source_head.target != self.source:
            raise ValueError("merge_source_binding_invalid")
        if self.source == self.destination:
            raise ValueError("merge_destination_is_source")
        dispatch = (self.dispatcher_episode_id, self.dispatcher_operation_id, self.command_key)
        if any(dispatch) and not all(dispatch):
            raise ValueError("merge_dispatcher_binding_incomplete")
        return self


class BranchMergeRunRequest(RunRequest):
    """Pinned, graph-only request for one authorized branch merge task."""

    model_config = ConfigDict(extra="forbid")

    episode_id: str = Field(min_length=1)
    merge_binding: MergeBinding | None = None

    @model_validator(mode="after")
    def require_dedicated_merge_shape(self) -> BranchMergeRunRequest:
        if (
            self.provider is None
            or self.model is None
            or self.reasoning is None
            or self.run_on is None
            or not self.run_truth_scope
        ):
            raise ValueError("branch merge requires one fully pinned orchestrator profile")
        if self.run_truth_scope != sorted(set(self.run_truth_scope)):
            raise ValueError("branch merge run truth scope must be sorted and unique")
        if self.mode != "work" or self.trigger != "human" or self.patch_kind != "work":
            raise ValueError("branch merge requires one human-triggered Work Patch contract")
        if (
            self.chat_scope != "project"
            or self.chat_id is not None
            or self.node_id is not None
            or self.message is not None
            or self.session_id is not None
            or self.control_node_id is not None
            or self.control_revision is not None
            or self.control_episode_id is not None
            or self.control_invocation is not None
            or self.control_invocation_ceiling is not None
            or self.control_decision_bundle
            or self.control_completion_criteria
            or self.watcher_ids
            or self.attachments
            or self.references
            or self.attachment_set_id is not None
            or self.attachment_client_id is not None
            or self.attachment_batch_id is not None
        ):
            raise ValueError("branch merge request contains unrelated chat or control state")
        return self
