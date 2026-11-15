"""One pass of provider session retention: mark eligible sessions, then delete them.

The store decides which recorded sessions are eligible and marks them in one
transaction; a marked session never resumes. Deletion runs afterwards on the
session's execution machine, in-process for the backend's own and over SSH in
the account's login shell elsewhere, in chunks a stop request can interrupt.
Each result clears the mark's work or keeps it for a later pass, until repeated
failures abandon it.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from collections import Counter, defaultdict
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING

from rcp.limits import (
    PROVIDER_SESSION_DELETE_TIMEOUT_SECONDS,
    PROVIDER_SESSION_SWEEP_BATCH,
    PROVIDER_SESSION_SWEEP_CHUNK,
    PROVIDER_SESSION_SWEEP_RETRY_BATCH,
    provider_session_ssh_timeout,
)
from rcp.machine_cards import card
from rcp.providers import PROVIDERS, remote_bundle
from rcp.transport.provider_discovery import discover_provider
from rcp.transport.provider_session_delete import SESSION_ID_SHAPES, delete_sessions
from rcp.transport.ssh import ssh_arguments
from rcp.transport.state import _remote_script

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from rcp.storage import AppStore
    from rcp.storage.models import SpaceMachineRecord
    from rcp.storage.provider_session_sweeps import ProviderSessionSweepTarget

    Guard = Callable[[], AbstractContextManager[object]]

logger = logging.getLogger(__name__)


def sweep_provider_sessions(
    store: AppStore, *, stop: threading.Event, guard: Guard = nullcontext
) -> None:
    """Run one pass over every machine with sessions to delete; log one line each.

    `guard` wraps each store write, and `stop` ends the pass between chunks.
    """

    now = store.now()
    pending: dict[str, list[ProviderSessionSweepTarget]] = defaultdict(list)
    fresh: dict[str, list[ProviderSessionSweepTarget]] = defaultdict(list)
    kept: dict[str, Counter[str]] = defaultdict(Counter)
    for target in store.provider_session_sweep_targets(now):
        if target.marked:
            pending[target.machine_id].append(target)
        elif _deletable(target):
            fresh[target.machine_id].append(target)
        else:
            kept[target.machine_id][target.provider] += 1
    for machine_id in sorted(pending.keys() | fresh.keys() | kept.keys()):
        if stop.is_set():
            return
        try:
            machine = card(store, machine_id)
        except KeyError:
            with guard():
                gone = store.abandon_provider_session_marks(machine_id, "machine removed", now)
            if gone:
                logger.warning(
                    "Provider session sweep abandoned %d marks on removed machine %s",
                    len(gone),
                    machine_id,
                )
            continue
        with guard():
            batch = pending[machine_id][:PROVIDER_SESSION_SWEEP_RETRY_BATCH] + (
                store.mark_provider_sessions_swept(
                    fresh[machine_id][:PROVIDER_SESSION_SWEEP_BATCH], now
                )
            )
        _sweep_machine(store, machine, batch, kept[machine_id], stop=stop, guard=guard, now=now)


def _sweep_machine(
    store: AppStore,
    machine: SpaceMachineRecord,
    batch: list[ProviderSessionSweepTarget],
    kept: Counter[str],
    *,
    stop: threading.Event,
    guard: Guard,
    now: str,
) -> None:
    states: dict[str, Counter[str]] = defaultdict(Counter)
    failures: list[str] = []
    abandoned: list[ProviderSessionSweepTarget] = []
    unreachable = False
    for start in range(0, len(batch), PROVIDER_SESSION_SWEEP_CHUNK):
        if stop.is_set() or unreachable:
            break
        chunk = batch[start : start + PROVIDER_SESSION_SWEEP_CHUNK]
        try:
            outcomes = _delete_on_machine(machine, chunk)
        except Exception as exc:
            reason = " ".join(str(exc).split())[:500] or type(exc).__name__
            outcomes = [("failed", reason)] * len(chunk)
            unreachable = True
        results = [
            (target, error if state == "failed" else None)
            for target, (state, error) in zip(chunk, outcomes, strict=True)
        ]
        with guard():
            abandoned += store.record_provider_session_sweep_results(results, now)
        for target, (state, error) in zip(chunk, outcomes, strict=True):
            states[state][target.provider] += 1
            if error is not None:
                failures.append(f"{target.provider} {target.session_id}: {error}")
    if not batch and not kept:
        return
    logger.info(
        "Provider session sweep on %s: deleted %s; absent %s; failed %d%s; kept %s",
        machine.machine_id,
        _counts(states["deleted"]),
        _counts(states["absent"]),
        len(failures),
        f" ({'; '.join(failures[:5])})" if failures else "",
        _counts(kept),
    )
    if abandoned:
        logger.warning(
            "Provider session sweep on %s abandoned %d sessions after repeated failures: %s",
            machine.machine_id,
            len(abandoned),
            ", ".join(f"{target.provider} {target.session_id}" for target in abandoned[:5]),
        )


def _deletable(target: ProviderSessionSweepTarget) -> bool:
    shape = SESSION_ID_SHAPES.get(target.provider)
    if shape is None or shape.fullmatch(target.session_id) is None:
        return False
    return target.provider != "claude" or target.cwd is not None


def _counts(counter: Counter[str]) -> str:
    return ", ".join(f"{provider} {count}" for provider, count in sorted(counter.items())) or "none"


def _delete_on_machine(
    machine: SpaceMachineRecord, targets: list[ProviderSessionSweepTarget]
) -> list[tuple[str, str | None]]:
    items = [
        {
            "provider": target.provider,
            "session_id": target.session_id,
            "binary": machine.provider_paths.get(target.provider),
            "install_paths": list(PROVIDERS[target.provider].install_paths)
            if target.provider in PROVIDERS
            else [],
            "claude_cwd": target.cwd if target.provider == "claude" else None,
        }
        for target in targets
    ]
    timeout = PROVIDER_SESSION_DELETE_TIMEOUT_SECONDS
    if not machine.host:
        return delete_sessions(items, timeout=timeout, discover=discover_provider)
    command = shlex.join(
        ["python3", "-c", _remote_program(), _remote_script("provider_discovery.py")]
    )
    result = subprocess.run(
        ssh_arguments(machine.host, shlex.join(["bash", "-lic", command])),
        input=json.dumps({"items": items, "timeout": timeout}),
        capture_output=True,
        text=True,
        timeout=provider_session_ssh_timeout(len(items)),
        check=False,
    )
    if result.returncode:
        raise RuntimeError(" ".join(result.stderr.split())[:500] or f"ssh exit {result.returncode}")
    lines = result.stdout.strip().splitlines()
    outcomes = json.loads(lines[-1]) if lines else None
    if not isinstance(outcomes, list) or len(outcomes) != len(targets):
        raise RuntimeError("the host returned an unexpected deletion result")
    return [(str(state), error) for state, error in outcomes]


def _remote_program() -> str:
    source = _remote_script("provider_session_delete.py")
    return remote_bundle(f"exec(compile({source!r}, 'provider_session_delete.py', 'exec'))")
