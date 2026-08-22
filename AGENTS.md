# AGENTS.md

Instructions for coding agents working in this repository. `CLAUDE.md` imports
this file; keep this one canonical and do not duplicate content into it.

**This file is a living document.** See "Maintaining this file" at the bottom —
you are expected to update it as the project changes.

## What RCP is

RCP implements the cross-cutting design in [`docs/design.md`](docs/design.md)
and the current module contracts under [`docs/specs/`](docs/specs/). Active
acceptance scenarios state selected observable promises, active decision records
explain rationale, and active handoffs authorize work not yet implemented. Read
the precedence hierarchy in `docs/design.md`; [`docs/archive/`](docs/archive/)
is historical and never current authority. When current sources or code
disagree, say so explicitly instead of silently picking one.

[`docs/open-questions.md`](docs/open-questions.md) holds design questions that
are raised and evidenced but **not decided**. Read it before proposing a change
to something it covers, and add an entry rather than deciding an open question
inside an implementation.

In one sentence: a local desktop app that turns agent-driven research conversations
into one durable research-graph record, a human authority queue, and a
human-authored paper introduction with a read-only writing coach.


## Default working mode

The main agent orients, plans, verifies, and reviews. **Implementation fans out
to subagents.**

0. **Decide whether the work creates a durable product promise.** Add and confirm
   an [`docs/acceptance/`](docs/acceptance/README.md) scenario first only for a
   cross-module journey, authority/recovery/data-loss boundary, external
   integration, or browser/desktop interaction not already covered by an active
   scenario. Bugs, refactors, API shapes, and module-local regressions normally
   get focused tests and a specification update only when semantics changed. A
   handoff explicitly marked human-confirmed and ready to implement does not
   require another confirmation interview.
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
7. **Close the documentation loop.** Update current specifications when
   behavior changed and archive an implemented, superseded, or abandoned handoff
   when its work closes. Never cite an archived file as current authority.

### Integrating work built outside this tree

If you find another session committing to `main` while a long change is being built,
so an integration must survive drift. Give each parallel worker an APFS clone
(`cp -c -R`, near-free, and unlike a worktree it carries uncommitted state),
delete the copied `.venv` before `uv sync` — it holds an editable-install `.pth`
pointing back at the original checkout, so tests would silently exercise the tree
they were cloned from. Commit inside the clone, `git fetch <clone> HEAD:refs/…`,
and merge that ref: the shared ancestor makes it a real three-way merge that
conflicts only where the drift genuinely overlaps. Do not integrate by piping
`git diff` into `git apply` — it fails whole-file on drifted context, which is
the opposite of what is wanted.

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

Baseline is pre-commit hooks and tyoe checks.

### Done means the checks and applicable scenario pass

[`docs/acceptance/`](docs/acceptance/README.md) holds selected durable promises
in the language of someone using RCP. Relevant focused checks and any active
scenario whose promise the change touches must pass; a baseline alone is not
proof of user-visible behavior.

