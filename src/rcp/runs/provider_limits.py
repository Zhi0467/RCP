"""One durable provider failure decision shared by settlement and recovery."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from rcp.limits import (
    AGENT_TASK_RECEIPT_MAX_BYTES,
    PROVIDER_LIMIT_PROBE_FAILURE_MAX_CHARS,
    PROVIDER_LIMIT_PROBE_TIMEOUT_SECONDS,
    PROVIDER_LIMIT_SNAPSHOT_MAX_BYTES,
)
from rcp.machine_cards import LoginKey, host_of
from rcp.providers import profile_for
from rcp.providers.usage_limits import LimitEvidence, reset_from_evidence

if TYPE_CHECKING:
    from rcp.agents.launcher import AgentLauncher
    from rcp.storage import AppStore
    from rcp.storage.models import AgentTaskRecord


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
    error = record.error or "" if error is None else error
    failed_at = failed_at or datetime.fromisoformat(record.finished_at or record.updated_at)
    classification = store.failure_classification(record.operation_id, error)
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
        evidence = profile_for(provider).limit_evidence(snapshot)
        if evidence.exhausted:
            classification = "session_limit"
            payload.update(
                reset_from_evidence(evidence, failed_at=failed_at).model_dump(mode="json")
            )
        elif classification == "session_limit":
            classification = "provider_error"
    elif classification == "session_limit":
        payload.update(
            reset_from_evidence(
                LimitEvidence(
                    exhausted=True,
                    resets_at=profile_for(provider).limit_text_reset(error, failed_at),
                ),
                failed_at=failed_at,
            ).model_dump(mode="json")
        )
    payload["classification"] = classification
    payload["snapshot"] = snapshot
    try:
        snapshot_size = len(json.dumps(snapshot, allow_nan=False).encode())
    except (TypeError, ValueError):
        snapshot_size = AGENT_TASK_RECEIPT_MAX_BYTES
    if snapshot_size > PROVIDER_LIMIT_SNAPSHOT_MAX_BYTES:
        payload["snapshot"] = None
        payload["snapshot_omitted"] = True
    if probe_failure is not None:
        payload["probe_failure"] = probe_failure[:PROVIDER_LIMIT_PROBE_FAILURE_MAX_CHARS]
    store.record_agent_task_receipt(
        record.operation_id, "provider_terminal_error", payload, tier="summary"
    )
    return payload


def _probe_limit(
    store: AppStore, launcher: AgentLauncher | None, record: AgentTaskRecord, provider: str
) -> dict[str, object] | None:
    profile = profile_for(provider)
    if profile.limit_probe is None:
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
