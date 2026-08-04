---
id: S40-discuss-and-work
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_api.py::test_work_without_patch_succeeds_without_spending_a_revision
  - tests/test_api.py::test_invalid_work_patch_is_corrected_without_repeating_operational_work
  - tests/test_api.py::test_exhausted_work_patch_correction_preserves_successful_answer
  - tests/test_api.py::test_work_proposal_is_applied_as_a_proposal_not_a_universal_gate
  - tests/test_api.py::test_background_work_rejection_succeeds_and_manual_repair_is_idempotent
  - tests/test_launcher.py::test_codex_work_bypasses_approvals_and_sandbox
  - tests/test_launcher.py::test_claude_work_bypasses_permissions
  - tests/test_agent_schema.py::test_agent_output_schema_omits_nested_rcp_bookkeeping
  - tests/test_staged_graph_validation.py
  - web/tests/chatWorkspace.test.mjs::conversation mode controls have stable storage keys and Shift+Tab semantics
  - web/tests/agentTasks.test.mjs::conversation reconstruction preserves immutable mode and graph receipt metadata
invariants: [4, 4b, 8, 9, 10, 10b, 10c, 10d, 10e, 11]
last_passed: 2026-08-02 — the 2026-08-01 live provider drive verified immutable
  turn modes and unchanged revision for a Work turn without a patch. After the
  shortcut-scope change, 128 web tests,
  typecheck, and production build passed. In the browser, Shift+Tab changed Work
  to Discuss while focus remained on the Chats navigation button, then changed
  Discuss to Work exactly once while the message box was focused. Leaving Chats,
  pressing Shift+Tab, and returning did not change the conversation mode.
---

# Change one conversation from discussion into work

Every node conversation and project conversation has two per-turn modes.
**Discuss** reads and reasons without changing project files or canonical graph
state. **Work** has unrestricted repository, command, network, and tooling access
and may optionally reflect what happened into the research graph. The exact
run-scope repositories still define what RCP puts in context; they are not a
Work permission allowlist. Changing mode keeps the same conversation and
provider session.

Work is one non-interactive agent run. Its answer, operational side effects,
preview artifacts, and optional graph update are independent outcomes. A graph
failure never causes RCP to repeat the operational work automatically.

## UI path

Confirmed by the human on 2026-08-01.

- Open an Experiment node conversation. The empty composer begins in
  plum **Discuss** mode.
- Ask a question. The reply completes without a project edit or graph revision.
- Anywhere on the **Chats** page, press **Shift+Tab** without first focusing the
  composer. The selected conversation changes to dark-green **Work** mode.
  Pressing it while the composer or another Chats control is focused makes the
  same single change. Clicking the labelled mode control remains available;
  mode is never communicated by color alone. A floating chat keeps the narrower
  composer-focused shortcut rather than capturing Shift+Tab app-wide.
- Ask the agent to make a harmless fixture edit, run a command, and launch a
  simulated experiment. The work completes in the same conversation. It may
  finish without writing `patch.json`; absence of a graph update is not an
  error and spends no revision.
- Send a Work turn whose optional valid patch contains an ordinary graph change.
  The change applies once and the reply shows **Graph updated · rN** with a
  History destination.
- Send a Work turn whose patch recommends one Decision choice/status transition
  or one Hypothesis status transition. The patch creates a `Proposal`, the reply
  reports that it was sent to Inbox, and the transition waits for human
  authority. The patch is not wrapped in a second universal proposal.
- Drive a scripted Work turn that increments an external counter once and then
  writes an invalid patch. RCP visibly enters **Correcting graph update**, asks
  the same native session to rewrite only the semantic patch with the original
  Work access, and leaves the counter at exactly one.
- Drive a scripted Work turn that runs the staged Python validator client. RCP
  answers through request/response files in the writable workspace and records
  the bounded check. Repeat with validator unavailability and confirm its client
  exit is distinct from semantic invalidity.
- Move the canonical graph after context assembly but before Apply. If the
  semantic patch remains legal, RCP re-prepares its bookkeeping and applies it
  against current state under the append lock instead of rejecting it as stale.
- Exhaust the bounded correction rounds. The Work task remains completed, its
  answer and artifacts remain visible, and the reply reports **Graph update
  rejected** with the exact bounded diagnostic. It never offers ordinary Work
  Retry merely because the graph side output failed.
- Switch the composer back to Discuss and ask a follow-up. No new conversation
  is created. Navigate away and return: every sent turn keeps its immutable mode
  badge, and the last composer choice remains the default for the next turn.