Name every applicable active scenario. If none covers the change, apply step 0's
durable-product-promise test before adding one. A scenario declares how cheaply it can be checked: `driver:
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
remote path works, reach and exercise it. Otherwise, state that it is untested and why.

State clearly what you verified and what you did not.

## Invariants

**An invariant is a guarantee whose violation breaks something major, and breaks
it quietly.** Each one below names what breaks. If you cannot name that, it is a
design rule or a note, not an invariant — put it in the right section.

The numbers are stable identifiers cited from source comments, tests, and
acceptance scenarios in more than twenty files. Never renumber one. Suffixed ids
(`4b`, `10c`) are labels, not an outline; a new invariant gets the next free
label in its own subject, not a suffix on the nearest topic.

Mechanism belongs in [`docs/specs/`](docs/specs/); state the guarantee here and
let the spec say how it is achieved.

### Canonical history

1. **Canonical main and graph-branch Patch logs are append-only.** Never edit or
   delete a patch file under `.research/patches/` or a branch Patch namespace.
   *Breaks:* replay silently produces a different graph, and canonical history is
   unrecoverable. An SSH mirror may discard an unpublished local batch after
   remote failure, because that mirror is explicitly not canonical history.
2. **Materialized files are never hand-edited.** `graph.json`, `research.md`,
   `glossary.json`, `proposals.json`, `coverage.json` are outputs.
   *Breaks:* the file disagrees with the log, and a human trusts the wrong one.
6. **Exactly one canonical state repository**, possibly remote. Writes go through
   the `StateWorkspace`; never write canonical files directly from a route
   handler. Auto-research graph branches are namespaces inside this repository,
   not additional project homes.
   *Breaks:* split-brain canonical state.
6b. **One synchronous transition manager owns semantic mutation.** Human Sync,
   agent Apply, branch Apply, and branch merge commit one exact-target revision
   or nothing. Replay applies recorded expanded operations and never reruns
   historical rules. Every mutation response replaces graph, control, guidance
   validity, and head from one final state; the client computes no rule outcome.
   *Breaks:* two mutation paths diverge, and client and server disagree about
   what a rule did.
7. **Atomic writes.** Manifest and materialized output writes go through the
   existing temp-file-then-`os.replace` helpers.
   *Breaks:* a crash mid-write leaves a truncated manifest.
7b. **Materialization never mutates a contained model in place.** Every change in
   `_apply_patch` replaces a container slot or a whole attribute. `_fork_state`
   in [materialize.py](src/rcp/core/materialize.py) relies on this: it copies
   only the containers and shares their contents.
   *Breaks:* a failed patch silently corrupts the **previous** revision.
   `test_patch_failing_part_way_leaks_no_earlier_operation` guards it.

### Human authority

3. **Agents assert or propose; humans hold the protected authority boundary.**
   Only human UI actions approve gated operations, change project truth
   membership, authorize a bounded episode, or dispatch a branch merge. No agent
   may approve a Proposal. The deliberate exception is the human-authorized
   Auto-research orchestrator on its own graph branch. A human authority action
   whose operations cannot distinguish it from an ordinary edit names itself on
   the patch (`human_action` in [models.py](src/rcp/core/models.py)); validation
   dispatches on that name.
   *Breaks:* an agent decides something only a human may decide. Never infer
   which action produced a patch from its operation shape — a Decision choice and
   a node edit are both one `update_nodes` on one node, so guessing silently
   reroutes the other one.
3b. **The protected-type rule.** An agent operation is free unless it touches an
   existing ResearchQuestion or Hypothesis — those two types are the project's
   beliefs. This covers update, remove, supersede, merge, and the edges that
   restructure or retire one. Connecting a node created in the same Patch is not
   restructuring, and attaching Evidence stays direct because the status change it
   argues for is gated one layer down. The rule binds **every** agent, including
   an ordinary Work turn a human is watching. A Proposal carries one declared
   intent checked against a closed set of shapes, never inferred from how its
   operations happen to look.
   *Breaks:* an agent silently rewrites what the project believes.
4. **Agent permission contracts are fixed by capability.** `permissions_for()` in
   [config.py](src/rcp/config.py) is the semantic contract; the manifest may not
   widen or narrow it. Discuss has writable conversation scratch but no project or
   graph authority. Work-like launches use native unattended exact-root
   enforcement; dangerous/bypass permission modes are forbidden. Seed, Refresh,
   and their correction write only their run scratch. The paper coach has no write
   or Apply path anywhere. Canonical `.research` is outside every agent write
   scope.
   *Breaks:* an agent writes where it was never authorized to. This is
   cooperative accidental-write containment, not hostile isolation or a
   read-secrecy claim.
4b. **`patch.json` in the agent's own stage is the only graph-change channel.**
   Never parse a patch out of stdout or a final message, and never add a
   canonical-state write path. Work repository edits carry operational authority,
   not graph authority.
   *Breaks:* a patch recovered from a stream is silently truncated or interleaved,
   and a corrupted graph change applies.
5. **Run context, write scope, and graph target are distinct.** The whole graph
   and canonical `research.md` for the exact target enter every graph-agent run;
   run-scope repositories enter only as raw pointers, while `ProjectWriteScope`
   independently grants exact repository roots.
   *Breaks:* conflating them grants write access that context alone implied.
10b. **Work is the only conversation mode with graph authority.** The captured
   per-turn `mode="work"` is the authority; there is no separate
   `allow_graph_change` gate, and an agent cannot grant itself authority by
   writing a file during Discuss. A stray Discuss patch is kept as a receipt and
   discarded. Never infer mode from the wording of the message.
   *Breaks:* an unauthorized turn changes the graph.
10c. **A chat's scratch folder belongs to the conversation, not the turn.** Keyed
   by stable project identity plus `chat_id` and reused, because a resumed native
   session must run in the directory it was given. Clear the previous turn's
   `patch.json` on entry, and let that clearing fail closed — the workspace
   operations in [run_stage.py](src/rcp/transport/run_stage.py) raise rather than
   report an unreachable workspace as an empty one.
   *Breaks:* a surviving `patch.json` is read as this turn's patch and applied
   under this turn's authorization. Deleting the folder per turn instead breaks
   multi-turn chat.
10d. **Discuss and Work never read, index, copy, project, prompt with, validate,
   or authorize from prior chat transcripts.** A provider session identifier may
   still continue the provider's own native session. The answer may be appended to
   chat history for the UI, but that write is never an input to a turn.
   *Breaks:* unreviewed history becomes agent authority.

### Runs and durability

8. **One RCP process per data directory**, enforced by an `fcntl` lock in
   [`__main__.py`](src/rcp/__main__.py). Remote canonical-state concurrency is
   likewise process-owned: `.agent-run.lock` and `.refresh.lock` are regular files
   held by an OS advisory lock through an SSH holder process. File existence is
   not ownership; live contention waits rather than failing, and process or
   connection death releases ownership.
   *Breaks:* two processes write one store, or a stale path is mistaken for a
   live owner. Never restore mkdir/rmdir lock ownership.
9. **A failed run keeps its scratch folder and its patch text.** The patch is
   persisted before validation runs, and the folder is deleted only after the
   patch applies.
   *Breaks:* good agent work is discarded over a validation failure and cannot be
   recovered.
10. **A conversation turn is not an ingest run.** Seed/Refresh and conversation
   turns share the launcher and background lifecycle, nothing else. Discuss and
   Work assemble chat context; neither loads `cursors.json` nor materializes an
   evidence slice, and they take the canonical append lock only if a Work patch
   needs applying. Discuss receives no patch contract. A Work patch is forbidden
   from touching coverage or cursors.
   *Breaks:* a corrupt ingest cursor kills an ordinary question, or a chat turn
   silently advances ingestion state.
10e. **A preview cannot reach RCP.** HTML runs in an opaque sandbox: it cannot
   access or navigate RCP, open popups, submit forms, start downloads, or use
   ordinary network resource APIs. RCP never discovers artifacts from provider
   directives, provider-owned paths, URLs in the answer, nested files, or
   symlinks, and never automatically copies one into canonical state.
   *Breaks:* agent-authored HTML acts on the app that renders it. Inline
   JavaScript may still navigate its own isolated child frame, so never describe
   this as a zero-network preview.
10f. **The Seed/Refresh watermark advances only when a patch applies.** Failed,
   paused, interrupted, and rejected runs leave it unchanged. It is an
   overlap-tolerant project timestamp, not an exactly-once record cursor.
   *Breaks:* conversations are silently skipped and never ingested.
10g. **One episode parent, one validated native-session binding, one graceful
   stop.** Every bounded episode binds exactly one provider, session id, execution
   host, and reusable stage, committed only by a mechanically successful joint
   Patch/watcher handoff. Every Auto-research episode is durably bound before
   launch to one graph-only branch on one coherent main head, and every path in it
   retains that exact target; the branch is never discarded and main stays
   independently writable. **Stop loop** is idempotent, durable, and restart-safe:
   intent is persisted before any unclaimed watcher can win a new claim.
   *Breaks:* work silently resumes on a fresh session or the wrong graph, or a
   stopped loop restarts itself. Nothing ever falls back to a fresh session
   silently.
11. **`answer` is the reply; `message` is a trace.** Providers label their final
   assistant message and RCP preserves that label in `AgentEvent`
   ([launcher.py](src/rcp/agents/launcher.py)).
   *Breaks:* the human is shown a reasoning or tool item instead of the answer —
   for Codex, the last text emitted usually is one.

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

## Design rules

**A design rule is a decision about how the code is arranged.** Break one and
nothing fails immediately — the code gets worse, or an invariant becomes easier
to break. That is why these are separate from invariants: they are guardrails,
not guarantees.

Rationale lives in [`docs/decisions/`](docs/decisions/) and in the design
philosophy inside [`docs/design.md`](docs/design.md) and
[`docs/specs/`](docs/specs/). State the rule here, not the argument for it.

### Where code goes

- **Policy lives in the caller, not in shared plumbing.** Keep Discuss, Work, and
  Seed/Refresh paths in distinct modules and entry points under
  [`src/rcp/runs/`](src/rcp/runs/). Shared *plumbing* is fine and expected — they
  call the same launch, receipt, staging, and event-pump helpers. The line is the
  discriminator: no shared helper may take a `kind`, `is_chat`, `surface`,
  `patch_kind`, or equivalent parameter, because anything that must know which
  surface it serves is policy. Leaving some lines duplicated is the correct
  outcome, not a missed cleanup.
- Agent execution policy belongs in individual run modules while `app.py` remains
  composition. Extract behavior unchanged before cleanup or deduplication.
- **Permission is code, not configuration.** Agent profiles are constants.
  Changing what an agent may do requires the governing specification and focused
  contract checks to change together. Every launch names its capability outright —
  `AgentLauncher.stream` and `_command` require it, and there is no boolean
  shorthand a caller can pass instead.
- **Tunables belong in one central place, not scattered as per-file globals.**
  Naming a magic number `_MAX_ENTRIES` at the top of the file that uses it is not
  enough. Limits, timeouts, retention windows, and cache bounds live together.
  Schema constants are the deliberate exception — slug patterns, node prefixes,
  field allowlists, and payload caps stay next to the models they constrain,
  because they are the contract rather than a knob.
- **Structured deliverables are file-backed.** Anything with a schema that a
  truncated or interleaved stream would silently corrupt is written to a file and
  read from that file. Conversational prose is the exception: a chat reply *is*
  the stream, so capture it from the provider's labelled final assistant message
  rather than writing it twice.
- Never copy a repository merely to make it readable — off-machine repos are read
  over SSH from the host and path in the prompt.
- **RCP must stay able to read records RCP wrote.** `extra="forbid"` is right for
  a live caller and wrong for a stored row: a request field removed from a model
  made every task already holding it unrecoverable. Read a persisted request
  through `load_stored_request` in
  [task_policy.py](src/rcp/runs/task_policy.py), which drops only undeclared keys
  and logs each one. Removing a request field is a data-compatibility change, not
  a rename. Canonical Patch history is the same rule with a worse failure: a
  retired field left on a stored operation, or a field the in-memory adapter adds
  when it retires a value, halts replay and makes the whole graph read-only. Both
  belong in `adapt_persisted_patch_document`
  ([operations.py](src/rcp/core/operations.py)) and in the replay branch of every
  rule that lists the fields a Patch may change.
- **Code that also runs on a remote host is never hand-transcribed into a string
  literal.** Two copies of the conversation parser drifted and the untestable one
  rotted. Ship the module's own source over ssh
  (`src/rcp/sources/record_parsing.py`).
- **Prose describing an enforcement boundary drifts from the code enforcing it.**
  The Work contract claimed no repository allowlist while `providers.py` denied
  every write outside the resolved roots, so an authorized action came back as an
  unexplained tool denial. Render the resolved object — `write_scope_section` in
  [prompts.py](src/rcp/agents/prompts.py) — so prompt and flags read one source.
- Only the episode report restricts its packages. Orchestrators and workers
  resolve Settings packages like any other Work agent; if a staging call asserts
  an exact skill id, check it is the report path before copying it.

### Authority shape

- **Graph branches are one narrow canonical exception, not version control.** Do
  not generalize them into user branches, Git worktrees, repository rollback,
  branch discard, branch-to-branch merge, or a conflict viewer. Project
  duplication or movement uses the canonical nameplate and refusal/transfer
  semantics, never inferred history reconciliation.
- **An Auto-research episode is scoped to the project**, not to the question it
  started from. The budget and the protected-type rule are the brakes; there is no
  second fence quietly doing that job. A seated worker gets no scope of its own —
  where it may be seated is bounded, what it may then touch is not.
- A Work patch is not a universal Proposal. Ordinary legal graph operations apply
  as asserted agent content; only the narrow gated operations create Proposal
  records for Inbox.
- The Auto-research orchestrator is a Work agent and resolves Settings packages
  like any other. Only the concluding report is narrowed to its one required skill.
- **Borrow the host's privilege system; do not restate it.** Where an operation
  needs real authority — installing, backing up, restoring, removing a person —
  require operating-system privilege rather than inventing an RCP admin role. A
  rule that reduces to "admins are admins" is circular and should be deleted.
- Human-readable patch prose is governed at the producer prompt and human-action
  boundary. Deterministic history rendering resolves ids to titles and derives
  truthful fallbacks; it never invents scientific causality.

### Talking to agents

- **Agent-facing prose is written, not accreted.** Every contract opens by saying
  what RCP is and what the agent's role in it is. Say a rule once: a precedence
  list that restates its own bullets, a section re-listing pointers already given,
  and a heading rendering "- none" are all noise. Describe RCP's internal
  enforcement only where the agent can act on it. Name a resolved path rather than
  an id the agent must substitute into a template.
- A retry that still holds its native provider session gets a short follow-up
  naming only what changed — diagnostics, output paths, schema, staged packages.
  It never rebuilds the task contract, or the agent receives its framing twice.
  Only a retry in a fresh process rebuilds.
- Staged skills and workflows reach the agent as one pointer block with id,
  version, wrapped description, and folder. RCP does not separately mark which
  package a slash token named; the token is already in the human's message.
- The official skill/workflow registry is authoritative at launch. A task stores
  selected ids and every attempt re-resolves them, so upgrading a package
  deliberately upgrades the next attempt and can never make a task un-retryable.
- Every agent invocation is durable background work. Closing its launch or chat
  surface must not cancel it.
- Nothing versioned is hardcoded into instructions; point at the source of truth.

## Notes

Working habits and local facts. Nothing here breaks if ignored — but the human
stated it, so follow it.

### How the human wants work done

- Implementation is delegated and fanned out; reading, planning, verification, and
  review stay with the main agent.
- A review is not a handoff: fix important bugs or gaps it finds in the same task,
  then verify the repaired behavior.
- UI-level verification is expected for features, user-reported bugs, and
  substantial changes — not just green tests.
- **Durable acceptance before code.** Propose and confirm a scenario — including
  the UI path — before building a new durable cross-module promise. Use focused
  regression tests for bugs, refactors, and module-local behavior already governed
  by a current specification or scenario. A confirmed handoff does not reopen
  design.
- A scenario is not a unit test wearing a costume. If its assertions are fully
  determined by one API call, its driver is `pytest`. A browser is earned only when
  the thing that can break lives in the browser.
- **Staleness is swept, not polled.** End a session by checking the `pending` and
  `blocked-external` scenarios for ones that became runnable or became wrong. Leave
  `implemented` ones alone unless asked.
- **Verify against the human's real records, not by clicking around.** Copy the
  data directory, build the app against the copy so the real constructor and
  startup run, then sweep every real row through the code path in question. That is
  exhaustive where a UI drive samples.
- **Measure a refactor's cost and benefit before committing to it, including one
  already agreed.** Two planned restructurings did not survive measurement in a
  single session. State the number before the redesign, and say plainly when it
  reverses an earlier recommendation.
- **Measure before adding a cache, and before removing one.** A quadratic
  `model_copy(deep=True)` sat behind a checkpoint cache that existed to hide it;
  the profile said 98% of replay time was that one line.
- **Ask plainly; do not answer a question with a pointer.** Put the decision itself
  in front of the human — self-contained, scoped, readable without opening a file.
  Name the choice, name what each option costs, and say which one you would take.
  Design documents are where a decision is *recorded*, never how it is *asked*.
- **The Git workflow is single-branch: commit directly to `main`.** Do not create a
  working branch first. Unrelated to RCP's canonical Auto-research graph branches.

### Local facts

- Server launch commands own singleton replacement and frontend builds; the human
  should never need to look up or kill an RCP PID.
- **`RCP Dev.app` is kept, always.** It is the desktop surface the human drives, so
  do not `cargo clean` it away as routine tidying — that deletes
  `web/src-tauri/target/`, the dev bundle included. Shrink the cache through
  `[profile.dev]` in [Cargo.toml](web/src-tauri/Cargo.toml) instead. "Desktop is
  done" refers to the dev app unless the human says release.
- **Only the dev bundle is kept in `target/`; the rest is disposable cache.**
  `target/debug/bundle/` holds `RCP Dev.app` and is self-contained — the binary
  resolves the checkout from a compiled-in `CARGO_MANIFEST_DIR`. Pruning is safe
  and reclaims several GB, at the cost of one cold compile:

  ```sh
  rm -rf web/src-tauri/target/debug/{deps,incremental,build} web/src-tauri/target/release
  ```

- The dev app is a thin shell over the checkout, not a copy. Under
  `debug_assertions` it runs `uv run rcp serve` from the checkout with
  `--web-assets source`, so Python and web changes are already live and need no
  rebuild. Rebuild only when `web/src-tauri/` itself changed — and then it is
  required. `tauri build --debug` is not a diagnostics flag: `debug_assertions` is
  what selects the checkout backend, source web assets, and dev navigation policy.
- Routine development uses the browser workflow and does not run Tauri builds. Run
  the desktop checks when native behavior changed or before a release.

### Environment facts

- **Build `web/dist` before anything Python, including `uv sync`.** It is
  gitignored and the wheel force-includes it ([pyproject.toml](pyproject.toml)),
  so on a fresh clone `uv sync` fails in hatchling with `Forced include not found`
  before you reach a single test. `uv run pytest` needs it for a second reason:
  `test_legacy_direct_human_write_endpoints_are_not_exposed` asserts `405` on
  paths that return `404` when the SPA catch-all is not mounted. Fresh-clone order
  is `npm --prefix web ci && npm --prefix web run build`, then `uv sync`, then
  pytest — which is what CI does.
- **`.research/` is excluded from every pre-commit hook.** A whitespace fixer
  rewriting a patch file would violate invariants 1 and 2. Keep the top-level
  `exclude:` in `.pre-commit-config.yaml` if you add hooks.
- Remote/SSH paths mean a path *on that machine*, always paired with a host.
- `examples/demo-project/state-repo` is a real fixture project with a
  multi-revision graph, a pending proposal, and an ambiguity. Running the demo
  mutates it; treat unexpected diffs there as a signal.
- `.recovery/` holds salvaged run artifacts, not source. Leave it alone.
- **Tauri treats the backend origin as a remote page**, so app commands are
  ACL-gated: without `AppManifest::commands` in
  [build.rs](web/src-tauri/build.rs) and matching `allow-*` entries in
  [capabilities/main.json](web/src-tauri/capabilities/main.json), every `invoke`
  fails with "not allowed. Plugin not found". Both bundles once shipped that way
  and neither opened.
- **Browser-pane quirks that silently invalidate a check.** The pane runs with
  `document.hidden`, so rAF never ticks and `scroll` events never dispatch — read
  offsets synchronously while the outgoing view is mounted. A forced `navigate`
  can collapse the viewport to 0x0, making every scroller report
  `clientHeight === scrollHeight`; call `resize_window` with explicit dimensions
  first. React batches, so a click and the assertion about it must be separate
  `javascript_tool` calls.

### Mistakes made more than once

Cause, then correction. Delete an entry once the structure that allowed it is gone.

- **Verify the verification before believing red or green.** `pytest | tail`
  reports *`tail`'s* exit code, so failing runs read as clean — write output to a
  file and read `$?`. `-p no:randomly` was a no-op for months because
  `pytest-randomly` is not installed. BSD `find` rejects `-newermt '30 minutes
  ago'` and prints nothing with stderr suppressed, which reads as "no recent
  edits" (2026-08-12).
