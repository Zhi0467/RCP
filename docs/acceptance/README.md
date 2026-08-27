# Active acceptance scenarios

An acceptance scenario is a durable product-level promise whose user journey,
authority boundary, recovery/data-loss risk, remote/live integration, or browser
or desktop interaction adds information that unit tests and the current
specification cannot express alone.

Do not create a scenario for every bug, refactor, transition rule, API shape, or
module-local regression. Add regression tests instead. Create or retain a
scenario only when work introduces or reveals a durable product promise not
already covered here. A major cross-module journey still merits one scenario.

For a new unconfirmed journey, write the proposed human path and settle it before
implementation. A handoff already marked human-confirmed and ready to implement
does not need another confirmation interview. Completion requires relevant tests
and every applicable active scenario, not a scenario invented after the code.

## Frontmatter

- `status`: `implemented`, `pending`, or `blocked-external`.
- `tier`: `hermetic`, `live`, `remote`, or `packaged`.
- `driver`: `pytest`, `api`, `browser`, or `desktop`, combined where needed.
- `covered_by`: exact repeatable checks already defending the promise.
- `last_passed`: the last complete API/browser/desktop drive. `last_checked`
  may record a partial check and is never presented as a pass.

Use the cheapest driver that can prove the promise. A browser is required when
the state that can break lives in the frontend; a desktop drive is required for
native shell behavior. Backend truth should use pytest or API checks. Unit tests
and a clean build never stand in for an applicable browser/desktop path.

`rcp serve --acceptance-agent` provides deterministic local provider behavior
for hermetic served-app drives and must never be used with canonical project
data. Live, remote, and packaged tiers require the named external environment;
report unavailable dependencies as skipped rather than passed.

## End-of-session sweep

After feature, bug, or substantial module work, inspect active pending and
external-blocked scenarios:

```bash
grep -l "^status: \(pending\|blocked-external\)" docs/acceptance/S*.md
```

Drive ones the session made runnable, rewrite paths the implementation made
wrong, and stamp only complete passes. Re-run an implemented scenario when the
code change touches its promise or when asked—not because time passed.

Scenario ids are permanent and never reused. Implemented minor, module-local,
or redundant scenarios live intact in [`../archive/acceptance/`](../archive/acceptance/README.md)
and are not current authority.

## Active index

This index is checked against frontmatter by `tests/test_documentation.py`.

