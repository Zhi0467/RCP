# Updates accept what writers store

Status: design, awaiting a start. Nothing implemented yet. The branch holds
one small uncommitted fix (operation ids, below).

## Why

Twice in one night a promoted build passed CI and every candidate check, and
then the team server refused the update:

1. Keep stores only `kept_at` since #226. Backup still wanted a kept filename.
2. Answered-question follow-up turns get bare-hex UUID5 ids since #235.
   Backup wants the hyphenated spelling.

Both are one class: a **reader** (backup capture, restore, update preparation)
checks a stored value with its own, stricter copy of a rule. CI data comes
from test setup code, so it never holds the shapes real use writes. The
nightly backup went partial within hours each time, but only `doctor` says so.

An audit found more of the same class, not yet hit:

- Backup screens names, paths and filenames with the secret redaction filter.
  Its `sk-` pattern matches `sk-learn`, so `/home/a/sk-learn-exps` as a
  project folder makes that project uncaptured, and fails restore outright.
- Backup requires machine aliases and project names in a stricter shape than
  config and provisioning accept.
- One legacy non-UUID4 project row fails the whole capture, not one project.
- The result-view transfer check still rejects hex operation ids (latent).

## Plan

### 1. One rule per stored value, owned by the writer

- Each stored value's shape rule lives on the store model that writes it.
  Backup, restore and transfer call that rule; they keep no copies.
- Operation id: UUID4 or UUID5, hyphenated or bare hex. The rule moves onto
  `AgentTaskRecord`, so a writer cannot store anything else. Minting stays as
  is: changing the follow-up spelling would duplicate follow-ups for questions
  answered before the update.
- Names, paths, filenames: the redaction filter is for diagnostics, not data.
  Backup checks only what restore needs to be safe: plain filename, absolute
  path, length bound.
- Aliases and project names: backup accepts what config and provisioning
  accept, by calling their rules.
- A project row backup cannot read marks only that project uncaptured.

Enforcing test: one **real-use corpus** fixture, written through the real
write paths with awkward but valid values (`sk-learn` names and folders,
follow-up ids, store-kept artifacts, a legacy project id). Backup capture,
restore and offline preparation must all accept it. The backup tests and the
installed-upgrade seed both use it.

### 2. The update dry run does the real preparation

- Today `rcp server update` without `--confirm-target` only shows the target.
  It will also run the target release's full preparation on a consistent
  online copy of the database, with the service still running: inventory,
  project-file capture, migration, candidate start, application proof. It
  then deletes the copy and reports pass or the exact refusal. Admission is
  never closed and nothing switches.
- A dry-run-only `--build <N>` rehearses an unpromoted build, checked against
  its manifest. A real update still takes only promoted releases.
- Pre-promote becomes "dry-run build N on each team server", replacing the
  hand-written `rehearse_inventory` step in `docs/release.md`, which is removed.
- This touches the supervisor, so its version moves to 0.1.12.

### 3. Partial backups are loud

- The app's server card shows when the last nightly backup is partial: which
  projects and the fixed reason. `doctor` keeps reporting it.

## Verification

- Corpus tests fail on main for every reader listed above, pass on the branch.
- Installed-upgrade from every base keeps the corpus through update and
  backup.
- Before merge: dry-run this PR's candidate build on the team server. It
  must pass on real data.

## Release

Version stays 0.4.14 (build 1869 was never promoted). After merge: candidate
checks, dry-run on the team server, promote, update.
