# AGENTS.md

Instructions for coding agents working in this repository. `CLAUDE.md` imports
this file; keep this one canonical and do not duplicate content into it.

**This file is a living document.** See "Maintaining this file" at the bottom —
you are expected to update it as the project changes.

## What RCP is

RCP is the implementation of the design blueprint in [`docs/`](docs/). The
blueprint is versioned (`research-control-panel-blueprint-v*.md`); **always read
the highest version present** rather than a version remembered from an earlier
session — `ls docs/` first. Note the newer files may be short amendments that
only supersede named sections, so the section you need may still live in an
older version. The blueprint is the specification: when code and blueprint
disagree, say so explicitly instead of silently picking one.

[`docs/open-questions.md`](docs/open-questions.md) holds design questions that
are raised and evidenced but **not decided**. Read it before proposing a change
to something it covers, and add an entry rather than deciding an open question
inside an implementation.

In one sentence: a local web app that turns agent-driven research conversations
into one project-global research graph, a human authority queue, and a
human-authored paper introduction with a read-only writing coach.

### Current stack — subject to change; verify before relying on it

Read `pyproject.toml` and `web/package.json` for the authoritative versions.

- Python 3.11+ backend (`src/rcp`): FastAPI, Pydantic v2, SQLite, `uv`.
- React + Vite + Tailwind frontend (`web/`), served from `web/dist` by the same
  FastAPI app when that directory exists ([app.py:365](src/rcp/api/app.py:365)).
- Agent providers: Codex CLI and Claude Code, launched as subprocesses locally
  or over SSH.

## Default working mode

The main agent orients, plans, verifies, and reviews. **Implementation fans out
to subagents.**

0. **Write the acceptance scenario first, and confirm it.** For a new feature, a
   bug the user actually hit, or a substantial change to a module: propose the
   scenario in [`docs/acceptance/`](docs/acceptance/README.md) — including the
   **UI path** for anything new — and get the human's confirmation *before*
   planning or writing code. The scenario is where the design decisions actually
   get made; settling it first means they get made deliberately rather than
   improvised inside an implementation and discovered later. Skip this only for
   changes too small to have a user-visible promise.
1. **Read, yourself.** Open the relevant files directly. Do not delegate
   orientation — you need the context to plan and to review later.
2. **Make single edits, yourself.** One-file or few-line changes: just do them
   inline. Spawning an agent for these costs more than it saves.
3. **Plan.** Decide the change set, the split lines, and the verification steps
   before writing anything substantial. Use a `Plan` agent only when the
   architecture is genuinely non-obvious.
4. **Delegate all implementation, fanned out.** Once the plan is set, hand the
   work to parallel subagents — one per module boundary below, issued in a
   single block so they run concurrently. Give each agent its exact file scope,
   the invariants it must not break, and its own check command.
5. **Verify, yourself.** Run the checks (see "Verification"). A subagent's
   "tests pass" is a claim, not evidence — re-run them.
6. **Review, yourself.** Read the resulting diff end to end. You planned it, so
   you are the one who can tell whether it matches the plan and respects the
   invariants. Delegate a review only as an extra pass on large changes, never
   as a substitute for reading the diff.

### Where to cut the fan-out

These boundaries are clean seams; parallel agents rarely collide across them.

| Area | Files | Owns |
|---|---|---|
| Graph core | `src/rcp/core/` | models, patch validation, materialization, `research.md` generation |
| Agent I/O | `src/rcp/agents/` | run context assembly, output schema, prompts, provider launch |
| Transport | `src/rcp/transport/` | SSH, canonical-state workspace, repo snapshots, remote run stage |
| Sources | `src/rcp/sources/` | Claude/Codex JSONL discovery, indexing, slicing |
| History | `src/rcp/history/` | append-only patch log, locking, materialized outputs |
| Run orchestration | `src/rcp/runs/` | distinct Seed/Refresh, Work, Discuss, graph repair, and paper coach workflows; policy-neutral staging and event plumbing |
| Service/API | `src/rcp/service.py`, `src/rcp/api/app.py`, `src/rcp/projects.py`, `src/rcp/background.py` | app construction, routes, project catalog, background task lifecycle |
| Paper | `src/rcp/paper/` | draft store, canonical introduction, writing sessions |
| Setup | `src/rcp/setup.py`, `src/rcp/config.py` | manifest rendering, preflight, manifest schema |
| Providers | `src/rcp/providers.py`, `web/src/providers.ts` | the provider registry: ids, labels, auth probe, model catalog, launch command |
| Web | `web/src/` | React views, components, hooks, API client, types |
| Desktop | `web/src-tauri/`, `packaging/` | Tauri shell, backend ownership handshake, window lifecycle, PyInstaller sidecar, bundle scripts |

`src/rcp/core/models.py`, `src/rcp/config.py`, `src/rcp/providers.py`, and
`web/src/types.ts` are shared contracts. **Do not parallelize across them** — land the contract change first,
serially, then fan out the consumers.

### When to stay serial

- Single-file or few-line changes.
- Any change to `.research/` semantics or the patch envelope — the invariants
  interact too much to split safely.
