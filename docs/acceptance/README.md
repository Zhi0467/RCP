# Acceptance scenarios

These describe what RCP must actually do, in the language of someone using it.
They are the definition of "done" for a feature, a bug fix, or any substantial
change. `uv run pytest` passing is a precondition, not the finish line.

A scenario states a **promise**. A test is one way to check a promise. Those are
different things, which is why both exist:

- `test_unauthorized_chat_patch_is_discarded_not_applied` is a filename.
- "Asking a question costs no graph revision, and an agent cannot grant itself
  permission by writing the file anyway" is a promise — and you can tell whether
  you still believe in it a year from now.

Many promises here are already checked by tests. That is a good outcome, not a
redundancy: the promise is written down once, in one place, traceable to the
checks that defend it.

## Two halves

- **Drive** — what happens, in prose. For browser scenarios that means what a
  person clicks; for others it is a request or a fixture. Written as prose on
  purpose: when a button moves or a label changes, the prose still reads true.
- **Assert** — what must be true afterward. Named checks that return the same
  answer every run. Many read state the screen never shows.

An agent asked "does this look right?" is a lenient judge — it will call a
broken column order a minor layout difference. So the agent drives, and the
checks decide. The agent's opinion is not the verdict.

## Driver — how a promise is checked

Not every promise needs a browser. Getting this wrong in either direction is
expensive: browser theater around a backend fact is slow and flaky, and a
backend test standing in for frontend state proves nothing.

| `driver` | Means | Cost |
|---|---|---|
| `pytest` | A test function. Usually one that already exists. | free |
| `api` | HTTP against a served app, no browser. | seconds |
| `browser` | Genuinely needs the UI driven. | minutes |
| `desktop` | Needs the app driven in its own window, not a browser. | minutes |

**The rule for choosing:** a promise needs `browser` exactly when the thing that
can break lives in the browser — pin state, draft state, split position, a
toggle resetting, a run staying visible across views. Backend truth is cheaper
and more reliable to check with pytest, always.

`desktop` is `browser`'s equivalent for the application window, and it is a
separate value because none of the browser tooling reaches one. It is earned by
the same rule and one addition: a promise about the shell itself — window
lifecycle, quit semantics, an embedded webview's own behavior. Server ownership
is not that, even when a desktop app is what motivated it.

`covered_by:` names the tests that already defend the promise. `covered_by:
none` is the interesting case — a promise nothing checks.

`last_passed:` is the date the scenario was last seen to pass, end to end. A
`pytest`-driven scenario carries no date, because its tests run on every change
and the date would only ever restate that. It is the `browser` and `api` ones
that go quietly stale, so those are the ones that carry it.

## End of a coding session

Anything that debugged, built a feature, or changed a module significantly ends
with a sweep — not a re-run:

1. **Grep the `pending` and `blocked-external` scenarios.** For each, ask two
   questions: *can this be run now* — did today's work build the feature, or make
   the missing machine reachable — and *should this be rewritten*, because what
   we learned today changed what the promise ought to say.
2. **Update anything the session invalidated.** A scenario describing a UI path
   that got built differently is worse than no scenario.
3. **Do not re-run `implemented` scenarios** unless asked. They cost real time,
   and a green result nobody asked for buys nothing.
4. **Stamp `last_passed`** on whatever you did drive to a pass.

The asymmetry is deliberate. Pending scenarios rot because the world moves under
them; implemented ones only rot when code changes, and the code change is what
prompts a re-run.

```bash
grep -l "^status: \(pending\|blocked-external\)" docs/acceptance/S*.md
```

## Status

| Status | Meaning | Red means |
|---|---|---|
| `implemented` | Built and expected to work | **Regression.** Something broke. |
| `pending` | Written before the feature exists | Nothing. It hasn't been built. |
| `blocked-external` | Needs something this machine lacks | Can't run. Never counted as passing. |

