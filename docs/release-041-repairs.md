# Release 0.4.41 qualification repairs

## Purpose and boundary

Extend the existing fork archive fence, private schedule store, Work write-scope
resolver and shared banner to satisfy their current contracts. Ship these repairs
with the dependency upgrades in PR #385. Keep the unreleased version at 0.4.41.
Promotion requires a new merged-main build; build 3111's receipts do not qualify
different bytes. This work does not deploy a server or promote a release.

The user explicitly asked to fix the qualification blockers in this PR. Existing
canonical history, human graph authority, provider enforcement and server-only
merged-main consumption remain unchanged. Tests and rehearsals use disposable
data or copies, never mutations of the team's live data.

## Fork archive fence

The current archive path checks only immediate children with unsettled work. An
unchanged ancestor can retire while a grandchild writes or holds settled changes,
then refuse delivery into the archived ancestor.

Keep the existing canonical write lock, exact candidate-head check and SQLite
`BEGIN IMMEDIATE`. Replace only the nested-fork check with a project-scoped
recursive traversal of immutable parent links. Traverse archived intermediates;
refuse archive if any reachable descendant is unarchived. Use `UNION` to terminate
cycles. A cycle containing the open candidate fails closed. Keep all candidate
writer, publication, watcher, child-episode, mail and admission fences.

Empty automatic forks retire from leaves toward ancestors, potentially across
polls. Manual or nonautomatic unarchived descendants retain their ancestors.
Merge does not archive a changed child. Do not add a fully-merged retirement rule
or change the existing ability to fork retained historical heads.

## Private schedule replay

Add validates the current clock before finding its existing key. Pause, Resume
and Cancel ignore their keys, so stale replay can either refuse or undo a later
human/agent state change. Extend the concrete schedule store transaction to own
both the successful mutation and its recorded outcome.

Add migration 79 creating `agent_schedule_command_receipts`. Its primary key is
`(lineage_root_operation_id, action, idempotency_key)`. Store the thread, target
schedule, canonical original arguments, successful result and creation time.
Reference the schedule with `ON DELETE CASCADE`; schedule deletion remains the
cleanup owner. Classify receipts beside private schedules as space-local state
excluded from project transfer. SQLite backup retains them. Do not reuse task
diagnostic receipts or alter their pruning/transfer policy.

The smaller alternatives do not satisfy the contract. Recursing only through
active writers still misses settled child changes awaiting delivery. Treating
an active schedule as a successful Resume replay can undo a later Pause. Task
diagnostic receipts have different pruning and project-transfer lifecycles from
private schedules; a schedule-owned table keeps replay with its actual owner.

For each keyed mutation, authorize the current task's live private thread and
project membership before replay, and recheck ownership within the write
transaction. Find the receipt before clock-dependent validation. An identical
request returns its stored result using the new transport request id, without
changing the current schedule. The same action/key with changed arguments or a
different schedule refuses with the existing key-conflict error. Separate
actions may reuse a key, preserving existing Add/Cancel behavior.

Canonical request identity includes every typed argument, including original
count and the distinction between an omitted and explicit timezone. It excludes
transport ids, mailboxes and credentials. Resolve an omitted timezone only for a
fresh add. The store executes the fresh mutation and records its result in one
transaction; failure to record rolls back the effect. Refactor the existing SQL
into connection-taking schedule helpers, preserving the same implementation for
human routes. Unkeyed human Resume of an active schedule keeps its refusal.

Old Add rows have immutable fields but only a mutable remaining count. Find them
before current-time validation and preserve their existing comparison, which
does not compare count. Compare an explicit timezone with the stored zone; an
omitted timezone uses that stored zone for this limited legacy match. Neither
the original count nor the original omission of a timezone can be recovered.
Return the current stored schedule without resetting any
state, pending fire, remaining count or due time. Do not invent a full-identity
receipt from missing original data. Old Pause/Resume/Cancel keys were never
stored and cannot be backfilled; full durable replay begins with the new writer.
List remains a live read. Closing a thread or losing membership still refuses a
previously accepted command. Restore must not reactivate disabled schedules when
an accepted command is replayed.

## Space Work grants

Extend ordinary Project Work's existing resolver input to Space Work:
`prepare_space_projects` passes the current base machine card's writable paths.
This uses already configured human grants, with the existing eligible-repository
gate, canonicalization and protected-storage exclusions. Add no new settings,
scope kind, fallback or graph authority. Existing composite roots continue to
feed both provider enforcement and prompt rendering. Each new turn refreshes
grants without abandoning the native session or changing the graph fingerprint.

## Native update notice

Use the shared `Banner` for the native updater, matching the existing release
notice. Desktop and 390px current/proposed renders were produced before UI edits.
Keep the existing Update action and active-work expansion flow. Use one icon-only
Dismiss with the shared per-device store; an available release's occurrence key
names this installation and target version. The compact active-work marker must
honor that same dismissal. Reloading or checking again must not redisplay the
same dismissed release; a different version must show.