- Anything the user asked to be done a specific way.

## Commands

Run from the repo root.

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run ruff check src tests
```

```bash
npm --prefix web install && npm --prefix web run build
```

Web-only checks:

```bash
npm --prefix web run typecheck && npm --prefix web test
```

Formatting and lint are enforced by a `pre-commit` hook over ruff and prettier.
Enable it once per clone; `--all-files` is what CI runs:

```bash
uv run pre-commit install
```

```bash
uv run pre-commit run --all-files
```

The hooks call the repo's own pinned tools, so `uv sync` and
`npm --prefix web install` must have run first. Note the hook covers *every*
tracked Python file, which is wider than the `ruff check src tests` above —
`packaging/` is only linted through pre-commit.

Desktop bundles and their Rust checks (`cargo` lives in the rustup toolchain, not
on the default `PATH`):

```bash
sh web/src-tauri/scripts/build-release.sh
```

```bash
sh web/src-tauri/scripts/build-dev.sh
```

Run the app:

```bash
uv run rcp open examples/demo-project/state-repo
```

```bash
uv run rcp serve --host 127.0.0.1 --port 8421
```

[`.claude/launch.json`](.claude/launch.json) records these start commands under
the names `rcp` (backend, 8421) and `rcp-web` (Vite dev server, 5173), so
preview tooling can start either one by name instead of improvising a command.

Add `--reload` to `serve` for Python auto-restart and continuous frontend
rebuilds. The Uvicorn watcher watches `src/` only; the frontend has its own
managed build watcher. Reload goes through the `reload_app` factory in
[`__main__.py`](src/rcp/__main__.py) and carries the project in
`RCP_RELOAD_PROJECT`, because uvicorn can only restart an app it imports itself
— preserve that path if you touch the CLI, or `--reload` silently stops working.

Only one RCP server may run per data directory. `rcp open` probes and reuses the
healthy lock owner, or gracefully replaces an owner unavailable at the requested
address. An explicit `rcp serve` always performs that graceful takeover. Both
wait for the shutdown hook to pause recoverable work before taking the lock; the
user should never need to discover or kill the PID manually.

`rcp open` with no project argument lands on the project index. `web/dist` is
gitignored; a new `open` or `serve` process builds it before creating the app.

## Verification

### Baseline — every change

Backend: `uv run pytest` and `uv run ruff check src tests`.
Web: `npm --prefix web run build` (typechecks) and `npm --prefix web test`.

### Finish every work chunk cleanly

Before reporting any logical chunk of work complete or handing it to the human
for a commit, run its relevant tests and then `uv run pre-commit run --all-files`.
If a hook modifies files, review and stage those changes, then rerun the full
hook suite until it passes. A first formatter pass that changed files is not a
successful final check.

### Done means the scenario passes

[`docs/acceptance/`](docs/acceptance/README.md) holds the promises the app makes,
one file each, in the language of someone using it. **For feature work, a
user-reported bug, or a substantial change, "done" is the named scenario
passing** — not the baseline going green.

Name the scenario your change belongs to. If none covers it, write it first (see
step 0 above). A scenario declares how cheaply it can be checked: `driver:
pytest` for backend truth, `api` for a served app without a browser, `browser`
only when the thing that can break lives in the frontend — pin state, draft
state, split position, a toggle resetting, a run staying visible across views.
Do not pay browser cost for a backend fact, and do not let a backend test stand
in for frontend state.

### Finishing a coding session

Anything that debugged, built a feature, or changed a module significantly ends
with a sweep of the scenarios that go stale — the `pending` and
`blocked-external` ones:

```bash
grep -l "^status: \(pending\|blocked-external\)" docs/acceptance/S*.md
```

For each, ask whether today's work made it **runnable** (the feature now exists,
the machine is now reachable) or made it **wrong** (the UI path got built
differently than the scenario proposed). Drive the ones that can now run, rewrite
the ones the session invalidated, and stamp `last_passed:` on whatever passed.

**Do not re-run `implemented` scenarios unless asked.** They cost real time, and
the thing that should prompt a re-run is a code change touching them, not the
calendar.

### Not enough on their own

**Unit tests and a clean web build do not prove the app works.** They pass
routinely while the UI is broken, a route 500s, or a background run hangs. For
any of these, the baseline is a precondition, not the verification:

- adding a new feature,
- debugging a failure the user actually hit,
- substantially changing an implementation (routes, background runs, agent
  launch, state transport, or any view's data flow).

In those cases you must **serve the app and exercise it at the UI level**:

```bash
uv run rcp serve --host 127.0.0.1 --port 8421
```

Then drive `http://127.0.0.1:8421` in a browser — open the affected view, take
the actions the change touches, and confirm both the rendered result and the
absence of console/network errors. Check the server log for tracebacks; a route
that returns 500 while the page still renders is a silent failure.

If you cannot drive the browser in this session, **do not report the change as
verified**. Say plainly that UI verification is outstanding and give the user
exact steps: the command to run, the URL and view to open, the clicks to make,
and what correct output looks like versus the symptom that would mean it failed.

