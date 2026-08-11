# Internal design modules

These documents record the confirmed working design for RCP team spaces. They
replace the earlier identity and team-space handoffs with one document per
module:

- [Spaces and project homes](spaces-and-project-homes.md) — where authority,
  execution, configuration, and canonical project state live.
- [Team authentication and membership](team-authentication-and-membership.md)
  — how a person joins a team space and remains individually attributable.
- [Identity, permissions, and agent profiles](identity-permissions-and-agent-profiles.md)
  — how RCP admits execution and semantic changes.
- [Team server operations](team-server-operations.md) — installation, updates,
  backup, restore, and routine administration.
- [Team API compatibility](team-api-compatibility.md) — how the desktop client
  reaches a team backend.

The design decisions in these files are confirmed, but they are not yet part of
the canonical blueprint and do not authorize implementation by themselves. Per
[`AGENTS.md`](../../AGENTS.md), each user-visible promise still needs a
human-confirmed acceptance scenario. The resulting design change then edits
[`research-control-panel-blueprint.md`](../research-control-panel-blueprint.md)
in place and bumps its version before code lands.

Each module keeps its own remaining design details beside the decisions they
refine. They are not duplicated in the repository-wide open-question tracker.

## Decisions that cut across the modules

A 2026-08-09 grilling session settled these, and each one removed machinery
rather than adding it. They are recorded here because reading any single module
would otherwise make them look like local choices.

- **The team backend runs under a dedicated `rcp` operating-system account**
  that exclusively owns the data directory and locally homed state repositories.
  RCP borrows the machine's privilege system instead of defining an
  administrator role, so members stay equal and a leaked member token cannot
  reach backup, restore, update, or removal.
- **A project carries its own identity and home space in canonical history.**
  Catalogs cache them. This is a nameplate, not version control: there is no
  fork, no branching, and no merge concept, because each project already lives
  in a git repository with an append-only patch log inside it.
- **Transfer is personal → team, one-way**, and it moves authority *and*
  repository ownership without moving files.
- **Selecting a team space navigates to that server's own interface.** This
  removes client/server skew for the application, removes any need for a page to
  hold a credential, and shrank
  [Team API compatibility](team-api-compatibility.md) to a handshake.
- **There is no application-level CLI in this release.** The launcher and the
  console operations remain; the "every meaningful operation" parity promise is
  withdrawn.
- **Team runs execute as the service account**, because agents read canonical
  `.research` by path and Work must write repositories the account owns.
- **A Proposal is the orchestrator escalating to a human.** No agent approves
  one. Sub-agent scoping is a prompt contract and is labelled as such.
- **Backups never pause work**, because WAL snapshots and append-only history
  are consistent by construction.

## Acceptance scenarios

The promises above are being turned into scenarios in
[`../acceptance/`](../acceptance/README.md). S97 and S99, plus their narrower
S111–S112 prerequisites, were implemented and passed on 2026-08-11. The
remaining team-space and campaign scenarios are still proposals and do not
authorize implementation.