| ID | Promise | Status | Driver |
|---|---|---|---|
| [S01](S01-first-project.md) | Start the app and build a first graph | implemented | api + browser |
| [S03](S03-views-and-graph-controls.md) | Move between views and work the graph | implemented | browser |
| [S08](S08-human-authority.md) | Human authority, and Sync as the only commit | implemented | pytest + browser |
| [S10](S10-pause-resume-retry.md) | Agent work is durable | implemented | pytest + browser |
| [S11](S11-paper-coach.md) | The coach reads and never writes | implemented | pytest + browser |
| [S13](S13-replay-halts.md) | A bad patch stops replay instead of vanishing | implemented | pytest |
| [S14](S14-remote-state.md) | Canonical state on another machine | implemented | api |
| [S15](S15-real-agent.md) | One real agent run, end to end | implemented | api |
| [S17](S17-real-agent-preview.md) | A real provider produces the same preview | implemented | browser |
| [S18](S18-remote-artifact-preview.md) | A remote preview stays remote and temporary | implemented | api + browser |
| [S19](S19-nothing-typed-is-lost.md) | Nothing typed is ever lost | implemented | browser |
| [S26](S26-delete-project.md) | Delete an RCP project without deleting the research project | implemented | pytest + browser |
| [S30](S30-desktop-window-is-not-the-app.md) | Closing the desktop window never cancels agent work | implemented | desktop |
| [S31](S31-quit-stops-what-it-started.md) | Quit stops what it started, and nothing else | implemented | desktop |
| [S32](S32-artifacts-in-the-desktop-window.md) | A preview opens and a download lands, with stronger desktop isolation | pending | desktop |
| [S34](S34-packaged-app-needs-no-toolchain.md) | Dev shell loads the checkout; release app needs no toolchain | implemented | desktop |
| [S35](S35-packaged-environment-parity.md) | RCP exposes and can correct its tool environment | blocked-external | desktop |
| [S36](S36-updating-never-interrupts-work.md) | Updating waits for idle and never interrupts silently | blocked-external | desktop |
| [S40](S40-discuss-and-work.md) | Change one conversation from discussion into work | implemented | pytest + browser |
| [S41](S41-bounded-experiment-control.md) | Run an Experiment through a bounded control loop | pending | pytest + browser |
| [S42](S42-watchers-wake-conversations.md) | Watch external work and wake its conversation | implemented | pytest + browser |
| [S53](S53-truthful-attention-and-run-surfaces.md) | Attention and Runs tell one truthful story | implemented | browser |
| [S59](S59-staged-graph-audit-skills.md) | An agent audits the graph Patch it will finish | pending — not human-confirmed | pytest + browser |
| [S60](S60-plain-language-project-setup.md) | Add a project with plain-language setup steps | pending — not human-confirmed | browser |
| [S62](S62-direct-provider-log-ingestion.md) | Seed and Refresh read provider logs in place | implemented | pytest + browser |
| [S63](S63-agent-run-lock-recovery.md) | RCP recovers run ownership; humans never remove locks | implemented | pytest + api |
| [S74](S74-boundary-inputs-fail-closed.md) | Boundary inputs and project write scopes fail closed | implemented | pytest + browser |
| [S75](S75-network-access-on-every-agent-surface.md) | Every user-facing agent task can read the public web | implemented | pytest + browser |
| [S76](S76-graph-condition-wake.md) | An agent can wait on canonical graph state | implemented | pytest |
| [S78](S78-one-budget-one-stop.md) | One Auto-research budget and one graceful Stop | implemented | browser |
| [S81](S81-live-canonical-state.md) | Canonical graph changes appear without UI reload | implemented | api + browser |
| [S90](S90-desktop-chat-dictation.md) | Speech becomes an editable chat draft | pending | desktop |
| [S95](S95-durable-team-space.md) | A team space outlives every serving process | pending — not human-confirmed | pytest + api |
| [S96](S96-joining-a-team-space.md) | Join a team space once and stay joined | implemented | pytest + api + browser |
| [S98](S98-move-a-project-into-a-team-space.md) | Hand a personal project to the lab once | pending — not human-confirmed | pytest + browser |
| [S99](S99-attribution-travels-with-history.md) | History says who authorized a change | implemented | pytest + browser |
| [S100](S100-permission-is-checked-twice.md) | Nothing unauthorized starts or lands | implemented | pytest |
| [S101](S101-project-membership.md) | Space membership is not project membership | implemented | pytest + browser |
| [S102](S102-team-runs-execute-as-the-space-account.md) | Team work executes where and as the space can reach | pending — not human-confirmed | pytest + api |
| [S103](S103-server-operations-are-console-operations.md) | Dangerous operations require machine authority | pending — not human-confirmed | pytest + api |
| [S104](S104-backups-never-pause-work.md) | Backup interrupts nothing and overclaims nothing | pending — not human-confirmed | pytest |
| [S105](S105-move-between-spaces-in-one-window.md) | One window can use several spaces without ambiguity | pending — not human-confirmed | desktop |
| [S109](S109-tabs-stay-current-without-freezing.md) | Project tabs stay current without waiting on remote state | implemented | pytest + browser |
| [S110](S110-paper-draft-survives-a-canonical-change.md) | A paper draft survives canonical movement | implemented | pytest + browser |
| [S113](S113-campaign-attribution.md) | Episode work retains authorization lineage | implemented | pytest + browser |
| [S114](S114-see-your-results-without-leaving.md) | See results without leaving RCP | implemented | pytest + browser + ssh |
| [S115](S115-beliefs-change-only-through-you.md) | Agents may rewrite anything except existing beliefs | implemented | pytest + browser |
| [S116](S116-choose-existing-or-fresh-research.md) | Choose retained research or fresh setup before mutation | implemented | pytest + browser + ssh |
| [S119](S119-stale-processes-cannot-command-the-next-turn.md) | A stale process cannot command the next episode turn | implemented | pytest + ssh |
| [S120](S120-episodes-wrap-up-with-a-visual-report.md) | Episodes wrap up with a visual report | implemented | pytest + browser + desktop |
| [S121](S121-a-refusal-explains-itself.md) | A refusal explains itself and what it did not undo | pending | pytest + browser |
| [S122](S122-project-invitations.md) | A member can invite you to a project and you can leave | implemented | pytest + browser |
| [S125](S125-auto-research-graph-branch-merge.md) | Auto-research changes its graph branch before a human merge | implemented | pytest + browser |
| [S126](S126-choose-local-repository-folder.md) | Choose a local repository folder in the desktop setup wizard | implemented | web + desktop |
| [S127](S127-select-codex-provider-runtime.md) | Select Codex exec or app-server per agent profile | implemented | pytest + browser + desktop |
