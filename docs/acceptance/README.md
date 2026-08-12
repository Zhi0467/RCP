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
2. **An automated desktop harness.** Browser tooling cannot attach to a native
   application window. Desktop scenarios can still be driven manually through
   the built macOS application, its accessibility tree, screenshots, and shell
   logs, as the existing `last_passed` records show. What does not yet exist is
   one reusable unattended harness for those checks.

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
| [S12](S12-ontology-evolution.md) | Keep historical ontology extensions readable without restoring a schema editor | implemented | pytest + **browser** | covered + driven 2026-08-03 |
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
| [S24](S24-provider-registry.md) | Every agent choice offered is one the provider accepts | implemented | **browser** | driven 2026-08-04 |
| [S25](S25-grounded-belief-ontology.md) | Belief changes are grounded and readable | implemented | pytest + **browser** | covered + driven 2026-07-30 |
| [S26](S26-delete-project.md) | Delete an RCP project without deleting the research project | implemented | pytest + **browser** | covered + driven 2026-07-31 |
| [S27](S27-agent-task-explains-and-recovers.md) | Every launch has one task, authority contract, and recovery cause | implemented | pytest + **browser** | `test_prompts.py`, `test_conversation_retry.py`, `test_api.py`, `test_proposal_boundary.py`, `runDialog.test.mjs`, browser 2026-08-03 |
| [S28](S28-one-backend-two-entrances.md) | One backend, two entrances | implemented | pytest + api | covered + driven 2026-07-31 |
| [S29](S29-refuse-instead-of-taking.md) | Nothing takes a backend that is doing work without saying what it interrupts | implemented | pytest | covered |
| [S30](S30-desktop-window-is-not-the-app.md) | Closing the desktop window never cancels agent work | implemented | **desktop** | live desktop + browser drive |
| [S31](S31-quit-stops-what-it-started.md) | Quit stops what it started, and nothing else | implemented | **desktop** | owned, reused, takeover, and reported forced-timeout paths passed |
| [S32](S32-artifacts-in-the-desktop-window.md) | A preview opens and downloads land, isolated more strongly than in a browser | pending | **desktop** | artifact card fixed; native preview/download drive pending |
| [S33](S33-a-seed-corrects-itself.md) | A seed that goes wrong corrects itself | implemented | pytest + **browser** | covered + driven 2026-07-31 |
| [S34](S34-packaged-app-needs-no-toolchain.md) | A dev shell that loads the checkout, and a release app that needs nothing | implemented | **desktop** | partial drive 2026-07-31 |
| [S35](S35-packaged-environment-parity.md) | RCP knows where your tools are, and you can see and correct it | blocked-external | **desktop** | none |
| [S36](S36-updating-never-interrupts-work.md) | An update waits for idle, and never interrupts work without being asked | blocked-external | **desktop** | source-covered; update channel required |
| [S37](S37-desktop-text-scale.md) | Text stays readable throughout the desktop app | implemented | **desktop** | covered + driven 2026-07-31 |
| [S38](S38-chat-workspace.md) | Keep the node in view while its conversation continues | implemented | **browser** | covered + driven 2026-08-01 |
| [S40](S40-discuss-and-work.md) | Change one conversation from discussion into work | implemented | pytest + **browser** | 10 checks |
| [S41](S41-bounded-experiment-control.md) | Run an experiment through a bounded control loop | implemented | pytest + **browser** | covered + driven 2026-08-05 |
| [S42](S42-watchers-wake-conversations.md) | Watch external work and wake its conversation | implemented | pytest + **browser** | covered + driven 2026-08-05 |
| [S45](S45-floating-window-dock.md) | Dock a floating node window without closing it | implemented | **browser** | driven 2026-08-02 |
| [S46](S46-project-header-and-chat-split.md) | Fold the project utilities and resize the Chats split | implemented | **browser** | driven 2026-08-02 |
| [S47](S47-agent-usage-ledger.md) | See counted provider usage in Settings | implemented | pytest + **browser** | 3 checks + driven 2026-08-02 |
| [S48](S48-screen-story-token-scale.md) | Measure project usage in favorite screen stories | implemented | **browser** | 5 checks + driven 2026-08-02 |
| [S49](S49-chat-node-reference-links.md) | Open an existing node from a chat answer | implemented | **browser** | covered + driven 2026-08-02 |
| [S50](S50-minimal-agent-proposal-boundary.md) | Hypothesis status Proposals stay evidence-grounded | implemented | pytest | covered |
| [S51](S51-live-agent-patch-validation.md) | Every patch-producing task checks the exact semantic patch RCP will apply | implemented | pytest | covered |
| [S52](S52-explicit-rejection-and-node-removal.md) | Judge explicitly and remove current graph nodes without rewriting history | implemented | **browser** | covered + driven 2026-08-03 |
| [S53](S53-truthful-attention-and-run-surfaces.md) | Attention and run surfaces tell one truthful story | implemented | **browser** | covered + driven 2026-08-08 |
| [S54](S54-paper-preview-and-resizable-node-detail.md) | Read authored Markdown and resize the node being inspected | implemented | **browser** | covered + driven 2026-08-03 |
| [S55](S55-project-owned-agent-profile.md) | Project Settings owns agent configuration | implemented | pytest + **browser** | covered + driven 2026-08-03 |
| [S56](S56-plain-language-revision-history.md) | Read what changed between graph revisions | implemented | pytest + **browser** | covered + driven 2026-08-03 |
| [S58](S58-inline-glossary-definitions.md) | Definitions appear where a term is read | implemented | **browser** | covered + driven 2026-08-03 |
| [S59](S59-staged-graph-audit-skills.md) | An agent audits the graph patch it is about to finish | pending — **not human-confirmed** | pytest + **browser** | none |
| [S60](S60-plain-language-project-setup.md) | Add a project with plain-language setup steps | pending — **not human-confirmed** | **browser** | none |
| [S62](S62-direct-provider-log-ingestion.md) | Seed and Refresh point agents at provider logs instead of moving them | implemented | pytest + **browser** | partial live check 2026-08-04 |
| [S63](S63-agent-run-lock-recovery.md) | RCP recovers agent-run ownership; the human never removes a lock | implemented | pytest + api | tests; api drive not recorded |
| [S64](S64-project-skill-workflow-selection.md) | Choose project workflows and skills, then load them into a run | implemented | pytest + **browser** | 16 checks + driven 2026-08-04 |
| [S65](S65-concurrent-agent-tasks.md) | Multiple agent tasks can run at once | implemented | pytest + **browser** | covered + driven 2026-08-04 |
| [S66](S66-no-global-task-banner.md) | Agent tasks do not appear as a global banner | implemented | **browser** | browser drive not recorded |
| [S67](S67-proposal-action-legibility.md) | Pending proposals state the exact option or status transition | implemented | pytest + **browser** | tests; browser drive not recorded |
| [S68](S68-chat-progress-start-feedback.md) | Chat progress appears immediately under a sent message | implemented | **browser** | browser drive not recorded |
| [S69](S69-agent-proposal-withdrawal.md) | Agents can withdraw obsolete pending proposals | implemented | pytest | covered |
| [S71](S71-chat-master-context-and-deltas.md) | Chat sends one master context, then turn markers and compact deltas | implemented | pytest | 2026-08-04 |
| [S72](S72-runs-operational-hierarchy.md) | Runs leads with live operational state | implemented | **browser** | tests + browser 2026-08-06 |
| [S73](S73-experiment-loop-native-wake-continuity.md) | Watcher wakes continue one bounded episode session | implemented | pytest | tests + browser 2026-08-06 |
| [S74](S74-boundary-inputs-fail-closed.md) | Uncommon boundary inputs fail closed without damaging the project | implemented | pytest + browser | tests + browser 2026-08-06 |
| [S75](S75-network-access-on-every-agent-surface.md) | Every user-facing agent task can read the public web | implemented | pytest + **browser** | tests + Codex 2026-08-07 + Claude 2026-08-12 |
| [S76](S76-graph-condition-wake.md) | Wake a conversation when a canonical graph condition becomes true | implemented | pytest | tests + web 2026-08-12 |
| [S77](S77-auto-research-stops-at-belief.md) | Let auto-research create freely and propose changes to existing epistemic nodes | implemented | pytest | covered |
| [S78](S78-one-budget-one-stop.md) | Give one auto-research campaign one budget and one graceful stop | pending | **browser** | none |
| [S79](S79-cold-desktop-launch-renders.md) | A cold desktop launch never rests on a blank window | implemented | **desktop** | driven 2026-08-07 |
| [S80](S80-question-hierarchy-flow-columns.md) | Read question hierarchy from the Research flow columns | implemented | **browser** | layout tests + driven 2026-08-07 |
| [S81](S81-live-canonical-state.md) | Canonical graph changes appear without reloading the UI | implemented | api + **browser** | tests + driven 2026-08-07 |
| [S82](S82-view-state-survives-navigation.md) | Return to the same panel position after navigating away | implemented | **browser** | driven 2026-08-07 |
| [S83](S83-agent-retires-experiment-watchers.md) | Let an Experiment agent retire observers for work it cancelled | implemented | pytest | covered |
| [S84](S84-watchers-poll-with-persistent-backoff.md) | Poll watchers patiently with durable error backoff | implemented | pytest | covered |
| [S85](S85-grouped-watchers-wake-once.md) | Wake once when every watcher in a group is finished or persistently degraded | implemented | pytest + **browser** | tests + driven 2026-08-07 |
| [S86](S86-human-decides-a-decision.md) | Decide a Decision by clicking an option | implemented | pytest + **browser** | tests + driven 2026-08-07 |
| [S87](S87-experiment-prerequisite-chains.md) | Construct causal action chains around experiments | implemented | pytest + api | tests + real provider 2026-08-08 |
| [S88](S88-node-attached-agent-authority.md) | Let permitted agents maintain resources attached to a node | implemented | pytest + **browser** | covered + driven 2026-08-08 |
| [S89](S89-provider-native-skill-inventory.md) | Offer provider-native skills beside RCP packages | implemented | pytest + **browser** + ssh | covered + driven 2026-08-08 |
| [S90](S90-desktop-chat-dictation.md) | Turn one spoken segment into an editable chat draft | pending | **desktop** | native + span tests + desktop control; live audio pending |
| [S91](S91-chat-input-attachments.md) | Send bounded temporary files with one chat turn | implemented | pytest + **browser** + ssh | tests + remote browser/SSH drive |
| [S93](S93-one-hop-relation-map.md) | Read a node's immediate structure without leaving it | implemented | **browser** | tests + browser 2026-08-08 |
| [S94](S94-decision-ripeness-and-the-agent-contract.md) | Ordinary agents queue Decisions; they do not decide them | implemented | pytest | covered |
| [S95](S95-durable-team-space.md) | A team space outlives every process that serves it | pending — **not human-confirmed** | pytest + api | none |
| [S96](S96-joining-a-team-space.md) | Join a team space once, and stay joined | pending — **not human-confirmed** | pytest + api | none |
| [S97](S97-a-project-carries-its-identity.md) | A project says who it is and where it belongs | implemented | pytest + **browser** | tests + browser 2026-08-11 |
| [S98](S98-move-a-project-into-a-team-space.md) | Hand a personal project over to the lab, once | pending — **not human-confirmed** | pytest + **browser** | none |
| [S99](S99-attribution-travels-with-history.md) | History says who authorized a change | implemented | pytest + **browser** | tests + browser 2026-08-11 |
| [S100](S100-permission-is-checked-twice.md) | Nothing unauthorized starts, and nothing unauthorized lands | implemented | pytest | dispatch authority + live Apply movement |
| [S101](S101-project-membership-and-invitations.md) | Being in the lab is not being on the project | pending — **not human-confirmed** | pytest + **browser** | none |
| [S102](S102-team-runs-execute-as-the-space-account.md) | Team work runs where the space can reach it, as the space | pending — **not human-confirmed** | pytest + api + ssh | none |
| [S103](S103-server-operations-are-console-operations.md) | Dangerous operations need the machine, not a login | pending — **not human-confirmed** | pytest + api | none |
| [S104](S104-backups-never-pause-work.md) | A backup interrupts nothing and overclaims nothing | pending — **not human-confirmed** | pytest | none |
| [S105](S105-move-between-spaces-in-one-window.md) | One window, several spaces | pending — **not human-confirmed** | **desktop** | none |
| [S106](S106-cross-project-experiment-board.md) | See every launched Experiment loop before opening a project | implemented | pytest + **browser** | tests + browser 2026-08-09 |
| [S107](S107-open-project-tabs.md) | Keep several projects open in one RCP window | implemented | **browser** + **desktop** | `web/tests/projectTabs.test.mjs` |
| [S108](S108-repository-file-links-preserve-desktop-window.md) | A repository file link never strands the desktop window | implemented | **desktop** | tests + desktop 2026-08-09 |
| [S109](S109-tabs-stay-current-without-freezing.md) | A project tab stays current without ever waiting on the remote | implemented | pytest + **browser** | tests + browser + live SSH 2026-08-11 |
| [S110](S110-paper-draft-survives-a-canonical-change.md) | A paper draft survives a canonical change without choosing a side | implemented | pytest + **browser** | paper/storage + Incoming UI checks |
| [S111](S111-durable-space-identity.md) | A space keeps one identity across process and path changes | implemented | pytest + api | tests + api 2026-08-11 |
| [S112](S112-basic-human-identity.md) | A person has one durable identity inside a space | implemented | pytest + api + **browser** | tests + api + landing browser 2026-08-12 |
| [S113](S113-campaign-attribution.md) | Campaign work retains its authorization lineage | pending | pytest + **browser** | none |
| [S114](S114-see-your-results-without-leaving.md) | See your results without leaving RCP | pending | pytest + **browser** + ssh | none |
| [S115](S115-beliefs-change-only-through-you.md) | An agent may rewrite anything except what you believe | implemented | pytest + **browser** | focused tests + Inbox drive 2026-08-12 |
| [S116](S116-choose-existing-or-fresh-research.md) | Choose existing research or start fresh before setup changes anything | implemented | pytest + **browser** + ssh | setup, transport, history, browser + live SSH 2026-08-12 |
| [S117](S117-project-owned-caches.md) | Clear one project's cache without clearing another project's cache | implemented | pytest + **browser** | cache lifecycle, deletion, API, web, browser 2026-08-12 |
| [S118](S118-identity-and-membership-start-at-the-index.md) | Put personal identity and an explicit team seam on the project index | implemented | pytest + **browser** | tests + browser 2026-08-12 |

