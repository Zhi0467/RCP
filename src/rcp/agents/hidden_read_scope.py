"""Contract seam for host policy resolution; implementation belongs to hidden_read."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from rcp.core.models import HiddenReadKeyEvidence, HiddenReadScope

if TYPE_CHECKING:
    from rcp.agents.write_scope import RegisteredRepositoryRoot
    from rcp.config import Manifest
    from rcp.providers import AgentCapability, ProviderId
    from rcp.transport.run_stage import RemoteRunStage


class HiddenReadScopeResolver(Protocol):
    def resolve_hidden_read_scope(
        self,
        *,
        manifest: Manifest,
        execution_machine: str,
        provider: ProviderId,
        capability: AgentCapability,
        stage_root: str,
        workspace_root: str,
        app_data_dir: Path | None,
        remote_stage: RemoteRunStage | None,
        repository_inventory: list[RegisteredRepositoryRoot],
        machine_hidden_folders: list[str],
        key_evidence: tuple[HiddenReadKeyEvidence, ...],
        browser_enabled: bool,
    ) -> HiddenReadScope:
        """Resolve once after launch-host key confirmation, before policy rendering.

        The implementation owns host canonicalization, bidirectional overlap
        checks, defaults, environment names and readiness. Evidence must belong
        to this execution account; an exempt key's parent must never be masked.
        No provider or browser policy is prepared from an unresolved scope.
        """
        ...


__all__ = ["HiddenReadKeyEvidence", "HiddenReadScope", "HiddenReadScopeResolver"]
