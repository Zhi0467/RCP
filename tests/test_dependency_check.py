from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from rcp import dependency_check
from rcp.core.models import DependencyStatus, MissingProgram
from rcp.dependencies import BY_NAME
from rcp.dependency_check import DependencyChecker, parse_output, script_check, staged_check_script

NONCE = "n0nce"


def _answer(*body: str, nonce: str = NONCE) -> str:
    return "\n".join(
        [f"rcp-dependency-check begin {nonce}", *body, f"rcp-dependency-check end {nonce}", ""]
    )


# Layer 1: the shipped script under the machine's real `sh`.


def test_real_script_reports_only_programs_it_cannot_start(tmp_path: Path) -> None:
    for name, body in (("uname", "echo Linux"), ("rsync", "")):
        program = tmp_path / name
        program.write_text(f"#!/bin/sh\n{body}\n")
        program.chmod(0o755)
    sh = shutil.which("sh")
    assert sh
    result = subprocess.run(
        [sh, "-s", "--", NONCE, "rsync", "git", str(tmp_path / "rsync"), "/nonexistent/bash"],
        input=staged_check_script(),
        capture_output=True,
        text=True,
        env={"PATH": str(tmp_path)},
        check=True,
    )
    lines = result.stdout.splitlines()
    assert lines[0] == f"rcp-dependency-check begin {NONCE}"
    assert lines[-1] == f"rcp-dependency-check end {NONCE}"
    assert "os Linux" in lines
    assert [line for line in lines if line.startswith("missing ")] == [
        "missing git",
        "missing /nonexistent/bash",
    ]


# Layer 2: the parser over recorded answers.


@pytest.mark.parametrize(
    ("stdout", "outcome"),
    [
        ("Welcome to the cluster!\n" + _answer("os Linux", "id ubuntu"), "ready"),
        (_answer("os Linux", "id ubuntu").rsplit("rcp-dependency-check end", 1)[0], "not_checked"),
        ("setenv: Command not found.\n", "not_checked"),
        (_answer("os Linux", "missing rsync", nonce="other"), "not_checked"),
        (_answer("os Darwin"), "unsupported"),
    ],
)
def test_only_a_complete_linux_frame_gives_a_definite_answer(stdout: str, outcome: str) -> None:
    assert parse_output(stdout, NONCE, ["remote"]).outcome == outcome


def test_untested_distribution_is_allowed_with_a_mark() -> None:
    status = parse_output(
        _answer("os Linux", "id rocky", "id_like rhel centos fedora"), NONCE, ["remote"]
    )
    assert (status.outcome, status.distribution, status.tested) == ("ready", "rocky", False)


@pytest.mark.parametrize(
    ("release", "apt"),
    [
        (["id ubuntu"], True),
        (["id linuxmint", "id_like ubuntu debian"], True),
        (["id rocky"], False),
    ],
)
def test_missing_required_program_carries_install_guidance(release: list[str], apt: bool) -> None:
    status = parse_output(
        _answer("os Linux", *release, "missing rsync", "missing python3"), NONCE, ["remote"]
    )
    assert status.outcome == "missing"
    assert {p.name for p in status.missing} == {"rsync", "python3"}
    if apt:
        assert status.install_command is not None
        assert status.install_command.split()[-2:] == ["python3", "rsync"]
        assert status.install_notes == ()
    else:
        assert status.install_command is None
        # One step per package, each naming it.
        assert len(status.install_notes) == 2
        assert all(any(p in n for n in status.install_notes) for p in ("python3", "rsync"))


def test_missing_optional_program_alone_is_ready_and_listed() -> None:
    status = parse_output(_answer("os Linux", "id ubuntu", "missing bwrap"), NONCE, ["remote"])
    assert status.outcome == "ready"
    (program,) = status.missing
    assert (program.name, program.required) == ("bwrap", False)
    assert (program.feature, program.fallback) == (
        BY_NAME["bwrap"].feature,
        BY_NAME["bwrap"].fallback,
    )


def test_login_shell_answer_maps_to_its_program() -> None:
    status = parse_output(
        _answer("os Linux", "id ubuntu", "missing login:setsid"), NONCE, ["remote"]
    )
    assert [p.name for p in status.missing] == ["setsid"]


def test_real_script_looks_up_login_names_in_the_login_shell() -> None:
    result = subprocess.run(
        ["sh", "-s", "--", NONCE, "login:sh", "login:rcp-no-such-program"],
        input=staged_check_script(),
        capture_output=True,
        text=True,
        check=True,
    )
    missing = [line for line in result.stdout.splitlines() if line.startswith("missing ")]
    assert missing == ["missing login:rcp-no-such-program"]


