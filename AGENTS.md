# AGENTS.md

Canonical instructions for coding agents in this repository. `CLAUDE.md` imports
this file; do not duplicate it there.

## What RCP is

RCP is a local research control panel. It turns agent-assisted conversations and
bounded autonomous episodes into one durable research graph, a human authority
queue, operational task history, and a human-authored paper introduction.

Current authority, highest first:

1. [`docs/design.md`](docs/design.md) for product boundaries and cross-cutting invariants.
2. The applicable file in [`docs/specs/`](docs/specs/) for current behavior.
3. Active decisions for rationale that remains easy to regress.
4. Active handoffs for human-confirmed work that is not yet complete.

Read the sources relevant to the affected behavior. A typo or formatting fix
does not require a design review. Report contradictions instead of silently
choosing a source.

## Find the relevant guidance

- Product boundaries or authority: `docs/design.md` and `docs/specs/authority-and-proposals.md`.
- Graph operations, persisted Patch changes, or replay: `docs/specs/graph-history-and-transitions.md`.
- Provider launches, prompts, skills, or task-engine ownership: `docs/specs/providers-and-containment.md`.
- Chat, Experiment control, or watcher behavior: `docs/specs/conversations-episodes-and-watchers.md`.
- Scheduler submission, the launch helper, or job watchers: `docs/specs/compute-jobs.md`.
- Auto-research or graph-branch scope: `docs/specs/auto-research-and-branch-merge.md` and `docs/decisions/2026-09-08-graph-branch-scope-is-reopened.md`.
- Task storage or persisted requests: `docs/specs/projects-spaces-and-operations.md`.
- API, Web, or native contracts: `docs/specs/api-web-and-desktop-projections.md`; visual design: `docs/specs/interface-and-visual-design.md`.
- Paper, previews, reports, or artifact viewing: `docs/specs/paper-artifacts-and-result-views.md`.
- Server, deployment, or release work: `docs/specs/server-and-machine-operations.md`, `docs/server.md`, and `docs/release.md`.
- Use `docs/handoffs/README.md` to find an active handoff or an open live check when continuing unfinished work.

## Working loop

1. Inspect the affected source and existing tests; use the guidance above when
   the change touches its contract.
2. Before substantial edits, identify file ownership, invariants, and focused
   checks. Make small edits directly; fan out larger implementation by module
   boundary, while the main agent retains integration, verification, and review.
3. Verify the affected behavior, fix failures caused by the change, and rerun
   affected checks. Add regression coverage when behavior changes; review prose
   edits directly. Test prompt data and enforcement, never exact instruction wording.
4. Read the complete diff and inspect check results yourself.
5. Update current behavior docs when semantics changed. Close or replace any
   handoff whose status changed in the same commit.

When another session changes the tree during long work, integrate through a real
three-way Git merge; never pipe a whole diff into `git apply`. All work uses a
short-lived branch, PR CI, and explicit human merge; servers consume only
merged `main`.

## Verification

Run focused checks locally; full suites run in PR CI. Broaden local checks when
failures, shared-contract changes, or integration uncertainty warrant it.

- Python behavior: `uv run pytest -n0 <affected test paths>` and `uv run ruff check <changed paths>`.
- Web behavior: `node --experimental-strip-types --test web/tests/<affected>.test.mjs`;
  run `npm --prefix web run build` when source or types change.
- Formatting and file hygiene: `uv run pre-commit run --files <changed paths>`,
  including new files. All-file hooks see tracked files only.
- User-visible workflows, reported product failures, and substantial route,
  background, or view changes need the affected served-app journey. Inspect
  network, console, and server logs; report the exact gap if it cannot be driven.
- Remote behavior needs a reachable host. Migration or recovery checks that
  depend on existing records use a copy of real data. Never test against the
  human's live data directory.
- Native changes require rebuilding Tauri and the relevant `docs/desktop.md` checks.

Fresh-clone setup and full-suite commands live in `docs/install.md`; build `web/dist`
before `uv sync` because the Python wheel includes it. Browser tests require the
Playwright-managed Chromium installation described there. Run the app with
`uv run rcp serve --host 127.0.0.1 --port 8421` using disposable data for tests.

## Stable invariants

This numbered registry is cited from source and tests. Never
renumber it; `docs/design.md` states the same promises unnumbered and coarser.

