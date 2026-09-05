# Graph authoring and settled product boundaries

Status: implementation approved by the human on 2026-09-05; implementation and
verification in progress. This PR retires `docs/open-questions.md` by resolving
its decisions, not by redistributing the same unanswered questions.

## Approved implementation

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

## Verification and closure

Use focused backend and web regressions, documentation/link checks, baseline
checks and a real served-browser graph creation/connection/removal/Sync drive.
Verify warning delivery without rejection, glossary rendering, replay, protected
authority, stale-draft behavior and immutable history. Use disposable data only;
do not mutate real research or production. Preserve unrelated working-tree edits.

Archive this handoff once implementation and its verification are complete, with
current behavior incorporated into the existing specifications. Until then the
PR remains draft and does not claim unverified user-facing behavior.
