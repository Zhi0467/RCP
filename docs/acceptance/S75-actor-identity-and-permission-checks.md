---
id: S75-actor-identity-and-permission-checks
status: pending — not human-confirmed
tier: hermetic
driver: pytest + browser
covered_by: none
invariants: [1, 3, 4]
---

# An agent cannot exceed the person who owns it

This scenario is a proposal and is **not yet human-confirmed**. The design it
describes is settled in
[the identity handoff](../handoffs/handoff-2026-08-07-actor-identity-and-permissions.md);
the **UI path below is not** — the human and agent have not discussed the
profile, directory, or sign-in surfaces in enough detail for the browser half to
be treated as agreed. Confirm those before implementing anything visual.

Every authority-bearing action resolves through one permission check against the
acting identity's profile. An agent's reach is bounded by its owning user's, and
a project whose history predates actors replays exactly as it did before.

## Setup

A project with existing canonical history written before actors existed, plus:

- two users in one group that shares the project;
- one agent owned by the first user;
- a profile permitting the action layer but not standing changes.

## Drive — proposal

1. Open **Settings** and see which actor you are acting as, its kind, and — for
   an agent — its owning user.
2. Assign a profile to the agent.
3. Have that agent attempt an action its profile permits, then one its owner's
   profile does not permit.
4. Open the project whose history predates actors and read its graph and
   revision history.

## Assert

- `every_authority_action_resolves_through_one_profile_check`
- `agent_reach_is_bounded_by_its_owning_user`
- `profile_is_global_to_the_actor_not_per_project`
- `project_membership_is_granted_through_a_group`
- `legacy_patches_without_actor_id_replay_identically`
- `patch_envelope_carries_actor_id_and_an_optional_empty_signature_field`
- `profile_is_not_readable_or_settable_from_an_agent_prompt_skill_or_manifest`
- `resolve_ambiguities_authority_comes_from_the_profile_not_the_author_binary`

## Boundary

Authentication is **L0 declared** — identity is recorded, not verified. The
signature field ships empty and nothing signs or verifies in this scenario; L1
transport identity and L2 signing are later levels described in the handoff.

`Patch.author` remains on the envelope as recorded history. Nothing backfills or
rewrites `.research/patches/`.

Sign-in, the actor directory, and person-to-person messaging are named in the
handoff as part of this piece's charter but are **not** promised by this
scenario. They need their own scenarios once their surfaces are discussed.
