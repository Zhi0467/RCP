from __future__ import annotations

import pytest

from rcp.agents import staged_command_client
from rcp.agents.command_protocol import FlagArguments, FlagCommandRequest
from rcp.core.models import Patch
from rcp.runs.evidence_flag_commands import hold_consolidation_additions
from rcp.runs.questions import work_command_handler
from rcp.transport import StateUnavailable

from .helpers import append_fixture_patch, create_named_app, seed_patch
from .test_consolidation_work import _admit
from .test_work_questions import identity, work_execution


def request(*, key="once", evidence_id="ev/result", finding="Claim exceeds the source"):
    return FlagCommandRequest(
        mailbox_id="a" * 32,
        request_id="b" * 32,
        credential="c" * 64,
        idempotency_key=key,
        verb="flag",
        arguments=FlagArguments(evidence_id=evidence_id, kind="overclaim", finding=finding),
    )


def test_flag_mailbox_authority_and_keyed_receipts(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "service")
    patch = seed_patch().model_dump(mode="json")
    patch["ops"][0]["nodes"].append(
        {
            "id": "ev/result",
            "type": "evidence",
            "title": "Result",
            "observation": "One run",
            "origin": "analytic",
        }
    )
    append_fixture_patch(app.state.service, Patch.model_validate(patch))
    execution, _ = work_execution(tmp_path)
    ordinary = work_command_handler(execution, None, service=app.state.service)
    assert "flag" not in ordinary.allowed_verbs
    assert ordinary(request(), identity()).status == "invalid"
    _, execution = _admit(app)
    caller = identity(task_id=execution.operation_id)
    handler = work_command_handler(execution, None, service=app.state.service)
    assert "flag" in handler.allowed_verbs
    assert handler(request(), identity("validate_only")).status == "invalid"
    assert handler(request(), identity(task_id="other")).status == "invalid"
    first = handler(request(), caller)
    assert first.status == "ok"
    assert first.result["flag"]["evidence_revision"] == app.state.service.history.state().revision
    assert handler(request(), caller).result == first.result
    assert handler(request(key="rcp:mine"), caller).result["code"] == "flag_key_invalid"
    assert handler(request(finding="Different"), caller).result["code"] == "flag_key_conflict"
    duplicate = handler(request(key="second"), caller)
    assert duplicate.result == {**first.result, "code": "flag_exists"}
    for evidence_id in ("missing", "rq/learning-after-shift"):
        refused = handler(request(key=evidence_id, evidence_id=evidence_id), caller)
        assert refused.status == "invalid"
        assert refused.result["code"] == "flag_evidence_invalid"


def test_flag_client_preserves_arguments(tmp_path):
    parsed = staged_command_client._parser().parse_args(
        [
            "--broker",
            "broker",
            "--timeout",
            "1",
            "--workspace",
            str(tmp_path),
            "flag",
            "--key",
            "once",
            "--evidence",
            "ev/result",
            "--kind",
            "underclaim",
            "--finding",
            "Source supports more",
            "--source",
            "repo-a/results.txt:4",
        ]
    )
    verb, key, arguments = staged_command_client._request_arguments(parsed, str(tmp_path))
    assert (verb, key) == ("flag", "once")
    assert FlagArguments(**arguments).model_dump() == {
        "evidence_id": "ev/result",
        "kind": "underclaim",
        "finding": "Source supports more",
        "source": "repo-a/results.txt:4",
    }


@pytest.mark.parametrize("finding", [" ", "a\nb", "a\n", "a" * 401])
def test_flag_rejects_invalid_findings(finding):
    with pytest.raises(ValueError):
        request(finding=finding)


def test_consolidation_hold_read_failure_is_a_task_warning(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    append_fixture_patch(app.state.service, seed_patch())
    _, execution = _admit(app)

    class History:
        def state(self):
            raise StateUnavailable("state host dropped")

    class Unreachable:
        history = History()

    hold_consolidation_additions(Unreachable(), execution.store, execution.operation_id)
    assert execution.store.agent_task_events(execution.operation_id)[-1].level == "warning"