Remote/SSH behavior cannot be verified without a reachable host. Never claim a
remote path works — state that it is untested and why.

State clearly what you verified and what you did not.

## Invariants

These encode blueprint decisions. Breaking one silently invalidates the app's
guarantees — surface the conflict instead of working around it.

1. **`.research/patches/` is append-only.** Never edit or delete a patch file.
   A human Sync is committed as one visible `patches/batch-*` directory; replay
   ignores its hidden `.batch-*` staging directory until the directory rename.
   An SSH mirror may discard an unpublished local batch after remote failure,
   because that mirror is explicitly not canonical history. An uncertain remote
   commit is quarantined from replay; a confirmed commit remains visible and
   blocks later canonical work behind derived-file repair rather than inviting
   a duplicate Sync.
   Every graph is rematerialized from the log and validated before it can affect
   current state (`src/rcp/history/manager.py`, `src/rcp/core/materialize.py`).
2. **Materialized files are never hand-edited.** `graph.json`, `research.md`,
   `glossary.json`, `proposals.json`, `coverage.json` are outputs.
3. **Agents assert or propose; humans hold authority.** Only human UI actions
   set `standing`, approve gated operations, or change project truth membership.
   Do not add an agent path that writes any of those.
4. **Agent permission contracts are fixed by capability.** `permissions_for()`
   in [config.py](src/rcp/config.py) is the contract; the manifest may not widen
   or narrow it. Discuss has writable conversation scratch but no project or
   graph authority. Work is unrestricted for tooling and repositories: Codex
   bypasses approvals and sandboxing, and Claude uses `bypassPermissions`.
   Work-originated graph and watcher corrections retain that same Work capability
   and native session; only their instruction changes. Direct canonical `.research` writes are
   forbidden by the Work prompt contract only, a known accepted prompt-enforced
   boundary for both providers. Seed, Refresh, and their generic patch correction
   write only their run scratch. The paper coach has no write or Apply path
   anywhere. Every launch names its capability outright — `AgentLauncher.stream`
   and `_command` require it, and there is no boolean shorthand a caller can pass
   instead.
4b. **One way to get a patch out of an agent.** There is no write-path mode. The
   provider is launched with its cwd on a scratch folder and writes `patch.json`
   there; that file is the only graph-change channel RCP reads. Work may edit any
   repository its unrestricted tools can reach, but those edits carry operational
   authority, not graph authority. Its prohibition on direct canonical
   `.research` writes is a prompt contract, not an OS boundary. Conversations may
   write optional preview files under the exact RCP-created artifact directory
   for its turn, but those files are temporary, non-canonical, and carry no
   graph authority. Never parse a patch out of stdout or a final message, never
   add a canonical-state write path, and never copy a repository merely to make
   it readable — off-machine repos are read over SSH from the host and path in
   the prompt.
5. **Run scope vs. project scope.** The whole graph and canonical `research.md`
   enter every graph agent run; only run-scope repositories enter as raw
   pointers. For Work this is a context boundary, not a repository permission
   boundary. Keep that distinction when touching `src/rcp/agents/context.py`.
6. **Exactly one canonical state repository**, possibly remote. Writes go
   through the `StateWorkspace` (lock, publish explicit changed files); never
   write canonical files directly from a route handler.
7. **Atomic writes.** Manifest and materialized output writes go through the
   existing temp-file-then-`os.replace` helpers.
7b. **Materialization never mutates a contained model in place.** Every change in
   `_apply_patch` replaces a container slot (`state.nodes[id] = …`) or a whole
   attribute; nothing reaches into a node, edge, proposal, ambiguity, or
   coverage object and assigns a field. `_fork_state` in
   [materialize.py](src/rcp/core/materialize.py) relies on this: it copies only
   the containers and shares their contents, which is what keeps replay fast.
   Break the rule and a failed patch silently corrupts the previous revision.
   `test_patch_failing_part_way_leaks_no_earlier_operation` guards it.
8. **One RCP process per data directory**, enforced by an `fcntl` lock in
   [`__main__.py`](src/rcp/__main__.py). Background seed/refresh is
   server-owned; a live run can be paused, a checkpointed attempt resumed, and a
   paused/interrupted/failed attempt retried.
9. **A failed run keeps its scratch folder and its patch text.** The patch is
   persisted before validation runs, and the folder is deleted only after the
   patch applies — otherwise it ages out on a retention window. Recovery is
   automatic (the ladder in
   [`stream_graph_run`](src/rcp/runs/graph.py)): rescan the folder for the patch,
   then hand validation errors back to the same live session for at most two
   scratch-only rounds. This is the Seed/Refresh generic patch-correction path;
   Work instead uses same-access `work_patch_correction`. A graph-level rejection
   is never retried.
