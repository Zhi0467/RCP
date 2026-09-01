# Codebase audit remediation handoff

Date: 2026-09-01
Status: active. A read-only audit of `src/rcp`, `web/src`, `tests/`, and `docs/`
on 2026-09-01 produced the findings below. Every finding was verified by reading
the code; the few that could only be reasoned about are marked *plausible*. Plan
steps 1 through 3 are implemented and verified on the remediation branch: H1
through H6, H8, H9, M15 through M21, M27, and M28 are closed with focused
regressions. Steps 4 through 8 remain. The
human confirmed the list and asked for the remediation to land on a dedicated
branch and pull request; implementation continues in that same PR.

Settled decisions:

- Fix order is the numbered plan at the end of this file. Authority and fence
  fixes land first, recovery paths second, the transition-id allowlist third.
- Dead surface listed under "Over-engineering" is deleted, not deprecated,
  together with the tests that only exercised it.
- Frontend derivations listed under "Design" move into the backend projection;
  the client renders the published field. No new client-side derivation is
  added while doing so.
- Duplicated filesystem primitives collapse into one private module; the
  self-contained `remote_*.py` modules keep their own copies because they are
  shipped as standalone source.
- No invariant is renumbered and no `AGENTS.md` rule is added by this work. If a
  rule must change, an equal number of lines is removed.

Guiding principle for every fix in this handoff: **a fact has one owner, and a
check consumes the owner's answer instead of restating it.** Almost every high
finding below is the same fact stated twice by careful code. The transition id
hashes fields by an exclusion list while the compatibility check uses an
inclusion list. Restore asserts "exactly one file" while its own later phases
write three more. The Stop fence filters on one task kind while child Work arms
watchers under another. The old release pins a protocol version the new one
moved past. Backup and restore each serialize the manifest and compare bytes.
The frontend recomputes `can_stop` on top of the backend's `can_stop`. Sixteen
files each own `_fsync_directory`. Tests hardcode the status set `models.py`
defines. None of that was careless; each check was added to be safe. A check
that restates a fact is a second source of truth, and the second copy is where
the drift lives. So before adding a guard, name the fact's owner and make the
guard call it. If there is no owner, create one and route both producer and
checker through it. If neither is possible, do not add the guard.

Closure condition: every High and Medium finding is fixed with a focused
regression test or explicitly closed as rejected in this file, the
over-engineering and design sections are resolved or reduced to a closed list
of accepted exceptions, `uv run pytest`, `uv run pre-commit run --all-files`,
and `npm --prefix web test` pass, and the affected UI paths were driven in the
served app. Then this handoff is archived.

Severity key: **H** leaves data, authority, or a recovery path in a state the
system cannot get out of on its own. **M** is wrong behavior or a maintenance
trap that will produce a bug. **L** is cleanup.

---

## 1. Bugs

### H1. Two latent ways to make a graph permanently read-only

`src/rcp/core/transitions.py`

- **Transition id hashes fields that prepare and replay see differently.**
  `_transition_id` (line 827) hashes the whole Patch envelope minus a pop list,
  so `run_truth_scope`, `repositories_read`, `processed_cursors`, and
  `branch_merge` are included. `_require_compatible_envelopes` (line 314) does
  not require them to match across initiating patches, and `_combined_patch`
  (line 805) rewrites them with unions. At replay, `_source_patch_for_group`
  (line 763) copies the combined patch, so patch #2's envelope differs from what
  was hashed. Sync (`append_batch_from_state`) and branch appends do pass
  multi-patch lists. If two initiating patches ever differ in those fields,
  commit succeeds and the next replay raises "transition id does not match",
  degrading the append-only log to read-only. Today `_build_sync_patches`
  leaves those fields at defaults, so this is latent.
- **Adding any optional `Patch` field rewrites every historical transition id.**
  The `project_home_transfer` special case at line 830 is the scar from the last
  time this happened. There is no allowlist and no golden-digest test.
- Also: an initiating patch with `ops=[]` contributes an envelope at prepare
  (line 271) but no group at replay, so the envelope count differs.