def test_undecodable_banner_bytes_do_not_break_the_check() -> None:
    def with_banner(argv: list[str]) -> list[str]:
        return ["sh", "-c", 'printf "\\377\\376 motd\\n"; exec "$@"', "banner", *argv]

    # The frame is still read: Linux gives ready or missing, macOS unsupported.
    assert script_check(with_banner, ["remote"]).outcome != "not_checked"


# Layer 3: the checker's retry, single flight, cache, and recheck.


class FakeRunner:
    """Answers each call from a queue of results; a nonce is copied from argv."""

    def __init__(self, *answers: tuple[int, tuple[str, ...]]) -> None:
        self.answers = list(answers)
        self.calls = 0
        self.gate: threading.Event | None = None

    def __call__(
        self, argv: list[str], script: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        code, body = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        nonce = argv[-1].split()[3]
        return subprocess.CompletedProcess(argv, code, _answer(*body, nonce=nonce), "")


READY = (0, ("os Linux", "id ubuntu"))
MISSING = (0, ("os Linux", "id ubuntu", "missing rsync"))
DROPPED = (255, ())


@pytest.fixture(autouse=True)
def _no_ssh_control_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dependency_check, "ssh_arguments", lambda host, command: ["ssh", host, command]
    )


def _local(outcome: str = "ready") -> DependencyStatus:
    missing = (MissingProgram(name="ssh", purpose="x", required=True),) * (outcome == "missing")
    return DependencyStatus(outcome=outcome, missing=missing, checked_at="t")  # type: ignore[arg-type]


def _checker(
    runner: FakeRunner, now: list[float] | None = None, local: str = "ready"
) -> DependencyChecker:
    clock = now or [0.0]
    return DependencyChecker(
        runner=runner, clock=lambda: clock[0], sleep=lambda _s: None, local=lambda _r: _local(local)
    )


def test_dropped_connection_is_retried_to_a_definite_answer() -> None:
    runner = FakeRunner(DROPPED, READY)
    assert _checker(runner).status("gpu").outcome == "ready"
    assert runner.calls == 2


def test_exhausted_attempts_are_not_checked_and_admit() -> None:
    runner = FakeRunner(DROPPED)
    checker = _checker(runner)
    status = checker.status("gpu")
    assert status.outcome == "not_checked" and status.reason
    assert checker.launch_refusal("gpu") is None
    # Reused briefly, so the refusal check above did not wait out the retries again.
    assert runner.calls == 3


def test_timeouts_count_as_failed_attempts() -> None:
    def timing_out(
        argv: list[str], script: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, timeout)

    calls: list[float] = []
    status = script_check(lambda argv: argv, ["remote"], runner=timing_out, sleep=calls.append)
    assert status.outcome == "not_checked"
    assert calls  # retried with backoff


def test_concurrent_callers_for_one_host_share_one_check() -> None:
    runner = FakeRunner(READY)
    runner.gate = threading.Event()
    checker = _checker(runner)
    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(checker.status("gpu").outcome))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    runner.gate.set()
    for thread in threads:
        thread.join(5)
    assert results == ["ready", "ready"]
    assert runner.calls == 1


def test_one_slow_host_does_not_block_another() -> None:
    ready = FakeRunner(READY)
    release = threading.Event()

    def runner(argv: list[str], script: str, timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[1] == "slow":
            release.wait(5)
        return ready(argv, script, timeout)

    checker = DependencyChecker(runner=runner, sleep=lambda _s: None)
    slow = threading.Thread(target=checker.status, args=("slow",))
    slow.start()
    assert checker.status("fast").outcome == "ready"
    assert slow.is_alive()
    release.set()
    slow.join(5)


def test_cached_ready_answers_within_its_lifetime() -> None:
    runner = FakeRunner(READY)
    now = [0.0]
    checker = _checker(runner, now)
    checker.status("gpu")
    now[0] = dependency_check.DEPENDENCY_CHECK_TTL_SECONDS / 2
    checker.status("gpu")
    assert runner.calls == 1
    now[0] = dependency_check.DEPENDENCY_CHECK_TTL_SECONDS * 2
    checker.status("gpu")
    assert runner.calls == 2


def test_remote_run_is_refused_when_the_local_machine_lacks_a_program() -> None:
    runner = FakeRunner(READY)
    assert _checker(runner, local="missing").launch_refusal("gpu") is not None
    assert runner.calls == 0


def test_cold_refusal_checks_once() -> None:
    runner = FakeRunner(MISSING)
    assert _checker(runner).launch_refusal("gpu") is not None
    assert runner.calls == 1


def test_cached_missing_is_rechecked_before_refusing() -> None:
    runner = FakeRunner(MISSING, MISSING, READY)
    now = [0.0]
    checker = _checker(runner, now)
    assert checker.status("gpu").outcome == "missing"
    now[0] = 1.0
    assert checker.launch_refusal("gpu") is not None
    now[0] = 2.0
    assert checker.launch_refusal("gpu") is None
    assert runner.calls == 3