An updater failure is a separate named occurrence; clearing the failure forgets
its dismissals so a later failure is visible. A newly visible failure must not be
hidden by an earlier release dismissal. Preserve retry/update controls and the
existing active-work confirmation. Remove the obsolete Later control and its
React-only clearing path. At 390px, all content and the 44px dismiss hit target
must fit inside the banner without horizontal overflow.

## Current-behavior documentation

Correct the Running row kind to `experiment_loop`; document the existing Apply
admission ceiling and replay counting; distinguish typed decoding from dynamic
node-update admission validation; describe the preserved natural-exit receipt
without a cancellation acknowledgement; and include attached saved-artifact
folders in the coach's write boundary. Link each fact to its concrete owner.
Do not weaken contracts to hide the archive, replay, grants or banner defects.

## Implementation plan and verification

The exact file ownership is:

Modify the existing files listed below; create or delete no production files.
This temporary design is the only created file and is deleted before merge.

| Owner | Production and current-behavior files | Regression files |
| --- | --- | --- |
| Archive | `src/rcp/storage/auto_research_children.py`, `docs/specs/auto-research-and-branch-merge.md` | `tests/test_auto_research_forks.py`, `tests/test_auto_research_children_storage.py`, `tests/test_owned_merge_storage.py` |
| Schedule | `src/rcp/storage/agent_schedules.py`, `src/rcp/runs/agent_schedules.py`, `src/rcp/storage/base.py`, `src/rcp/transfer/archive.py`, `docs/specs/graph-consolidation-and-lessons.md` | `tests/test_agent_schedules.py`, `tests/test_resource_versions.py`, `tests/test_server_upgrade.py`, `tests/server_upgrade_state.py`, `tests/lifecycles/schedules.py`, `tests/lifecycles/test_schedules.py`, `tests/lifecycles/writers.py`, `tests/test_agent_thread_commands.py`, `tests/test_transfer_archive_manifest.py` |
| Scope | `src/rcp/runs/space_projects.py`, `docs/specs/providers-and-containment.md` | `tests/test_space_chat_runtime.py`, `tests/test_space_write_scope.py` |
| Lead UI | `web/src/desktop/UpdateNotice.tsx`, `web/src/desktop/useDesktopShell.ts`, `web/src/desktop/updateNoticeModel.ts`, `web/src/App.tsx`, `web/src/styles/09-workspace.css`, `docs/specs/api-web-and-desktop-projections.md` | `web/tests/updateNotice.test.mjs`, `web/tests/fixtures/updateNotice.tsx` |
| Lead prose | `docs/specs/api-web-and-desktop-projections.md`, `docs/specs/auto-research-and-branch-merge.md`, `docs/specs/graph-history-and-transitions.md`, `docs/specs/machines-and-compute.md`, `docs/specs/paper-artifacts-and-result-views.md` | Existing owning regressions and per-spec drift review |

Workers must report a newly discovered required owner before editing outside
their list. The lead coordinates shared upgrade or lifecycle-inventory changes.

1. Archive owner: `storage/auto_research_children.py`, owning auto-research spec,
   fork/storage/owned-merge tests. Test queued and settled grandchild work,
   archived intermediates, manual descendants, isolation, cycle termination and
   eventual leaf-first empty retirement. Exercise polling between nested fake
   agent deliveries. Preserve canonical history bytes.
2. Schedule owner: `storage/agent_schedules.py`, `runs/agent_schedules.py`, the
   migration plan, private-transfer classification and receipt-version exemption,
   owning schedule spec and schedule/upgrade/lifecycle tests. Test delayed Add,
   all keyed mutations, stale replay after a later opposite mutation, same-action
   collisions, cross-action compatibility, timezone changes, authorization,
   restart, atomic rollback, pruning, cleanup, transfer/restore and old-writer
   Add with a consumed count. The legacy writer check also changes/removes the
   member's timezone before delayed replay and refuses a changed explicit zone.
   Extend the real schedule lifecycle explorer.
3. Scope owner: `runs/space_projects.py`, the containment spec and existing Space
   chat/write-scope tests. Verify canonical grant/protection equality between the
   staged master, launch receipt and provider enforcement, and next-turn grant
   add/removal in the same native session. Verify no eligible repo means no grant.
4. Lead owns updater UI/model/hook/CSS and served update-notice fixtures/tests,
   the five prose-only corrections, integration and final qualification. Test
   release/error dismissal, reload, newer release, active-work marker and update
   confirmation at desktop/390px; inspect network, console and backend logs.
5. Use isolated worker branches and integrate through real three-way merges.
   Run focused checks, scoped quality/mutation checks and changed-file hooks;
   fresh EGM/code reviews and PR feedback checks; and every exact-head CI job.
   Move these rules into their owning specs and remove this design before merge.
   After human merge, select the new main build and repeat all 14 drift reviews,
   signed candidate verification/native qualification and copied-team rehearsal.

## Approval and review record

The user's request to fix the blockers is the explicit implementation start.
The implementation remains subject to EGM design, implementation-readiness and
zoom-out review before production edits. No unresolved product choice is used to
widen write authority beyond the existing configured-grant contract.
