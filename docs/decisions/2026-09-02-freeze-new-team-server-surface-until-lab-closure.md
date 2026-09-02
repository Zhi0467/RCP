# Freeze new team/server surface until lab closure

**Status:** accepted by the human on 2026-09-02
**Current implementation contract:**
[`../handoffs/handoff-2026-08-27-dev-team-space-and-server.md`](../handoffs/handoff-2026-08-27-dev-team-space-and-server.md)

## Decision

Until the active dev-team-space-and-server handoff meets its closure condition
and is archived, add no new team/server lifecycle, transfer phase, privileged
operation, or desktop/server protocol surface.

Work that closes the existing drives or corrects defects those drives expose is
allowed. New research-control features stay out of the team infrastructure path
unless that integration is necessary to close the handoff.

The exit is the existing handoff's archive condition. This decision does not add
an intermediate milestone or redefine completion for that work.

## Why

Personal mode already supports evaluation of RCP's core research workflow, while
team mode introduces a second infrastructure product spanning enrollment,
provisioning, transfer, backup and restore, desktop compatibility, remote
execution, and source deployment. Those surfaces now affect ordinary startup,
storage, routing, identity, and release behavior before one complete reference
deployment has closed. Holding the boundary steady lets the remaining lab drives
provide end-to-end evidence before more lifecycle state is added.

## Rejected alternatives

- Delete team mode: the problem is sequencing and closure evidence, not the
  accepted team-space direction.
- Keep adding surface in parallel: this would expand compatibility obligations
  while the current desktop, server, transfer, and recovery paths still lack
  their complete shared reference drive.
