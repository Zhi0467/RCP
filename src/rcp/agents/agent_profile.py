"""One agent's grants, resolved once per turn from its placement and episode role.

Every verb an agent may call is granted here and nowhere else. The command
handler enforces `AgentProfile.verbs`, the prompt renders `AgentProfile.usage`,
and the welcome lists `AgentProfile.welcome`, so the three cannot drift.
Placement grants live in `_placement_grants`; episode role grants live in
`_ROLE_GRANTS`.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rcp.agents.command_protocol import CommandVerb
from rcp.agents.graph_read_scope import GRAPH_HISTORY_VERBS

if TYPE_CHECKING:
    from rcp.agents.graph_read_scope import GraphReadScope
    from rcp.background import AgentTaskExecution
    from rcp.runs.tasks.compute_commands import WorkComputeCommands
    from rcp.service import ProjectService

Entry = Literal["single_entry", "project"]
Visibility = Literal["private", "shared"]
EpisodeRole = Literal[
    "experiment_loop", "orchestrator", "auto_research_worker", "child", "consolidation", "discuss"
]


@dataclass(frozen=True)
class Placement:
    entry: Entry
    visibility: Visibility


SINGLE_ENTRY = Placement("single_entry", "private")
PRIVATE_PROJECT = Placement("project", "private")
SHARED_PROJECT = Placement("project", "shared")
PLACEMENTS = (SINGLE_ENTRY, PRIVATE_PROJECT, SHARED_PROJECT)


@dataclass(frozen=True)
class AgentTurn:
    """The launch facts a grant depends on, resolved once by the turn's owner."""

    read_verbs: frozenset[str] = frozenset()
    launch: bool = False
    can_self_name: bool = False
    bulletin: bool = False
    ask: bool = False
    episode_start: bool = False
    projects: bool = False
    schedule: bool = False


OFFER = AgentTurn(
    read_verbs=frozenset(GRAPH_HISTORY_VERBS),
    launch=True,
    can_self_name=True,
    bulletin=True,
    ask=True,
    episode_start=True,
    projects=True,
    schedule=True,
)


@dataclass(frozen=True)
class UsageContext:
    """What a prompt site knows when it renders the profile's usage."""

    client: str
    execution: AgentTaskExecution | None = None
    service: ProjectService | None = None
    compute_commands: WorkComputeCommands | None = None
    graph_read_scope: GraphReadScope | None = None
    ask_wait_seconds: float | None = None
    approval_cards: bool = False
    turn_artifacts: bool = False


@dataclass(frozen=True)
class AgentProfile:
    placement: Placement
    episode_role: EpisodeRole | None
    verbs: frozenset[str]

    @property
    def ordered_verbs(self) -> tuple[CommandVerb, ...]:
        return tuple(verb for verb in VERBS if verb in self.verbs)

    @property
    def binds_questions(self) -> bool:
        """Whether an answer resumes the asking session under its write binding.

        A single-entry agent asks only approval cards; their answers resume nothing.
        """
        return self.placement.entry == "project"

    def welcome(self) -> list[str]:
        return list(dict.fromkeys(VERBS[verb].welcome for verb in self.ordered_verbs))

    def usage(self, context: UsageContext) -> dict[CommandVerb, str]:
        return {verb: VERBS[verb].usage(self, context) for verb in self.ordered_verbs}

    def instructions(self, context: UsageContext) -> str:
        sections = [text for text in self.usage(context).values() if text]
        return "\n\n".join(dict.fromkeys(sections))


def resolve_agent_profile(
    placement: Placement, episode_role: EpisodeRole | None, turn: AgentTurn
) -> AgentProfile:
    """Grant a turn its verbs; an Experiment turn never starts another episode."""
    if episode_role is None or episode_role == "experiment_loop":
        verbs = _placement_grants(placement, turn)
    else:
        verbs = _ROLE_GRANTS[episode_role](turn)
    if episode_role == "experiment_loop":
        verbs = verbs - {"episode"}
    return AgentProfile(placement, episode_role, frozenset(verbs))