Fix: hash an explicit named list of provenance fields, add the four fields to
the compatibility check or drop them from the envelope, reject op-less
initiating patches, and pin the digest with a golden test.

### H2. Restore dead-ends at its own human pause and never takes the data-dir lock

`src/rcp/server_ops/restore.py:3391`

`_require_restored_target` refuses unless the data directory contains exactly
`rcp.sqlite3`. Later phases write `bootstrap-manifests/` (through
`rebind_restored_project_registration`, line 2099), `project-sources/<pid>/`
(line 2292), and `state-cache/` (through `restored_project_owners`, line 2193)
into the same directory. Steps 9 and 10 are mandatory operator pauses that
require a rerun, and the rerun re-executes `install_sqlite_candidate`
(line 1795) and `verify_offline_candidate` (line 1848), both of which call the
check. The existing resume test holds only one file in the data directory, so it
cannot see this.

Restore also never takes `instance_lock(data_dir)` (invariant 8). Ownership is
proved by directory emptiness plus systemd. A manual `rcp serve` during the
review window acquires the lock and serves an unreviewed roster.

Fix: check the journaled database plus the exact set of owners the reached phase
may have created; hold the instance lock across the mutating phases.

### H3. Server update can roll back forever, and one failure path starts the wrong release

`src/rcp/server_ops/control.py:122`, `update.py:1916`, `install.py:2240`,
`update_cutover.py:1207`, `install.py:929`

- `ServerControlRequest.protocol_version` is a `Literal` of the current constant
  with no compatibility decoder. Update runs from the old release and verifies
  the new release's control socket, so any commit that bumps the protocol is
  rejected as `invalid_request`, rolled back, and can never be reached.
- `switch_current` does `os.replace`, then fsync and readback. If either raises,
  the pointer has moved but `switched` stays `False`, so `_abort_before_switch`
  restarts the service on the candidate while reporting no switch happened.
- A crash between source-key creation (`install.py:931`) and config write
  (line 941) makes install unrunnable until a human deletes the key by hand.

Fix: accept a bounded set of prior protocol versions server-side; set
`switched` before calling `switch_current` or report "replaced but unverified"
distinctly; adopt an existing key pair whose public half matches.

### H4. `DELETE /api/caches` bypasses project membership

`src/rcp/api/index.py:407`

Registered on the unguarded `router` with `project_id` as a query parameter. The
membership enumeration test only inspects routes with `{project_id}` in the
path, so it cannot see this. Any enrolled team member can clear every project's
caches and probe project existence.

Fix: move to `membership_router` with a path segment, and extend the
enumeration test to routes that declare a `project_id` query parameter.

### H5. Auto-research Stop does not fence watchers armed by its own child Work

`src/rcp/storage/auto_research.py:1553`, `src/rcp/runs/tasks/work.py:1183`

The settle fence is `WHERE episode_id = ? AND origin_task_kind =
'auto_research'`. Child Work arms watchers with `episode_id=None` and
`origin_task_kind=turn.surface`. After Stop or Finish, a later watcher
completion queues an unfenced, unmetered `node_chat` turn.

Fix: settle by episode lineage, and add an Auto-research episode guard beside
`_experiment_wake_is_stopped` in `create_watcher_notification_task`.

### H6. Auto-research Stop can pause a stale worker attempt

`src/rcp/runs/auto_research_admission.py:931`

Reads `current.operation_id` before writing the route Stop, then pauses that id
instead of re-reading `route.current_operation_id`. A message wake that advanced
the route in between survives Stop.

### H7. A transient graph condition permanently kills a reserved Experiment replacement

`src/rcp/runs/auto_research_experiments.py:342`

Bare `except ValueError` maps "control not ready" (open Proposal, admin
transfer) to the terminal `fail_auto_research_experiment_replacement`.

Fix: distinguish permanently invalid intent from not-yet-satisfiable and leave
the latter pending.

### H8. Chat Work's enforced write root includes RCP's own `inputs/` tree

`src/rcp/runs/tasks/work.py:492`, `src/rcp/runs/chat.py:992`,
`src/rcp/runs/shared.py:447`