A `pending` scenario turning green is how a feature is known to have landed —
not "the code looks right." Green.

## Scenarios come before code

For a new feature, a bug you actually hit, or a substantial change to a module,
**the scenario is written and confirmed with the human first.** Not after, and
not as documentation of what got built.

This is the point of the whole directory. Settling the scenario is where the
design decisions actually get made — where the UI path lives, what is refused,
what happens to existing work. Those decisions get made either way; writing them
first means they get made deliberately, by the person who has to live with them,
instead of being improvised inside an implementation and discovered later.

So a `pending` scenario carries one more section than an implemented one:

### UI path (proposal)

Where the thing lives, what the controls are, what is deliberately **not**
possible, and the open questions that need a human answer. Marked as a proposal
until confirmed, because the agent proposing it is guessing.

An implemented scenario does not need this section — the app is the answer.

The workflow, in `AGENTS.md` terms: propose scenarios → **confirm with the
human** → then plan and implement → the scenario turning green is done.

## Tier

- `hermetic` — a fake agent, a throwaway data directory, a temporary copy of a
  state repo. Deterministic, free, unattended.
- `live` — a real Codex or Claude run. Nondeterministic, so it asserts shape,
  never content. Before a release, not every change.
- `remote` — needs a reachable SSH host.
- `packaged` — needs a built application bundle rather than a source checkout,
  and sometimes an account that has granted it nothing. Never runs from `uv`.

## Hermetic served-app agent

`rcp serve --acceptance-agent` explicitly replaces provider launch with the
deterministic local acceptance agent. It refuses remote projects, uses bounded
CPU-only fixture jobs, reports `agent_mode: acceptance` from `/api/health`, and
shows a persistent warning in the UI. Never use it with canonical project data.

## What does not exist yet

1. **The check functions** for anything whose `covered_by` is `none`.
2. **The desktop harness.** No tooling here drives an application window; the
   browser tools cannot attach to one. The intended mechanism is WebdriverIO's
   Tauri service with its embedded macOS driver, and it is unproven in this
   repository — proving it is part of building the shell, not a later chore. A
   `desktop` scenario is not runnable until it exists.

A `browser` scenario is **runnable today** — an agent drives the app and reports
what it found. S03 and S08's browser half need no fake agent at all, since no
agent runs in either. What is missing there is not a harness, it is that nobody
has run them and nothing persists the result.

## Scenarios

A scenario earns a file when it carries something code cannot tell you: a design
decision, a UI path, a refusal rule, an open question — or when nothing can check
it but a browser or a machine we do not have.

