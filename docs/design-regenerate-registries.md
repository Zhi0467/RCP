# Design: one command regenerates the schema-derived registries

Delete this file before merge. The frozen-line rule moves to the comment above
`SUPPORTED_RESTORE_DATABASE_SCHEMAS`; the command gets one `AGENTS.md` line.

## Problem

A PR that changes the SQLite schema fails CI until someone hand-edits three
derived lists:

| List | Owner today | Churn per schema PR |
| --- | --- | --- |
| Restore schema digests | `SUPPORTED_RESTORE_DATABASE_SCHEMAS` in `src/rcp/server_ops/restore.py` | about 13 new digests |
| Pinned live digest | last assert of `tests/test_backup_integrity.py` | 1 constant |
| Python 3.9 shipped set | `[tool.ruff.per-file-target-version]` in `pyproject.toml` | 1 line when a shipped file is added |

Agents recompute the digests with a throwaway pytest plugin each round and
paste them by hand. A wrong paste costs one CI round.

## Decisions (human, 2026-11-14)

1. A digest is frozen once it is on merged `main`. Every `main` merge becomes
   an installable `build/N`, so a server may hold backups that carry it.
2. The backup-integrity test pins a frozen fixture, not the current schema.

## The command

`uv run python scripts/regenerate_registries.py` takes no arguments. It
rewrites the py39 block first, then the restore digest block. It prints one
line per file: `updated <path>` or `unchanged <path>`. A second run changes
nothing. Any stop below raises `SystemExit` with a one-line reason and leaves
the restore file untouched.

The script inserts the repository root into `sys.path`, as
`scripts/bench_graph_history.py` does, and imports its logic from `tests`.
It holds no logic of its own beyond reading, calling and writing.

### Python 3.9 shipped set

This step is static and takes under a second. It runs first so a shipped-file
change never waits on the slow step.

The derivation (`_Shipped`, `_ReaderScan`, `_scan`, `_shipped_sources`,
`_import_violations`, `_relative`, their helpers, constants and the module
docstring) moves unchanged from `tests/test_shipped_sources.py` to
`tests/shipped_sources.py`. The test file imports what it uses.

`rewrite_py39_block(pyproject_text: str, shipped: list[str]) -> str` in the
same module replaces the contiguous run of `"…" = "py39"` lines with one exact
path per shipped file, sorted. The three globs there today expand to exact
paths once. The command prints each entry it adds or removes, so a removal
caused by a reader shape the scan stopped recognising is seen, not buried. The function raises `ValueError` unless it finds exactly one such
run. The command stops when the scan reports a non-literal reader, as the
test does. The test's mismatch assert names the command.

### Restore digests

`tests/restore_schemas.py` owns two functions.

`assert_restore_schema_registered(digest: str) -> None` replaces the ten
`assert … in SUPPORTED_RESTORE_DATABASE_SCHEMAS` sites (eight in
`tests/test_restore_archive_compatibility.py`, two in
`tests/test_notification_storage.py`).

- When `RCP_RESTORE_SCHEMA_RECORD` names a file, it appends the digest as one
  line and returns.
- Otherwise it asserts membership, reading the set through the `restore`
  module at call time, with an explicit message that names the command.

No plugin, no patched constant: production code runs unmodified during
recording. `tests/test_application_restore.py` keeps its own monkeypatch and
does not change.

`splice_restore_block(working_text: str, base_text: str, observed: set[str]) -> str`
does the rewrite.

1. The block is the text between the line
   `SUPPORTED_RESTORE_DATABASE_SCHEMAS = frozenset(` + `    {` and the closing
   `    }` + `)`. It raises `ValueError` unless each text holds exactly one.
2. `new = observed - digests(base block)`.
3. The working block becomes the base block byte-for-byte, then one line per
   digest in `sorted(new)`, formatted `        "<digest>",` so `ruff format`
   leaves it alone.

`base_text` is the file at the tip of `origin/main`, not at the merge-base.
The result is always "main's block, then this branch's new digests", which is
also the correct resolution of a merge conflict in the block. The command
gives the same answer before, during and after merging `main`.

The command wires them:

1. `git fetch origin main`. It stops when the fetch fails, so a stale ref
   can never make main's digests look like the branch's.
