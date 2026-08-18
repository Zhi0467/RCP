# Dispatch — see your results without leaving

> Archived 2026-08-17. Complete:
> [S114](../../acceptance/S114-see-your-results-without-leaving.md) is
> `implemented`. Where the served bytes live was then changed by
> [the result-view bytes slice](handoff-2026-08-12-slice-result-view-bytes.md), so
> read that file for the design that actually stands. Retained only as historical
> execution context.

**Date:** 2026-08-12
**Scenario:** [S114](../../acceptance/S114-see-your-results-without-leaving.md) —
confirmed by the human 2026-08-12.
**Design:** the blueprint's
[Result views](../../research-control-panel-blueprint.md#result-views) section.
What remains open is [Q6](../../open-questions.md), narrowed to one question this
work must not answer by accident.

Read [`AGENTS.md`](../../../AGENTS.md) first, then the scenario, then the
blueprint's [Conversation scratch and
artifacts](../../research-control-panel-blueprint.md#conversation-scratch-and-artifacts)
section.

## What you are building

The researcher opens a run in Runs and asks how it went. An ordinary Work turn
reads the run's own output files and writes a page — curves across seeds, or a
grid of samples. They read it in the run detail. They box a region of the curve,
which drafts a message they edit and send; the turn resumes the same session and
edits that same file. Everything expires unless they press **Keep**, which
copies one page into their project repository.

The point is loop latency, not pictures. If a revision redraws instead of
edits, the feature has failed even when every test passes.

## Stage zero — measure before building

**Do this first and report the number.** The whole route rests on an assumption
nobody has tested: that an agent can draw a usable page quickly and then *amend*
it cheaply.

On a real project with real run outputs, using an ordinary Work turn:

1. Ask for loss curves across seeds. Time it. Is the page readable?
2. Then ask only for "log scale". Time it. Did it edit the file, or redraw it
   from scratch?
3. Then ask for a grid of sample outputs. Time it.

If step 2 costs about what step 1 cost, the loop does not tighten and the
premise is wrong. **Stop and report that** rather than building the
keep-and-repository half on top of it. This is the one place where a negative
result is more valuable than shipped code.

## Land this serially, first

`web/src/types.ts` and the stored view record are shared contracts. So is the
outbound gesture message shape. Land those, then fan out.

## Fan-out

| Agent | Files | Owns |
|---|---|---|
| Runs | `src/rcp/artifacts.py`, `src/rcp/transport/run_stage.py`, `src/rcp/storage.py` | the view file's life in conversation scratch, discovery, serving, expiry |
| Agent I/O | `src/rcp/agents/prompts.py`, `src/rcp/runs/work.py` | the drawing instruction, the revision instruction, resume |
| Keep | `src/rcp/transport/` (`StateWorkspace`) | copying into `views/`, naming, disambiguation, the remote path |
| Web | `web/src/components/ExperimentRunDetail.tsx`, `web/src/` | the card, gestures, the draft, the Keep control |
| Tests | `tests/`, `web/tests/` | the scenario's checks |

## Invariants you must not break

- **The view file is not a per-turn artifact.** A turn's artifact directory
  cannot hold a file that survives to the next turn. The page lives in the
  conversation's reusable scratch stage — already keyed by project and
  conversation precisely because a resumed native session must run in the
  directory it was given (invariant 10c) — and RCP serves the exact stable file
  from there. No turn copies it, links it, or switches directories to expose it.
  Get this wrong and every revision becomes a redraw.
- **Resume is the mechanism, so its failure must be loud.** If the session
  cannot be resumed, say so. Never quietly start a fresh session that redraws
  the page from nothing: that looks like success and loses the human's edit.
- **One outbound channel, nothing inbound.** The page reports a gesture through
  a one-way, fixed-shape, size-capped message treated as untrusted text. RCP
  exposes no API, no state, and no project data to the page in return. The page
  still cannot navigate RCP, open popups, submit forms, or start downloads
  (invariant 10e). A page that reports nothing must stay usable through an
  ordinary typed revision.
- **A gesture never dispatches a turn.** It writes a draft the human reads,
  edits, and sends. This is what keeps a wrong selection a visible mistake.
- **Keep goes through the workspace.** Invariant 6: the lock and an explicit
  publish, never a direct file write from a route handler, and it must work when
  canonical state is remote.
- **Never under `.research/`.** That directory is append-only history and
  materialized outputs (invariants 1 and 2). Kept views land in `views/` at the
  state repository root.
- **A kept view carries no graph authority.** No Patch, no revision, no
  Proposal, no attention count. It travels beside the research record.
- **RCP owns the final filename.** The agent supplies a descriptive base name;
  RCP qualifies it with the project and a `yy-mm-dd` suffix and disambiguates
  rather than overwrites. A name the agent picks freely is a name that can
  collide with an existing file in a repository RCP does not own.
- **Prompt contracts have length guards.** `tests/test_prompts.py` asserts line
  caps. Check the guard before adding instruction prose, and write tight prose
  rather than one enormous unwrapped line to slip under a line count.

## Out of scope

- **Any control that turns a view into a research action** — selecting runs and
  calling them evidence. That is the narrowed [Q6](../../open-questions.md), and it
  must not be answered by accident here. A view is read-only.
- A dashboard. Utilization, throughput, and scalar browsing answer *is my
  machinery working*, have incumbents, and stay out.
- Shapes outside the blueprint's table: no field viewer, no trace viewer, no
  per-domain connector.
- An un-keep control. Deleting a kept view is deleting a file.

## Done means

S114 passes. It is `pytest + browser + ssh`, so the browser half is not
optional: serve the app, open a run, drive the gesture, the revision, and Keep,
and check `read_console_messages` and `read_network_requests` alongside
`preview_logs`. The remote half needs the configured SSH host — check it is
reachable before calling it blocked.

Backend baseline `uv run pytest` and `uv run ruff check src tests`; web
`npm --prefix web run build` and `npm --prefix web test`; then
`uv run pre-commit run --all-files` with `git add -A` first.
