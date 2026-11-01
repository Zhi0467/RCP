"""Persist provider usage and its task diagnostic together for every run owner."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rcp.providers import ProviderUsage
    from rcp.storage import AppStore


def record_provider_usage(
    store: AppStore,
    operation_id: str,
    usage: ProviderUsage,
    *,
    recorded: bool = False,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    usage_record = store.record_agent_usage(
        operation_id, usage, idempotent=recorded, provider=provider, model=model
    )
    if recorded and any(
        receipt.category == "provider_usage"
        and receipt.payload.get("usage_id") == usage_record.usage_id
        for receipt in store.agent_task_receipts(operation_id)
    ):
        return
    store.record_agent_task_receipt(
        operation_id,
        "provider_usage",
        {
            "usage_id": usage_record.usage_id,
            "counted": usage_record.counted,
            "count_reason": usage_record.count_reason,
            "provider_profile": usage_record.provider_profile,
            "processed_input_tokens": usage_record.processed_input_tokens,
            "generated_tokens": usage_record.generated_tokens,
        },
        tier="diagnostic",
    )
