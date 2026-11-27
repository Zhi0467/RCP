"""A chat's open plan cards as files the agent edits, and its edits as card requests.

The card in the store stays the one record. Each Work turn sees every open or parked
card of its chat as `plans/<card id>.json`, written from the card as it stands, so the
agent reads the human's edits and the card's state. A new file proposes a card; an
edited file revises its card, unless the human changed that card during the turn.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.limits import (
    AUTO_RESEARCH_PROMPT_FILE_MAX_BYTES,
    EPISODE_TEXT_MAX_LENGTH,
    JSON_SAFE_INTEGER_MAX,
    OPEN_PLAN_CARDS_MAX,
)
from rcp.providers import ProviderId
from rcp.transport.watcher_workspace import direct_json_stem, replace_agent_folder

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.storage import AppStore
    from rcp.transport.run_stage import RemoteRunStage

PLANS = "plans"
_BASELINE = "plan_folder"
_RCP_OWNED = {"state", "version"}


def plan_folder_contract() -> str:
    """How a Work turn reads and writes plan cards, shared by both plan kinds."""

    return (
        "Plan cards are files in plans/ of this run workspace. Each open or parked card of "
        "this chat is there as <card id>.json, written from the card as it stands, so it "
        "shows the human's edits. Fields: kind (experiment or auto_research), node (an "
        "experiment's node id), goal (the handoff), invocation_limit, and optional provider, "
        "model, reasoning; an auto_research plan may add code_worktree, worktree_repository, "
        "branch_name. state (waiting or parked) and version are RCP's. A new file proposes a "
        "new card and edits to a card's file revise that card when this turn ends; deleting a "
        "file changes nothing. The human's edit made during this turn wins over yours. When "
        "the discussion settles on a study, raise its card in the same turn instead of asking "
        "whether to. When the talk changes an open card's plan, edit its file in the same "
        "turn. Nothing starts until the human presses Start; a parked card waits for the human."
    )


class PlanFile(BaseModel):
    """One plan card. `state` and `version` are RCP's; edits to them change nothing."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["experiment", "auto_research"]
    node: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str = Field(min_length=1)
    invocation_limit: int | None = Field(default=None, gt=0, le=JSON_SAFE_INTEGER_MAX)
    provider: ProviderId | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning: str | None = Field(default=None, min_length=1, max_length=100)
    code_worktree: bool | None = None
    worktree_repository: str | None = Field(default=None, min_length=1, max_length=200)
    branch_name: str | None = Field(default=None, min_length=1, max_length=200)
    state: Literal["waiting", "parked"] = "waiting"
    version: str | None = None

    @model_validator(mode="after")
    def fits_its_kind(self) -> Self:
        auto_only = (self.code_worktree, self.worktree_repository, self.branch_name)
        if self.kind == "experiment" and (
            self.node is None or any(value is not None for value in auto_only)
        ):
            raise ValueError("an experiment plan names its node and no Auto-research options")
        if self.kind == "auto_research" and (self.node is not None or not self.invocation_limit):
            raise ValueError("an auto_research plan names an invocation_limit and no node")
        if not self.goal.strip():
            raise ValueError("the goal is blank")
        if self.kind == "auto_research" and len(self.goal) > EPISODE_TEXT_MAX_LENGTH:
            raise ValueError(f"the goal exceeds {EPISODE_TEXT_MAX_LENGTH} characters")
        if len(self.goal.encode()) > AUTO_RESEARCH_PROMPT_FILE_MAX_BYTES:
            raise ValueError(f"the goal exceeds {AUTO_RESEARCH_PROMPT_FILE_MAX_BYTES} bytes")
        return self

    @property
    def subject(self) -> tuple[str, str | None]:
        """What a card is for: its kind and node. A card's subject never changes."""
        return self.kind, self.node

    def intent(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude=_RCP_OWNED, exclude_none=True)


def _plan(card: dict[str, Any]) -> PlanFile:
    """The stored card as a file, unvalidated: the card's own write paths own its limits."""

    action: dict[str, Any] = card["action"]
    auto = action["kind"] == "auto_research"
    return PlanFile.model_construct(
        kind="auto_research" if auto else "experiment",
        node=None if auto else action["node_id"],
        goal=(action.get("starting_instruction") if auto else action["goal"]) or "",
        invocation_limit=action.get("invocation_ceiling"),
        provider=action.get("provider"),
        model=action.get("model"),
        reasoning=action.get("reasoning"),
        code_worktree=action.get("code_worktree") if auto else None,
        worktree_repository=action.get("worktree_repository") if auto else None,
        branch_name=action.get("branch_name") if auto else None,
        state="parked" if card["state"] == "parked" else "waiting",
        version=str(card["pinned_sha256"]),
    )