1. **Canonical Patch logs are append-only.** Never edit or delete main or branch Patch history; replay would change the past.
2. **Materialized graph files are outputs.** Never hand-edit `graph.json`, `research.md`, glossary, Proposal, or control projections.
3. **Humans retain protected authority.** Only humans approve Proposals, change project truth membership, authorize episodes, or dispatch branch merges. The Auto-research orchestrator is the one explicit Decision exception, and a human-dispatched branch merge inherits that profile.
3b. **Existing ResearchQuestions and Hypotheses are protected beliefs.** Agents may create them; structural or semantic changes to existing ones use a Proposal. Never infer protected intent from operation shape.
4. **Agent capability is fixed in code.** Configuration cannot widen it. Discuss has no graph/project authority, though every team launch carries its repositories' Git transport ([decision](docs/decisions/2026-09-24-team-launches-carry-git-transport.md)); Work names exact write roots to provider enforcement, which bounds every file-editing tool and bounds the shell only where a provider can do so without disabling compute ([decision](docs/decisions/2026-09-13-claude-work-runs-without-the-os-sandbox.md)); ingestion writes only scratch; paper coach is read-only.
4b. **`patch.json` in the task stage is the only graph-change channel.** Never parse graph authority from answers, traces, artifacts, or repository edits.
5. **Context, graph target, and write scope are distinct.** Receiving a pointer or graph context grants no filesystem authority.
6. **One canonical state repository.** Routes never write canonical files directly; `StateWorkspace` owns local/remote locking and publication.
6b. **One synchronous transition owns one semantic mutation.** Sync, Apply, branch Apply, and merge commit one expanded transition or nothing; replay does not rerun historical rules.
7. **Canonical and manifest writes are atomic.** Use the existing temporary-file and `os.replace` paths.
7b. **Materialization never mutates a shared contained model in place.** Replace container slots or whole attributes so a failed patch cannot corrupt the previous revision.
8. **One RCP process owns one data directory.** OS advisory locks, not path existence, establish local and remote ownership.
9. **Failed runs retain scratch and patch text.** Delete a stage only after its graph patch applies successfully.
10. **Conversation and ingestion are different lifecycles.** They share launch
   plumbing only; chat never advances ingestion cursors.
10b. **Only a captured Work turn has conversation graph authority.** Message wording or a stray file cannot upgrade Discuss.
10c. **Conversation scratch belongs to the stable chat, not one turn.** Clear the previous turn's patch on entry and fail closed if that cannot be proved.
10d. **Discuss and Work do not consume prior RCP chat transcripts.** A native provider session may continue, but stored chat history is never task authority.
10e. **Agent HTML previews cannot act on RCP.** Keep the opaque sandbox, bounded
   artifact discovery, no popups/forms/downloads, and no implied zero-network claim.
10f. **The ingestion watermark advances only after accepted Apply.** It is an
   overlap-tolerant timestamp, not an exactly-once cursor.
10g. **One episode, one graph target, one validated session/stage, one graceful
   Stop fence.** Never silently fall back to a fresh session or main graph.
11. **`answer` is the human reply; `message` is a trace.** Preserve the provider's
   final-assistant label.

## Cross-cutting implementation rules

- Policy stays with its concrete owner. Do not add `kind`, `surface`,
  `patch_kind`, or equivalent selectors to shared execution plumbing merely to
  collapse visible policy.
- Permission contracts are code, not manifest configuration. Every launch names
  its capability explicitly.
- Structured deliverables are file-backed. Conversational prose is the labelled
  provider answer; do not create a second answer file.
- Limits and timeouts live in `limits.py`, except schema constants that belong
  beside the model they constrain.
- Remote-executed code is shipped from its source module, never hand-copied into
  a command string.
- Prompt prose describing enforcement must render the same resolved object used
  by enforcement. Do not maintain parallel human-written allowlists.

## Documentation lifecycle

- Keep docs and PR titles, descriptions, and comments free of real hostnames,
  account names, personal/backup paths, and private project/repository identities.
  Use neutral descriptions or placeholders; check the full text before publishing.
- Specs own current behavior and durable journeys; decisions explain active tradeoffs.
- A handoff is active work, not a diary; its opening names implemented and remaining work and settled decisions.
- When a handoff decision changes, update its plan and status in the same commit.
  Rejected work is closed, not “not done.” Never leave mutually contradictory
  old and new plans active in one file.
- When work completes, is rejected, superseded, or abandoned, delete the handoff
  in the same change. If later work materially changes scope, delete the
  predecessor and create a new handoff rather than appending a second plan.
  Git history is the record; rationale that must outlive the work goes in a
  decision record.
- Delete stale instructions; avoid caveats and rules duplicated across AGENTS, specs, and handoffs.

## Conventions and local facts

- Python uses `uv`, `pyproject.toml`, Pydantic, and `from __future__ import annotations`.
- Ruff settings live in `pyproject.toml`; do not assume them.
- `.research/`, `.recovery/`, and `web/dist/` remain outside formatting hooks.
- Never trust a piped test command's exit status unless `pipefail` is set.
- Use shared test wait helpers rather than copied short polling loops.
- Literal expiry dates are test time bombs; derive them from the test clock.

## Maintaining this file

Keep only cross-cutting rules and pointers needed to find task-specific guidance.
There is no minimum length; the test enforces a ceiling of 230 lines. Move
module-specific behavior, rationale, and procedures to their owning documents.
Consolidate existing rules before adding another. A new global invariant must
name its concrete code owner and cite an executable test; preserve existing ids.
