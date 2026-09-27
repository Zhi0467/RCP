"""The shared construction rule for launches that start or continue a native session.

Owners keep their own builders and pass the parts they rendered. This module decides
only where the master contract and the context delta go for each node type.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from rcp.agents.graph_rules import GRAPH_RULES_VERSION

PromptNode = Literal["session_start", "human_turn", "wake", "recovery", "correction"]

# Bumped by hand when the shared master framing changes. Version 1 adds nothing to the
# key, so keys minted before this module existed stay valid and sessions keep their master.
MASTER_VERSION = 1

_CONTINUATION_NODES: dict[str, PromptNode] = {
    "turn": "human_turn",
    "wake": "wake",
    "recovery": "recovery",
    "correction": "correction",
}

# The shared prose, by section id. Owners never restate these.
SECTIONS = {
    "master_pointer": (
        "Master contract: `{path}`. This is the same contract you were given at the start of "
        "this session. Read it only after a compaction, or if you have lost track of the graph "
        "rules or your authority. Current instructions in this message take precedence over it."
    ),
    "master_bootstrap": (
        "Open and retain the RCP master contract at:\n{path}\n"
        "It defines the stable pointers and contracts for this native session."
    ),
    "master_rebootstrap": (
        "Open and retain the RCP master contract at:\n{path}\n"
        "It replaces the master contract this session held before, and defines the stable "
        "pointers and contracts for this native session."
    ),
    "context_delta": "RCP context update — these master-context values have changed:",
    "report_revocation": (
        "The operational instructions this session held no longer apply. Follow only the "
        "report instructions in this message."
    ),
    "report_rebootstrap": (
        "The episode report instructions have ended. Open the operational master contract "
        "again at:\n{path}\nand follow it together with the current instructions in this message."
    ),
}


@dataclass(frozen=True)
class LaunchPhase:
    """What the owner knows at its call boundary: the session it hands the provider."""

    session_id: str | None
    phase: Literal["turn", "wake", "recovery", "correction"]


@dataclass(frozen=True)
class MasterRef:
    """The staged master for this launch, and whether the session must open it now."""

    path: str
    bootstrap: bool
    replaces: bool = False


def classify(phase: LaunchPhase) -> PromptNode:
    """A launch without a session id starts one; any other launch continues it."""

    if phase.session_id is None:
        return "session_start"
    return _CONTINUATION_NODES[phase.phase]


def master_key(owner_policy_version: str, *, ontology_extensions: bool) -> str:
    """Identify one master: shared framing, graph rules, and the owner's own policy.

    The rules render differently once a project has ontology extensions, so gaining or
    losing its first one changes the key and re-sends the master.
    """

    shared = "" if MASTER_VERSION == 1 else f"-master-v{MASTER_VERSION}"
    ontology = "-ontology" if ontology_extensions else ""
    return f"{owner_policy_version}{shared}-rules-{GRAPH_RULES_VERSION}{ontology}"


def compose(
    node: PromptNode,
    *,
    parts: Sequence[str],
    master: MasterRef | None,
    delta: dict[str, object] | None,
) -> str:
    """Assemble one launch prompt by its node type's rule.

    A session start opens with its master. A continuation carries the owner's parts,
    then what changed, then a pointer to the master it already holds, or the master
    itself when that is new or replaced. A continuation never carries master text.
    """

    sections = list(parts)
    if node == "session_start":
        if delta:
            raise ValueError("a session start has no earlier context to update")
        if master is not None:
            if not master.bootstrap or master.replaces:
                raise ValueError("a session start must open its master contract")
            sections.insert(0, SECTIONS["master_bootstrap"].format(path=master.path))
        return "\n\n".join(sections)
    sections.extend(_delta_sections(delta))
    if master is not None:
        if not master.bootstrap:
            section = "master_pointer"
        elif master.replaces:
            section = "master_rebootstrap"
        else:
            section = "master_bootstrap"
        sections.append(SECTIONS[section].format(path=master.path))
    return "\n\n".join(sections)


def context_delta(
    previous: dict[str, object],
    current: dict[str, object],
) -> dict[str, object] | None:
    changed = {
        key: value
        for key, value in current.items()
        if key != "compute" and (key not in previous or previous[key] != value)
    }
    prior_compute = _compute_profiles(previous.get("compute"))
    current_compute = _compute_profiles(current.get("compute"))
    if prior_compute != current_compute:
        prior_ids = set(prior_compute)
        current_ids = set(current_compute)
        changed["compute"] = {
            "added": [current_compute[item] for item in sorted(current_ids - prior_ids)],
            "removed": [prior_compute[item]["name"] for item in sorted(prior_ids - current_ids)],
            "updated": [
                current_compute[item]
                for item in sorted(prior_ids & current_ids)
                if prior_compute[item] != current_compute[item]
            ],
        }
    removed = sorted(key for key in previous if key not in current)
    if removed:
        changed["removed"] = removed
    return changed or None


def _compute_profiles(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or not isinstance(value.get("active"), list):
        return {}
    profiles: dict[str, dict[str, str]] = {}
    for item in value["active"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        profiles[item["id"]] = {
            key: str(item.get(key, ""))
            for key in ("id", "name", "kind", "ssh_target", "access_hint")
        }
    return profiles


def _delta_sections(delta: dict[str, object] | None) -> list[str]:
    if not delta:
        return []
    ordinary = dict(delta)
    sections = []
    compute = _compute_delta_section(ordinary.pop("compute", None))
    if compute:
        sections.append(compute)
    if ordinary:
        sections.append(
            SECTIONS["context_delta"]
            + "\n"
            + json.dumps(ordinary, ensure_ascii=False, indent=2, sort_keys=True)
        )
    return sections


def _compute_delta_section(delta: object) -> str:
    if not isinstance(delta, dict):
        return ""
    actions = []
    for key, verb in (("added", "added"), ("updated", "updated")):
        profiles = delta.get(key)
        if isinstance(profiles, list):
            rendered = [
                _compute_profile_delta(profile) for profile in profiles if isinstance(profile, dict)
            ]
            if rendered:
                actions.append(f"{verb} " + ", ".join(rendered))
    removed = delta.get("removed")
    if isinstance(removed, list) and removed:
        actions.append("removed " + ", ".join(f"`{value}`" for value in removed))
    return "RCP compute update: " + "; ".join(actions) + "." if actions else ""


def _compute_profile_delta(profile: dict[object, object]) -> str:
    name = str(profile.get("name", ""))
    compute_id = str(profile.get("id", ""))
    kind = str(profile.get("kind", ""))
    if kind == "local":
        location = "kind: local; this agent execution machine"
    else:
        location = f"kind: SSH; target: `{profile.get('ssh_target', '')}`"
    hint = str(profile.get("access_hint", ""))
    if hint:
        location += f"; access hint: {hint}"
    return f"`{name}` (`{compute_id}`; {location})"
