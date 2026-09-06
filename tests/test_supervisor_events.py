"""The supervisor's human wizard rendering and failure breakpoints."""

from __future__ import annotations

import json
import os
import pwd
from io import StringIO
from types import SimpleNamespace

import pytest
from rcp_supervisor import cli, driver, events
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.events import EventEmitter, failure_recovery, plan
from rcp_supervisor.limits import INTERACTIVE_FIELD_LIMIT

from rcp.server_ops.models import ServerStepEvent

CONFIRMATION = f"v0.3.4:{'a' * 64}"
INVOCATION = ["server", "update", "--confirm-target", CONFIRMATION]


class Terminal(StringIO):
    def isatty(self):
        return True


@pytest.fixture
def color_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    return Terminal()


def test_terminal_keeps_one_live_line_and_expands_only_the_stop(color_terminal):
    emitter = EventEmitter("server update", stream=color_terminal, invocation=INVOCATION)
    emitter.emit("running", "Verifying the installed machine and requested operation.")
    emitter.emit(
        "failed",
        "Invalid journal; service remains closed.",
        fields=[{"name": "deployment_phase", "value": "candidate_probe"}],
    )
    text = color_terminal.getvalue()
    assert text.startswith("\x1b[1;36mRCP  server update\x1b[0m\n\n")
    # The running line is replaced in place, never appended as its own block.
    assert text.count("\r\x1b[2K") == 2
    assert text.count("RUNNING") == 1
    assert "Verifying the installed machine" not in text
    body = text.split("FAILED")[1]
    assert "  Invalid journal; service remains closed." in body
    assert f"  On: {plan('server update')['steps'][0]['target']['host']} (as " in body
    assert "  Continue when: The requested operation is verified" in body
    assert "  deployment phase: candidate_probe" in body
    resume = f"sudo /usr/local/bin/rcp server update --confirm-target {CONFIRMATION}"
    assert f"  1. $ {resume} --machine-readable\n" in body
    assert "  2. $ sudo -u rcp -H /usr/local/bin/rcp server doctor\n" in body
    assert body.rstrip().endswith(f"Continue:\n  $ {resume}")


def test_redirected_output_is_plain_bounded_status_lines(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = StringIO()
    emitter = EventEmitter("server doctor", stream=stream)
    emitter.emit("running", "Reading.")
    emitter.emit("succeeded", "Readable.", fields=[{"name": "build", "value": 8}])
    lines = stream.getvalue().splitlines()
    assert "\x1b[" not in stream.getvalue() and "\r" not in stream.getvalue()
    assert lines[0] == "RCP  server doctor"
    assert lines[2] == "RUNNING  1/1  Inspect server"
    assert lines[3] == "DONE  1/1  Inspect server"
    assert lines[4] == "  Readable."
    assert lines[-1] == "  build: 8"
    assert not any(line.startswith(("Next", "Continue")) for line in lines)


def test_fields_beyond_the_interactive_bound_point_to_the_complete_record():
    stream = StringIO()
    emitter = EventEmitter("server doctor", stream=stream)
    emitter.emit("running", "Reading.")
    fields = [
        {"name": f"field_{index}", "value": index} for index in range(INTERACTIVE_FIELD_LIMIT + 2)
    ]
    emitter.emit("succeeded", "Readable.", fields=fields)
    text = stream.getvalue()
    assert f"  field {INTERACTIVE_FIELD_LIMIT - 1}: {INTERACTIVE_FIELD_LIMIT - 1}" in text
    assert f"field_{INTERACTIVE_FIELD_LIMIT}" not in text
    assert "  … 2 more field(s); use --machine-readable for the complete record." in text


@pytest.mark.parametrize(
    "argv",
    [
        [*INVOCATION, "--machine-readable"],
        ["--machine-readable", *INVOCATION],
        ["server", "--machine-readable", "update", "--confirm-target", CONFIRMATION],
    ],
)
def test_failure_carries_exact_diagnostic_and_continue_commands(monkeypatch, capsys, argv):
    def fail(*_args, **_kwargs):
        raise SupervisorError("Invalid journal; service remains closed")

    monkeypatch.setattr(driver, "update", fail)
    monkeypatch.setattr(
        driver,
        "safe_state_fields",
        lambda: [
            {"name": "deployment_phase", "value": "candidate_probe"},
            {"name": "deployment_active", "value": False},
        ],
    )
    assert cli.main(argv) == 1
    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[0])["event"] == "plan"
    step = ServerStepEvent.model_validate_json(lines[-1]).step
    assert step.state == "failed"
    assert step.message == "Invalid journal; service remains closed"
    resume = ("sudo", "/usr/local/bin/rcp", *INVOCATION)
    assert step.resume_argv == resume
    assert [action.argv for action in step.actions] == [
        (*resume, "--machine-readable"),
        ("sudo", "-u", "rcp", "-H", "/usr/local/bin/rcp", "server", "doctor"),
    ]
    assert {field.name: field.value for field in step.fields} == {
        "deployment_phase": "candidate_probe",
        "deployment_active": False,
    }


def test_failing_root_doctor_offers_service_account_inspection():
    actions, resume = failure_recovery(["server", "doctor"])
    assert resume == ["sudo", "/usr/local/bin/rcp", "server", "doctor"]
    assert actions == [
        {"kind": "command", "argv": [*resume, "--machine-readable"]},
        {
            "kind": "command",
            "argv": ["sudo", "-u", "rcp", "-H", "/usr/local/bin/rcp", "server", "doctor"],
        },
    ]


def test_explicit_failure_recovery_is_not_overridden():
    stream = StringIO()
    emitter = EventEmitter("server install", stream=stream, invocation=["server", "install"])
    emitter.emit(
        "failed",
        "The interrupted adoption recovered the previous source installation.",
        actions=[{"kind": "external", "instruction": "Inspect the retained adoption journal."}],
        resume_argv=["sudo", "/usr/local/bin/rcp", "server", "install", "--team-name", "Lab"],
    )
    assert emitter.last_step["actions"][0]["kind"] == "external"
    assert emitter.last_step["resume_argv"][-1] == "Lab"


def test_plan_target_survives_an_unmapped_effective_uid(monkeypatch):
    def unmapped(_uid):
        raise KeyError("no passwd entry")

    monkeypatch.setattr(pwd, "getpwuid", unmapped)
    assert plan("server doctor")["steps"][0]["target"]["os_account"] == str(os.geteuid())
    assert events.os_account() == str(os.geteuid())


def test_safe_state_fields_never_raise_at_a_failure_breakpoint(tmp_path, monkeypatch):
    paths = SimpleNamespace(supervisor=tmp_path)
    monkeypatch.setattr(driver, "_read_file", lambda path, **_kwargs: path.read_bytes())
    assert driver.safe_state_fields(paths) == []
    (tmp_path / "status.json").write_text("not json")
    assert driver.safe_state_fields(paths) == []
    (tmp_path / "status.json").write_text(json.dumps({"version": 1, "active": "yes"}))
    assert driver.safe_state_fields(paths) == []
    (tmp_path / "status.json").write_text(
        json.dumps({"version": 1, "phase": "candidate_chosen", "active": True})
    )
    assert driver.safe_state_fields(paths) == [
        {"name": "deployment_phase", "value": "candidate_chosen"},
        {"name": "deployment_active", "value": True},
    ]