10. **A conversation turn is not an ingest run.** Seed/refresh and conversation
   turns share the launcher and background lifecycle, nothing else. Discuss and
   Work assemble chat context
   (`ContextAssembler.chat_context`); neither loads `cursors.json` or
   materializes an evidence slice, and they take the canonical append lock only if a
   Work patch actually needs applying. Discuss receives no patch contract. A
   Work patch is optional and forbidden from touching coverage or cursors;
   asking a question and Work with no net graph change spend no revision. Keep
   the Discuss, Work, and Seed/Refresh policy paths in distinct modules and entry
   points under [`src/rcp/runs/`](src/rcp/runs/) rather than reintroducing a
   shared `is_chat` branch. Shared *plumbing* is fine and expected — they call
   the same launch, receipt, staging, and event-pump helpers. The line is the
   discriminator: no
   shared helper may take a `kind`, `is_chat`, `surface`, or equivalent parameter,
   because anything that must know which surface it serves is policy and belongs
   in the caller. Leaving some lines duplicated is the correct outcome, not a
   missed cleanup. (The blueprint still describes one shared mandatory-patch
   path for chat and ingestion — that section is superseded by this invariant.)
10b. **Work is the only conversation mode with graph authority.** The captured
   per-turn `mode="work"` is the authority; there is no separate
   `allow_graph_change` gate, and the agent cannot grant itself authority by
   writing a file during Discuss. A stray Discuss patch is kept as a receipt and
   discarded. A Work patch contains semantic operations only; RCP adds patch,
   Proposal, revision, scope, and lifecycle bookkeeping. An empty patch spends no
   revision. Never infer mode from the wording of the message.

   Every Work stage contains an RCP-staged Python validator client. It exchanges
   bounded request and response files through the writable workspace while RCP
   polls locally or through the existing SSH run stage, prepares the candidate
   against live current state in process, and records each check. Client exits
   distinguish valid, semantically invalid, and validator unavailable so a
   transport failure cannot become a correction loop. Validation stages
   operations in their written order against earlier valid operations while
   retaining whole-patch node and edge lookup for legal forward references; it
   never reorders operations.

   Apply re-prepares bookkeeping and reruns the same semantic validator against
   current state while holding the canonical append lock. There is no original
   context-revision pin or Resume-ancestor walk, and graph movement alone is not
   a rejection. Work graph and watcher corrections reuse the same native Work
   session and unrestricted Work permissions; only the instruction changes, and
   they must not repeat completed operational side effects.

   A remote single-patch append uses the patch file as an observable atomic
   commit point: a confirmed commit succeeds and repairs derived outputs, an
   absent commit rolls the mirror back, and an unknown commit is quarantined
   until a canonical refresh proves what happened.
10c. **A chat's scratch folder belongs to the conversation, not the turn.** It is
   keyed by stable project identity plus `chat_id` and reused, because a resumed
   native session must run in the directory it was given — Claude keys its
   sessions by that directory. Resume attaches only to the persisted stage after
   its host and exact project/chat path are validated; never recompute or trust a
   client UUID as the checkpoint. Clear the previous turn's `patch.json` on entry
   and let the sweepers age the folder out;
   deleting it per turn breaks multi-turn chat. That clearing fails closed — a
   survivor would be read as this turn's patch and applied under this turn's
   authorization — so the workspace list/remove operations in
   [run_stage.py](src/rcp/transport/run_stage.py) raise instead of reporting an
   unreachable workspace as an empty one.
10d. **Chat is not transcript ingestion.** `chat_context` contains the graph,
   focused node, current request, and exact run-scope repositories. Discuss has
   those repositories read-only. Work receives the same exact pointers as
   context, but its tooling and repository access are unrestricted; it may return
   its optional semantic `patch.json`. Discuss and Work never read, index, copy,
   project, prompt with, validate, or authorize from prior chat transcripts. A
   provider continuation/session identifier may still be passed to the provider
   for its own native session behavior. The answer may be appended to canonical
   chat history for the UI, but that write is not an input to the turn. Every
   conversation launches with its scratch workspace writable; canonical state is
   read-only for Discuss, and direct canonical `.research` writes are
   prompt-forbidden for Work.
10f. **Seed/Refresh source assembly is best effort.** Seed and Refresh are the
   only paths that assemble conversation-source context, cursors, coverage, and
   slices. If source metadata or a pointer cannot be assembled, RCP records the
   exact diagnostic and still launches with provider/source names or roots and
   the last accounted coverage boundary. The provider may inspect those sources
   directly. RCP never advances a cursor or claims coverage for input it could
   not read.
10e. **The answer and preview artifacts are independent.** The labelled final
   assistant message is the Markdown reply. A turn may also leave supported
   files as direct regular children of its exact RCP-created artifact directory;
   RCP never discovers artifacts from provider directives, provider-owned paths,
   URLs in the answer, nested files, or symlinks. Artifact descriptors may live
   with the task, but bytes remain only in temporary local or remote scratch.
   Preview discovery, validation, rendering, expiry, SSH unavailability, and
   explicit Download failure never change the reply, task verdict, or graph.
   RCP serves or proxies bounded files on demand and never automatically copies
   one into canonical state, the chat transcript, or durable app storage. HTML
   runs in an opaque sandbox: it cannot access or navigate RCP, open popups,
   submit forms, start downloads, or use ordinary network resource APIs. Because
   inline JavaScript remains useful, it may still navigate its own isolated
   child frame and thereby cause a navigation request; never describe this as a
   zero-network preview.