| # | Promise | Status | Driver | Covered |
|---|---|---|---|---|
| [S01](S01-first-project.md) | Start the app and build a first graph | implemented | api + browser | partial |
| [S03](S03-views-and-graph-controls.md) | Move between views and work the graph | implemented | **browser** | partial |
| [S08](S08-human-authority.md) | Human authority, and Sync as the only commit | implemented | pytest + **browser** | backend only |
| [S10](S10-pause-resume-retry.md) | Agent work is durable | implemented | pytest + **browser** | backend only |
| [S11](S11-paper-coach.md) | The coach reads and never writes | implemented | pytest + **browser** | partial |
| [S12](S12-ontology-evolution.md) | Keep historical ontology extensions readable | implemented | pytest | covered |
| [S13](S13-replay-halts.md) | A bad patch stops replay instead of vanishing | implemented | pytest | covered |
| [S14](S14-remote-state.md) | Canonical state on another machine | implemented | api | partial |
| [S15](S15-real-agent.md) | One real agent run, end to end | implemented | api | **none** |
| [S16](S16-chat-artifact-contract.md) | A preview is optional; the answer and graph are not | implemented | pytest | covered |
| [S17](S17-real-agent-preview.md) | A real provider produces the same preview | implemented | browser | driven 2026-07-30 |
| [S18](S18-remote-artifact-preview.md) | A remote preview stays remote and temporary | implemented | api + browser | driven 2026-07-30 |
| [S19](S19-nothing-typed-is-lost.md) | Nothing typed is ever lost | implemented | **browser** | driven 2026-07-30 |
| [S20](S20-no-ui-commentary-lines.md) | Primary UI elements stand on their own | implemented | **browser** | driven 2026-07-30 |
| [S21](S21-compact-project-navigation.md) | The project shell says only what is needed | implemented | **browser** | driven 2026-07-30 |
| [S22](S22-fast-project-open.md) | Opening a project does one authoritative replay | implemented | pytest + **browser** | driven 2026-07-30 |
| [S23](S23-margin-visual-system.md) | RCP uses Margin's visual grammar | implemented | **browser** | driven 2026-07-30 |
| [S24](S24-provider-registry.md) | Every agent choice offered is one the provider accepts | implemented | **browser** | driven 2026-08-01 |
| [S25](S25-grounded-belief-ontology.md) | Belief changes are grounded and readable | implemented | pytest + **browser** | covered + driven 2026-07-30 |
| [S26](S26-delete-project.md) | Delete an RCP project without deleting the research project | pending | pytest + **browser** | none |
| [S27](S27-agent-task-explains-and-recovers.md) | Every launch has one task, authority contract, and recovery cause | implemented | pytest + **browser** | `test_prompts.py`, `test_conversation_retry.py`, `test_api.py`, `test_proposal_boundary.py`, `runDialog.test.mjs`, browser 2026-08-03 |
| [S28](S28-one-backend-two-entrances.md) | One backend, two entrances | pending | pytest + api | none |
| [S29](S29-refuse-instead-of-taking.md) | Nothing takes a backend that is doing work without saying what it interrupts | pending | pytest | none |
| [S30](S30-desktop-window-is-not-the-app.md) | Closing the desktop window never cancels agent work | pending | **desktop** | none |
| [S31](S31-quit-stops-what-it-started.md) | Quit stops what it started, and nothing else | pending | **desktop** | none |
| [S32](S32-artifacts-in-the-desktop-window.md) | A preview opens and downloads land, isolated more strongly than in a browser | pending | **desktop** | none |
| [S33](S33-a-seed-corrects-itself.md) | A seed that goes wrong corrects itself | implemented | pytest + **browser** | covered + driven 2026-07-31 |
| [S34](S34-packaged-app-needs-no-toolchain.md) | A dev shell that loads the checkout, and a release app that needs nothing | pending | **desktop** | none |
| [S35](S35-packaged-environment-parity.md) | RCP knows where your tools are, and you can see and correct it | blocked-external | **desktop** | none |
| [S36](S36-updating-never-interrupts-work.md) | An update waits for idle, and never interrupts work without being asked | pending | **desktop** | none |
| [S37](S37-desktop-text-scale.md) | Text stays readable throughout the desktop app | implemented | **desktop** | covered + driven 2026-07-31 |
| [S38](S38-chat-workspace.md) | Keep the node in view while its conversation continues | implemented | **browser** | covered + driven 2026-08-01 |
| [S39](S39-project-sized-run-preparation.md) | Repeated run preparation reuses unchanged source metadata | superseded by S62 | pytest | historical |
| [S40](S40-discuss-and-work.md) | Change one conversation from discussion into work | implemented | pytest + **browser** | 10 checks |
| [S41](S41-bounded-experiment-control.md) | Run an experiment through a bounded control loop | implemented | pytest + **browser** | covered + driven 2026-08-05 |
| [S42](S42-watchers-wake-conversations.md) | Watch external work and wake its conversation | implemented | pytest + **browser** | covered + driven 2026-08-05 |
| [S43](S43-agent-execution-module-boundaries.md) | Keep agent behavior intact while execution code moves | implemented | pytest | 10 checks |
| [S44](S44-chat-conversation-projection-permissions.md) | Chat does not ingest transcripts; Seed/Refresh read logs in place | pending | pytest + **browser** | 4 checks |
| [S45](S45-floating-window-dock.md) | Dock a floating node window without closing it | implemented | **browser** | driven 2026-08-02 |
| [S46](S46-project-header-and-chat-split.md) | Fold the project utilities and resize the Chats split | implemented | **browser** | driven 2026-08-02 |
| [S47](S47-agent-usage-ledger.md) | See counted provider usage in Settings | implemented | pytest + **browser** | 3 checks + driven 2026-08-02 |
| [S48](S48-screen-story-token-scale.md) | Measure project usage in favorite screen stories | implemented | **browser** | 5 checks + driven 2026-08-02 |
| [S49](S49-chat-node-reference-links.md) | Open an existing node from a chat answer | implemented | **browser** | covered + driven 2026-08-02 |
| [S50](S50-minimal-agent-proposal-boundary.md) | Agents propose only decisions and belief changes | implemented | pytest | covered |
| [S51](S51-live-agent-patch-validation.md) | A Work agent checks the exact semantic patch RCP will apply | implemented | pytest | 2026-08-03 |
| [S52](S52-explicit-rejection-and-node-removal.md) | Judge explicitly and remove current graph nodes without rewriting history | implemented | pytest + **browser** | covered + driven 2026-08-03 |
| [S53](S53-truthful-attention-and-run-surfaces.md) | Attention and run surfaces tell one truthful story | implemented | **browser** | covered + driven 2026-08-03 |
| [S54](S54-paper-preview-and-resizable-node-detail.md) | Read authored Markdown and resize the node being inspected | implemented | **browser** | covered + driven 2026-08-03 |
| [S55](S55-project-owned-agent-profile.md) | Project Settings owns agent configuration | implemented | pytest + **browser** | covered + driven 2026-08-03 |
| [S56](S56-plain-language-revision-history.md) | Read what changed between graph revisions | implemented | pytest + **browser** | covered + driven 2026-08-03 |
| [S57](S57-fixed-product-ontology.md) | Existing ontology extensions remain readable without a schema editor | implemented | pytest + **browser** | covered + driven 2026-08-03 |
| [S58](S58-inline-glossary-definitions.md) | Definitions appear where a term is read | implemented | **browser** | covered + driven 2026-08-03 |
| [S59](S59-staged-graph-audit-skills.md) | An agent audits the graph patch it is about to finish | pending — **not human-confirmed** | pytest + **browser** | none |
| [S60](S60-plain-language-project-setup.md) | Add a project with plain-language setup steps | pending — **not human-confirmed** | **browser** | none |
| [S61](S61-app-scoped-provider-readiness.md) | Opening a project does not recheck providers | implemented | pytest + **browser** | driven 2026-08-04 |
| [S62](S62-direct-provider-log-ingestion.md) | Seed and Refresh point agents at provider logs instead of moving them | implemented | pytest + **browser** | covered + live Seeds 2026-08-04 |
| [S63](S63-agent-run-lock-recovery.md) | RCP recovers agent-run ownership; the human never removes a lock | implemented | pytest + api | covered |
| [S64](S64-project-skill-workflow-selection.md) | Choose project workflows and skills, then load them into a run | implemented | pytest + **browser** | 16 checks + driven 2026-08-04 |
| [S65](S65-concurrent-agent-tasks.md) | Multiple agent tasks can run at once | implemented | pytest + **browser** | covered + driven 2026-08-04 |
| [S66](S66-no-global-task-banner.md) | Agent tasks do not appear as a global banner | implemented | **browser** | implemented |
| [S67](S67-proposal-action-legibility.md) | Pending proposals state the exact option or status transition | implemented | pytest + **browser** | covered |
| [S68](S68-chat-progress-start-feedback.md) | Chat progress appears immediately under a sent message | implemented | **browser** | covered |
| [S69](S69-agent-proposal-withdrawal.md) | Agents can withdraw obsolete pending proposals | implemented | pytest | covered |
| [S70](S70-uniform-patch-validation-contract.md) | Patch-producing tasks uniformly self-check through the validator | implemented | pytest | 2026-08-04 |
| [S71](S71-chat-master-context-and-deltas.md) | Chat sends one master context, then turn markers and compact deltas | implemented | pytest | 2026-08-04 |

