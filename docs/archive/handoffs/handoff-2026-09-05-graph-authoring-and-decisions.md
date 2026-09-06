# Graph authoring and settled product boundaries

Status: implementation and local verification complete on 2026-09-05 in PR #50;
archived as evidence, pending PR CI and human merge. The current specifications
and the [decision record](../../decisions/2026-09-05-graph-authoring-and-product-boundaries.md)
own behavior. `docs/open-questions.md` is retired, with its previous text retained
only as an archived historical snapshot.

## Implemented

- Graph-writing agents may add and revise thin project-wide glossary definitions
  through ordinary Patches. Glossary entries are not nodes. Preserve inline
  rendering and historical replay; Discuss remains without graph authority.
- Add nonblocking validator flags for internal-run Evidence missing its producing
  Experiment, newly isolated operational nodes (Experiment, Evidence, Decision,
  Blocker), and identical normalized titles on same-type nodes. Inspect the final
  candidate graph, including later edges, and warn only about introduced issues.
  Do not add a scanner package, another model call, or replay-time quality rules.
- Expose general human graph editing: node insertion, editing and removal, and
  edge creation/removal with relation selection. Use existing draft preview and
  atomic Sync. Removal never deletes history or bypasses active-work safeguards.
- Update owning specs, acceptance and references, retire the rejected scanner
  proposal, and delete the open-questions file after decisions are incorporated.

## Settled boundaries

- Watchers remain completion-based; no wake-on-intermediate-output feature.
  Keep observation-failure handling and retained watcher history. No additional
  watcher cleanup feature or exclusive repository lease.
- No artifact-selection-to-Evidence action. General human graph editing is
  independent of artifact viewing and does not widen WebMCP authority.
- RCP's core is general-purpose. Future data interaction, visualization and domain
  connectors may extend it; remove unsupported research-domain rankings.
- No peer-to-peer agent mail and no new client-side restored-server rollback
  detection. Preserve the existing restore safety procedure without promising
  that a client detects an older snapshot.
- Worktree execution/merge-back and modest live human provider steering belong
  in separate draft PRs for further discussion. Neither is implemented here.

## Verification and limits

The full backend suite passed (3,712 passed, 9 skipped). Focused tests cover thin
glossary payloads and revisions, ordinary/orchestrator/Experiment authority,
Discuss refusal, branch isolation and replay. The real validator client receives
the quality flags as a valid result with exit code zero; final atomic-batch
advice omits issues repaired by later source Patches in that same transition.
The web suite passed all 611 tests; build/typecheck, Ruff, documentation checks
and the all-files pre-commit checks passed. The existing large-chunk build warning
remains advisory.

Web interaction and draft regressions cover node creation, connection staging,
Evidence assessments, drag and keyboard endpoints, undo, read-only controls,
reload persistence, and preservation of the original edge-edit revision. All
structural edits request backend preview, including new ResearchQuestions and
Hypotheses that previously vanished from local preview projections.

The served in-app-browser drive used disposable acceptance data on port 8437.
See [S08's bounded receipt](../../acceptance/S08-human-authority.md). Creation,
connection, undo, prose editing and removal reached revision 6; SHA-256 checks
proved the prior five Patch files unchanged. With the disposable server stopped,
the regression fixture then appended and revised a glossary definition through
the normal typed agent Patch/history owner (revisions 7–8). After restart, the
served browser rendered the revised definition inline; its console, page-error
and HTTP-error capture was empty. This was fixture-backed, not a real provider
dispatch or a production drive. No native desktop code changed.

Current docs replace the former questions; S59's separate scanner proposal is
rejected and archived. Other pending acceptance scenarios were inspected; none
became runnable from this bounded change. The existing full S08 proposal,
Decision, standing and truth-scope combination was not redriven in the browser.

## What remains outside this PR

- PR #48 is a discussion-only draft for composer worktrees and explicit Git
  merge-back. It does not implement repository isolation.
- PR #49 is a discussion-only draft for modest local/SSH live provider steering.
  It does not implement delivery or assume a persistent provider daemon.
- Human review/merge and normal CI remain required. No merge, server update,
  deployment, provider login, or real research-data mutation was performed.
- Unrelated edits in the original main checkout were preserved; this work used
  an isolated worktree and does not include those edits.
