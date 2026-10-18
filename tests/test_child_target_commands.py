from __future__ import annotations

import pytest

from rcp.agents import staged_command_client as client
from rcp.agents.command_protocol import (
    ExperimentKickoffArguments,
    SpawnArguments,
)


@pytest.mark.parametrize(
    ("model", "old_record"),
    [
        (SpawnArguments, {"seat_node_id": "exp-1", "instruction_file": "task.md"}),
        (
            ExperimentKickoffArguments,
            {
                "action": "kick_off_experiment",
                "node_id": "exp-1",
                "goal_file": None,
                "invocation_limit": None,
            },
        ),
    ],
)
def test_old_child_command_records_replay_unchanged(model, old_record):
    assert model.model_validate(old_record).model_dump(mode="json") == old_record


@pytest.mark.parametrize("seat", [{}, {"project": True, "seat_node_id": "exp-1"}])
def test_spawn_requires_exactly_one_seat(seat):
    with pytest.raises(ValueError):
        SpawnArguments.model_validate({"instruction_file": "task.md", **seat})


@pytest.mark.parametrize(
    "model,base",
    [
        (SpawnArguments, {"project": True, "instruction_file": "task.md"}),
        (ExperimentKickoffArguments, {"action": "kick_off_experiment", "node_id": "exp-1"}),
    ],
)
@pytest.mark.parametrize(
    "target",
    [
        {"at": "main@3", "target": "other"},
        {"seed_file": "seed.json"},
        {"at": "main@head"},
        {"at": "@3"},
        {"target": " "},
        {"at": "branch:root@3", "seed_file": "nested/seed.json"},
    ],
)
def test_child_target_protocol_refuses_invalid_selection(model, base, target):
    with pytest.raises(ValueError):
        model.model_validate({**base, **target})


@pytest.mark.parametrize(
    "verb,seat",
    [
        ("spawn", ["--project", "--instruction-file", "task.md"]),
        ("spawn", ["--seat-node", "exp-1", "--instruction-file", "task.md"]),
        ("episode", ["--kick-off-experiment", "--node", "exp-1"]),
    ],
)
@pytest.mark.parametrize(
    "flags,expected",
    [
        (["--target", "owned"], {"target": "owned"}),
        (
            ["--at", "branch:root@3", "--seed-file", "seed.json"],
            {"at": "branch:root@3", "seed_file": "seed.json"},
        ),
    ],
)
def test_child_target_client_transmits_resolved_options(
    tmp_path, monkeypatch, verb, seat, flags, expected
):
    monkeypatch.chdir(tmp_path)
    parsed = client._parser().parse_args(
        [
            "--credential",
            "credential.json",
            "--timeout",
            "1",
            "--workspace",
            str(tmp_path),
            verb,
            "--key",
            "once",
            *seat,
            *flags,
        ]
    )
    actual_verb, key, arguments = client._request_arguments(parsed, str(tmp_path))
    model = SpawnArguments if verb == "spawn" else ExperimentKickoffArguments
    request = model.model_validate(arguments)
    assert (actual_verb, key) == (verb, "once")
    assert {name: getattr(request, name) for name in expected} == expected
    if isinstance(request, SpawnArguments):
        assert request.project == ("--project" in seat)


@pytest.mark.parametrize(
    "arguments",
    [
        ["spawn", "--project", "--seat-node", "exp-1", "--instruction-file", "task.md"],
        ["spawn", "--instruction-file", "task.md"],
        ["spawn", "--project", "--instruction-file", "task.md", "--seed-file", "seed.json"],
        [
            "spawn",
            "--project",
            "--instruction-file",
            "task.md",
            "--at",
            "main@3",
            "--target",
            "owned",
        ],
        ["episode", "--stop", "child", "--at", "main@3"],
        ["episode", "--resume", "child", "--target", "owned"],
        ["episode", "--stop", "child", "--seed-file", "seed.json"],
    ],
)
def test_child_options_refused_before_mailbox(tmp_path, monkeypatch, arguments):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(client.ClientInputError):
        parsed = client._parser().parse_args(
            [
                "--credential",
                "credential.json",
                "--timeout",
                "1",
                "--workspace",
                str(tmp_path),
                *arguments,
                "--key",
                "once",
            ]
        )
        client._request_arguments(parsed, str(tmp_path))
