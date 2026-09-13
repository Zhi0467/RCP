from __future__ import annotations

from rcp.providers import ProviderProfile
from rcp.storage.models import AgentFailureKind

__all__ = ["AgentFailureKind", "classify_agent_failure", "transport_failure"]


def transport_failure(return_code: int | None, host: str) -> bool:
    """Whether SSH itself reported that the connection failed or was lost."""

    if not host or return_code is None:
        return False
    # 255 is ssh's own "the connection failed or was lost" exit code. A
    # negative code is a signalled child, and for a remote run the signalled
    # process is the ssh client, so the run ended with the link rather than
    # with anything the provider decided.
    return return_code == 255 or return_code < 0


def classify_agent_failure(
    *,
    error: str,
    return_code: int | None,
    host: str,
    profile: ProviderProfile | None,
    provider_spoke_for_itself: bool,
) -> AgentFailureKind | None:
    """Name one provider failure so recovery can offer the right next step."""

    # The provider's own diagnostic is more specific than an exit code, and a
    # revoked login can still exit the way a dropped link does.
    if profile is not None and profile.credential_failure(error):
        return "provider_auth"
    # A provider that reached its own terminal event, or reported its own
    # error, said what happened before the process ended. The exit code cannot
    # contradict that: ssh returns 255 for a provider that exits 255 as readily
    # as for a link it lost. Reattempting such a turn would repeat work the
    # provider already did for a reason another attempt cannot change.
    if provider_spoke_for_itself:
        return None
    if transport_failure(return_code, host):
        return "transport_lost"
    return None
