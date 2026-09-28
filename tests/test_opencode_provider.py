import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from rcp.agents.write_scope import ProjectWriteScope
from rcp.providers import ProviderTurnRequest, profile_for
from rcp.providers.turn_fence import turn_fence

OPENCODE = profile_for("opencode")


def _scope() -> ProjectWriteScope:
    return ProjectWriteScope.create(
        project_id="project",
        execution_machine="local",
        execution_host="",
        capability="work_auto",
        stage_root="/home/rcp/data/stages/s",
        workspace_root="/home/rcp/data/stages/s/workspace",
        repositories=[],
        granted_roots=["/home/rcp", "/tmp"],
        protected_write_paths=["/home/rcp/.rcp", "/home/rcp/data"],
        granted_protected_paths=["/home/rcp/.rcp", "/home/rcp/data"],
    )


def _request(capability, *, scope=None, write_dirs=()) -> ProviderTurnRequest:
    return ProviderTurnRequest(
        prompt="Do the work.",
        binary="opencode",
        cwd=Path("/home/rcp/data/stages/s/workspace"),
        model="opencode/big-pickle",
        reasoning=None,
        session_id=None,
        read_dirs=[],
        write_dirs=list(write_dirs),
        write_scope=scope,
        capability=capability,
        provider_version="1.18.30",
    )


