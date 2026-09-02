# PR #7 verification — 2026-09-02

Branch `audit-remediation-2026-09-01` at `970fad0` versus `origin/main`. 224 files,
+9473 / −4959. Seven read-only verifiers, one per area, each read the full diff of
its area and checked every finding for: fixed with a regression that fails on
main, partially fixed, not fixed, or closed as an accepted exception in the
archived handoff. I re-verified the blocking items by hand.

## Checks I ran

| Check | Result |
| --- | --- |
| `uv run pytest` (full) | pass, only pre-existing skips |
| `uv run ruff check src tests packaging web/src-tauri/scripts` | pass |
| `uv run pre-commit run --all-files` | pass |
| `npm --prefix web run build` | pass |
| `npm --prefix web test` | 480 / 480 |
| GitHub CI (pytest 3.11 + 3.12, lint, web, old-data upgrade) | pass |
| GitHub Desktop (Rust) | pass |

## Verdict

The implementation is real. Every High finding and almost every Medium finding
has code plus a focused regression that fails on `main`. The scope reductions are
declared in the handoff's "Accepted exceptions" rather than hidden. Tests and CI
are green.

**Do not merge yet.** The fixes introduced one production-blocking regression
and a handful of smaller ones, and the handoff was archived while its closure
condition was unmet.

## Blocking

**B1. Restore can no longer activate on a real machine.** The H2 fix adds
`instance_lock(data_dir)` to `admission()` (`server_ops/restore.py:1587`).
`admission()` is re-entrant and `execute` wraps the entire run in it
(`restore.py:829`), including `activate_replacement`, which calls
`service_control.enable_and_start()` (`restore.py:~2608`). The `rcp serve` that
systemd starts tries the same lock with a zero timeout, gets `InstanceLockHeld`,
and enters `_replace_existing_server` (`__main__.py:410`), which tries to
SIGTERM the restore process. The test at
`tests/test_server_restore_activation.py:320` calls `activate_replacement`
directly, outside `admission()`, so it cannot see this. Fix: release the
instance lock before step 11, and add a test that drives
`prepare_restore_command` through activation under `admission()`.

## Fix before merge

- **F1. `_require_restored_target` can raise an unhandled `FileNotFoundError`.**
  `restore.py:3405-3409`: the new permitted-set check accepts a data dir holding
  only `rcp.lock` and sidecars, then `target.lstat()` runs with no guard. On
  main the exact-entry check made that unreachable.
- **F2. Contended remote lock waits are now capped at 30 s.** `transport/state.py:1561`
  dropped the `contended` flag entirely, so `STATE_LOCK_ATTEMPT_TIMEOUT_SECONDS`
  now bounds the run lock held for a whole remote agent run. A second run
  queued behind a first now fails with `StateUnavailable` after 30 s and marks
  the workspace unreachable. M6 asked for a deadline, not this one.
- **F3. Two-read race can 500 the cross-project episodes index.** `api/index.py:126`
  reads the projection target-agnostically, `:206` re-reads it main-only, no
  lock between. A concurrent Experiment start raises an uncaught `ValueError`
  at `:286`. New in this diff.
- **F4. `record_chat_stage_layout` is an unlocked read-then-insert**
  (`storage/agent_tasks.py:707-798`). A duplicate marker row makes every later
  turn of that chat raise "multiple layout markers" with no repair path.
  Hard to reach today; same shape M14 just fixed one file over.
- **F5. `_proposal_action` indexes `[0]` into op lists with no length guard**
  (`core/attention.py:190,208,225,235,247`) and now runs inside canonical
  projection on every prepare and replay. Blocked by admission validation today,
  but replay mode and human-authored proposals skip that validator.
- **F6. `forceDagSemanticKey` is computed in the render body**
  (`web/src/hooks/useForceDag.ts:86`), so two JSON-stringify sorts run on every
  animation frame while the DAG settles or a node is dragged. Wrap in `useMemo`.
- **F7. Attention ids and the attention membership graph come from different
  sources for one render** (`web/src/App.tsx:1811` vs `:1816`), one synchronous
  and one from a layout effect. A stale preview naming a draft-created decision
  node makes `decisionsAwaitingChoice` throw into the root error boundary.
- **F8. Two spec sentences now contradict the code.**
  `docs/specs/server-and-machine-operations.md:1211` still says restore requires
  an exactly empty data directory. Nothing documents that the control protocol
  now accepts version 8 alongside 9 (`server_ops/control.py:47`).
- **F9. Handoff archived with its closure condition unmet.** The status admits
  the full rerun was deferred, but never mentions the "affected UI paths were
  driven in the served app" half, which also did not happen. The plan promises
  a "Rejected" list; the file has "Accepted exceptions" instead. The full pytest
  run is now done (this report); the UI drive is still owed.

## Decide (behavior changes the audit did not ask for)

- **D1. Retry now deletes the previous attempt's `patch.json`, `watch.json`,
  and message files.** `work_turn_runtime.py:216-226` puts `retry` in the
  clearing set; on main `retry` reused the checkpoint and did not clear. Patch
  text is recorded to the database only once `apply_work_patch` is entered
  (`work_turn_runtime.py:490`), so an attempt that failed before settlement
  loses its only copy on Retry. This is the 10c-versus-9 tension: my handoff
  text asked to clear "every new logical turn", and whether Retry is one is a
  product call. The test was renamed from "ignores unchanged predecessor
  outputs" to "clears predecessor outputs" to match.
