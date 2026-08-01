---
id: S40-discuss-and-work
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_api.py::test_work_without_patch_succeeds_without_spending_a_revision
  - tests/test_api.py::test_invalid_work_patch_is_corrected_without_repeating_operational_work
  - tests/test_api.py::test_exhausted_work_patch_correction_preserves_successful_answer
  - tests/test_api.py::test_stale_work_patch_is_rejected_without_correction_or_rebase
  - tests/test_api.py::test_work_proposal_is_applied_as_a_proposal_not_a_universal_gate
  - tests/test_api.py::test_background_work_rejection_succeeds_and_manual_repair_is_idempotent
  - tests/test_launcher.py::test_codex_work_uses_auto_review_and_exact_writable_roots
  - tests/test_launcher.py::test_claude_work_uses_noninteractive_edits_and_only_explicit_directories
  - web/tests/chatWorkspace.test.mjs::conversation mode controls have stable storage keys and Shift+Tab semantics
  - web/tests/agentTasks.test.mjs::conversation reconstruction preserves immutable mode and graph receipt metadata
invariants: [4, 4b, 8, 9, 10, 10b, 10c, 10d, 10e, 11]
last_passed: 2026-08-01 — 458 backend tests, 109 web tests, lint, and the
  production build passed. A live Codex Discuss turn and Work turn ran through
  the browser against an isolated project; Work wrote the exact requested file,
  returned graph_update none, left revision 7 unchanged, and persisted across
  reload. Experiment, Decision, and project composers all exposed the same mode
  switch, and the browser reported no warnings or errors.
---

# Change one conversation from discussion into work

Every node conversation and project conversation has two per-turn modes.
**Discuss** reads and reasons without changing project files or canonical graph
state. **Work** may edit the exact repositories in the turn's run scope, run
commands and networked tools, and optionally reflect what happened into the
research graph. Changing mode keeps the same conversation and provider session.

Work is one non-interactive agent run. Its answer, operational side effects,
preview artifacts, and optional graph update are independent outcomes. A graph
failure never causes RCP to repeat the operational work automatically.

## UI path

Confirmed by the human on 2026-08-01.

- Open an Experiment node conversation. The empty composer begins in
  plum **Discuss** mode.
- Ask a question. The reply completes without a project edit or graph revision.
- With the composer focused, press **Shift+Tab**. The same composer changes to
  dark-green **Work** mode. Clicking the labelled mode control makes the same
  change; mode is never communicated by color alone.
- Ask the agent to make a harmless fixture edit, run a command, and launch a
  simulated experiment. The work completes in the same conversation. It may
  finish without writing `patch.json`; absence of a graph update is not an
  error and spends no revision.
- Send a Work turn whose optional valid patch contains an ordinary graph change.
  The change applies once and the reply shows **Graph updated · rN** with a
  History destination.
- Send a Work turn whose patch requests a gated canonical change. The patch
  creates a `Proposal`, the reply reports that it was sent to Inbox, and the
  gated operation itself waits for human authority. The patch is not wrapped in
  a second universal proposal.
- Drive a scripted Work turn that increments an external counter once and then
  writes an invalid patch. RCP visibly enters **Correcting graph update**, asks
  the same native session to rewrite only the patch under scratch-only
  permissions, and leaves the counter at exactly one.
- Exhaust the bounded correction rounds. The Work task remains completed, its
  answer and artifacts remain visible, and the reply reports **Graph update
  rejected** with the exact bounded diagnostic. It never offers ordinary Work
  Retry merely because the graph side output failed.
- Switch the composer back to Discuss and ask a follow-up. No new conversation
  is created. Navigate away and return: every sent turn keeps its immutable mode
  badge, and the last composer choice remains the default for the next turn.
- Repeat the mode switch from a non-Experiment node and from project chat.
- Pause and resume a Work turn after changing the composer to Discuss. Resume
  retains the interrupted turn's Work mode, permission envelope, run scope, and
  original graph revision.
- Request an operation outside the mode's permission envelope. RCP never opens
  an approval dialog: the provider adapts to the denial or the task fails with
  the exact provider/tool diagnostic.

The transcript and reading surface stay neutral paper/sheet. Plum and forest are
semantic accents on the mode control, composer binding, send focus, and compact
turn badge. The control has a visible keyboard focus and reduced-motion-safe
state change. There is no helper subtitle beneath it.

## Assertions

- `mode` is persisted on each new human/assistant exchange and task receipt.
  Legacy transcript records remain unlabelled rather than being reclassified.
- The composer mode may change between ordinary turns even when provider,
  model, execution machine, and run scope are locked for the conversation.
- A running or resumed task always uses the mode captured when it was launched.
- New requests contain no `allow_graph_change` authority switch. Work itself is
  the per-turn authorization for an optional validated graph patch.
- Discuss receives no graph-patch path or schema. A stray `patch.json` is kept
  only as a diagnostic receipt and never validated or applied.
- Work receives exact writable run-scope repository roots, writable scratch,
  network access, and no direct canonical `.research` write path. Off-machine
  repositories remain host/path pointers and are never copied.
- A missing or valid empty Work patch spends no revision. A valid non-empty
  Work patch is appended exactly once under the original expected revision.
- Ordinary legal operations land as agent-authored asserted content. Only the
  existing narrow human-authority operations are represented by `Proposal`s.
- Work cannot move ingest cursors or coverage.
- Automatic graph correction reuses the native session but changes to a
  patch-only, scratch-only launch. It never re-runs project commands or external
  actions and stops after the configured correction limit.
- A stale expected revision is rejected without rebasing. The operational Work
  result remains completed.
- The final result independently records `graph_update.status` as `none`,
  `applied`, or `rejected`, plus applied revision, proposal ids, bounded
  validation messages, and correction rounds.
- Codex Work uses automatic non-interactive review with network enabled; Codex
  Seed, Refresh, Discuss, and graph correction use bounded workspace permission
  with no approval prompt and network enabled. Claude Work, Seed, Refresh, and
  graph correction use `acceptEdits`: its CLI accepts `auto` as an argument but
  a real non-interactive probe downgraded it to `default` and denied required
  writes.
- No console error, failed network request, or server traceback appears during
  the browser drive.

Deliberately not possible: a persistent “may change graph” checkbox, an RCP
approval event or modal, danger-full-access, a universal patch proposal, a
second graph-write channel, repository copying, silent graph rebasing, or an
automatic Work rerun after a graph-only failure.