Locally `workspace = local_stage`, so the write root covers `<stage>/inputs/`
with every staged task contract. Retry re-serves the parent contract by path
without checking the recorded sha256. `branch_merge.py:95` uses a `workspace/`
subdirectory precisely to avoid this. Remote runs are unaffected.

Fix: give chat Work a `workspace/` subdirectory, or verify the recorded digest
when re-serving a parent contract.

### H9. Invariant 10c (clear the previous turn's patch) fails open

`src/rcp/runs/tasks/work.py:510`

Clearing happens `if not reusing_checkpoint or waking`, where `waking` means
only `watcher_wake`. `_NATIVE_CHECKPOINT_CONTINUATIONS` also includes
`message_wake`, `graph_condition_wake`, and `lifecycle_wake`. None reach
`stream_work_run` today, but `work.py:1729` already emits a `message_wake`
launch kind.

Fix: clear for every new logical turn and raise on any continuation the rule
does not recognize.

### H10. SSH multiplexing uses a predictable world-writable control socket (*plausible*)

`src/rcp/transport/ssh.py:15`

`ControlPath=/tmp/rcp-ssh-%C`. A local user can pre-plant a socket there; the
OpenSSH mux client does not verify socket ownership. Move the path under a 0700
RCP-owned directory.

### Medium bugs

