# Slice — being in the lab is not being on the project

**Both slices are implemented and passed on 2026-08-15.** This file is kept as
the record of why the design is what it is. What actually got built, and the two
things this plan did not anticipate, are at the bottom under "What the build
changed".

Written 2026-08-15, then rewritten the same day after a grilling session. The
first draft got the central instruction wrong; see "What the grilling changed" at
the bottom before trusting anything you remember about it.

Two slices, two scenarios, both confirmed 2026-08-15:

- **Slice A — the boundary.** [S101](../acceptance/S101-project-membership.md).
  Membership exists, is seeded, and is enforced.
- **Slice B — the invitations.** [S122](../acceptance/S122-project-invitations.md).
  Membership can be granted, declined, and given up, and an agent running on a
  departed member's authorization is fenced.

Do them in that order. A is meaningful alone: it closes boundary 8.

## Why this is the next slice

[S96](../acceptance/S96-joining-a-team-space.md) shipped. A team space now
authenticates people — bootstrap claim, invitations, permanent tokens, browser
sessions, rotation, revocation — and then hands every one of them everything. The
space is the only membership boundary that exists.

Of the ten acceptance boundaries in
[Identity, permissions, and agent profiles](../design/identity-permissions-and-agent-profiles.md#acceptance-boundaries),
seven are proven. Boundary 8 — *a space member without project membership cannot
read, dispatch, or Apply* — cannot be driven at all until slice A lands. Slice B
then supplies the permission-that-changes that
[S100](../acceptance/S100-permission-is-checked-twice.md) deliberately deferred.

## What already exists — read these first

| Thing | Where | Note |
|---|---|---|
| Space members, tokens, sessions | [`storage/spaces.py`](../../src/rcp/storage/spaces.py) | `rcp_` token, SHA-256 hash, constant-time compare, 14-day sliding session |
| Team routes, session middleware, `acting_user` | [`api/identity.py`](../../src/rcp/api/identity.py) | `acting_user` returns a user without demanding a display name — that matters, see below |
| The two permission gates | [`core/authority.py`](../../src/rcp/core/authority.py) | `require_dispatch` **cannot** check membership; `require_apply` can |
| Project catalog and cards | [`projects.py`](../../src/rcp/projects.py) | `cards()` returns every project, unfiltered |
| Cross-project Experiment board | `GET /api/episodes` in [`api/app.py`](../../src/rcp/api/app.py) | Loops every project and returns node titles |
| Space-level invitations | `team_invitations` in [`storage/spaces.py`](../../src/rcp/storage/spaces.py) | **Do not reuse this table.** A project invitation is a different thing |
| The Stop fence | invariant 10g, [`runs/`](../../src/rcp/runs/) | Slice B reuses it rather than inventing a fence |
| Canonical contracts | [Team space enrollment and sessions](../research-control-panel-blueprint.md#team-space-enrollment-and-sessions) | The blueprint has enrollment; membership is not canonical yet |

## Facts that decided the design — verify them, don't assume them

Each of these was read out of the code on 2026-08-15 and each one killed an
earlier plan.

1. **`AgentDispatchAuthority` has no project and no user.** It is `profile`,
   `task_contract`, `scope`. `require_dispatch` is a shape validator. It cannot
   consult membership, and it is not where the check goes.
2. **Most routes never resolve a person.** 36 routes live under
   `/api/projects/{project_id}`; the whole file has 11 calls to
   `require_patch_capable_identity`. Reads reach no gate at all today.
3. **Nothing records who created a project.** `ProjectRecord` has no creator
   field, and neither creation route takes a `Request`. A membership gate without
   seeding locks people out of projects they just made.
4. **Background work has no request.** `background.py` imports no `Request`.
   Watcher wakes, orchestrator spawns, and episode continuations dispatch with no
   HTTP request in scope, so a request-level dependency covers none of them.
5. **There is no `APIRouter` in the codebase.** All 78 routes are `@app.<method>`
   inside the `create_app` factory. A router created inside that same factory
   still closes over `catalog`, `store`, and `background_tasks`, so the move is
   contained.

## Slice A — the boundary

### Land these serially, first

Shared contracts. Do not fan out across them.

1. **The membership record** in [`storage/spaces.py`](../../src/rcp/storage/spaces.py).
   `project_members` keyed by `(project_id, user_id)`. Index the new columns
   **below** the `_ensure_column` migration block, never inside the
   `CREATE TABLE IF NOT EXISTS` — an existing database runs the `executescript`
   before migration and crashes on every start. Verify against a copy of a real
   store; every test builds a fresh SQLite file, so a green suite proves nothing
   here.
2. **The backfill.** Projects with no membership rows get every current space
   member, once, guarded so a later start does not reapply it. This is the only
   place this design fails open, and it is deliberate: failing closed locks a
   team out with no administrator to recover.
3. **The membership predicate** in [`core/authority.py`](../../src/rcp/core/authority.py),
   consulted by the request dependency and by `require_apply`. A personal space
   has exactly one member seated the same way, so this is one query with no
   personal-space branch. **Do not add a membership argument to
   `require_dispatch`.**
4. **The wire shape** in [`web/src/types.ts`](../../web/src/types.ts).

### Then fan out

- **The router.** Move every `/api/projects/{project_id}` route onto one
  `APIRouter` declared inside `create_app`, with the membership dependency
  attached once, and `include_router` it. Plus a test that walks `app.routes` and
  fails on any `{project_id}` path declared outside it, with a named allowlist
  for deliberate exceptions.
- **Seeding.** Both creation routes take a `Request`, resolve `acting_user`, and
  write the first membership row. Bind the durable `user_id`. **Do not route this
  through `require_patch_capable_identity`** — that demands a display name, and
  creating a project deliberately does not, which S01, S112, and S116 all rely on.
- **Filtering.** `catalog.cards()` and `GET /api/episodes` both filter to the
  caller's memberships. The board is the sensitive one: it returns node titles
  out of every project's cached graph.
- **404, never 403,** for a non-member's exact project id.
- **The tab.** A project tab polls `/cached/revision` every three seconds
  ([App.tsx](../../web/src/App.tsx)); one that starts 404ing closes itself and
  returns to the index. Reuse the deleted-project path rather than adding one.

## Slice B — the invitations

Depends on A. Land the `project_invitations` table first — a separate table from
`team_invitations`, because a project invitation issues no credential — then fan
out across routes, index cards, the in-project invite control, and leaving.

The fence is the subtle part. Losing membership must fence new admissions exactly
the way **Stop loop** does under invariant 10g: durable, restart-safe, intent
persisted before any unclaimed watcher can win a claim, and the already-authorized
turn finishing normally. Reuse that machinery. Do not add a second fence, and do
not let Resume or Retry walk a fenced episode back into running.

Slice B also adds the revocation drive to
[S100](../acceptance/S100-permission-is-checked-twice.md) — membership is the
permission-that-changes S100 could not previously demonstrate. Add it there and
keep S100 green.

## Invariants you must not break

- **3.** Membership is project truth. Only a human action changes it. No agent
  path writes a membership row, and no agent invites anyone.
- **1 and 2.** Membership is operational. It lives in SQLite, never in
  `.research/`, and replay must keep succeeding with no membership records —
  S100 asserts that today.
- **10g.** Slice B's fence is the existing Stop fence, with its existing
  durability and restart guarantees.

Attribution is the one to think about twice. A Patch snapshots its authorizer at
dispatch ([S99](../acceptance/S99-attribution-travels-with-history.md)). Losing
membership later must not rewrite, annotate, or invalidate history that was
truthful when written.

## Out of scope

Project transfer between spaces ([S98](../acceptance/S98-move-a-project-into-a-team-space.md)),
running work as the space account ([S102](../acceptance/S102-team-runs-execute-as-the-space-account.md)),
console operations including removing another person ([S103](../acceptance/S103-server-operations-are-console-operations.md)),
the desktop **Add team space** form, and SSH transport.

Say plainly in both scenarios, and to anyone who asks: **membership is authority
inside RCP, not confidentiality on disk.** Canonical state is a git repository
that agents read by path, and putting those repositories under the space's own
operating-system account is S102, which is unbuilt. A lab member with a shell
reads any project's `.research` today regardless of membership.

## Done means

S101 passes, and boundary 8 in
[the permission module](../design/identity-permissions-and-agent-profiles.md#acceptance-boundaries)
stops reading "not proven". Then S122 passes, and S100 gains its revocation drive
and stays green.

The blueprint's
[Team space enrollment and sessions](../research-control-panel-blueprint.md#team-space-enrollment-and-sessions)
currently ends by saying the space is the only membership boundary. Slice A
replaces that sentence; slice B completes it. Bump the version both times.

## What the build changed

Two things this plan did not anticipate, both found by running the code rather
than reading it.

**A project can arrive with no creator to seat.** `rcp open` from a console, and
server startup in a team space where nobody has enrolled yet, both register a
project with no acting person. Seating nobody makes it invisible to every member
with no way to invite yourself to it — unrecoverable, with no administrator rank.
So an unclaimed project is claimed by whoever is there: everyone present at
registration, or the first person to enrol afterwards. Recorded in S101.

**A team space refuses a bodyless mutation.** Accept, decline, and leave send no
payload, and the team middleware answers `415 team_json_required` because
JSON-only is what stops a cross-site form forging a mutation. The first client
shipped without a body and was caught by driving a real server, not by the test
suite — every membership test used a trusted principal resolver, which bypasses
that middleware entirely. There is now one test that goes through a real browser
session and pins the 415.

Also worth knowing: `include_router` on this FastAPI version leaves an opaque
`_IncludedRouter` in `app.routes` rather than merging its routes, so a flat walk
of `app.routes` finds **zero** project-scoped routes. The route-enumeration test
descends through `original_router`; a flat version of that test passes while
proving nothing.

## What the grilling changed

The first draft of this handoff said to put one membership predicate in
`core/authority.py` and have both gates call it. That is unbuildable —
`require_dispatch` receives no user and no project — and it would have missed
every read route regardless.

Also absent from the first draft, and added here: the backfill, creator seeding,
the `user_id`-not-display-name binding, the Experiment board leak, 404-versus-403,
the self-closing tab, the fence for background work, the last-member refusal, the
router, and the route-enumeration test. The slice roughly doubled, which is why
it is now two.

Three things the first draft left as open questions are now decided and live in
the scenarios: what a non-member can see (nothing), what happens to the last
member (they cannot leave), and whether revocation removes memberships (no, and
both scenarios say why).
