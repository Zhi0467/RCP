# Updates accept what writers store

Status: approved, in progress on one PR. Implemented: backup, restore and
transfer readers accept stored values, with a real-use corpus; update preparation
warns on backup inventory failures while preserving identity and path refusals;
the dry run cleans up its workspace and bundle; CI seeds historical follow-ups
through compatible Work authority. Remaining: installed-upgrade CI for every
supported base and the merged build's team-server rehearsal.

## Why

Twice in one night a promoted build passed CI and every candidate check, and
then the team server refused the update:

1. Keep stores only `kept_at` since #226. Backup still wanted a kept filename.
2. Answered-question follow-up turns get bare-hex UUID5 ids since #235.
   Backup wanted the hyphenated spelling.

Both are one class. A reader (backup, restore, update preparation) checked a
stored value with its own stricter rule. CI data comes from test setup code,
so it never held the shapes real use writes. And the update borrowed backup's
record checks, so one record backup disliked refused the whole update.

## Settled decisions

- Core and side modules are separate. The update must work. Backup, restore
  and other side modules deserve fixes, but never block an update; a side
  module that fails is reported as a named warning and the update continues.
- Smaller cut tonight. No new supervisor/app contract versions, no
  capture-free stop command, no legacy bridge, no `--build N`, no module
  framework. Those stay possible later.
- Follow-up id minting stays bare hex. Changing it would duplicate follow-ups
  for questions answered before an update.

## Plan

### 1. Readers accept what writers store (done)

- Backup, restore and transfer check only what restore needs to be safe:
  plain filename, absolute path, length. The secret redaction filter is for
  diagnostics, never a validity rule for stored data (it matched `sk-learn`).
- Operation ids: one helper accepts UUID4 or UUID5, hyphenated or bare hex.
- Machine aliases and project names: backup calls the config writers' rules.
- A legacy project row backup cannot read marks only that project uncaptured.
- `tests/real_use_corpus.py` writes awkward but valid data through the app's
  own writers. Backup, restore and transfer must all accept it.

### 2. Backup can never block an update

- When backup cannot read a project, the update still finds that project's
  folder from the projects table. The supervisor's byte-for-byte checkpoint
  covers it, so rollback stays complete.
- The pre-switch copy check skips only that project. The update output names
  it as a warning instead of refusing.
- Identity, ownership, migration, proof mismatches on proven projects, and
  path safety stay hard refusals.

### 3. The dry run does the real preparation

- `rcp server update` without `--confirm-target` installs the target and runs
  its `update-rehearsal` on a consistent online copy: the same inventory,
  preparation and application proof as the real update. The service keeps
  running; nothing switches. It prints ready or the exact refusal, plus
  warnings, then deletes the copy.
- Pre-promote runs the same command from the build's wheel on each team
  server (`docs/release.md`).
- Supervisor version moves to 0.1.12, so an older supervisor updates first.

### 4. CI drives the real path

- The installed-upgrade harness seeds a Keep and an answered-question
  follow-up through real routes, runs the dry run, and forces a backup
  rejection for one project: the real update must still commit and warn.

## Verification

- Corpus tests fail on the parent commit and pass on the branch.
- Installed upgrade from every base passes with the new steps.
- Before promoting: the dry run of the merged build passes on the team server.

## Release

Version stays 0.4.14 (build 1869 was never promoted).