| # | Where | What |
| --- | --- | --- |
| M1 | `api/health.py:789` | `async def health` does sync SQLite with a 30 s connect timeout on the event loop. This is what `_probe_owner` polls with a 2 s budget. |
| M2 | `api/project_state.py:61` | Cache-hit path does file IO and a lock inline; only the cold path is threaded. |
| M3 | `watchers.py:1568` | `WatcherRetryWorker` runs reconciliation inside its lifecycle lock, so `stop(timeout=)` cannot time out and `signal()` stalls the poll loop. |
| M4 | `transport/state.py:1855` | `rsync --delete` excludes three lock files but not `.append.lock` and `.chat.lock`; the backup export excludes all five. Two concurrent append-lock owners are possible in degraded mode. |
| M5 | `transport/state.py:1671` | Lock holder's stderr pipe is never drained until exit; a chatty remote `.bashrc` wedges it. `provider_skills.py:224` already fixed the same bug. |
| M6 | `transport/state.py:1529` | Lock wait deadline is disabled after the first `contended` line; `transaction()` passes no `cancelled`. |
| M7 | `agents/write_scope.py:341,411` | Overlap and home-root checks are string comparisons; macOS is case-insensitive. |
| M8 | `transport/run_stage.py:110` | `open(reuse=True)` adopts an existing `/tmp/rcp-run.*` without the lstat and ownership probe that `attach()` does. |
| M9 | `storage/base.py:1316,1608`, `episodes.py:2101` | Lineage and legacy migrations rescan every task and event row on every process open, inside `BEGIN IMMEDIATE`, with no completion marker and no schema version. |
| M10 | `storage/experiments.py:2085` | Experiment Stop settlement writes watchers in one transaction and terminalizes the episode in a second. |
| M11 | `storage/experiments.py:2774`, `api/index.py:136` | One inconsistent episode row raises `ValueError` and takes down the whole project index and cross-project `/api/episodes`. |
| M12 | `storage/projects.py:866` | `migrate_legacy_project_data` moves 10 of the 14 tables in `_PROJECT_ID_TABLES`; the legacy `paper_drafts` row is never deleted. |
| M13 | `storage/provisioning.py:2769` | Restore detachment writes a status outside `_PROVISIONING_TRANSITIONS` with no revision guard. |
| M14 | `storage/watchers.py:989` | `record_watcher_check` is an unlocked read-modify-write on `consecutive_error_count`. |
| M15 | `server_ops/backup.py:716` | Archive is hard-linked before the receipt is written; a failure in between leaves ciphertext that retention can never delete. |
| M16 | `server_ops/backup_project_files.py:240` | One project's provider-history capture failure aborts the whole nightly backup instead of marking it uncaptured. |
| M17 | `server_ops/backup.py:790` vs `restore.py:3294` | Two private `_manifest_bytes` copies form an unstated byte-for-byte contract; same for `_database_schema_sha256`. |
| M18 | `server_ops/doctor.py:612` vs `update_cutover.py:535` | Doctor flags only the newest update receipt; admission flags any non-terminal one. |
| M19 | `server_ops/update.py:1947` | Rollback worker's stderr diagnostic is discarded; the operator sees a fixed string. |
| M20 | `server_ops/members.py:290` | Printed Continue command is a bare `rcp …` that fails the identity check when run as root. |
| M21 | `runs/auto_research_admission.py:1558` | `suppress(Exception)` around the budget-exhaustion wrap-up hook, with no log. |
| M22 | `runs/auto_research_delivery.py:198,224` | Reconcile ignores the episode filter and full-scans the store twice per episode per poll; no per-episode `try`, so one bad row starves delivery for all later episodes. |
| M23 | `runs/shared.py:212` vs `chat.py:727` | Pre-launch patch fingerprint scans all `*.json`; the patch reader reads only `patch.json`. A stray draft makes the "unchanged correction" guard never fire. |
| M24 | `api/tasks.py:268`, `experiments.py:150`, `index.py:370` | HTTP status chosen by substring-matching exception prose. |
| M25 | `api/index.py:104`, `experiment_controls.py:96` | `GET` handlers commit lifecycle writes without the operation lock the POST path holds. |
| M26 | `api/app.py:1868` | CORS middleware is inner to the two `@app.middleware` handlers, so early 503/403/409 refusals lose CORS headers. |
| M27 | `core/materialize.py:327` | Re-declares `LEGACY_COMPATIBILITY_UPDATE_FIELDS` inline instead of importing it. |
| M28 | `core/materialize.py:476` | `rejection_reason` is written on approved and withdrawn Proposals. |
| M29 | `artifacts.py:322,435` | Wrapper secret is embedded in the untrusted document; agent HTML can forge `rcp-reference` messages and open popups (invariant 10e). |
| M30 | `web/src/views/PaperWorkspace.tsx:147,262` | Autosave and the 5 s poll share one generation counter; a poll during a PUT drops the save's `base_hash`. |
| M31 | `web/src/App.tsx:1819,2252` | `attentionGraph` is an alias of `presentedGraph`, so the two-map `decisionsAwaitingChoice` is a no-op; the test proves behavior production never supplies. |
| M32 | `web/src/hooks/useAgentTasks.ts:175` | "Dismiss notification" persists `dismissedTaskIds` that only dead code reads. |
| M33 | `web/src/components/NodeChat.tsx:351` | `runScope` is copied into state keyed by chat id and never resyncs after scope pruning. |
| M34 | `web/src/hooks/useForceDag.ts:96` | DAG simulation restarts on every 5 s watcher snapshot because it keys on array identity. |
| M35 | `web/src/App.tsx:2220` | Watcher poll swallows all errors with a comment claiming the reload surfaces them; in that state the poll is the reload. |

Low-severity bugs noted by the audit and fixed opportunistically when touching
the file: `_bind_chat_stage` mutates its caller's record
(`storage/agent_tasks.py:687`); two `_status_for_ending` with opposite `None`
handling (`storage/episodes.py:1785,2603`); `detach_experiment_episodes_for_restore`
wipes session ids for terminal episodes against its docstring
(`storage/episodes.py:214`); `_required_timestamp` reports "result view" for every
caller (`storage/models.py:3211`); `record_agent_usage` has no unique index or
immediate transaction (`storage/agent_tasks.py:1508`); `mark_result_view_kept`
accepts a different filename silently (`storage/result_views.py:433`); the
authorizer-terminalization UPDATE omits `stopped_by` (`storage/watchers.py:1327`);
a failed remote apply leaks its staging directory (`remote_lock_holder.py:136`);
`(error as Error).message` casts produce empty toasts (`web/src/App.tsx:2542`);
`readTrustView` casts an arbitrary string (`useGraphSelection.ts:522`).

---

## 2. Over-engineering and duplication