- **D2. An unreadable `patch.json` now aborts an Experiment-loop turn before
  watcher arming** (`runs/tasks/experiment_loop.py:1791-1794`). A turn that
  settles under the ceiling with no observer armed leaves the episode live with
  nothing to wake it. No test covers the branch.
- **D3. Recovery of a pending child admission now defers indefinitely instead of
  cancelling** when provider or host do not resolve, because the merged
  validator raises `AutoResearchCommandUnavailable` where the old private copy
  raised `ValueError` (`auto_research_child_reconcile.py:116-138, 293-298`).
  Possibly better; unmarked and untested.
- **D4. `record_agent_task_patch_output` semantics for episode task roles.** The
  backend labels `"wake"` only when an auto-research invocation row exists, so
  non-auto-research episodes can never show `"wake"`; the client used to infer
  it from `trigger`/`continuation_cause`.
- **D5. Local drafts now freeze header counts and primary question at canonical**
  (`web/src/App.tsx:1883, 3641`). Intended direction; visible change; untested.
- **D6. `models.absolute_path` is stricter** for `--archive-path`,
  `--identity-file`, `--confirm-data-dir`: rejects non-normalized paths and
  trailing slashes it used to normalize. Operator-facing, no note.

## Not fixed and not declared as an exception

- M12: `project_members` and `graph_watcher_reconciliation` still never migrated
  (11 of 14 tables now, was 10).
- M25: `ProjectDisplayCache._complete_live_control` (`projects.py:2439`) still
  settles stops with no operation lock on `GET /api/projects/{id}`, and
  `run_experiment` (`api/experiments.py:74`) violates the new docstring
  precondition. The comment at `api/index.py:137` claiming the index is the
  only settling GET is wrong.
- M2 sibling: `cached_project_revision` (`api/project_state.py:201`) still does
  the cache read on the event loop; it is the endpoint the client polls.
- M22: no per-wake `try` inside `_reconcile_committed_auto_research_wakes`;
  no regression test for either half.
- H4 residual: the route is now membership-gated, but the handler still clears
  every project's caches, not just the one whose membership was proved.
- M7: case-folding heuristic on `sys.platform == "darwin"`, not a same-file test.
- M4: exclusion list is now correct but still literal in three places.
- `head_ref` dead chain check and its misleading comment
  (`history/manager.py:817`, `history/branches.py:1124`).
- Most of the storage Low list: `_bind_chat_stage` mutation, two
  `_status_for_ending`, unscoped session wipe in
  `detach_experiment_episodes_for_restore`, `_required_timestamp` label,
  `record_agent_usage` index, `mark_result_view_kept` conflict, `stopped_by`,
  `_campaign_status`, `auto_research_finish_blockers` lock,
  `invocation_ceiling * 5`.
- Web: `readTrustView` cast, floating `refreshChatSummaries` at `App.tsx:2205`,
  index-based transcript keys, `watcherDeliveryLabel` derivation, PaperWorkspace
  `skills.reset` effect, ProjectSettings repeated `stagedOrSaved`,
  `CampaignRuns` dead `tasks` prop still declared and passed.
- Tooling: `test_s125_records_implemented_verification` deleted rather than
  converted; `CLAUDE.md` deletion orphaned `.claude/launch.json`;
  `docs/handoffs/README.md:16` lost a blank line so the paragraph renders
  inside the list item; `tests/helpers.py:118` `wait_until` treats a falsy
  settled value as unsettled; `tests/test_graph_condition_watchers.py:1671`
  still has a fixed sleep before a positive assertion.

## Weakly pinned fixes

Correct in code, but the test would not catch a regression:

- M30, M33, M35 (web) are pinned by source-text regex or pure-function tests;
  the actual races have no behavioral test.
- `episodeTaskRole`/`depth` and `can_check_now` have no test that fails on main.
- `web/tests/proposalJudgment.test.mjs:98-190` re-implements the backend's
  proposal-action derivation inside the test to build its expectations, which
  is the second-source-of-truth pattern the handoff set out to remove.
- The two provenance lists in `core/transitions.py:47-69` and `:328-348` are
  hand-maintained twins with no equality test. The golden digest only fires if
  the hashed list changes.
- `_RESTORE_REENTRY_PROVISIONING_TRANSITIONS` (`storage/provisioning.py:67`) is a
  second transition map beside `_PROVISIONING_TRANSITIONS` with nothing tying
  their key sets together.
- M17's byte contract is proven at the manifest-model layer only; no test
  restores a frozen archive produced by old code.
- M18 (doctor) re-implements the two-clause "active operation" rule instead of
  calling `update_operation_needing_recovery`.

## Confirmed closed, with regressions that fail on main

H1 (historical ids byte-identical, verified two ways), H3 (all three parts),
H4 (route), H5, H6, H7, H8 (including legacy flat-stage migration), H9, H10,
M1, M3, M5, M8, M9, M10, M11, M13, M14, M15, M16, M17, M19, M20, M21 (thin),
M23, M24, M26, M27, M28, M29, M31, M32, M34; the `_replay_branch_tail`,
auto-research, and `work_turn_runtime` consolidations; the `noqa: F401` blocks;
the storage `_local_primitives` consolidation; every declared dead-surface
deletion. No `remote_*.py` module imports from `rcp`. The export-worktree
workflow is gone from the branch tip.