def _when(granted: bool, *verbs: str) -> set[str]:
    return set(verbs) if granted else set()


def _shared(turn: AgentTurn) -> set[str]:
    return {*turn.read_verbs, *_when(turn.can_self_name, "name"), *_when(turn.bulletin, "bulletin")}


def _placement_grants(placement: Placement, turn: AgentTurn) -> set[str]:
    """A single-entry agent hands project work off.

    It never starts an episode, launches jobs, asks a plain question, opens a
    worktree, or posts to a Bulletin; its project clients read and write instead.
    """
    if placement.entry == "single_entry":
        return {"projects", "handoff", "schedule", "ask"}
    return {
        "validate",
        *_shared(turn),
        *_when(turn.launch, "launch"),
        *_when(turn.projects, "projects"),
        *_when(turn.schedule, "schedule"),
        *_when(turn.ask, "ask"),
        *_when(turn.episode_start, "episode"),
    }


_ROLE_GRANTS: dict[EpisodeRole, Callable[[AgentTurn], set[str]]] = {
    "orchestrator": lambda turn: {
        "validate",
        "apply",
        "status",
        "spawn",
        "merge",
        "branch",
        "pause",
        "resume",
        "stop",
        "message",
        "episode",
        "inbox",
        "finish",
        "ask",
        *_shared(turn),
    },
    "auto_research_worker": lambda turn: {
        "validate",
        "status",
        "message",
        *turn.read_verbs,
        *_when(turn.bulletin, "bulletin"),
    },
    "child": lambda turn: {"validate", "message", *_shared(turn), *_when(turn.launch, "launch")},
    "consolidation": lambda turn: {"validate", "apply", "flag", "bulletin", *turn.read_verbs},
    "discuss": lambda turn: {
        *turn.read_verbs,
        *_when(turn.can_self_name, "name"),
        *_when(turn.ask, "ask"),
    },
}


def single_entry_client_profile(read_verbs: frozenset[str], *, writer: bool) -> AgentProfile:
    """A single-entry turn's per-project client: reads, Bulletin, and a writer's validate."""
    verbs = {*read_verbs, "bulletin", *_when(writer, "validate")}
    return AgentProfile(SINGLE_ENTRY, None, frozenset(verbs))


def _auto_research(profile: AgentProfile) -> bool:
    return profile.episode_role in {"orchestrator", "auto_research_worker"}


def _bullets(context: UsageContext, forms: tuple[str, ...]) -> str:
    return "\n".join(f"- `{context.client} {form}`" for form in forms)


def _forms(*forms: str) -> Callable[[AgentProfile, UsageContext], str]:
    return lambda _profile, context: _bullets(context, forms)


def _projects_usage(_profile: AgentProfile, context: UsageContext) -> str:
    from rcp.agents.staged_command_client import projects_usages

    return _bullets(context, projects_usages())


def _handoff_usage(_profile: AgentProfile, context: UsageContext) -> str:
    from rcp.agents.staged_command_client import handoff_usage
    from rcp.limits import AGENT_HANDOFF_MAX_CHARS

    capabilities = "; ".join(resolve_agent_profile(PRIVATE_PROJECT, None, OFFER).welcome())
    return _bullets(
        context,
        (
            f"{handoff_usage()} (message file up to {AGENT_HANDOFF_MAX_CHARS} characters; "
            "a pick becomes the project thread's agent; the request is delivered after this "
            "turn succeeds; a later wake reports its status, failure kind and reason, and "
            "answer, so a failed provider can be retried with another pick). A project "
            f"thread's agent can do this for its member: {capabilities}.",
        ),
    )