**Same helper, many copies, drifting bodies.** `_fsync_directory` in 16 files
with 6 distinct bodies (some without `O_DIRECTORY` or `O_NOFOLLOW`),
`_canonical_uuid4` in 13 files with 7 bodies, `_absolute_path` in 8 (two callers
of "the same" check accept different inputs), `_write_all` in 7, `_fsync_tree`
in 6, `_read_private_file` in 5, `_model_bytes` in 4, `_canonical_json` in 6.
None are the self-contained `remote_*.py` modules, and `server_ops/models.py`
already exports `canonical_uuid4` and `absolute_path`.

**`runs/tasks/work.py` and `runs/tasks/experiment_loop.py` share about 1000
copied lines.** 21 same-named functions with similarity 0.86 to 1.00
(`_apply_work_patch`, `_stream_work_graph_repair`, mailbox lifecycle, run-lock
and crash-dedup). Already diverged: `work.py:1227` stages a validator command it
discards and carries a dead `except` block; `experiment_loop.py:709` computes a
failure and unconditionally discards it. Extract the mechanism, keep the policy
in each owner, and do not add a kind selector.

**Auto-research family: coherent layering, duplicated bodies.** Three
restatements of the deterministic child-id derivation (`auto_research.py:1892,1914`,
`auto_research_child_reconcile.py:456`); `_validate_worker_request` twice with
different exception vocabularies; `AutoResearchSeatNodeType` defined twice with
different arity; two byte-identical `_prepare_*_handoffs` in
`auto_research_stream.py:1102`; the wrap-up receipt and the `status` command build
the same projection twice.

**Dispatcher fallbacks for a worker model that no longer exists.**
`runs/auto_research.py:1760-1822` validates workers as `auto_research` rows with
invocation rows; real workers are `node_chat` rows with child-work routes. The
fallbacks are reachable only from test doubles. `AutoResearchCommandEffects`
optionality (line 655) exists only for tests. Make the fields required.

**Dead or test-only production code to delete:**

- `storage/episodes.py`: `create_episode`, `create_episode_with_invocation`,
  `allocate_episode_invocation`, `_created_episode_pair_matches`;
  `storage/agent_tasks.py:840` `mark_agent_tasks_history_only`.
- `storage/auto_research*.py`: `auto_research_task_history`,
  `auto_research_message_history`, `settle_auto_research_watchers`,
  `pending_auto_research_experiment_replacement`,
  `terminalize_auto_research_child_experiment`,
  `harvest_auto_research_lifecycle_notices` and `clear_…` (these two skip the
  response-size bound the live path enforces).
- `core/transitions.py`: `TransitionRule` and `RULE_REGISTRY` (zero readers),
  `GraphTransitionManager` as a class around one never-overridden int,
  `TransitionCauseRef(kind="event")` machinery never constructed;
  `core/ontology.py:50` `parse_ontology_operation`.
- `history/delta.py:82` `build_revision_summaries` (test-only second renderer);
  `history/branches.py:1293` `_replay_branch_tail` hand-copies
  `materialize_patches` and has diverged.
- `server_ops/cli.py:642` `_unavailable_command` plus its exit code, step state,
  and renderer branches across five files; `update_cutover.py:1281`
  `repair_committed`; `install.py:249` unreachable raise; `install.py:573`
  `_ = facts`.
- `runs/patch_validator.py:67` `prepare_patch_validation_mailbox`;
  `runs/tasks/coach.py:378` paper-snapshot path copy;
  `runs/auto_research_recovery.py:188` three constant role branches.
- `web/src/runProjection.ts:256` `buildRunProjection`, `buildRunTaskProjection`,
  `RunEntry`, `RunProjection` and the tests that only cover them;
  `App.tsx:326` `terminalTasksSince`; `humanDraft.ts:277` `stageAttemptRelease`;
  `experimentBoard.ts` `buildExperimentBoard` where only `health` is read;
  the twelve test-only re-exports at `App.tsx:191-211`.
- `agents/acceptance.py` (1729 lines) is a test double shipped in the production
  wheel behind `--acceptance-agent`. Move it out of the wheel and inject it
  through the existing `agent_mode` composition point.