11. **`answer` is the reply; `message` is a trace.** Providers label their final
   assistant message and RCP preserves that label in `AgentEvent`
   ([launcher.py](src/rcp/agents/launcher.py)). Never treat "the last text the
   provider emitted" as the answer — for Codex that is usually a tool or
   reasoning item.

## Conventions

- Python: `uv` + `pyproject.toml` only — no `pip install`, no `requirements.txt`.
- Ruff config lives in `pyproject.toml` (currently `py311`, line length 100,
  rules `E,F,I,UP,B,SIM`, `E501` ignored). Read it rather than assuming.
- `from __future__ import annotations` at the top of every module.
- Pydantic models are the schema layer. Node models use `extra="forbid"`;
  agent-facing schemas in `src/rcp/agents/schema.py` are strict by design —
  loosening one is a spec change, not a fix.
- Tests live in `tests/`, one file per module area, sharing the `manifest`
  fixture in `tests/conftest.py` and patch builders in `tests/helpers.py`.
  Prefer extending those over inventing new scaffolding.
- Web: TypeScript strict via `tsc -b`; `web/src/types.ts` mirrors backend
  response shapes — update both sides in the same change.
- App data lives at `~/Library/Application Support/research-control-panel/`
  (SQLite + caches); override with `RCP_DATA_DIR`. Tests must not touch the real
  data dir — pass an explicit `data_dir`.

## Gotchas

- **Build `web/dist` before anything Python, including `uv sync`.** It is
  gitignored, and the wheel force-includes it
  ([pyproject.toml](pyproject.toml)), so on a fresh clone `uv sync` fails in
  hatchling with `Forced include not found` before you reach a single test.
  `uv run pytest` needs it for a second reason:
  `test_legacy_direct_human_write_endpoints_are_not_exposed` asserts `405` on
  paths that return `404` when the SPA catch-all is not mounted. So the order on
  a fresh clone is `npm --prefix web ci && npm --prefix web run build`, then
  `uv sync`, then pytest — which is what CI does.
- **`.research/` is excluded from every pre-commit hook.** Patch files are
  append-only and materialized files are outputs (invariants 1 and 2); a
  whitespace fixer rewriting one would violate both. Keep the top-level
  `exclude:` in `.pre-commit-config.yaml` if you add hooks.
- The blueprint is long. Read the specific section you need (headings are
  greppable) rather than the whole file.
- Remote/SSH paths mean a path *on that machine*, always paired with a host.
- `examples/demo-project/state-repo` is a real fixture project with a
  multi-revision graph, a pending proposal, and an ambiguity. Running the demo
  mutates it; treat unexpected diffs there as a signal.
- `.recovery/` holds salvaged run artifacts, not source. Leave it alone.

## Human preferences

Recorded from the user; add to this list when they state a preference worth
carrying forward, and correct an entry when they change their mind.

- Implementation is delegated and fanned out; reading, planning, verification,
  and review stay with the main agent.
- Agent execution policy belongs in individual run modules while `app.py`
  remains composition and routes; extract behavior unchanged before cleanup or
  deduplication.
- A review is not a handoff: fix important bugs or gaps it finds in the same
  task, then verify the repaired behavior.
- UI-level verification is expected for features, user-reported bugs, and
  substantial changes — not just green tests.
- **Acceptance before code.** The human's attention goes to design and to
  scenarios, not to reading code or tests. So the agent proposes the scenario —
  including the UI path for anything new — and confirms it *before* building.
  Real flows catch what unit tests structurally cannot; unit tests remain the
  cheap way to *check* a promise, never the statement of one.
- A scenario is not a unit test wearing a costume. If its assertions are fully
  determined by one API call, its driver is `pytest` and it should say so. A
  browser is earned only when the thing that can break lives in the browser.
- **Staleness is swept, not polled.** End a coding session by checking the
  `pending` and `blocked-external` scenarios for ones that became runnable or
  became wrong. Leave `implemented` ones alone unless asked — a code change is
  what should prompt a re-run, not elapsed time.
- Server launch commands own singleton replacement and frontend builds; the
  human should never need to look up or kill an RCP PID manually.
- Routine RCP development uses the browser workflow and does not run Tauri
  builds. Run the desktop checks only when native behavior changed or for final
  pre-push/release verification. After that verification, run
  `cargo clean --manifest-path web/src-tauri/Cargo.toml` unless more Tauri work
  is planned, so disposable Rust build artifacts do not accumulate on disk.
- Nothing versioned should be hardcoded into instructions; point at the source
  of truth instead.
- **Tunables belong in one central place, not scattered as per-file globals.**
  Naming a magic number `_MAX_ENTRIES` at the top of the file that uses it is not
  enough. Limits, timeouts, retention windows, and cache bounds are configuration
  and live together. Schema constants are the deliberate exception — slug
  patterns, node prefixes, field allowlists, and payload caps stay next to the
  models they constrain, because they are the contract rather than a knob.
