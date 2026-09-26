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
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rcp.agents.launcher import AgentProcessControl
from rcp.limits import (
    REMOTE_PROVIDER_STOP_TIMEOUT_SECONDS,
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
        result = subprocess.run(
            ssh_arguments(host, shlex.join(command)),
            capture_output=True,
            text=True,
            timeout=REMOTE_PROVIDER_STOP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JournalUnavailable(str(exc)) from exc
    if result.returncode == _JOURNAL_REJECTED:
        raise JournalCorrupt(result.stderr.strip() or "The provider journal is unreadable.")
    if result.returncode != 0:
        raise JournalUnavailable(result.stderr.strip() or "The provider journal could not be read.")
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise JournalUnavailable("The provider journal was not readable JSON.") from exc
    if not isinstance(value, dict):
        raise JournalUnavailable("The provider journal was not a record.")
    return None if value.get("missing") is True else value


def reconcile_remote_pass(
    store: AppStore,
    record: AgentTaskRecord,
    *,
    stopped: Callable[[str, str], bool | None] = AgentProcessControl.remote_stopped,
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