**Unused configurability.** `BACKUP_APP_DATA_DEFERRED = frozenset()` threaded
through five layers; `serialize_episodes` `limit` and `branch_summary` knobs no
caller passes (`api/episodes.py:383`); `max_attempts` on recovery that overwrites
stored state if ever passed (`storage/auto_research.py:1030`);
`_watcher_state(phase=)` where Discuss passes `"resume"` to suppress one branch
(`runs/experiment_loop.py:977`); `binding: WatcherBinding | None` that is
required (`storage/experiments.py:1020`).

**Blanket `# noqa: F401` import blocks.** Eight storage modules copy a
65-name import block and use 8 to 20 of them, disabling the unused-import check
across the largest package. `episodes.py` shows the alternative works.

**Size.** 93 functions over 150 lines, 12 over 300. `create_app` is 1695 lines;
`AppStore._initialize` is 1414 lines of inline DDL gated by column presence with
no schema version. `restore.py` (3577 lines) holds six owners.
`update_checkpoint.create` copies the data directory twice and hashes it three
times inside the closed maintenance window. `_experiment_control` and
`_experiment_control_for_target` are 30-line near-duplicates
(`api/experiment_controls.py:76,110`).

---

## 3. Design

**Kind selectors in shared plumbing, against the first structural rule.**

- `storage/watchers.py` branches on `patch_kind == "experiment_loop"` and
  `origin_task_kind == "auto_research"` at eight sites; H5 is the mode this
  plumbing forgot.
- `runs/task_policy.py:137` `resolved_dispatch_authority` collapses branch-merge,
  episode-report, auto-research, and generic policy into one function.
- `agents/command_mailbox.py:98` picks between two authentication schemes on
  `episode_id is not None`.
- `server_ops/project_checkout.py:235` labels a restore
  `request_kind="incoming_transfer"` to reach a permissive branch.
- `runs/tasks/episode_report.py:498` inherits its capability from the concluding
  task's profile.

**Frontend deriving backend state, against the web-layer convention.**

- `web/src/components/AttentionRail.tsx:158-285` reconstructs Proposal
  expansion, including which relations a removal also deletes, from raw ops plus
  the graph. A human approves a protected mutation against a client-side preview.
- `web/src/App.tsx:4052-4094` invents a primary-question ordering and recomputes
  `counts` after a committed transition; the transition response does not carry
  them.
- `web/src/components/CampaignRuns.tsx:87` re-decides `can_stop` on top of the
  backend's answer; `ExperimentRunDetail.tsx:113` honors it directly. Two Stop
  surfaces disagree.
- `web/src/campaigns.ts` infers orchestrator, worker, and wake roles and depth
  from untyped `request` fields that compile only via an index signature.
- `web/src/graphAuthority.ts:3` derives app-wide mutation gating from
  `replay_status`; `ExperimentRunDetail.tsx:597` derives the "Check now" gate.
- `web/src/projectTransition.ts:3-41` restates head and trigger shapes that
  `types.ts` already owns.
- `/api/episodes` (`api/index.py:227`) returns an undeclared dict and re-derives
  a projection `ProjectDisplayCache` already owns; `types.ts` says
  `episode: Episode | null` but the server never emits null.

**Prompt prose parallel to enforcement.** `runs/tasks/auto_research_child_work.py:227`
hand-lists denied command verbs; enforcement is a type check. The list matches
`CommandVerb` today by coincidence. Render the denied set from the enum.

**Misleading names and comments.** `is_existing_protected_node(patch=…)` never
reads `patch` while its docstring says it matters (`core/authority.py:558`);
`head_ref`'s chain check can never fire and `history/branches.py:1124` says it
validates (`history/manager.py:817`); `settle_auto_research_watchers` says
"retain" while the body stops; `lease_boundary_sha256` holds random hex
(`storage/provisioning.py:1988`); `RunLockLease.assert_owned()` is a no-op on
local workspaces but shared publish code calls it as a proof
(`transport/state.py:847`); `_campaign_status` has an if/else with identical
branches (`storage/episodes.py:2388`); `auto_research_finish_blockers` is
documented read-only but takes `BEGIN IMMEDIATE`
(`storage/auto_research_children.py:1874`).