- **Measure before adding a cache, and before removing one.** A quadratic
  `model_copy(deep=True)` in materialization sat behind a checkpoint cache that
  existed to hide it; the profile said 98% of replay time was that one line.
  Profile the real path first — the expensive thing is rarely where the
  machinery already is.
- Structured deliverables are file-backed. A patch — anything with a schema that
  a truncated or interleaved stream would silently corrupt — is written to a file
  and read from that file, never parsed out of a message stream. Conversational
  prose is the exception: a chat reply *is* the stream, and making the agent also
  write it to a file would be two channels for one payload. Capture it from the
  provider's labelled final assistant message instead.
- Conversation scratch is writable in both modes. Discuss has no graph contract;
  Work is the per-turn authorization for unrestricted operational execution and
  one optional semantic `patch.json`. RCP, not the agent, adds graph bookkeeping.
  Optional previews stay temporary and provider-agnostic.
- Discuss and Work are switchable on every node and project conversation.
  Discuss is plum, Work is dark forest, `Shift+Tab` toggles while the composer is
  focused, and every sent turn keeps an immutable visible mode label. A resumed
  task keeps its original mode regardless of the current composer setting.
- Work is non-interactive and unrestricted for both providers. Codex uses
  `--dangerously-bypass-approvals-and-sandbox`; Claude uses
  `--permission-mode bypassPermissions`. Work graph and watcher corrections retain
  that same native session and Work permission profile. The direct canonical
  `.research` prohibition is prompt-enforced for both providers and is recorded
  as that accepted limitation, never as sandbox enforcement. Discuss,
  Seed/Refresh and their generic patch correction, and paper coaching retain
  their existing narrower profiles.
- A Work patch is not a universal Proposal. Ordinary legal graph operations
  apply as asserted agent content; only the existing narrow gated operations
  create Proposal records for Inbox.
- Invalid Work patches enter bounded same-session `work_patch_correction` with
  the original Work access. Only the instruction becomes patch-focused;
  operational work is never repeated merely to repair graph reflection. Apply
  re-prepares and revalidates live state under the append lock, so graph movement
  alone cannot turn completed Work into a rejected operational task.
- HTML previews keep useful inline JavaScript. Their security boundary is
  isolation from the RCP parent, not literal zero network traffic: a script may
  navigate only its own sandboxed child frame and cause that navigation request.
- Never copy a repository to make it readable. Give the agent the host and path.
- Context boundaries are named exactly, never approximated by a containing
  directory: Discuss and Work get the graph/current node and exact run-scope
  repository pointers, but no provider-root or prior-transcript input. Those
  pointers bound context, not unrestricted Work permission. Seed and Refresh are
  the only paths that receive provider-source roots for ingestion.
- Every agent invocation is durable background work. Closing its launch or chat
  surface must not cancel it; one shared activity and notification design
  surfaces progress, completion, failure, resume, and retry while the app stays
  usable.
- Node wording correction is a literal human edit, not an agent request. Open a
  direct prose editor, stage it in the project draft, clear the draft standing
  to asserted, and never start node chat merely to rewrite text. Canonical
  history changes only when the human presses Sync.
- A node must be understandable when opened alone: prefer ordinary language,
  enough context-setting sentences, and inline explanations over terse project
  jargon. Relation rows should open a focused one-hop DAG view.
- The Paper editor/coach split is human-resizable, and the editor begins with
  authored content rather than a redundant canonical-file banner. The authored
  Markdown switches between Write and Preview in the same pane, using the chat
  renderer so unsaved text can be read without creating a second document.
- Agent configuration is owned by Project Settings. Chat and coaching show one
  non-expandable provider-name box only: no model, reasoning, machine,
  permission summary, or locked/editable label. Seed/Refresh keeps its explicit
  launch controls, and chat keeps Raw truth inputs because those select context,
  not execution configuration. Settings supplies fresh conversation defaults;
  an existing native conversation retains the profile it last ran with so
  continuation does not silently move providers or machines.
- Chat and coaching surfaces never contain sample prompts, slogans,
  instructional empty-state copy, or textarea placeholder text. An empty
  conversation is simply empty.
- **No commentary lines in UI design.** Never place a smaller, muted, or more
  transparent explanatory line beneath a button, title, large label, card heading,
  or other primary UI element. Remove such helper subtitles and descriptive
  microcopy wherever they appear; make the primary wording, hierarchy, shape,
  color, motion, and control state communicate the design. Keep actual errors,
  conflicts, required warnings, and accessibility labels explicit.
- **Share Margin Dev's visual grammar; do not copy its catalog literally.** RCP
  uses a restrained paper, sheet, walnut, and oxblood system, warm rules and
  shadows, compatible typography, and tactile book materials. Project covers
  share one oxblood base and differ by texture only; never assign decorative
  colors per card. Semantic accents are reserved for meaningful type or state.
  RCP keeps its own information architecture and behavior.