- **A shifting set of failures is a changing tree until proven otherwise.** Three
  tests failing in three combinations across four runs looked like a load race;
  the runs were simply not the same code. Re-run the suspect test in a pristine
  clone of the exact commit before theorizing about timing (2026-08-12).
- **`pre-commit run --all-files` means all *tracked* files**, and it rewrites
  files you are not working on. A green run proves nothing about files a change
  adds, and `ruff check` hides this because it walks directories and does see
  untracked files. Use `ruff format --check`, and `pre-commit run --files <paths>`
  for exact new files or when the tree holds someone else's changes (recurred
  2026-08-17).
- **Tests that pass while proving nothing.** A test bypassing the middleware
  cannot see the middleware refuse — every membership test used
  `trusted_principal_resolver`, so the first invitation UI shipped and got `415`
  from a real server. A fake `stream` calling a nonexistent method still passes,
  because the `AttributeError` becomes the task's `failed` status. `include_router`
  leaves an opaque `_IncludedRouter` in `app.routes`, so a flat walk finds zero
  routes and asserts vacuously — descend through `original_router`. And covering
  one representative verb proved nothing about the six that normalize their
  arguments (2026-08-12, 2026-08-15).
- **Test timeouts are not tuning knobs, and copied waits drift.** Ten copies of
  one settle-loop disagreed about which statuses are terminal and bounded the same
  wait at 2, 4, 5, and 60 seconds; tight bounds invent failures under load while a
  generous one costs nothing. Use `wait_for_task`/`wait_for_task_response` in
  [helpers.py](tests/helpers.py) and the shared `TASK_SETTLE_TIMEOUT`. Grep for
  `time.monotonic() + <number>` when a test fails only under load. **Still open:**
  `test_staged_command_client.py` hardcodes `timeout_seconds=2` in nine places
  (three instances, latest 2026-08-15).
