# Update rollback audit follow-ups

Status: implemented on this branch. Remove this file when the PR merges.

## Where this started

On 2026-09-24 the v0.3.26 team-server update rolled back. Rollback replaced each
root with the old release's `prepare` payload, which held only the database and
backup-typed project files. `providers/`, `jobs/`, `transfer-inbox/`,
`cursors.json`, and empty `.research` directories were lost and copied back by hand.

## Already fixed on main before this branch

- #187 and #191 (supervisor 0.1.7 and 0.1.8): every update takes a whole-root
  `cp -a` snapshot of each stopped root before `prepare` runs, and rollback renames
  that snapshot back. Backup exclusions no longer shape rollback.
- Regression coverage exists: `test_real_project_payload_restores_schema_graph_stage_and_attachment`
  and the installed-upgrade CI job, which forces a real rollback and checks the
  provider token, `jobs/`, `cursors.json`, and an empty branch `patches/`.
- Audit item 2: remote projects are never opened before the release commits;
  local writes during probation are undone by whole-root rollback.
- Audit item 4: control-socket refusals carry their code and message, and the
  update dry run shows the application's refusal reason.
- Audit item 5: backup logs the cause when it cannot capture a project.
- Audit item 6: the installed-upgrade CI job commits a real update and reads a
  project through the restarted service.
- The September quarantine folders are gone from the team server (searched all of
  `/home/rcp` on 2026-10-14), so no cleanup step was added.

## Changed on this branch

- Audit item 3: update validation replays every captured branch head on the copy
  and refuses when one differs, as protected restore already did.
- Real-data check before shipping (2026-10-14, on copies): all 7 team projects
  (12 branches) and all 5 personal projects (4 branches) replay to their recorded
  main and branch heads, including on capture-only copies.
- The shared deployment fixture now carries an empty branch, so the restore test
  also covers an empty branch `patches/`.
- RCP 0.4.21.

## Not changed

- Audit item 1: a provider sign-in running when maintenance starts is not drained.
  Stopping the service ends it, and the next start marks it interrupted. Login state
  is not part of the read model validation compares, so it cannot fail an update.
- Post-restore completeness check: #191 removed the rescan on purpose. Rollback is a
  rename of the stopped copy, and CI proves the copy on real agent scratch.