- RCP branding is one unified mark. Never place an initial tile beside the full
  acronym, which reads as a duplicated letter; the visible logo contains
  **RCP** exactly once.
- The project shell is intentionally bare: no RCP wordmark, product logo, or
  revision label beside the project name. Agent tasks and Refresh are icon-only
  accessible controls; project chat is **Ask**. The attention destination is
  **Inbox** with a colored count, and DAG is a subpanel of **Research**, not a
  primary destination. Group the header semantically: labeled **Sync / Ask**
  together, then icon-only
  **History / Refresh** together; do not space all four as unrelated peers.
  Glossary definitions appear inline where terms are read; Glossary has no
  navigation destination, and glossary authoring remains an open question.
- A previously opened project must feel immediate even when canonical state is
  remote. Render one rebuildable durable display snapshot first, refresh the
  authoritative state in the background, and keep the cache out of every
  history, agent, Sync, paper-write, and other authority path. Canonical
  mutation controls wait for reconciliation, and blocking remote refreshes run
  off the web event loop.
- DAG controls include boundary-aware page scroll chaining, brighten/dim-all,
  fullscreen with visible node details, **Release all pins**, and per-node pin
  release. Repulsion must visibly affect spacing, and the canvas must leave
  generous room for manual dragging beyond auto-layout positions. Touchpad
  pinch zoom stays anchored at the gesture focal point without turning ordinary
  two-finger scrolling into zoom or disrupting other DAG interactions.
- The visible projections are **Research** and **Runs**: Research shows
  question-centered paths with unconnected records separated, while Runs shows
  Seed/Refresh ingestion runs and experiments, prioritizes failed/paused
  ingestion work and graph blockers, nests ingestion retries, and reports a
  truthful as-of time. Node chat, project chat, and paper-coach tasks live in
  the Agent tasks drawer, never in Runs. DAG **Research flow** columns follow
  semantic stage rather than relation-arrow direction.
- Node detail is a resizable floating inspection window. Its project-scoped
  size survives minimize/restore and close/reopen, remains reachable after a
  viewport change, and closes when the human enters Chats.
- Human-readable patch prose is governed primarily at the producer prompt and
  human-action boundary. Deterministic history rendering is a safety net that
  resolves ids to titles and derives truthful operation fallbacks; it does not
  invent scientific causality.

## Repeated failures

Condense recurring mistakes here — one line each, cause then correction — so the
next agent does not rediscover them. Keep it short; delete entries that no
longer apply.

- Do not copy commands out of the README without running them. `serve --reload`
  was documented there while it exited instantly instead of serving (fixed
  2026-07-28 via the `reload_app` factory).
- Only one RCP server may own a data directory (`fcntl` lock). `open` reuses its
  healthy owner and replaces an unavailable one; `serve` gracefully replaces
  it and waits for the lock, so do not send the human through PID discovery.
- Do not delete a run's scratch folder on failure. Repeated seed/refresh attempts
  discarded good agent work over a filename mismatch; the fix (2026-07-28) was to
  scan the folder, retain it, and correct in-session instead of asking the human
  to press Retry.
- A reused graph-run stage still contains the prior attempt's `patch.json`.
  Fingerprint it before every correction launch and refuse an unchanged file;
  otherwise a provider that writes nothing can appear to complete successfully.
  Refusing means handing the unchanged-file diagnostic straight to the next
  correction, never revalidating the same bytes — revalidation overwrites that
  diagnostic with the original one and the agent never learns it wrote nothing.
- A Work validator self-check is not a reservation. Human Sync may move the graph
  after any response, so Apply must reload current state, re-prepare RCP-owned
  bookkeeping, and rerun semantic validation under the append lock. Never restore
  an expected-revision check or Resume-ancestor lookup as a substitute.
- Whole-patch lookup is not operation reordering. Build lookup indexes for legal
  node and edge references, but stage each operation against the temporary state
  produced by earlier valid operations in exactly the order written.
- "One shared background lifecycle" is not "one shared execution pipeline". Node
  chat was routed through the ingest path, so a corrupt ingest cursor killed an
  ordinary question before the provider ever launched. Chat now has its own
  graph/repository-only context and prompt; Seed/Refresh retain the separate
  source-ingestion context and deliverable.
- One unreadable conversation source must not kill Seed/Refresh before launch.
  `ContextAssembler` drops only the source it cannot assemble, reports the exact
  diagnostic, names the provider roots and last accounted coverage boundary in
  the fallback prompt, and leaves the provider to inspect the source. No cursor
  or coverage claim advances for input RCP did not read.
- `codex exec resume` accepts neither `--sandbox` nor `--cd`. A Work Resume must
  still receive `--dangerously-bypass-approvals-and-sandbox`; narrower resumed
  capabilities carry their sandbox mode through `--config`. Check the
  subcommand's own `--help` before assuming a flag carries over from `codex exec`.
- Materialized files are regenerated from the patch log, so hand-editing
  `.research/cursors.json` to reproduce a bug does nothing — the next
  materialization wipes it. Append a patch carrying `processed_cursors` instead.