- **Wait on the condition you assert, not the signal before it.** Two watcher
  tests waited for the delivery callback to be invoked and then asserted
  `notified`, which is written after it returns. Put the asserted predicate inside
  `_wait_until` (2026-08-12).
- **A literal date is a time bomb.** `teamEnrollment.test.mjs` pinned an
  `expires_at` and asserted "Available"; on that date the ledger correctly read
  "Expired" and the suite went red on the calendar rather than on a code change.
  Derive fixtures from `Date.now()` (2026-08-19).
- **Every test builds a fresh SQLite file, so a green suite says nothing about
  migration.** Relaxing a constraint in the create path is not a migration —
  `CREATE TABLE IF NOT EXISTS` never alters an existing table, and `ON CONFLICT …
  DO NOTHING` does not help because NOT NULL is checked first. New columns
  declared in the create block *and* indexed in the same `executescript` ran
  before `_ensure_column` and crashed every start, with 785 tests passing over a
  store that could not open one real file. `connection.executescript` also issues
  an implicit COMMIT, destroying the surrounding `BEGIN IMMEDIATE`. Verify by
  opening a copy of the real store and comparing `PRAGMA table_info`
  (2026-08-07, 2026-08-15).
- **Sign the bytes, verify the same bytes.** The broker HMAC'd the request as
  written but recomputed it from `model_dump()` of the validated model, and
  `status_in` is sorted during validation — so the orchestrator's only non-polling
  wait was refused unless the agent wrote its statuses alphabetically. Any
  normalizer reopens this; canonicalize the raw request text (2026-08-13).