def open_plan_cards(
    store: AppStore, project_id: str, chat_id: str
) -> list[tuple[str, dict[str, Any]]]:
    """This chat's pending or parked plan cards, oldest first."""

    from rcp.runs.approval_cards import PLAN_CARD_KINDS

    pending = [
        item.question_id
        for item in store.list_questions(project_id=project_id, owner_kind="chat", owner_id=chat_id)
        if item.state == "pending"
    ]
    cards = store.approval_cards(pending)
    return [
        (question_id, cards[question_id])
        for question_id in pending
        if question_id in cards
        and cards[question_id]["state"] in {"pending", "parked"}
        and cards[question_id]["action"]["kind"] in PLAN_CARD_KINDS
    ]


def plan_files(store: AppStore, project_id: str, chat_id: str) -> dict[str, str]:
    """Every open or parked plan card of this chat, as the files to stage."""

    return {
        f"{question_id}.json": _plan(card).model_dump_json(exclude_none=True, indent=2)
        for question_id, card in open_plan_cards(store, project_id, chat_id)
    }


def stage_plan_folder(
    store: AppStore,
    operation_id: str,
    project_id: str,
    chat_id: str,
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    *,
    continues: str | None = None,
) -> None:
    """Write the chat's cards once per turn and keep that view as the turn's baseline.

    A Resume or Retry (`continues` names the turn it continues) keeps that turn's
    baseline and the files its agent already wrote, so unsubmitted plans survive.
    """

    saved = store.agent_task_contract(operation_id, _BASELINE)
    inherited = False
    if saved is None and continues is not None:
        saved = store.agent_task_contract(continues, _BASELINE)
        inherited = saved is not None
    if saved is None:
        saved = json.dumps(plan_files(store, project_id, chat_id), sort_keys=True)
    store.record_agent_task_contract(
        operation_id, _BASELINE, saved, hashlib.sha256(saved.encode()).hexdigest()
    )
    if inherited and _written_plans(workspace, remote_stage):
        return
    files = json.loads(saved)
    if remote_stage is None:
        replace_agent_folder(workspace, PLANS, files)
    else:
        remote_stage.replace_agent_folder(PLANS, files)


def _written_plans(workspace: Path, remote_stage: RemoteRunStage | None) -> bool:
    """Whether the continued turn left plan files here; a new workspace has none."""

    try:
        return bool(read_plan_folder(workspace, remote_stage))
    except (OSError, ValueError):
        return False


def read_plan_folder(workspace: Path, remote_stage: RemoteRunStage | None) -> dict[str, str]:
    from rcp.watcher_folder import read_agent_folder

    return read_agent_folder(workspace, remote_stage, PLANS)


def record_plan_folder(
    execution: AgentTaskExecution, workspace: Path, remote_stage: RemoteRunStage | None
) -> str | None:
    """Turn new and edited plan files into card requests; return what the agent must fix."""

    from rcp.runs.work_experiment_commands import record_plan_card

    saved = execution.store.agent_task_contract(execution.operation_id, _BASELINE)
    if saved is None:
        return None
    baseline: dict[str, str] = json.loads(saved)
    staged_plans = {
        name: PlanFile.model_construct(**json.loads(text)) for name, text in baseline.items()
    }
    problems: list[str] = []
    held = len(staged_plans)
    for name, text in sorted(read_plan_folder(workspace, remote_stage).items()):
        if baseline.get(name) == text:
            continue
        try:
            direct_json_stem(name)
            plan = PlanFile.model_validate_json(text)
        except ValueError as exc:
            problems.append(f"plans/{name}: {exc}")
            continue
        staged = staged_plans.get(name)
        if staged is None:
            problem = _new_plan_problem(plan, staged_plans, held)
            revises = None
        elif plan.intent() == staged.intent():
            continue
        else:
            problem = _revision_problem(plan, staged)
            assert staged.version is not None
            revises = (Path(name).stem, staged.version)
        problem = problem or record_plan_card(execution, name, plan, revises=revises)
        held += staged is None and not problem
        if problem:
            problems.append(f"plans/{name}: {problem}")
    return "\n".join(problems) or None


def _new_plan_problem(plan: PlanFile, staged_plans: dict[str, PlanFile], held: int) -> str | None:
    if held >= OPEN_PLAN_CARDS_MAX:
        return f"this chat already holds {OPEN_PLAN_CARDS_MAX} open plan cards"
    same = next(
        (name for name, staged in staged_plans.items() if staged.subject == plan.subject), None
    )
    return None if same is None else f"plans/{same} already holds this plan; edit it"


def _revision_problem(plan: PlanFile, staged: PlanFile) -> str | None:
    if plan.subject != staged.subject:
        return "a card's kind and node are fixed; write a new file for another plan"
    return None
