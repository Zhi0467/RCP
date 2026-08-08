# Handoff — actor identity and per-action permission checks

> **Superseded 2026-08-08.** Do not implement the user-owned agent model or the
> shared-repository multiplayer topology below. The current working design is
> [`handoff-2026-08-08-team-spaces-identity-and-permissions.md`](handoff-2026-08-08-team-spaces-identity-and-permissions.md),
> which replaces agent ownership with authorization lineage and makes one RCP
> team-space backend authoritative for identity, admission, execution, and
> project homes.

**Date:** 2026-08-07
**State:** scope and the rulings marked *decided 2026-08-07* are confirmed by the
human. Everything not marked decided is proposed. **No acceptance scenario has
been written or confirmed**, and no code exists.

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
   ([delta.py:48](../../src/rcp/history/delta.py:48)). This field records who
   wrote a patch. It also controls what the patch is allowed to do. Only a patch
   with `author == "human"` may write a Decision's `selected_option` or set its
   status to `decided`. The check is `permits` in
   [authority.py:63](../../src/rcp/core/authority.py:63), which the validator
   calls at [ops.py:105](../../src/rcp/core/validation/ops.py:105). So this is a
   permission check, and the only answer it can give is "human" or "agent".

   There are really two actions here, named in the
   [2026-08-08 handoff](handoff-2026-08-08-inbox-decisions-proposals-blockers.md):
   `decide_decision` (write `selected_option`, or set status to `decided`) and
   `queue_decision` (set status to `open`, `ready`, or `revisit`). Any agent may
   queue a Decision. Only a human may decide one. Both already go through the
   same `permits` function, so this piece only has to replace what is inside it.
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
| Action-layer | `decide_decision`; `queue_decision`; Experiment status; Blocker status |
| Structural | create/update nodes and edges by type; `remove_nodes` |
| Project | truth-scope membership; Settings; Proposal approve/reject |
| Verbs (piece 3) | `dispatch`; `address` |

The epistemic/action split is not invented here — it is the `layer` already
declared per relation, and it is the line the human drew for the orchestrator.
Recording it as the permission axis is what makes the table principled rather
than an arbitrary type list, which matters once human roles use the same table.

The Action-layer row used to say "Decision status and `selected_option`" as one
action. That hid a real difference: deciding a Decision is the human's job, but
moving it in and out of the Inbox is not. "Experiment status" and "Blocker
status" have not been checked for the same problem yet. They may need splitting
too.

### Legacy resolution — invariant 1 is not negotiable

Historical patches carry only `author: "agent" | "human"` and no actor id.
Replay must keep working and history is never rewritten.

- `actor_id` is **optional** on the patch envelope.
- Absent → resolve to a synthetic legacy actor of that kind, whose profile
  reproduces today's behavior exactly.
- The `author` binary stays on the envelope. It is not replaced or migrated.
- An **optional, empty signature field** ships on the envelope from the start,
  for the reason given in section 7 — adding one later is a migration
  append-only history cannot accept.
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

## 5. Decisions taken 2026-08-07

Settled with the human. Do not relitigate.

| | Ruling | Why |
|---|---|---|
| Do humans get profiles too, or only agents? | **Both, one table.** | This is the point of building it now instead of retrofitting it for multiplayer. Single-player is one owner profile permitting everything. |
| Is a profile global to the actor or set per project? | **Global to the actor; project membership grants reach.** | Per-project profiles produce an actor × project matrix nobody can reason about. |
| How fine-grained is the table? | **Per node type, plus a named exception list for the fields that carry authority** — `Decision.selected_option`, `Hypothesis.status`, node standing. | Pure per-type cannot express the orchestrator's line; per-field is a table that will not be maintained correctly. |

## 6. This is the research-lab foundation, not a permission patch

**Framing set by the human, 2026-08-07.** Identity here is the base for a human
research lab or team sharing one truthful RCP state. That makes four
interactions part of this piece's charter rather than someone else's problem
later:

1. **Signing in** — how a person establishes which actor they are.
2. **Where the profile lives** — a surface where an actor's identity, owner, and
   permitted actions are visible.
3. **Seeing other actors** — a directory of the people and agents in a group,
   and which projects they reach.
4. **Messaging them** — person-to-person messaging inside RCP.

The data model must not preclude any of these, and the design should name where
each one lives before implementation starts.

Two consequences worth flagging now:

- **Human messaging interacts with [Q9](../open-questions.md).** Once people can
  message each other in RCP, "my agent needs to reach another user" has an
  obvious home, and the deferred peer-mail question becomes easier to answer
  rather than harder. Do not build agent peer mail here, but do not design the
  human message surface in a way that could never carry it.
- **This may want to be its own piece.** The identity model plus per-action
  checks is what unblocks the orchestrator; sign-in, directory, and messaging
  are surfaces on top of it. Splitting them is reasonable — what is *not*
  reasonable is designing the model without knowing these four are coming.