- **What a provider CLI accepts is read from that CLI.** A hand-typed reasoning
  list offered `minimal` (rejected by every current Codex model) and omitted
  `max`/`ultra`. `codex exec resume` accepts neither `--sandbox` nor `--cd`, so a
  Work Resume carries its profile through `--config` — check the subcommand's own
  `--help` rather than assuming a flag carries over (2026-07-30).
- **`blocked-external` is a claim with an expiry date.** Two scenarios sat blocked
  on "needs a reachable SSH host" while one was configured and up the whole time.
  Run the check before repeating a blocker (2026-07-30).
- **A desktop launch must be verified cold**, with nothing on 8421. A warm backend
  answers instantly and hides every startup ordering bug — the window once aimed
  at the backend origin before it existed and stayed blank forever, while every
  warm relaunch looked perfect. "The bundle built and the process is running" is
  not "the window appeared": read the startup milestones on stderr or
  `lsappinfo front` (2026-08-07, 2026-08-12).
- **Name the exact path a turn must edit.** A revision contract that named a fresh
  per-turn artifact directory and omitted the view file made the provider redraw a
  657 KB page instead of editing it. The agent followed what it was given, so this
  is an orchestration error, never a native-session limit (2026-08-12, S114).
- **A reused graph-run stage still holds the prior attempt's `patch.json`.**
  Fingerprint it before every correction launch and refuse an unchanged file, or a
  provider that wrote nothing appears to succeed. Hand the unchanged-file
  diagnostic forward; revalidating the same bytes overwrites it.

