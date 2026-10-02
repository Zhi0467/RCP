from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from rcp.agents.command_protocol import (
    AskArguments,
    AskCommandRequest,
    AskResult,
    command_requires_idempotency_key,
)
from rcp.limits import (
    ASK_ANSWER_MAX_LENGTH,
    ASK_CHOICE_MAX_COUNT,
    ASK_CHOICE_MAX_LENGTH,
    ASK_QUESTION_MAX_LENGTH,
)
from rcp.runs import patch_validator
from rcp.runs.tasks import auto_research_child_work
from rcp.runs.tasks.auto_research_stream import _ValidateOnlyAutoResearchCommandDispatcher

from .test_auto_research_commands import _dispatcher, _Effects, _setup_auto_research, _worker
from .test_compute_jobs_commands import commands  # noqa: F401


def ask_request(**updates):
    return AskCommandRequest.model_validate(
        {
            "mailbox_id": "a" * 32,
            "request_id": "b" * 32,
            "credential": "c" * 64,
            "verb": "ask",
            "idempotency_key": "choice",
            "arguments": {"question": "Which path?"},
            **updates,
        }
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"question": "q" * (ASK_QUESTION_MAX_LENGTH + 1)},
        {"question": "q", "choices": [str(i) for i in range(ASK_CHOICE_MAX_COUNT + 1)]},
        {"question": "q", "choices": ["c" * (ASK_CHOICE_MAX_LENGTH + 1)]},
        {"question": "q", "multiple": True},
        {"question": " "},
        {"question": "q", "choices": [" "]},
        {"question": "q", "choices": ["a", "a"]},
    ],
)
def test_invalid_ask_arguments(arguments):
    with pytest.raises(ValidationError):
        AskArguments.model_validate(arguments)


def test_ask_accepts_caps_and_requires_a_key():
    arguments = AskArguments(
        question="q" * ASK_QUESTION_MAX_LENGTH,
        choices=[str(i).ljust(ASK_CHOICE_MAX_LENGTH, "x") for i in range(ASK_CHOICE_MAX_COUNT)],
        multiple=True,
    )
    assert ask_request(arguments=arguments).arguments == arguments
    assert command_requires_idempotency_key("ask")
    for key in (None, "", " "):
        with pytest.raises(ValidationError):
            ask_request(idempotency_key=key)
    value = ask_request().model_dump()
    del value["idempotency_key"]
    with pytest.raises(ValidationError):
        AskCommandRequest.model_validate(value)


def test_ask_result_requires_resolution_only_for_answered():
    assert AskResult(state="answered", question_id="q", answer="").choices == []
    assert (
        len(AskResult(state="answered", question_id="q", answer="a" * ASK_ANSWER_MAX_LENGTH).answer)
        == ASK_ANSWER_MAX_LENGTH
    )
    for value in (
        {"state": "answered"},
        {"state": "pending", "answer": "a"},
        {"state": "parked", "choices": ["a"]},
        {"state": "answered", "answer": "a" * (ASK_ANSWER_MAX_LENGTH + 1)},
    ):
        with pytest.raises(ValidationError):
            AskResult(question_id="q", **value)


def test_work_and_experiment_compute_handler_refuses_ask(commands):  # noqa: F811
    assert commands.handler(ask_request(), commands.identity).status == "invalid"
    assert "ask" not in commands.handler.allowed_verbs


@pytest.mark.parametrize("role", ["worker", "correction"])
def test_auto_research_workers_and_corrections_refuse_ask(tmp_path, role):
    store, episode, root = _setup_auto_research(tmp_path)
    effects = _Effects(store, episode, root)
    dispatcher = _dispatcher(store, effects.bundle())
    operation_id = root.operation_id
    if role == "worker":
        operation_id = _worker(store, episode, root, "worker").operation_id
    elif role == "correction":
        dispatcher = _ValidateOnlyAutoResearchCommandDispatcher(store, effects.bundle())
    assert dispatcher.dispatch(operation_id, ask_request()).status == "invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("child", [False, True])
async def test_validate_only_and_child_work_handlers_refuse_ask(monkeypatch, child):
    captured = []
    identity = SimpleNamespace(episode_id="episode", task_id="task")

    async def serve(**kwargs):
        captured.append(await kwargs["handler"](ask_request(), identity))

    module = auto_research_child_work if child else patch_validator
    monkeypatch.setattr(module, "serve_command_mailbox", serve)
    kwargs = dict(
        staged=SimpleNamespace(mailbox=SimpleNamespace(remote_stage=None), invocation_gate=None),
        execution=None,
        validate=lambda _: pytest.fail("ask must not validate a patch"),
        stop=asyncio.Event(),
        budget=patch_validator.PatchValidationBudget(),
    )
    if child:
        kwargs.update(
            execution=SimpleNamespace(operation_id="task"),
            route=SimpleNamespace(episode_id="episode"),
            compute_commands=SimpleNamespace(allowed_verbs=frozenset({"launch"})),
        )
        await module._serve_auto_research_child_work_mailbox(**kwargs)
    else:
        await module.serve_patch_validation_mailbox(**kwargs)
    assert [response.status for response in captured] == ["invalid"]