def _launch(request: ProviderTurnRequest) -> tuple[list[str], dict[str, object]]:
    turn = OPENCODE.runtime(OPENCODE.legacy_runtime_id).turn(request)
    policy = json.loads(turn.environment["OPENCODE_CONFIG_CONTENT"])
    assert turn.environment["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    (agent, definition), *others = policy["agent"].items()
    assert not others
    assert turn.command[turn.command.index("--agent") + 1] == agent
    return turn.command, definition["permission"]


def test_work_grants_exact_roots_and_denies_protected_storage_after_them():
    scope = _scope()
    command, permission = _launch(
        _request("work_auto", scope=scope, write_dirs=[Path(p) for p in scope.repository_roots])
    )
    assert command[:2] == ["sh", "-c"]
    assert "--pure" in command
    # OpenCode applies the last matching rule: every grant, then protected
    # storage, then the stage that sits inside it.
    assert list(permission["edit"].items()) == [
        ("*", "deny"),
        ("home/rcp/**", "allow"),
        ("tmp/**", "allow"),
        ("home/rcp/.rcp/**", "deny"),
        ("home/rcp/data/**", "deny"),
        ("home/rcp/data/stages/s/workspace/**", "allow"),
    ]


def test_paper_coach_denies_every_edit_so_no_path_base_is_needed():
    command, permission = _launch(_request("paper_readonly"))
    assert "rev-parse" not in command[2]
    assert permission["edit"] == "deny"
    assert permission["bash"] == "deny"
    assert permission["external_directory"] == "allow"


def test_discuss_edits_only_its_own_folders_and_has_no_shell():
    command, permission = _launch(_request("discuss", write_dirs=[Path("/work/notes")]))
    assert "--pure" in command
    assert permission["bash"] == "deny"
    assert permission["edit"] == {
        "*": "deny",
        "home/rcp/data/stages/s/workspace/**": "allow",
        "work/notes/**": "allow",
    }


@pytest.mark.parametrize("capability", ["discuss", "paper_readonly", "work_auto"])
def test_every_capability_refuses_a_cli_older_than_the_probed_rules(capability):
    scope = _scope() if capability == "work_auto" else None
    request = replace(_request(capability, scope=scope), provider_version="1.18.29")
    with pytest.raises(ValueError):
        OPENCODE.runtime(OPENCODE.legacy_runtime_id).turn(request)
    with pytest.raises(ValueError):
        OPENCODE.validate_readiness_version("1.18.29", capability=capability)


def _lines(*values: dict) -> list[str]:
    return [json.dumps({"sessionID": "ses_1", **value}) for value in values]


def test_the_reply_is_every_text_part_and_the_exit_report_ends_the_turn():
    turn = OPENCODE.runtime(OPENCODE.legacy_runtime_id).turn(_request("discuss"))
    tokens = {"input": 10, "output": 2, "reasoning": 1, "cache": {"read": 5, "write": 0}}
    # Probed: a step can finish with `stop` and still be followed by another.
    steps = [
        turn.receive_line(line)
        for line in _lines(
            {"type": "step_start", "part": {"type": "step-start"}},
            {"type": "text", "part": {"type": "text", "text": "I'll create it."}},
            {"type": "tool_use", "part": {"tool": "bash", "state": {"status": "completed"}}},
            {"type": "step_finish", "part": {"id": "p1", "reason": "stop", "tokens": tokens}},
            {"type": "step_start", "part": {"type": "step-start"}},
            {"type": "text", "part": {"type": "text", "text": "Created it."}},
            {"type": "step_finish", "part": {"id": "p2", "reason": "stop", "tokens": tokens}},
        )
    ]
    steps.append(turn.receive_line('{"type":"rcp.provider_exit","code":0}'))
    events = [event for step in steps for event in step.events]
    assert [event.session_id for event in events if event.event == "session"] == ["ses_1"]
    assert [event.text for event in events if event.event == "answer"] == [
        "I'll create it.",
        "Created it.",
    ]
    usage = [event.usage for event in events if event.usage is not None]
    assert [item.dedupe_key for item in usage] == ["p1", "p2"]
    assert usage[0].processed_input_tokens == 15
    assert [step.complete for step in steps] == [False] * 7 + [True]


def test_an_error_event_ends_the_turn_as_a_failure():
    turn = OPENCODE.runtime(OPENCODE.legacy_runtime_id).turn(_request("discuss"))
    (line,) = _lines({"type": "error", "error": {"name": "UnknownError", "data": {"message": "x"}}})
    step = turn.receive_line(line)
    assert step.complete and step.explicit_terminal
    assert [event.event for event in step.events] == ["session", "error"]


def test_the_host_fence_ends_only_at_the_exit_report():
    fence = turn_fence(OPENCODE.legacy_runtime_id)
    fence.output({"type": "step_finish", "part": {"reason": "stop"}})
    fence.output({"type": "error", "error": {"name": "UnknownError"}})
    assert not fence.terminal
    fence.output({"type": "rcp.provider_exit", "code": 1})
    assert fence.terminal


def test_catalog_and_skill_probes_read_what_the_cli_prints():
    catalog = (
        'opencode/big-pickle\n{\n  "name": "Big Pickle",\n  "status": "active",\n'
        '  "variants": {}\n}\n'
        'opencode/space-bunny-free\n{\n  "name": "Space Bunny",\n  "status": "active",\n'
        '  "variants": {"low": {}, "high": {}}\n}\n'
    )
    models = OPENCODE.parse_catalog(catalog)
    assert [(model.id, model.reasoning) for model in models] == [
        ("opencode/big-pickle", []),
        ("opencode/space-bunny-free", ["low", "high"]),
    ]
    skills = OPENCODE.parse_skills(
        json.dumps(
            [
                {"name": "customize-opencode", "description": "d", "location": "<built-in>"},
                {"name": "grill-me", "description": "d", "location": "/s/grill-me/SKILL.md"},
            ]
        )
    )
    assert [(skill.name, skill.scope, skill.path) for skill in skills] == [
        ("customize-opencode", "built-in", None),
        ("grill-me", None, "/s/grill-me/SKILL.md"),
    ]


def test_the_wrapper_reports_the_exit_and_refuses_a_git_work_tree(tmp_path: Path):
    fake = tmp_path / "opencode"
    fake.write_text("#!/bin/sh\necho started\nexit 3\n")
    fake.chmod(0o755)
    command = OPENCODE.runtime(OPENCODE.legacy_runtime_id).turn(_request("discuss")).command
    command = [str(fake) if part == "opencode" else part for part in command]
    outside = tmp_path / "outside"
    inside = tmp_path / "repo"
    outside.mkdir()
    inside.mkdir()
    subprocess.run(["git", "init", "-q", str(inside)], check=True)

    ran = subprocess.run(command, cwd=outside, capture_output=True, text=True)
    refused = subprocess.run(command, cwd=inside, capture_output=True, text=True)

    assert ran.returncode == 3
    assert ran.stdout.splitlines() == ["started", '{"type":"rcp.provider_exit","code":3}']
    assert refused.returncode == 64 and refused.stdout == ""