## Maintaining this file

Treat `AGENTS.md` as evolving, not fixed — but write to it under this structure,
and write concisely. It grew to twice this length by appending, and the cost was
real: a 458-word entry filed under the wrong number, rules stated three times in
three sections, and forty-eight "repeated failures" of which a third could no
longer occur.

**Decide which section a new line belongs to by asking what happens if it is
violated.**

| Section | Test | Shape |
| --- | --- | --- |
| **Invariants** | Something major breaks, and breaks quietly | State the guarantee, then name what breaks. Name the guarding test if there is one. Invariant `7b` is the model — 75 words. |
| **Design rules** | Nothing fails now, but the code gets worse or an invariant becomes easier to break | State the rule, not the argument for it. |
| **Notes** | Nothing breaks — a working habit, a local fact, or a mistake made twice | One or two sentences. Date a repeated mistake. |

There are three sections and no others. If a line fails all three tests, it does
not belong in this file.

**What belongs elsewhere.** Behavior belongs in [`docs/specs/`](docs/specs/);
interface and visual decisions belong in
[`docs/specs/interface-and-visual-design.md`](docs/specs/interface-and-visual-design.md);
rationale belongs in [`docs/decisions/`](docs/decisions/); unfinished work belongs
in [`docs/handoffs/`](docs/handoffs/). Do not restate any of it here — a rule
stated in two places drifts, and the copy nobody edits is the one that gets read.

**Update it when:**

- a pointer went stale (a path moved, a command changed, a version bumped),
- a rule turned out to be wrong or too rigid in practice,
- the human stated something → file it by the test above,
- you hit the same failure a second time → condense it under Notes, with a date,
- a new module boundary appeared → add it to the fan-out table.

**Rules.** Keep edits small and specific. Delete what is no longer true rather
than appending a caveat. Never hardcode a version, count, or line number that
will drift — point at the file that owns it. Never renumber an invariant; the ids
are cited from source, tests, and scenarios. Mention in your response what you
changed here and why.
