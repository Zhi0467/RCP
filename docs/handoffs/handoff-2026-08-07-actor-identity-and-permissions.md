# Handoff — actor identity and per-action permission checks

**Date:** 2026-08-07
**State:** scope confirmed by the human in a design conversation. The detailed
contract below is **proposed, not confirmed**, and **no acceptance scenario
exists yet**. No code has been written.

**Order in the program:** this is piece 1 of 3. Piece 2 is
[graph-condition wake](handoff-2026-08-07-graph-condition-wake.md); piece 3 is
the [orchestrator](handoff-2026-08-07-orchestrator.md), which depends on both.

Read [`AGENTS.md`](../../AGENTS.md) first, then the blueprint's
[Human and agent authority](../research-control-panel-blueprint.md#human-and-agent-authority)
section, then this file.

---

## 1. Why this exists

RCP expresses authority three incompatible ways, none of which can say *"this
agent, owned by this user, may do X but not Y."*

1. **`Patch.author: Literal["agent", "human"]`**
   ([delta.py:48](../../src/rcp/history/delta.py:48)). This is not merely
   provenance — it is already an **authorization check**:
   [delta.py:412](../../src/rcp/history/delta.py:412) permits
   `resolve_ambiguities` only when `patch.author == "human"`.
2. **`Proposal.created_by` / `resolved_by`**, the same binary
   ([models.py:364](../../src/rcp/core/models.py:364)).
3. **`permissions_for()` in [config.py](../../src/rcp/config.py)** — capability
   fixed *by surface* (Seed/Refresh, Discuss, Work, coach), per invariant 4.

So a permission system already exists in embryo. The work is generalizing a
binary into a profile lookup, without breaking replay of every historical patch
that only ever carried the binary.

The immediate driver is the orchestrator (piece 3), which must hold authority
that no current surface has. The longer-range driver is multiplayer: several
users operating against one truthful RCP state.

## 2. Requirements the human stated

Identity must distinguish:

- an **agent** from a **user**;
- one **user** from another **user**;
- a **group**, whose members share projects; and
- for every agent, its **owning user**.

Permission is checked **per action against the actor's profile** — not inferred
from which surface launched the run.

## 3. Proposed model

```
Actor    { id, kind: "user" | "agent", display_name,
           owner_actor_id (required when kind == "agent"), profile_id }
Group    { id, name, member_actor_ids (users) }
Profile  { id, name, permitted_actions }
```

Project membership is granted to a group; an actor reaches a project through
group membership. An agent's reach is bounded by its owner's — **an agent can
never exceed the user who owns it**, which is the property that keeps the
authority story honest when a second user appears.

### The action vocabulary is the real design work

Per-action checking needs a named, closed set of actions. Derive it from the
authority list already in the blueprint plus the `layer` field that already
exists in [`RELATION_SPEC`](../../src/rcp/core/models.py:288):

| Group | Actions |
|---|---|
| Epistemic | set standing; accept a Hypothesis status transition; edit ResearchQuestion or Hypothesis status |
| Action-layer | Decision status and `selected_option`; Experiment status; Blocker status |
| Structural | create/update nodes and edges by type; `remove_nodes` |
| Project | truth-scope membership; Settings; Proposal approve/reject |
| Verbs (piece 3) | `dispatch`; `address` |

The epistemic/action split is not invented here — it is the `layer` already
declared per relation, and it is the line the human drew for the orchestrator.
Recording it as the permission axis is what makes the table principled rather
than an arbitrary type list, which matters once human roles use the same table.

### Legacy resolution — invariant 1 is not negotiable

Historical patches carry only `author: "agent" | "human"` and no actor id.
Replay must keep working and history is never rewritten.

- `actor_id` is **optional** on the patch envelope.
- Absent → resolve to a synthetic legacy actor of that kind, whose profile
  reproduces today's behavior exactly.
- The `author` binary stays on the envelope. It is not replaced or migrated.
- No backfill, no rewriting of `.research/patches/`.

## 4. Invariant 4 becomes a generalization, not an erosion

Current text fixes capability *by surface* and forbids the manifest from
widening or narrowing it. The amendment keeps the intent and changes the lookup
key:

> Capability is fixed **by actor profile**. A profile is assigned outside the
> agent's reach; no manifest, skill, prompt, or agent action may widen or
> narrow it. Surface capability profiles remain the defaults for Seed/Refresh,
> Discuss, Work, correction, and coaching.

The property that must survive verbatim: **an actor cannot grant itself
authority.** State the amendment that way in the blueprint so it does not read
as a loosening.

## 5. Scope

**In scope:** the data model, the action vocabulary, per-action checks at every
authority site (including the existing `author == "human"` check), provenance
carrying `actor_id`, and profile assignment in Settings.

**Out of scope, deliberately:** authentication, credential handling, a hosted
service, and remote identity verification. This piece makes multiplayer
*possible*; it does not make it *safe against an adversary*.

Worth recording, because it reframes the hosting worry the human raised: the
multi-writer substrate already exists. Canonical state is an append-only log in
a possibly-remote repo guarded by `flock`-based advisory locks
(`.agent-run.lock`, `.refresh.lock`) held by real processes, with fenced
publication. The `fcntl` single-instance lock is per **data directory** — the
local operational SQLite — not per state repo. So RCP is already git-shaped: a
shared state repo with per-user local processes and per-user operational caches
is a viable multiplayer topology, and the DB is exactly the thing that should
*not* be shared. What is missing is identity, which is this piece.

## 6. How to build it

**Serial. Do not fan out.** This touches `src/rcp/core/models.py`,
`src/rcp/config.py`, and `web/src/types.ts` — three shared contracts at once.
Land the contract, then fan out consumers.

Suggested order:

1. Actor/Group/Profile models and storage, with the legacy actor resolution.
2. The action vocabulary and a single checkpoint function. One place, so the
   check cannot drift per call site.
3. Replace the existing binary authority checks with profile lookups, starting
   with [delta.py:412](../../src/rcp/history/delta.py:412).
4. Carry `actor_id` in patch and Proposal provenance.
5. Settings UI for profile assignment; History renders the acting identity.

## 7. Proposed acceptance scenario — needs the human's confirmation first

**"An agent cannot exceed its owner."** Promise: every authority-bearing action
resolves through one profile check; an agent's reach is bounded by its owning
user's; and a project opened from history written before actors existed replays
identically with the legacy actor.

Driver: `pytest` for the authority table and replay compatibility; `browser`
only for profile assignment in Settings.

Per [`AGENTS.md`](../../AGENTS.md) step 0, write and confirm this scenario
before implementation.

## 8. Do not

- Do not rewrite or backfill `.research/patches/` to add actor ids.
- Do not drop `Patch.author`; the binary stays as recorded history.
- Do not let a profile be readable or settable from inside an agent prompt,
  skill, or manifest.
- Do not build authentication here. An unauthenticated local identity is enough
  to unblock the orchestrator, and pretending otherwise invites a security
  claim the implementation cannot support.
