# Update rollback audit follow-ups

Status: design, waiting on two answers. Nothing implemented yet.

## Where this started

On 2026-09-24 the v0.3.26 team-server update rolled back. Rollback replaced each
root with the old release's `prepare` payload. That payload held only the database
and backup-typed project files. Live-only state was lost: `providers/`, `jobs/`,
`transfer-inbox/`, `cursors.json`, and empty `.research` directories, including
empty branch `patches/`. The next backup was partial and the next update refused.
The data was copied back by hand from `.rcp-quarantine-*`.

## What main already does

Two PRs merged the day after fixed the core bug:

- #187 and #191 changed every update to take a whole-root `cp -a` snapshot of each
  stopped root before the old release's `prepare` runs
  (`SystemRuntime.snapshot`, `checkpoint.create_stopped_snapshot`).
- Rollback renames that snapshot back into place. It no longer uses the prepare
  payload, so backup exclusions no longer shape rollback.
- `prepare`'s roots are only checked against the snapshot (`check-roots`).
- Quarantines now sit in the supervisor's checkpoint area, not in project
  repositories, and retention deletes them with their checkpoint.
- An incomplete pre-update backup is a warning, not a refusal.
- `tests/test_supervisor_checkpoint.py` covers empty directories and special entries;
  the installed-upgrade CI gate forces a real rollback on agent scratch.

So the old release no longer decides what rollback restores. Updates from any
release that has the new supervisor are protected.

## What is still open

### A. Post-restore completeness check (question 1)

#187 added a post-restore rescan. #191 removed it on purpose: per-entry rules had
refused real updates on uv hardlinks and lock files. Today nothing checks that
the restored tree holds every entry the stopped tree had. Rename makes loss
unlikely, but `cp` completeness is trusted, not proven.

Options:

1. **Check at snapshot time (recommended).** After `cp -a`, compare the set of
   relative paths and entry types of each source root against its copy. Any
   difference refuses the update before the release switches. Names and types
   only, no modes or hashes, so nothing RCP creates can trip it. Rollback is a
   rename of that proven copy, so "restored equals pre-update" holds by construction.
2. **No runtime check.** Rely on rename semantics and the CI rollback gate.
3. **Rescan after restore.** Record the path list at snapshot, compare after
   rename, stay stopped on a mismatch. Same data as option 1, but it only fires
   after the damage.

### B. Old `.rcp-quarantine-*` folders (question 2)

The v0.3.26 rollback left quarantine folders inside three project repositories.
Their checkpoint journals are gone, so no current code knows they exist.
Retention only removes workspaces that a journal names.

Options:

1. **Retention removes them (recommended).** Supervisor retention looks beside the
   data directory and each registered project `.research` root for entries named
   `.rcp-quarantine-<16 hex>-<n>` that no retained checkpoint names. It removes them
   and lists each removal in its result. Runs on the next update or `server prune`.
2. **Report only.** Retention lists them under `left_alone`; doctor shows a warning.
   A human removes them with a new supervisor command.

## Other audit items: decisions

| # | Item | Decision |
| - | ---- | -------- |
| 1 | Maintenance does not stop a running provider sign-in thread | **Fix.** `pause_update_runtime_owners` cancels running sign-ins and waits for their threads, like `terminals.end_all`. The member sees a canceled sign-in. |
| 2 | Writes before the startup fence during probation | **Close.** Remote writes before commit are already blocked (`verify_live_application`, test `test_live_check_opens_a_remote_project_only_after_the_release_commits`). Local writes (migrations, login settlement, cache) are undone by whole-root rollback. |
| 3 | Update validation does not compare branch heads | **Fix.** Candidate validation replays each captured branch and refuses when its head revision differs from `capture.branch_heads`, as `restore.py` already does. |
| 4 | Supervisor drops the application's refusal reason | **Fix the rest.** Control-socket refusals now carry code and message. Failed `inventory`, `prepare`, and `validate` still say only "inspect <log path>"; include the captured `diagnostic` in that message. |
| 5 | Backup capture hides per-project failure reasons | **Close.** `_capture_or_preserve_failure` now logs the exception type and redacted text. |
| 6 | Startup after `candidate_chosen` is never driven by the old-data gate | **Fix.** The every-release gate starts the switched app through its lifespan (`TestClient`) and reads a project, not just `create_app` plus direct calls. |

## Regression test

The prepare payload is no longer the rollback source, so the test belongs to the
snapshot. Extend `_case` in `tests/test_supervisor_checkpoint.py` with a provider
token file, a `jobs/` file, `cursors.json`, and empty `facts/`, `paper/`, and
`branches/<id>/patches/`, and assert they survive `restore_checkpoint`.

## Checks

- `uv run pytest -n0 tests/test_supervisor_checkpoint.py tests/test_supervisor_runtime.py tests/test_supervisor_retention.py tests/test_application_deployment.py`
- Provider sign-in and maintenance tests for item 1.
- `uv run ruff check` and `uv run pre-commit run --files` on changed paths.
- The exact-base gate (item 6) runs in PR CI.
- Update `docs/specs/server-and-machine-operations.md` for A, B, 1, and 3.