- Chat must not be made dependent on source-pointer reachability. A prior chat
  transcript is UI history only; Discuss and Work never resolve, copy, or validate
  it as agent input. A provider session id may continue the provider's native
  session without becoming RCP context.
- Failing a task discarded the answer it had already produced, so a chat whose
  graph change was rejected showed an error and no reply. `TaskFailed` carries
  the partial messages into `fail_agent_task`.
- A remote PID wrapper built `exec cd <cwd> && <provider>`; the shell returned 0
  after changing directory without launching the provider, so chat reported no
  answer. Change directory first, then `exec` the provider.
- macOS spells `/tmp` as `/private/tmp` after path resolution. Resolve `RCP_DATA_DIR`
  once in `create_app` so cache roots and loaded manifest paths stay comparable.
- A `null` model on the wire meant provider default to the client but keep the
  stored value to the resolver, so switching providers launched the previous
  provider's model. A provider change now clears an inherited model unless the
  caller supplies the new one explicitly.
- History id rescue matched arbitrary `word/word` text and rewrote repository
  paths, while an inventory-style filter silently dropped authored changes.
  Substitute only identifiers resolved at that revision and preserve every
  non-empty legacy sentence; style is governed at the producer boundary.
- Eager full-history prose replay delayed project entry and then reapplied every
  accepted patch inside the renderer. Load only the latest summary after project
  state, load the complete projection when History opens, and collect prose from
  the existing replay observer rather than adding a second pass or a cache.
- Pause/resume is parent→child, not one operation id. A test that reuses one id
  models nothing: read task state across the chain, and exercise resume through
  `POST …/tasks/{id}/resume` so the child is real. Validate the saved native
  stage and session provenance, but never walk that lineage to recover a Work
  patch base revision; Apply always uses live state under the append lock.
- Claude `--add-dir` is context plumbing, not a Work authority boundary:
  `bypassPermissions` makes Work repository/tool access unrestricted. Do not put
  provider roots into Discuss or Work context. Seed/Refresh may receive provider
  roots only when source assembly degraded and the prompt explicitly explains
  why.
- Benign shell noise must never become a failure reason. `bash -lic` writes
  "cannot set terminal process group" on every remote run, so a connection
  dropped mid-run was reported to the human as a tty error instead of a lost
  connection. Filter known-harmless stderr before surfacing it, and translate a
  signalled or 255 exit into what actually happened (fixed 2026-07-30 in
  `_meaningful_stderr`/`_exit_reason`).
- `blocked-external` is a claim with an expiry date, not a permanent label. S14
  and S18 sat blocked on "needs a reachable SSH host" while one was configured
  and up the whole time, and a "remote path is untested" caveat was written into
  S24 on the same unchecked assumption. Run the check (`ssh -o BatchMode=yes -o
  ConnectTimeout=8 <host> true`) before repeating a blocker, and re-check every
  one of them during the end-of-session sweep (fixed 2026-07-30).
- What a provider CLI accepts is read from that CLI, never from memory. A
  hand-typed reasoning list offered `minimal` (rejected by every current Codex
  model) and omitted `max`/`ultra`, and a working Claude control was deleted on
  the false belief that `_command` dropped `--effort`. `codex debug models`
  enumerates models with per-model efforts; `claude --help` documents its own.
  Provider facts now live only in `src/rcp/providers.py` (fixed 2026-07-30).
- Code that must also run on a remote host is never hand-transcribed into a
  string literal. Two copies of the conversation parser drifted — only the remote
  one guarded a non-dict `payload` — and the copy nobody can test locally is the
  copy that rots. Put the logic in a stdlib-only module and ship *that file's
  source* over ssh (`src/rcp/sources/record_parsing.py`).
- The RCP window loads the backend's own origin, which Tauri treats as a **remote**
  page, so app commands are ACL-gated: without `AppManifest::commands` in
  [build.rs](web/src-tauri/build.rs) and the matching `allow-*` entries in
  [capabilities/main.json](web/src-tauri/capabilities/main.json), every `invoke`
  fails with "not allowed. Plugin not found". Both bundles shipped that way and
  neither ever opened (fixed 2026-07-31).
- "The bundle built and the process is running" is not "the window appeared."
  A window created hidden and shown only by a successful frontend handshake turns
  any failure into an app that silently does not open, so it now shows itself when
  the handshake does not arrive, and startup milestones go to stderr. Verify a
  desktop change by reading those milestones or `lsappinfo front`, not by the
  process still being alive.

## Maintaining this file

Treat `AGENTS.md` as evolving, not fixed. During or at the end of a task, update
it when any of these happen:

- a pointer went stale (a path moved, a command changed, a version bumped),
- a rule turned out to be wrong or too rigid in practice,
- the user stated a preference → add it under "Human preferences",
- you hit the same failure a second time → condense it under "Repeated
  failures",
- a new module boundary appeared → add it to the fan-out table.

Rules: keep edits small and specific; delete anything no longer true rather than
appending a caveat; never hardcode a version, count, or line number that will
drift — point at the file that owns it. Mention in your response what you
changed here and why.