## 7. Authentication — declared now, signature-ready schema

**Decided by the human, 2026-08-07: ship L0, take L1 free, design the schema for
L2.**

### The constraint that decides the shape

There is no server to log into. Canonical state is a repo reached over SSH,
operational SQLite is a per-user local cache, and one process owns a data
directory. When two people each run their own RCP against one shared state repo,
**no shared authority exists to ask who they are** — each instance believes its
own local config.

So attribution integrity can only come from the transport or from the patch
content. There is no third option, and that is what makes the ladder below
short.

### The ladder

| | What it is | What it lets RCP claim | Cost |
|---|---|---|---|
| **L0 Declared** | Actor id in local config, like `git config user.name` | "records who *said* they did it" | ~zero |
| **L1 Transport** | SSH access to the state host is the gate; record which principal published | "only authorized machines can write" | ~zero — remote state already runs over SSH |
| **L2 Signed** | Each actor signs its patches; replay verifies | "this approval is provably Alice's" | key management, verification, failure policy |
| **L3 Hosted IdP** | OAuth/SSO against a server | a full account model | requires RCP to become a service; contradicts local-first |

L1 arrives free the moment canonical state is remote: the SSH ACL already
maintained on the state host *is* the authentication, and RCP only has to record
the principal. Its honest limits are that it authenticates a key rather than a
person, and cannot separate two people sharing an account.

L2 is the only level that survives an untrusted transport, and it is the one
that matches what RCP claims to be — an append-only account of how research
state came to be. Unsigned, a shared repo lets anyone with write access author
as anyone, permanently. It likely needs no new key material: SSH keys can sign
(`ssh-keygen -Y sign`), the same mechanism behind SSH-signed git commits.

L3 is out of scope while RCP stays local-first.

### The threat model is confusion, not malice

Worth stating before anyone spends effort here. What actually goes wrong in a
lab is not an attacker: it is two people on a shared machine with the wrong
actor selected, an agent acting under the wrong owner's authority, and someone
asking six months later who approved a belief change.

**Confusion is solved by L0 plus a surface that always shows who you are acting
as** — far more cheaply than by cryptography. L2 earns its cost only when the
record becomes evidence: paper provenance, a dispute about who decided what, or
an external audit.

### What this obliges the schema to do today

Nearly free now, expensive to retrofit later, because invariant 1 forbids
rewriting a patch once written:

- put `actor_id` **and an optional, empty signature field** on the patch
  envelope from the first version.

That way enabling signing later makes historical patches render as *unverified*
rather than demanding a migration that append-only history cannot accept.

### One policy to settle before L2 ever lands

**A bad or unverifiable signature must not halt replay.** Replay currently stops
at the first invalid revision, so treating a signature failure as invalidity
would let a rotated or lost key brick an entire project. Unverified is a
*rendering* state, not a validity state. Do not let this decision get made
implicitly by whoever implements verification.

## 8. Scope

**In scope:** the data model, the action vocabulary, per-action checks at every
authority site (including the existing `author == "human"` check), provenance
carrying `actor_id`, profile assignment in Settings, and the four interactions
in section 6.

Worth recording, because it reframes the hosting worry the human raised: the
multi-writer substrate already exists. Canonical state is an append-only log in
a possibly-remote repo guarded by `flock`-based advisory locks
(`.agent-run.lock`, `.refresh.lock`) held by real processes, with fenced
publication. The `fcntl` single-instance lock is per **data directory** — the
local operational SQLite — not per state repo. So RCP is already git-shaped: a
shared state repo with per-user local processes and per-user operational caches
is a viable multiplayer topology, and the DB is exactly the thing that should
*not* be shared. What is missing is identity, which is this piece.

## 9. How to build it

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

## 10. Acceptance scenario — written, not yet confirmed

[S92 — An agent cannot exceed the person who owns it](../acceptance/S92-actor-identity-and-permission-checks.md).

Driver `pytest + browser`. The backend half is settled; the **UI path is not** —
the profile, directory, and sign-in surfaces have not been discussed in enough
detail. Per [`AGENTS.md`](../../AGENTS.md) step 0, confirm it with the human
before implementation.

Sign-in, the actor directory, and person-to-person messaging (section 6) are
deliberately outside S92 and need their own scenarios once their surfaces exist
as designs.

## 11. Do not

- Do not rewrite or backfill `.research/patches/` to add actor ids or
  signatures.
- Do not drop `Patch.author`; the binary stays as recorded history.
- Do not let a profile be readable or settable from inside an agent prompt,
  skill, or manifest.
- Do not build a login surface that implies verification L0 does not perform. A
  declared identity shown honestly is better than a sign-in screen that invites
  a security claim the implementation cannot support.
- Do not let a signature failure halt replay when L2 arrives (section 7).
- Do not build agent peer mail alongside the human message surface. That is
  [Q9](../open-questions.md), and its budget and consent problems are unsolved.