S95–S105 are the original team-space set. They come from the confirmed design in
[`../design/`](../design/README.md). S97, S99, and the narrower S111–S112
prerequisites are implemented; the other team-space scenarios are not
human-confirmed and do not authorize implementation. S77 was rewritten in the
same design pass: its earlier child-produced-Proposal approval rule was removed,
and every agent-produced Proposal now waits for a human.

Ids are never reused. Gaps are retired scenarios or promises folded into another
scenario or the test-defended list below; a new scenario takes the next free
number.

## Promises already defended by tests

These need no file. Each is a real promise, each is fully covered, and none of
them carries a design decision or a frontend half — so a file would only restate
its tests and add somewhere else to keep in sync.

If one of these ever grows a UI path, a refusal rule, or a browser assertion,
promote it back to a file with a fresh id.

| Promise | Defended by |
|---|---|
| Reopening and refreshing a project appends, and never edits a prior patch | `test_sync.py::test_replay_ignores_an_uncommitted_hidden_batch`, `::test_interrupted_batch_write_exposes_none_of_the_sync`, `test_history.py::test_successful_patch_materializes_processed_cursors` |
| Preparing an agent run does not replay canonical history merely to rediscover its revision, and Sync reuses its validated pending replay after commit | `test_api.py::test_graph_stream_reuses_revision_from_assembled_context`, `test_sync.py::test_graph_sync_builds_from_the_single_in_lock_current_replay`, `::test_batch_reuses_pending_replay_for_committed_outputs` |

Do not maintain hand-counted coverage summaries here; they become stale as soon
as a scenario is added or merged. Frontmatter is the inventory. For an
implemented `api`, `browser`, or `desktop` scenario, a missing `last_passed`
means its end-to-end drive remains verification debt. `last_checked` records a
partial drive and must not be presented as a pass.

S13 also preserves an ordering constraint from v0.5: the structural/authoring
split landed before the replay halt, so replay can distinguish a patch rejected
at admission from an accepted patch that later fails structural integrity.

The artifact-preview feature has a three-part gate: S16 is the implemented
deterministic merge contract, S17 is the live provider/version gate and must be
re-run when a provider launch contract changes, and S18 is the live remote gate
and must be re-run when remote stage or SSH transport behavior changes.

## Adding one

When you hit a bug, write the scenario that would have caught it, then fix the
bug. Every bug becomes a permanent check instead of a memory.

When you build a feature, the scenario comes first, as `pending`, with its UI
path proposed and confirmed.
