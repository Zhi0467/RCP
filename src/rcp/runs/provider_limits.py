"""One durable provider failure decision shared by settlement and recovery."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime
from typing import TYPE_CHECKING

from rcp.limits import AGENT_TASK_RECEIPT_MAX_BYTES, PROVIDER_LIMIT_PROBE_TIMEOUT_SECONDS
from rcp.machine_cards import LoginKey, host_of
from rcp.providers import classify_terminal_error, profile_for
from rcp.providers.usage_limits import limit_evidence, limit_reset, reset_from_evidence

if TYPE_CHECKING:
    import sqlite3

    from rcp.agents.launcher import AgentLauncher
    from rcp.storage import AppStore
    from rcp.storage.models import AgentTaskRecord


def failure_classification(
    store: AppStore,
    operation_id: str,
    error: str = "",
    *,
    connection: sqlite3.Connection | None = None,
) -> str:
    with nullcontext(connection) if connection is not None else store.connection() as active:
        receipt = active.execute(
            "SELECT payload_json FROM graph_run_receipts WHERE operation_id=? "
            "AND category='provider_terminal_error' ORDER BY receipt_id DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
    if receipt is not None:
        return str(json.loads(receipt[0]).get("classification", "provider_error"))
    return classify_terminal_error(error)


def record_provider_limit_decision(
    store: AppStore,
    launcher: AgentLauncher | None,
    record: AgentTaskRecord,
    *,
    snapshot: dict[str, object] | None = None,
    error: str | None = None,
    failed_at: datetime | None = None,
) -> dict[str, object]:
    receipts = store.agent_task_receipts_by_category(record.operation_id, "provider_terminal_error")
    if receipts and "source" in receipts[-1].payload:
        return receipts[-1].payload
    provider = str(record.request.get("provider") or "")
    resets = store.agent_task_receipts_by_category(record.operation_id, "provider_limit_reset")
    if resets:
        payload = {
            **resets[-1].payload,
            "provider": provider,
            "classification": "session_limit",
            "source": resets[-1].payload.get(
                "source", "event" if resets[-1].payload.get("snapshot") is not None else "text"
            ),
        }
        store.record_agent_task_receipt(
            record.operation_id, "provider_terminal_error", payload, tier="summary"
        )
        return payload
    error = record.error or "" if error is None else error
    failed_at = failed_at or datetime.fromisoformat(record.finished_at or record.updated_at)
    classification = failure_classification(store, record.operation_id, error)
    source = "event" if snapshot is not None else "text"
    probe_failure = None
    if snapshot is None and provider:
        try:
            snapshot = _probe_limit(store, launcher, record, provider)
            if snapshot is not None:
                source = "probe"
        except (KeyError, OSError, ValueError, RuntimeError) as exc:
            probe_failure = str(exc)
    payload: dict[str, object] = {"provider": provider, "source": source}
    if snapshot is not None:
        evidence = limit_evidence(provider, snapshot)
        if evidence.exhausted:
            classification = "session_limit"
            payload.update(
                reset_from_evidence(evidence, failed_at=failed_at).model_dump(mode="json")
            )
        elif classification == "session_limit":
            classification = "provider_error"
    elif classification == "session_limit":
        payload.update(
            limit_reset(provider, None, error, failed_at=failed_at).model_dump(mode="json")
        )
    payload["classification"] = classification
    payload["snapshot"] = snapshot
    try:
        snapshot_size = len(json.dumps(snapshot, allow_nan=False).encode())
    except (TypeError, ValueError):
        snapshot_size = AGENT_TASK_RECEIPT_MAX_BYTES
    if snapshot_size > AGENT_TASK_RECEIPT_MAX_BYTES // 2:
        payload["snapshot"] = None
        payload["snapshot_omitted"] = True
    if probe_failure is not None:
        payload["probe_failure"] = probe_failure[: AGENT_TASK_RECEIPT_MAX_BYTES // 32]
    store.record_agent_task_receipt(
        record.operation_id, "provider_terminal_error", payload, tier="summary"
    )
    return payload


def _probe_limit(
    store: AppStore, launcher: AgentLauncher | None, record: AgentTaskRecord, provider: str
) -> dict[str, object] | None:
    profile = profile_for(provider)
    if profile.limit_probe(provider) is None:
        return None
    if launcher is None:
        raise ValueError("No launcher is wired for provider limit probes.")
    if record.login_id is None:
        raise ValueError("The failed turn has no recorded provider login.")
    login = LoginKey(provider, record.stage_machine_id or "", record.login_id)
    host = host_of(store, login.machine_id)
    binary = launcher.provider_binary(provider, login.machine_id) or launcher.discover_provider(
        provider, host=host
    )
    if binary is None:
        raise ValueError("The provider executable is unavailable.")
    probe = profile.limit_probe(binary)
    assert probe is not None
    result = launcher._probe(
        host,
        probe.command,
        stdin=probe.stdin,
        timeout=PROVIDER_LIMIT_PROBE_TIMEOUT_SECONDS,
        environment=launcher.process_environment(login),
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"Limit probe exited with {result.returncode}.")
    return probe.parser(result.stdout)
