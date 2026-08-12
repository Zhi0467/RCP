# Slice — one real person can authenticate to a team space

**Date:** 2026-08-12
**Scenario:** [S96](../acceptance/S96-joining-a-team-space.md) — **its server half
only.** The nine decisions below were confirmed by the human 2026-08-12 and are
not yet written into S96 itself; **rewrite S96 first** (see "Before you code").
**Design:** [Team authentication and membership](../design/team-authentication-and-membership.md)
and [Spaces and project homes](../design/spaces-and-project-homes.md).

Read [`AGENTS.md`](../../AGENTS.md), then both design modules, then
[S118](../acceptance/S118-identity-and-membership-start-at-the-index.md), which
already built the index surface this slice fills in.

## Why this is the first team slice

[app.py](../../src/rcp/api/app.py) resolves the acting member through a
`trusted_principal_resolver`, and when that is `None` every team action returns
`401 team_identity_required`. Nothing fills it. S112 built the durable identity
records and explicitly disclaimed "the enrollment, invitation, token, or
browser-session lifecycle in S96", and S118 placed the index control with its
team actions visibly disabled. This slice is what makes that seam real. Every
other team scenario is unbuildable until it is.

## What already exists

- `Store(path, space_kind="team")` in [storage.py](../../src/rcp/storage.py), with
  an immutable `personal | team` kind on `space_identity` — but **no CLI or API
  path creates a team space**, so today it is reachable only from tests.
- `space_identity` holds `space_id` and `space_kind` and **no name**.
- Durable per-person `user_id` with a mutable display name, and
  `require_patch_capable_identity`, which already refuses an attributed write
  until a name is chosen.
- The index identity card and the disabled team controls from S118.

## Decided 2026-08-12 — do not re-litigate

1. **Scope is S96's server half.** The "Add team space" client form, the SSH
   transport, and OS credential storage are a later slice. S96 sheds that UI
   path; its driver widens from `pytest + api` to include `browser` for the login
   boundary below.
2. **`rcp space init --team` creates the space** and prints the one-time
   bootstrap code once, interactively, to whoever ran it. `serve` never prints a
   secret. This deviates from the design doc's "on first start" wording because
   under systemd there is no terminal and the code would land in the journal —
   the one place the design says secrets must never be. Update that doc with the
   reason.
3. **Tokens are `secrets.token_urlsafe(32)` with an `rcp_` prefix**, stored as an
   indexed SHA-256 hash, compared in constant time. No KDF: there is no
   low-entropy human secret to grind, so a memory-hard hash buys nothing and adds
   a dependency to a deliberately tiny list. The prefix is not security — it makes
   a leaked token greppable.
4. **Sessions are server-side rows** with an `HttpOnly`, `Secure`,
   `SameSite=Lax` cookie and a sliding idle expiry (14 days). Revocation and
   rotation delete that member's rows, which is what makes "revocation
   invalidates existing sessions" true rather than eventual. `Lax` rather than
   `Strict` because the desktop shell sets the cookie and then navigates, and a
   `Strict` cookie is not sent on that navigation. Combined with JSON-only
   mutations, a cross-site form cannot forge a request. An explicit CSRF token is
   additive if a security review wants one.
5. **The space gets a required name at `init`, mutable afterwards by any member**
   — the same rule S112 set for people: the id is identity, the name is a label.
   Required so an invitation block is never nameless.
6. **The invitation block shows its expiry; a member sees only the invitations
   they created.**
7. **Rotation and revocation never touch running work.** A task already
   authorized keeps running and its patch lands on the `authorized_by` snapshot
   taken at dispatch. Stopping someone's work is member *removal*, a console
   operation in [S103](../acceptance/S103-server-operations-are-console-operations.md).
8. **High-entropy pasted codes plus a per-code attempt lockout.** No IP-based
   limiting anywhere: the client reaches the backend through an SSH tunnel, so
   every attempt arrives from localhost and IP logic would be a lie.
9. **Include the browser login boundary** — a page on the team server where a
   member presents their token once and receives the session cookie. Without it
   the slice ends with nothing a person can use.

**Non-negotiable:** the personal-space path grows no login. `acting_user` keeps
returning the local owner, and none of this appears unless the space kind is
`team`.

## Before you code

Rewrite [S96](../acceptance/S96-joining-a-team-space.md) to match the nine
decisions, and get it confirmed. This repo's rule is that the scenario is settled
first; nine decisions living only in a handoff is exactly the drift that rule
exists to prevent.

## Land these serially, first

1. **The schema** in [storage.py](../../src/rcp/storage.py): a space name, plus
   tables for bootstrap codes, invitations, member tokens, and sessions, with
   their atomic state transitions. Every new column goes in the
   `CREATE TABLE IF NOT EXISTS` **and** is indexed only in the migration block
   below the `_ensure_column` calls, or every existing database fails to open
   with "no such column" while all tests pass on their fresh files. Verify
   against a copy of a real store.
2. **The `space init` subcommand** in [`__main__.py`](../../src/rcp/__main__.py),
   beside the existing `serve` and `open` parsers.
3. **`web/src/types.ts`** for the space name and the login boundary.

Then fan out across service, web, and tests.

## Invariants you must not break

- **Invariant 8.** One RCP process per data directory. `space init` creates a
  directory; it does not take the lock or start serving.
- **Invariant 3.** Only human UI actions hold authority. A token grants product
  authority only — never backup, restore, update, or member removal, which
  require operating-system privilege on the machine. RCP defines no admin role.
- **Equal members.** No rank hierarchy. A member may not authenticate as, read
  the token of, rotate the credential of, or submit work attributed to another
  member.
- **Redaction is structural.** The token never travels past the exchange
  endpoint. Assert it by grepping the store, logs, prompts, task receipts, and
  canonical history after a full enrollment.
- **The space kind is immutable.** `init` cannot convert a personal space.

## Out of scope

Project membership and invitations ([S101](../acceptance/S101-project-membership-and-invitations.md)),
the desktop client and SSH transport, member removal and every other console
operation, backups, project transfer, and the multi-space index. Also out: the
Linux deployment itself — the server half is plain Python and is built and
driven on the developer's own machine. The service account, systemd, and install
paths belong to [S95](../acceptance/S95-durable-team-space.md) and S103.

## Still undecided — raise, do not answer by accident

Whether a client should detect that a familiar `space_id` was rolled back to an
older restored archive. The design accepts split-brain; rollback detection is
additional machinery and is not decided.

## Done means

The rewritten S96 passes: `pytest + api` for bootstrap, enrollment, single-use
codes, expiry, lockout, rotation, revocation, restart without re-enrollment, and
redaction; `browser` for the login boundary and for a team space being usable by
an attributed member afterwards.

Backend `uv run pytest` and `uv run ruff check src tests`; web
`npm --prefix web run build` and `npm --prefix web test`; then `git add -A` and
`uv run pre-commit run --all-files`.