- Repeat the mode switch from a non-Experiment node and from project chat.
- Pause and resume a Work turn after changing the composer to Discuss. Resume
  retains the interrupted turn's Work mode, unrestricted permission envelope,
  contextual run scope, native session, and saved stage.
- Inspect an operation outside Discuss authority and the Work prohibition on
  direct canonical `.research` writes. RCP never opens an approval dialog.
  Discuss remains bounded; Work's `.research` boundary is explicitly recorded
  as prompt-enforced for both providers.

The transcript and reading surface stay neutral paper/sheet. Plum and forest are
semantic accents on the mode control, composer binding, send focus, and compact
turn badge. The control has a visible keyboard focus and reduced-motion-safe
state change. There is no helper subtitle beneath it.

## Assertions

- `mode` is persisted on each new human/assistant exchange and task receipt.
  Legacy transcript records remain unlabelled rather than being reclassified.
- The composer mode may change between ordinary turns even when provider,
  model, execution machine, and run scope are locked for the conversation.
- Shift+Tab toggles the selected conversation once whenever the Chats page is
  active, without requiring message-box focus or affecting non-Chats pages.
- A running or resumed task always uses the mode captured when it was launched.
- `allow_graph_change` is gone rather than decoded: `mode` is the only graph
  authority a request carries, and an old payload still naming the retired
  switch is ignored entirely instead of being honoured as a graph-only turn.
- Discuss has no active graph-patch channel. Its native-session master may retain
  the inactive Work contract and stable schema/client pointers for a later mode
  switch; the Discuss marker and CLI capability grant none of that authority. A
  stray `patch.json` is kept only as a diagnostic receipt and never validated or
  applied.
- Discuss and Work receive no indexed conversation pointers, provider roots, or
  prior chat transcript input. Canonical chat history is written only for the
  Chats UI after the turn completes.
- Work receives writable scratch, network access, and unrestricted tooling and
  repository access. Codex bypasses approvals and sandboxing; Claude uses
  `bypassPermissions`. The direct canonical `.research` prohibition is a known
  accepted Work prompt contract for both providers, not an OS-enforced boundary.
  Off-machine repositories remain contextual host/path pointers and RCP never
  copies them.
- A missing or valid empty Work patch spends no revision. A valid non-empty
  semantic Work patch is prepared with RCP-owned patch, Proposal, revision,
  scope, and lifecycle bookkeeping, then revalidated against current state and
  appended exactly once under the canonical append lock.
- Ordinary legal operations land as agent-authored asserted content, including
  edges touching accepted nodes. Only a one-node Decision choice/status
  transition or Hypothesis status transition is represented by an agent
  `Proposal`.
- Work cannot move ingest cursors or coverage.
- Work graph and watcher corrections reuse the native Work session and the same
  unrestricted Work permissions. Only the correction instruction changes; neither
  may re-run project commands or completed external actions, and both stop after the
  configured correction limit. Seed/Refresh generic patch correction remains scratch-only.
- The RCP-staged Python client exchanges request/response files through
  the writable Work workspace. RCP polls locally or through its existing SSH
  run-stage transport, validates against live current state in process, bounds
  and records checks, and returns distinct exits for valid, invalid, and
  unavailable results.
- Apply repeats preparation and the same semantic validation under the append
  lock. It has no original context-revision pin or Resume-ancestor walk, and
  graph movement alone is not a rejection.
- Validation stages operations in their written order against earlier valid
  operations while retaining whole-patch node and edge lookup. It never reorders
  operations; a Proposal may target its node only when that node was created
  earlier in the same outer patch or already exists.
- The final result independently records `graph_update.status` as `none`,
  `applied`, or `rejected`, plus applied revision, proposal ids, bounded
  validation messages, and correction rounds.
- Codex Work and its graph and watcher corrections use
  `--dangerously-bypass-approvals-and-sandbox`; Claude Work and those corrections
  use `--permission-mode bypassPermissions`. Discuss,
  Seed/Refresh and their generic scratch-only patch correction, paper coaching,
  and preview sandbox rules remain unchanged.
- No console error, failed network request, or server traceback appears during
  the browser drive.

Deliberately not possible: a persistent “may change graph” checkbox, an RCP
approval event or modal, a hidden Work repository allowlist, a universal patch
proposal, a second graph-write channel, RCP repository copying, trusting an
earlier self-check at Apply, or an automatic Work rerun after a graph-only
failure.