**Limits outside `limits.py`.** 28 literal timeouts and several literal sleeps
across `transport/state.py`, `transport/run_stage.py`, `server_ops/update*.py`,
`server_ops/restore.py`, `agents/launcher.py`, `background.py:773`
(`shutdown(timeout=7.0)` against the 45 s the replacement waits), and
`storage/auto_research_children.py:941` (`invocation_ceiling * 5`).

---

## 4. Tests, docs, tooling

- 26 hand-rolled polling loops in 13 test files, with 2 to 5 s bounds that
  `tests/helpers.py:17` explains cause flakes. Six of those files already import
  `wait_for_task`. `test_api_tasks.py:32` and `test_dispatch_authority.py:113`
  hardcode status sets that have drifted from `ACTIVE_AGENT_TASK_STATUSES`.
  Add a generic `wait_until` to `tests/helpers.py` and replace all 26.
- `docs/handoffs/README.md` says Transfer relay, decode, activation, cleanup, and
  UI "remain"; the handoff it indexes says they are done.
- `docs/acceptance/S96-joining-a-team-space.md` is `status: implemented` while its
  frontmatter says steps 5 to 11 were never driven. 28 scenarios use
  `last_passed`, 10 use `last_checked`, 8 implemented ones have neither; the doc
  test requires none of them.
- `tests/test_documentation.py:300` pins S125's `last_passed` to a literal date,
  so re-verifying the scenario breaks the suite.
- `pyproject.toml:54` `extend-exclude = ["web"]` hides two tracked Tauri probe
  scripts that currently fail `I001`. Pytest has no `--strict-markers`. The
  documented lint command omits `packaging/`. Prettier's regex misses
  `web/vite.config.ts` and eight `web/src-tauri/*.json` files.
- `AGENTS.md` is at exactly 230 lines, its hard ceiling. The invariant registry
  is out of numeric order. `CLAUDE.md` restates the UI-verification trigger with
  drifted wording.
- `_store(tmp_path)` is duplicated 5 times in tests; two copies hardcode
  `/tmp/project/...` paths.

---

## What the audit found solid

Transactional discipline in storage (`BEGIN IMMEDIATE` plus compare-and-swap
guards, parameterized SQL throughout). Materialization's container-only forking
holds against every write site. Backup uses the real SQLite online-backup API
and validated `age` recipients with no credential leakage. Tar extraction and
archive import are traversal-safe. Graph authority is read only from
`patch.json`; Discuss has no path to graph authority; stored transcripts are
never task authority. The opaque lifecycle types in `types.ts` are never cast
around. Remote code is shipped from source modules. The auto-research module
split is acyclic with clear owners. None of that is changed by this handoff.

---

## Plan

Each step is one or more commits on the PR branch. A step is done when its
finding has a focused regression test and the checks in the closure condition
pass for the touched area.

1. **Authority and fences — completed 2026-09-01.** H4, H5, H6, H8, H9, M21.
2. **Recovery paths — completed 2026-09-01.** H2, H3, M15, M16, M17, M18,
   M19, M20.
3. **Transition id — completed 2026-09-01.** H1, M27, M28, and the
   `_replay_branch_tail` duplication.
4. **Event loop and transport.** M1, M2, M3, M4, M5, M6, M7, M8, H10.
5. **Storage lifecycle.** M9, M10, M11, M12, M13, M14, M22, M23, M24, M25, M26.
6. **Frontend.** M30 to M35, then move the section 3 derivations into the
   projection, starting with the two Stop surfaces.
7. **Consolidation.** Filesystem primitives, `work.py` and `experiment_loop.py`
   mechanism, auto-research duplicates, dead surface, unused configurability,
   `noqa: F401` blocks, limits into `limits.py`.
8. **Tests and docs.** `wait_until`, the handoff README, S96 status, the
   `last_passed` key, ruff and pytest config, prettier scope, `AGENTS.md`
   ordering and headroom.

Findings closed as rejected are moved to a "Rejected" list in this file with a
one-line reason in the same commit.