def _schedule_usage(profile: AgentProfile, context: UsageContext) -> str:
    from rcp.agents.staged_command_client import schedule_usages

    if profile.placement.entry == "single_entry" or context.execution is None:
        return _bullets(context, schedule_usages())
    from rcp.runs.agent_schedules import schedule_instructions

    return schedule_instructions(
        context.execution.store,
        context.execution.operation_id,
        tuple(f"{context.client} {usage}" for usage in schedule_usages()),
    )


def _ask_usage(profile: AgentProfile, context: UsageContext) -> str:
    if not profile.binds_questions:
        from rcp.runs.approval_cards import space_card_usage

        return _bullets(context, (space_card_usage(),))
    if _auto_research(profile):
        return _bullets(
            context, ("ask --key <key> --question <text> [--choice <text> ...] [--multiple]",)
        )
    from rcp.agents.prompts import COMMAND_CLIENT_WAIT_SECONDS, live_ask_contract

    wait = context.ask_wait_seconds or COMMAND_CLIENT_WAIT_SECONDS
    sections = [live_ask_contract(wait)]
    if context.approval_cards:
        from rcp.runs.approval_cards import approval_card_usage

        sections.append(approval_card_usage(context.client))
    return "\n\n".join(sections)


def _validate_usage(_profile: AgentProfile, context: UsageContext) -> str:
    return _bullets(context, ("validate patch.json",))


def _apply_usage(profile: AgentProfile, context: UsageContext) -> str:
    if _auto_research(profile):
        return _bullets(context, ("apply --key <key> patch.json",))
    return (
        "Use apply --key <key> patch.json to commit patch.json and read the returned revision "
        "before reporting. If apply returns unavailable, retry the same bytes with the same key. "
        "This turn cannot create watchers."
    )


def _graph_history_usage(_profile: AgentProfile, context: UsageContext) -> str:
    """Empty where the run's contract appends the same scope's history section."""
    if context.graph_read_scope is None:
        return ""
    from rcp.agents.prompts import render_graph_history

    return render_graph_history(context.graph_read_scope, context.client)


def _spawn_usage(_profile: AgentProfile, context: UsageContext) -> str:
    from rcp.agents.staged_command_client import child_target_usage, spawn_seat_usage

    return _bullets(
        context,
        (
            f"spawn --key <key> {spawn_seat_usage()} --instruction-file <filename> "
            f"{child_target_usage()} "
            "[--provider <provider>] [--model <model>] [--effort <effort>]",
        ),
    )


def _merge_usage(_profile: AgentProfile, context: UsageContext) -> str:
    from rcp.agents.staged_command_client import merge_usage

    return _bullets(context, (merge_usage(),))


def _branch_usage(_profile: AgentProfile, context: UsageContext) -> str:
    from rcp.agents.staged_command_client import branch_usage

    return _bullets(context, (branch_usage(),))


def _message_usage(profile: AgentProfile, context: UsageContext) -> str:
    if profile.episode_role == "orchestrator":
        return _bullets(context, ("message --key <key> --recipient <worker-id> <body>",))
    return _bullets(context, ("message --key <reply-key> <body>",))


def _episode_usage(profile: AgentProfile, context: UsageContext) -> str:
    if _auto_research(profile):
        from rcp.agents.staged_command_client import experiment_kickoff_usage

        return _bullets(
            context,
            (
                "episode --key <key> "
                + experiment_kickoff_usage(goal_required=False, child_targets=True),
                "episode --key <key> --stop <episode-id>",
                "episode --key <key> --resume <episode-id>",
            ),
        )
    from rcp.agents.auto_research_prompt import render_child_profile_choices
    from rcp.runs.auto_research_admission import auto_research_child_profile_choices
    from rcp.runs.work_episode_controls import work_episode_control_usage
    from rcp.runs.work_experiment_commands import work_experiment_command_usage

    execution = context.execution
    authorizer = (
        execution.store.agent_task_authorizer(execution.operation_id)
        if execution is not None
        else None
    )
    member_id = authorizer.user_id if authorizer else None

    def choices(kind: Literal["node_chat", "orchestrator"]) -> str:
        if context.service is None:
            return render_child_profile_choices({})
        return render_child_profile_choices(
            auto_research_child_profile_choices(context.service, kind=kind, member_id=member_id)
        )

    return "\n\n".join(
        (
            work_experiment_command_usage(context.client, choices("node_chat")),
            work_episode_control_usage(context.client, choices("orchestrator")),
        )
    )