Ids are never reused. The gaps are scenarios that were folded into the list
below; a new scenario takes the next free number.

## Promises already defended by tests

These need no file. Each is a real promise, each is fully covered, and none of
them carries a design decision or a frontend half — so a file would only restate
its tests and add somewhere else to keep in sync.

If one of these ever grows a UI path, a refusal rule, or a browser assertion,
promote it back to a file with a fresh id.

| Promise | Defended by |
|---|---|
| Reopening and refreshing a project appends, and never edits a prior patch | `test_sync.py::test_replay_ignores_an_uncommitted_hidden_batch`, `::test_interrupted_batch_write_exposes_none_of_the_sync`, `test_history.py::test_successful_patch_materializes_processed_cursors` |
| A question with no authority changes nothing, and an agent cannot grant itself authority by writing the file; scratch remains writable for disposable outputs | `test_api.py::test_unauthorized_chat_patch_is_discarded_not_applied`, `::test_node_chat_answers_without_writing_a_patch`. One frontend residual is uncovered: authorization is **per turn**, so the toggle must not stay on after a send — correct today at [NodeChat.tsx:85](../../web/src/components/NodeChat.tsx:85), defended by nothing |
| An authorized question changes exactly one thing, and is refused if the graph moved under it | `test_api.py::test_authorized_chat_launch_is_not_read_only`, `::test_chat_patch_cannot_move_the_ingest_boundary`, `::test_chat_patch_is_refused_when_the_graph_moved_under_it` |
| A conversation outlives its turns: one folder, prior patch cleared, and provider continuation state is retained without using chat history as agent context | `test_api.py::test_chat_turns_share_one_scratch_folder_and_drop_the_last_patch`, `::test_same_chat_id_uses_distinct_stages_for_distinct_projects` |
| A bad patch is corrected in-session, and a failed run keeps its work | `test_api.py::test_invalid_patch_is_corrected_in_the_same_native_session`, `::test_failed_run_retains_its_patch_and_scratch_folder`, `::test_patch_under_an_unexpected_filename_is_still_applied`, `::test_patch_collector_prefers_patch_json_and_refuses_ambiguity` |
| An authorized turn that changes nothing spends no revision | **nothing — test to write.** The Sync analogue exists (`test_sync.py::test_graph_sync_no_net_change_writes_no_patch`); the chat path has none |

Three things these tables say out loud:

- **Ten of sixteen implemented API/browser scenarios have persisted verdicts.**
  S01, S03, S08, S10, S11, and S15 still need their named end-to-end drive.
- **Six implemented scenarios remain without automated coverage.** Some are
  deliberately live or visual. The two highest-value holes are an authorized
  chat turn that changes nothing (a cheap test) and S15's real-provider seam.
- **S13 established an ordering constraint in v0.5.** The
  structural/authoring split (§6.4) landed before the replay halt (§6.4b), so
  replay can distinguish a patch rejected at admission from an accepted patch
  that later fails structural integrity.

The artifact-preview feature has a three-part gate: S16 is the implemented
deterministic merge contract, S17 is the live provider/version gate and must be
re-run when a provider launch contract changes, and S18 is the live remote gate
and must be re-run when remote stage or SSH transport behavior changes.

## Adding one

When you hit a bug, write the scenario that would have caught it, then fix the
bug. Every bug becomes a permanent check instead of a memory.

When you build a feature, the scenario comes first, as `pending`, with its UI
path proposed and confirmed.
