"""The provider registry.

Registering an agent CLI means adding one subpackage here, `rcp/providers/<id>/`,
and listing its profile in `PROVIDERS`. The subpackage holds everything RCP
knows about that CLI:

- `profile.py`: the `ProviderProfile` subclass that launches, contains, and
  decodes it;
- `auth.py`: how a member signs it in, verifies, and signs out, when RCP
  manages that; without one, the CLI's own login applies;
- `remote.py`: its session-file format and turn fence. This module runs on
  execution hosts, so it imports only the standard library.

Nothing about a provider belongs anywhere else: not a `("codex", "claude")`
tuple, not a display-name ternary, not an option list in a React component.
Two bugs on 2026-07-30 came from provider facts written from memory into the
frontend: a reasoning list that offered a value the models reject, and a correct
control hidden on a false premise about the launch command.

Where the CLI can enumerate its own models, the profile probes it and RCP offers
exactly what came back. Where it cannot, the profile declares the lists and
records the CLI version they were read from, so the staleness is visible to
whoever maintains them.
"""

from __future__ import annotations

import importlib.resources
from functools import cache
from typing import Annotated

from pydantic import AfterValidator

from rcp.providers.base import (
    AgentCapability,
    ModelChoice,
    ProviderNativeUpdate,
    ProviderProfile,
    ProviderRuntime,
    ProviderRuntimeChoice,
    ProviderRuntimeStep,
    ProviderSkill,
    ProviderSkillProbe,
    ProviderSkillReference,
    ProviderSteeringState,
    ProviderSteerReceipt,
    ProviderStreamEvent,
    ProviderTurn,
    ProviderTurnRequest,
    ProviderUsage,
)
from rcp.providers.claude.profile import ClaudeProfile
from rcp.providers.codex.profile import CodexProfile
from rcp.providers.opencode.profile import OpenCodeProfile
from rcp.providers.session_format import SESSION_FORMATS
from rcp.providers.turn_fence import TURN_FENCES

PROVIDERS: dict[str, ProviderProfile] = {
    profile.id: profile for profile in (CodexProfile(), ClaudeProfile(), OpenCodeProfile())
}
#: Iteration order for every place that walks all providers.
PROVIDER_IDS: tuple[str, ...] = tuple(PROVIDERS)
DEFAULT_PROVIDER = CodexProfile.id

for _profile in PROVIDERS.values():
    if _profile.session_format is not None:
        SESSION_FORMATS[_profile.id] = _profile.session_format
    TURN_FENCES.update(_profile.turn_fences)

#: Shipped ahead of every provider's `remote.py`, in this order.
_REMOTE_BASE_MODULES = (
    "rcp.providers.session_format",
    "rcp.providers.turn_fence",
    "rcp.sources.record_parsing",
)


@cache
def remote_bundle(driver: str) -> str:
    """One `python3 -c` program: the shared bases, every provider's remote code, then `driver`.

    Each module is compiled on its own so its `from __future__` import stays
    first. The registration lines are generated from the same profiles that
    register locally above, so a host and this process cannot disagree.
    """
    session_formats = {
        profile.id: type(profile.session_format)
        for profile in PROVIDERS.values()
        if profile.session_format is not None
    }
    fences = {
        runtime_id: fence
        for profile in PROVIDERS.values()
        for runtime_id, fence in profile.turn_fences.items()
    }
    provider_modules = sorted(
        {cls.__module__ for cls in (*session_formats.values(), *fences.values())}
        - set(_REMOTE_BASE_MODULES)
    )
    names = [cls.__name__ for cls in {*session_formats.values(), *fences.values()}]
    if len(names) != len(set(names)):
        raise ValueError("Shipped provider classes must have distinct names.")
    parts = [
        f"exec(compile({_module_source(module)!r}, {module!r}, 'exec'))"
        for module in (*_REMOTE_BASE_MODULES, *provider_modules)
    ]
    parts.append(
        "SESSION_FORMATS.update({"
        + ", ".join(f"{source!r}: {cls.__name__}()" for source, cls in session_formats.items())
        + "})"
    )
    parts.append(
        "TURN_FENCES.update({"
        + ", ".join(f"{runtime_id!r}: {cls.__name__}" for runtime_id, cls in fences.items())
        + "})"
    )
    return "\n".join((*parts, driver))


def _module_source(module: str) -> str:
    package, _, name = module.rpartition(".")
    return importlib.resources.files(package).joinpath(f"{name}.py").read_text(encoding="utf-8")


def classify_terminal_error(text: str) -> str:
    """Classify a persisted provider error without depending on a provider id."""
    folded = " ".join(text.casefold().split())
    if any(
        marker in folded
        for marker in (
            "session limit",
            "usage limit",
            "hit your limit",
            "quota exceeded",
            "out of credits",
            "weighted tokens left",
        )
    ):
        return "session_limit"
    # The provider still answers, but the native session RCP asked it to resume
    # is gone. Resuming again cannot work; a fresh session can. Observed from
    # Codex on 2026-09-12 as "collab spawn failed: no thread with id: <uuid>".
    # Only the missing-thread half is the evidence: a spawn that failed for some
    # other reason still has its session, and starting clean would throw away a
    # live checkpoint and repeat the work it holds.
    if "no thread with id" in folded:
        return "stale_session"
    return "provider_error"


def profile_for(provider: str) -> ProviderProfile:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise ValueError(f"Unknown agent provider: {provider!r}") from None


def legacy_runtime_id(provider: str) -> str:
    """Runtime assigned to records created before runtime identity was durable."""

    return profile_for(provider).legacy_runtime_id


def configured_runtime(provider: str, value: str | None) -> str:
    """Normalize one project-profile runtime without exposing provider internals."""

    return profile_for(provider).configured_runtime(value)


def configured_runtime_id(provider: str, value: str | None) -> str:
    """Resolve one normalized project-profile runtime to its durable identifier."""

    return profile_for(provider).configured_runtime_id(value)


def runtime_label(provider: str, runtime_id: str) -> str:
    """Display name for one durable runtime id, so no surface maps ids itself."""

    return profile_for(provider).runtime_label(runtime_id)


def require_runtime_id(provider: str, runtime_id: str) -> str:
    profile_for(provider).runtime(runtime_id)
    return runtime_id


def project_write_enforcement_mode(provider: str) -> str:
    return profile_for(provider).project_write_enforcement_mode()


def _known_provider(value: str) -> str:
    profile_for(value)
    return value


#: A provider id validated against the registry. Replaces the
#: `Literal["claude", "codex"]` that used to be repeated across the schema
#: layer, so adding a provider does not mean editing every model that names one.
ProviderId = Annotated[str, AfterValidator(_known_provider)]

__all__ = [
    "AgentCapability",
    "ClaudeProfile",
    "CodexProfile",
    "DEFAULT_PROVIDER",
    "ModelChoice",
    "PROVIDERS",
    "PROVIDER_IDS",
    "ProviderId",
    "ProviderNativeUpdate",
    "ProviderProfile",
    "ProviderRuntime",
    "ProviderRuntimeChoice",
    "ProviderRuntimeStep",
    "ProviderSkill",
    "ProviderSkillProbe",
    "ProviderSkillReference",
    "ProviderSteerReceipt",
    "ProviderSteeringState",
    "ProviderStreamEvent",
    "ProviderTurn",
    "ProviderTurnRequest",
    "ProviderUsage",
    "classify_terminal_error",
    "configured_runtime",
    "configured_runtime_id",
    "legacy_runtime_id",
    "profile_for",
    "remote_bundle",
    "project_write_enforcement_mode",
    "require_runtime_id",
    "runtime_label",
]
