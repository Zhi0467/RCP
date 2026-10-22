"""What to do about a remote pass RCP stopped watching.

One table, asked of one pass, answering for every owner:

===========================================  =======================
Remote state                                 What RCP does
===========================================  =======================
Host unreachable                             wait
Provider still running                       wait
Stopped, journal reached the turn's end      finalize the original task
Stopped, journal did not                     fail the original task
Already settled                              nothing
===========================================  =======================

Losing a connection is not a task failure and never starts a second task. The
pass belongs to the operation that opened it, and whatever it produced is
finalized on that same operation, under the authority it already had.
"""

from __future__ import annotations

import json
import os
import selectors
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rcp.agents.launcher import AgentProcessControl
from rcp.limits import (
    REMOTE_JOURNAL_IDLE_TIMEOUT_SECONDS,
    TURN_JOURNAL_MAX_BYTES,
)
from rcp.runs.recorded_turn import RecordedProviderTurn, recorded_provider_turn
from rcp.transport.ssh import ssh_arguments
from rcp.transport.state import _remote_script

if TYPE_CHECKING:
    from rcp.storage import AgentTaskRecord, AppStore

ReconciliationAction = Literal["wait", "finalize", "fail", "settled"]


@dataclass(frozen=True)
class Reconciliation:
    """One answer from the table above, and the evidence it rests on."""

    action: ReconciliationAction
    reason: str
    pid_file: str | None = None
    recorded: RecordedProviderTurn | None = None


class JournalUnavailable(Exception):
    """The host could not be asked, which is never an answer about the pass."""


class JournalCorrupt(Exception):
    """The host was asked and its evidence cannot be trusted, which is an answer."""


#: What the shipped reader exits with when it rejects the journal itself, as
#: opposed to any status ssh or a broken link produces.
_JOURNAL_REJECTED = 3


def read_remote_journal(host: str, pid_file: str) -> dict[str, object] | None:
    """Read one stopped pass's journal off its host, or None if it wrote none."""

    command = [
        "python3",
        "-c",
        _remote_script("remote_turn_journal.py"),
        pid_file,
        str(TURN_JOURNAL_MAX_BYTES),
    ]
    try:
        returncode, stdout, stderr = _run_while_moving(
            ssh_arguments(host, shlex.join(command)), REMOTE_JOURNAL_IDLE_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JournalUnavailable(str(exc)) from exc
    if returncode == _JOURNAL_REJECTED:
        raise JournalCorrupt(stderr.strip() or "The provider journal is unreadable.")
    if returncode != 0:
        raise JournalUnavailable(stderr.strip() or "The provider journal could not be read.")
    try:
        value = json.loads(stdout)
    except ValueError as exc:
        raise JournalUnavailable("The provider journal was not readable JSON.") from exc
    if not isinstance(value, dict):
        raise JournalUnavailable("The provider journal was not a record.")
    return None if value.get("missing") is True else value


def _run_while_moving(argv: list[str], idle_seconds: float) -> tuple[int, bytes, str]:
    """Run one transfer to its end for as long as its output keeps arriving.

    A fixed deadline fails every journal larger than the link carries in that
    time, and a failed read only waits to be tried again the same way.
    """

    with subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        assert process.stdout is not None and process.stderr is not None
        output = {process.stdout.fileno(): bytearray(), process.stderr.fileno(): bytearray()}
        try:
            with selectors.DefaultSelector() as selector:
                for descriptor in output:
                    selector.register(descriptor, selectors.EVENT_READ)
                while selector.get_map():
                    ready = selector.select(idle_seconds)
                    if not ready:
                        raise subprocess.TimeoutExpired(argv, idle_seconds)
                    for key, _ in ready:
                        chunk = os.read(key.fd, 65536)
                        if chunk:
                            output[key.fd].extend(chunk)
                        else:
                            selector.unregister(key.fd)
            returncode = process.wait(timeout=idle_seconds)
        except BaseException:
            process.kill()
            raise
        return (
            returncode,
            bytes(output[process.stdout.fileno()]),
            output[process.stderr.fileno()].decode("utf-8", "replace"),
        )


def waiting_pass_stopped(host: str, pid_file: str) -> bool | None:
    """Whether a waiting pass's process is gone, or None if the host cannot say.

    A pass only waits once its launch has ended, so a standing stage with no
    pidfile is the host saying the pass started nothing; reading that as
    unreachable would wait for it forever.
    """

    return AgentProcessControl.remote_stopped(host, pid_file, absent_is_stopped=True)


def reconcile_remote_pass(
    store: AppStore,
    record: AgentTaskRecord,
    *,
    stopped: Callable[[str, str], bool | None] = waiting_pass_stopped,
    read_journal: Callable[[str, str], dict[str, object] | None] = read_remote_journal,
) -> Reconciliation:
    """Decide what this task's outstanding remote pass is owed."""

    host = record.stage_host or ""
    root = record.stage_root or ""
    passes = [
        pid_file
        for operation_id, pid_file in store.unresolved_remote_provider_passes(host, root)
        if operation_id == record.operation_id
    ]
    if not host or not root or not passes:
        return Reconciliation("settled", "This task has no outstanding remote pass.")
    pid_file = passes[-1]
    state = stopped(host, pid_file)
    if state is None:
        # Unreachable is not an answer, and inventing one here is what would
        # replace a provider that is quietly still working.
        return Reconciliation("wait", "The execution host could not be reached.", pid_file)
    if state is False:
        return Reconciliation("wait", "The provider is still running on its host.", pid_file)
    try:
        journal = read_journal(host, pid_file)
    except JournalCorrupt as exc:
        # The host answered. Waiting for it to answer differently is waiting
        # forever, and this turn is owed a human rather than a timer.
        return Reconciliation("fail", f"The provider journal cannot be trusted: {exc}", pid_file)
    except JournalUnavailable as exc:
        return Reconciliation("wait", f"The provider journal could not be read: {exc}", pid_file)
    if journal is None:
        return Reconciliation(
            "fail",
            "The provider stopped without writing a journal, so its turn cannot be recovered.",
            pid_file,
        )
    try:
        recorded = recorded_provider_turn(pid_file, journal)
    except ValueError as exc:
        return Reconciliation("fail", f"The provider journal could not be trusted: {exc}", pid_file)
    if not recorded.accepted:
        # The host says this pass never took the prompt. Nothing ran, so there is
        # nothing to finalize -- and, uniquely, a replacement would be safe.
        return Reconciliation(
            "fail", "The provider never took this turn's prompt.", pid_file, recorded
        )
    if not recorded.intact or not recorded.outcome.get("terminal_event"):
        return Reconciliation(
            "fail",
            "The provider stopped before its turn reached an end.",
            pid_file,
            recorded,
        )
    return Reconciliation(
        "finalize", "The provider finished this turn on its host.", pid_file, recorded
    )
