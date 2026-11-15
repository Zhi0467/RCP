"""One pass of provider session retention: mark eligible sessions, then delete them.

The store decides which recorded sessions are eligible and marks them in one
transaction; a marked session never resumes. Deletion runs afterwards on the
session's execution machine, in-process for the backend's own and over one SSH
call elsewhere. Each result clears the mark's work or keeps it for the next pass.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.limits import PROVIDER_SESSION_DELETE_TIMEOUT_SECONDS, PROVIDER_SESSION_SWEEP_BATCH
from rcp.machine_cards import card
from rcp.providers import PROVIDERS
from rcp.providers.claude.remote import claude_project_directory
from rcp.transport.provider_discovery import discover_provider
from rcp.transport.provider_session_delete import SESSION_ID_SHAPES, delete_sessions
from rcp.transport.ssh import ssh_arguments
from rcp.transport.state import _remote_script

if TYPE_CHECKING:
    from rcp.storage import AppStore
    from rcp.storage.provider_session_sweeps import ProviderSessionSweepTarget

logger = logging.getLogger(__name__)


def sweep_provider_sessions(store: AppStore) -> None:
    """Run one pass over every machine with sessions to delete; log one line each."""

    now = store.now()
    by_machine: dict[str, list[ProviderSessionSweepTarget]] = defaultdict(list)
    kept: dict[str, Counter[str]] = defaultdict(Counter)
    for target in store.provider_session_sweep_targets(now):
        if target.marked or _deletable(target):
            by_machine[target.machine_id].append(target)
        else:
            kept[target.machine_id][target.provider] += 1
    for machine_id in sorted(by_machine.keys() | kept.keys()):
        targets = by_machine[machine_id]
        pending = [target for target in targets if target.marked]
        fresh = [target for target in targets if not target.marked]
        room = max(PROVIDER_SESSION_SWEEP_BATCH - len(pending), 0)
        batch = pending[:PROVIDER_SESSION_SWEEP_BATCH] + store.mark_provider_sessions_swept(
            fresh[:room], now
        )
        try:
            errors = _delete_on_machine(store, machine_id, batch)
        except Exception as exc:
            logger.warning(
                "Provider session sweep on %s kept %d marked sessions: %s",
                machine_id,
                len(batch),
                exc,
            )
            continue
        results = list(zip(batch, errors, strict=True))
        store.record_provider_session_sweep_results(results, now)
        deleted = Counter(target.provider for target, error in results if error is None)
        failures = [
            f"{target.provider} {target.session_id}: {error}"
            for target, error in results
            if error is not None
        ]
        logger.info(
            "Provider session sweep on %s: deleted %s; failed %d%s; kept %s",
            machine_id,
            _counts(deleted),
            len(failures),
            f" ({'; '.join(failures[:5])})" if failures else "",
            _counts(kept[machine_id]),
        )


def _deletable(target: ProviderSessionSweepTarget) -> bool:
    shape = SESSION_ID_SHAPES.get(target.provider)
    if shape is None or shape.fullmatch(target.session_id) is None:
        return False
    return target.provider != "claude" or target.cwd is not None


def _counts(counter: Counter[str]) -> str:
    return ", ".join(f"{provider} {count}" for provider, count in sorted(counter.items())) or "none"


def _delete_on_machine(
    store: AppStore, machine_id: str, targets: list[ProviderSessionSweepTarget]
) -> list[str | None]:
    if not targets:
        return []
    machine = card(store, machine_id)
    items = [
        {
            "provider": target.provider,
            "session_id": target.session_id,
            "binary": machine.provider_paths.get(target.provider),
            "install_paths": list(PROVIDERS[target.provider].install_paths)
            if target.provider in PROVIDERS
            else [],
            "claude_project": _claude_project(target, local=not machine.host),
        }
        for target in targets
    ]
    timeout = PROVIDER_SESSION_DELETE_TIMEOUT_SECONDS
    if not machine.host:
        return delete_sessions(items, timeout=timeout, discover=discover_provider)
    command = shlex.join(
        [
            "python3",
            "-c",
            _remote_script("provider_session_delete.py"),
            _remote_script("provider_discovery.py"),
        ]
    )
    result = subprocess.run(
        ssh_arguments(machine.host, command),
        input=json.dumps({"items": items, "timeout": timeout}),
        capture_output=True,
        text=True,
        timeout=timeout * 2 * len(items) + 60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(" ".join(result.stderr.split())[:500] or f"ssh exit {result.returncode}")
    errors = json.loads(result.stdout)
    if not isinstance(errors, list) or len(errors) != len(targets):
        raise RuntimeError("the host returned an unexpected deletion result")
    return errors


def _claude_project(target: ProviderSessionSweepTarget, *, local: bool) -> str | None:
    if target.provider != "claude" or target.cwd is None:
        return None
    cwd = Path(target.cwd)
    return claude_project_directory(cwd.resolve() if local else cwd)
