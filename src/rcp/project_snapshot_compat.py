from __future__ import annotations

from rcp.config import DEFAULT_AUTO_RESEARCH_INVOCATION_CEILING
from rcp.providers import configured_runtime
from rcp.skill_registry import SkillDefaults


def migrate_display_snapshot_settings(snapshot: dict[str, object]) -> bool:
    """Decode explicitly supported settings from an older display snapshot."""

    if not _migrate_agent_profile_runtimes(snapshot):
        return False
    skill_defaults = snapshot.get("skill_defaults")
    if skill_defaults is not None:
        try:
            snapshot["skill_defaults"] = SkillDefaults.model_validate(skill_defaults).model_dump(
                mode="json"
            )
        except (TypeError, ValueError):
            return False
    legacy_key = "default_campaign_invocation_ceiling"
    current_key = "default_auto_research_invocation_ceiling"
    if legacy_key not in snapshot:
        snapshot.setdefault(current_key, DEFAULT_AUTO_RESEARCH_INVOCATION_CEILING)
        return True
    if current_key in snapshot and snapshot[current_key] != snapshot[legacy_key]:
        return False
    snapshot.setdefault(current_key, snapshot[legacy_key])
    del snapshot[legacy_key]
    return True


def _migrate_agent_profile_runtimes(snapshot: dict[str, object]) -> bool:
    """Name the runtime on profiles cached before runtime selection existed."""

    profiles = snapshot.get("agent_profiles")
    if not isinstance(profiles, dict):
        return True
    for profile in profiles.values():
        if not isinstance(profile, dict):
            return False
        provider = profile.get("provider")
        if not isinstance(provider, str):
            return False
        runtime = profile.get("runtime")
        if runtime is not None and not isinstance(runtime, str):
            return False
        try:
            profile["runtime"] = configured_runtime(provider, runtime)
        except ValueError:
            # A retired provider or runtime cannot be named. Re-deriving the
            # snapshot from the manifest is cheaper than guessing.
            return False
    return True