def _launch_usage(_profile: AgentProfile, context: UsageContext) -> str:
    arguments = ("launch", "--key", "<idempotency-key>", "--cwd", "<working-directory>")
    command = f"{context.client} {shlex.join((*arguments, '--', '<argv...>'))}"
    if context.compute_commands is None:
        return f"- `{command}`"
    return context.compute_commands.execution_instructions(command)


def _bulletin_usage(_profile: AgentProfile, context: UsageContext) -> str:
    from rcp.runs.bulletin import bulletin_command_usage

    return bulletin_command_usage(context.client, turn_artifacts=context.turn_artifacts)


def _name_usage(_profile: AgentProfile, context: UsageContext) -> str:
    from rcp.runs.agent_names import name_command_usage

    return name_command_usage(context.client)


def _flag_usage(_profile: AgentProfile, context: UsageContext) -> str:
    from rcp.runs.evidence_flag_commands import flag_command_usage

    return flag_command_usage(context.client)


@dataclass(frozen=True)
class VerbSpec:
    """A verb's member-facing welcome line and its prompt usage renderer."""

    welcome: str
    usage: Callable[[AgentProfile, UsageContext], str]


_GRAPH = "Answers questions about the graph and records results in it"
_JOBS = "Runs and stops jobs on the project's machines and pings you when they end"
_RESEARCH = "Runs Auto-research children and merges their work"

VERBS: dict[CommandVerb, VerbSpec] = {
    "projects": VerbSpec("Reads and answers questions about any of your projects", _projects_usage),
    "handoff": VerbSpec("Hands work to a project's own agent", _handoff_usage),
    "schedule": VerbSpec("Schedules reminders and weekly catch-ups", _schedule_usage),
    "validate": VerbSpec(_GRAPH, _validate_usage),
    "merge_answers": VerbSpec(_GRAPH, _forms("merge-answers")),
    "graph_diff": VerbSpec(_GRAPH, _graph_history_usage),
    "graph_log": VerbSpec(_GRAPH, _graph_history_usage),
    "apply": VerbSpec(_GRAPH, _apply_usage),
    "status": VerbSpec(
        _RESEARCH, _forms("status [--worker-id <worker-id> | --episode-id <episode-id>]")
    ),
    "spawn": VerbSpec(_RESEARCH, _spawn_usage),
    "merge": VerbSpec(_RESEARCH, _merge_usage),
    "branch": VerbSpec(_RESEARCH, _branch_usage),
    "pause": VerbSpec(_RESEARCH, _forms("pause --key <key> <worker-id>")),
    "resume": VerbSpec(_RESEARCH, _forms("resume --key <key> <worker-id>")),
    "stop": VerbSpec(_RESEARCH, _forms("stop --key <key> <worker-id>")),
    "message": VerbSpec("Messages the agents it works with", _message_usage),
    "episode": VerbSpec(
        "Starts, pauses, steers, extends and stops experiments and Auto-research you ask for",
        _episode_usage,
    ),
    "inbox": VerbSpec(
        _RESEARCH, _forms("inbox --key <key> --harvest", "inbox --key <key> --clear")
    ),
    "finish": VerbSpec(_RESEARCH, _forms("finish --key <key>")),
    "launch": VerbSpec(_JOBS, _launch_usage),
    "ask": VerbSpec("Asks your approval, including merges, with a tap", _ask_usage),
    "bulletin": VerbSpec("Talks with other agents on the project Bulletin", _bulletin_usage),
    "name": VerbSpec("Picks its own name", _name_usage),
    "flag": VerbSpec("Flags evidence that needs another look", _flag_usage),
}
