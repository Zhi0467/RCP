"""Guardrails 4 and 5: one verb registry, and one profile that grants, renders and welcomes.

`_NOT_GRANTS` names the modules whose verb-name collections are not grants. It only shrinks.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest

from rcp.agents.agent_profile import (
    OFFER,
    PLACEMENTS,
    PRIVATE_PROJECT,
    SHARED_PROJECT,
    SINGLE_ENTRY,
    VERBS,
    AgentTurn,
    UsageContext,
    resolve_agent_profile,
    single_entry_client_profile,
)
from rcp.agents.command_protocol import CommandVerb
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.core.transition_models import GraphTargetRef

_SOURCE = Path(__file__).resolve().parent.parent / "src" / "rcp"
_ROLES = (None, "experiment", "orchestrator", "auto_research_worker", "child", "consolidation")
_PROFILES = [
    (placement, role)
    for placement in PLACEMENTS
    for role in (*_ROLES, "discuss")
    if placement.entry == "project" or role is None
]

_NOT_GRANTS = {
    "agents/command_protocol.py": "the CommandVerb and mutating-verb type definitions",
    "agents/staged_command_client.py": "the client's argument parser",
    "agents/graph_read_scope.py": "the persisted graph read grant field",
    "agents/prompts.py": "graph history argument forms",
    "agents/acceptance.py": "result fields naming each verb's durable effect",
    "runs/work_episode_controls.py": "refusal codes for member turn controls",
    "server_ops/backup_capture.py": "unrelated keys that happen to be verb names",
    "storage/consolidation.py": "unrelated keys that happen to be verb names",
}


def _usage_context() -> UsageContext:
    scope = GraphReadScope(project_id="p", target=GraphTargetRef(kind="main"))
    return UsageContext(client="rcp", graph_read_scope=scope)


def test_the_registry_has_exactly_one_entry_per_command_verb() -> None:
    assert tuple(VERBS) == get_args(CommandVerb)


def _verb_literals(tree: ast.AST) -> list[int]:
    verbs = set(get_args(CommandVerb))
    literals = {
        id(node.slice)
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript) and getattr(node.value, "id", None) == "Literal"
    }

    def named(elements: list[ast.expr | None]) -> bool:
        names = [
            element.value
            for element in elements
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        return len(names) >= 2 and len(names) == len(elements) and set(names) <= verbs

    return [
        node.lineno
        for node in ast.walk(tree)
        if (
            isinstance(node, (ast.Set, ast.List, ast.Tuple))
            and id(node) not in literals
            and named(list(node.elts))
        )
        or (isinstance(node, ast.Dict) and named(list(node.keys)))
    ]


def test_no_verb_set_is_built_outside_the_agent_profile() -> None:
    found = {}
    for path in sorted(_SOURCE.rglob("*.py")):
        relative = path.relative_to(_SOURCE).as_posix()
        if relative == "agents/agent_profile.py":
            continue
        lines = _verb_literals(ast.parse(path.read_text()))
        if lines:
            found[relative] = lines
    assert set(found) <= set(_NOT_GRANTS), {
        path: lines for path, lines in found.items() if path not in _NOT_GRANTS
    }
    assert set(_NOT_GRANTS) <= set(found), "Drop allowlist entries that no longer match"


@pytest.mark.parametrize(("placement", "role"), _PROFILES)
def test_enforced_prompt_and_welcome_verbs_are_one_set(placement, role) -> None:
    from rcp.runs.questions import WorkCommandHandler

    profile = resolve_agent_profile(placement, role, OFFER)
    usage = profile.usage(_usage_context())
    assert set(usage) == profile.verbs
    assert all(usage.values())
    assert set(profile.welcome()) == {VERBS[verb].welcome for verb in profile.verbs}
    assert WorkCommandHandler(None, None, profile).allowed_verbs == profile.verbs


def test_the_single_entry_handler_enforces_its_profile() -> None:
    from rcp.runs.agent_thread_commands import ThreadCommandHandler

    handler = ThreadCommandHandler(None, None, "turn")  # pyright: ignore[reportArgumentType]
    assert handler.allowed_verbs == resolve_agent_profile(SINGLE_ENTRY, None, OFFER).verbs
    assert handler.profile.usage(_usage_context()).keys() == handler.allowed_verbs


def test_profile_flags_differ_only_by_entry() -> None:
    flags = {
        placement: resolve_agent_profile(placement, None, OFFER).binds_questions
        for placement in PLACEMENTS
    }
    assert flags == {SINGLE_ENTRY: False, PRIVATE_PROJECT: True, SHARED_PROJECT: True}
    assert (
        resolve_agent_profile(PRIVATE_PROJECT, None, OFFER).verbs
        == resolve_agent_profile(SHARED_PROJECT, None, OFFER).verbs
    )


def test_single_entry_hands_project_work_off_but_keeps_its_own_verbs() -> None:
    profile = resolve_agent_profile(SINGLE_ENTRY, None, OFFER)
    assert profile.verbs == {"projects", "handoff", "schedule", "ask"}
    assert not profile.binds_questions
    for writer in (False, True):
        client = single_entry_client_profile(OFFER.read_verbs, writer=writer)
        assert client.verbs.isdisjoint({"episode", "launch", "ask", "watcher", "handoff"})


def test_auto_research_child_keeps_its_enforced_surface() -> None:
    child = resolve_agent_profile(SHARED_PROJECT, "child", OFFER)
    assert child.verbs == {
        "validate",
        "message",
        "name",
        "bulletin",
        "launch",
        "graph_diff",
        "graph_log",
    }
    without_helper = resolve_agent_profile(SHARED_PROJECT, "child", AgentTurn())
    assert without_helper.verbs == {"validate", "message"}


def test_episode_roles_keep_their_grants() -> None:
    orchestrator = resolve_agent_profile(SHARED_PROJECT, "orchestrator", AgentTurn())
    assert orchestrator.verbs == {
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
        "watch_graph",
        "episode",
        "inbox",
        "finish",
        "ask",
    }
    worker = resolve_agent_profile(SHARED_PROJECT, "auto_research_worker", OFFER)
    assert worker.verbs == {"validate", "status", "message", "bulletin", "graph_diff", "graph_log"}
    experiment = resolve_agent_profile(SHARED_PROJECT, "experiment", OFFER)
    plain = resolve_agent_profile(SHARED_PROJECT, None, OFFER)
    assert experiment.verbs == plain.verbs - {"episode", "watcher"}


def test_every_project_agent_may_read_the_authorizing_members_projects() -> None:
    turn = AgentTurn(projects=True)
    for placement in (PRIVATE_PROJECT, SHARED_PROJECT):
        assert "projects" in resolve_agent_profile(placement, None, turn).verbs


@pytest.mark.parametrize("placement", PLACEMENTS)
def test_every_placement_offers_schedule(placement) -> None:
    assert "schedule" in resolve_agent_profile(placement, None, OFFER).verbs