2. `base_text = git show origin/main:src/rcp/server_ops/restore.py`.
3. It runs `[sys.executable, "-m", "pytest", *files]` with the repository
   root as `cwd` and `RCP_RESTORE_SCHEMA_RECORD` naming a file in a fresh
   temporary directory. `files` is every `tests/test_*.py` that names
   `assert_restore_schema_registered`.
4. If pytest fails, or nothing was recorded, it stops. A failure here is a
   real test failure, not a missing digest.
5. It splices and writes. It prints the SQLite library version and every
   digest it added or dropped.

Consequences:

- Frozen lines and their comments keep their bytes. The command cannot
  remove or reorder them.
- A digest the branch added earlier and no longer produces is dropped. So is
  a comment hand-written beside a branch digest; generated lines carry none.
- Two schema PRs both append to the block, so the second to merge conflicts
  there. Resolution: take either side, finish the merge, run the command.
- A digest exists only while a test asserts it. A hand-added digest with no
  asserting test is dropped and printed.
- The run takes about five minutes (probed on `main`: 58 distinct lookups,
  all already registered). Most of that is the server-upgrade fixture loop.
- A digest that only the production gate at `restore.py:1611` sees, in a test
  that never calls the helper, is not recorded. Today the fresh current schema
  is the only digest those tests reach, and the helper sites cover it. CI
  remains the backstop.

Forgetting to run the command still fails CI, now with a message that names it.

### Pinned live digest

`test_database_schema_digest_is_shared_by_live_and_immutable_readers` keeps
`live == immutable` and drops the constant. Schema PRs no longer touch the
file, and the command does not write it.

The hashing function stays pinned by the existing assert at
`tests/test_notification_storage.py:50-53`: the frozen
`pre-notifications-v27` fixture must hash to `0f0456be…fbb967`. No second
copy is added.

## Open: enforce the frozen line in CI

Nothing fails today when a merged digest disappears: tests look up only the
current era's shapes. The command cannot drop one, but a hand edit or a bad
conflict resolution can. A CI step could fetch `origin/main` and check that
its block is a byte prefix of the PR's block. That is a CI change and needs
the human's yes; it is not part of this design unless approved.

## Not regenerated

- **Writer census** (`tests/lifecycles/catalog.py`, machine `writers`). Each
  entry carries a hand-written owner, status type, rule and reason. The scan
  already prints the missing `module:function` names. Nothing more is
  derivable.
- **Upgrade fixture registry** (`EXPECTED_BOUNDARIES`). A new boundary is a
  deliberate capture of the old shape. Out of scope.

## Files

| File | Change |
| --- | --- |
| `scripts/regenerate_registries.py` | new: entry point |
| `tests/restore_schemas.py` | new: assert helper and `splice_restore_block` |
| `tests/shipped_sources.py` | new: derivation moved from the test file, plus `rewrite_py39_block` |
| `tests/test_regenerate_registries.py` | new: the two pure functions and the helper's two modes |
| `tests/test_restore_archive_compatibility.py`, `tests/test_notification_storage.py` | call the assert helper |
| `tests/test_backup_integrity.py` | drop the constant |
| `tests/test_shipped_sources.py` | import the moved derivation; message |
| `src/rcp/server_ops/restore.py` | reword the comment above the set: merged digests are frozen, the command adds the rest |
| `pyproject.toml` | reword the comment above the py39 block; globs expand to exact paths |
| `AGENTS.md` | one Verification line naming the command (229 of 230 lines today) |

No behavior in `src/` changes. The comment ratchet counts comments, so both
comments are reworded in place, not added. New files carry no comments.

## Tests

- Splice: a base block with comments plus observed digests gives the base
  block byte-for-byte followed by the sorted new digests; a branch-only digest
  that is not observed disappears; splicing the result again is a no-op.
- Splice refuses a text with zero or two blocks.
- py39 rewrite: globs and stale entries are replaced by the sorted exact
  paths; other tables in the file keep their bytes.
- Helper: with the variable set it appends and does not fail on an unknown
  digest; without it an unknown digest fails.
- End to end, once, by hand: add a throwaway column on a scratch commit, run
  the command, confirm the restore tests pass and the diff holds only new
  digest lines; then drop the commit.
