# AGENTS.md

This file contains the canonical instructions for coding agents.
`CLAUDE.md` imports this file.
Find task-specific guidance in the [design spec index](docs/design.md#module-specifications).

## Working loop

1. Inspect affected source, tests, and the applicable spec before changing behavior.
2. Identify file ownership, invariants, and focused checks before substantial edits.
   Make small edits directly. Split larger implementation by module boundary,
   retaining integration, verification, and review.
3. Verify behavior, fix failures caused by the change, and rerun affected checks. Add
   regression coverage for behavior changes. Review prose directly. Test prompt data and
   enforcement, never exact instruction wording.
4. Read the complete diff and inspect check results yourself.
5. Update current behavior docs with semantic changes in the same commit.

## Design phase

Anything beyond a small fix is designed before it is built.

1. Investigate with agents in parallel: source, specs, and prior art. Start from the
   existing sibling and frame the design as "extend X to Y".
2. Ask the human each open decision through the blocking question tool. Explain the
   mechanism first, recommend one option, and ask at most two questions per round.
3. Show every changed UI surface as a rendered mockup in the app's own styling before
   writing UI code. Let the human judge the real layout, not a sketch.
4. Write the design on the feature branch, never in a design-only PR. Review it with
   egm (`design`, `implementation-readiness`, `zoom-out`) and fix Blockers and Majors.
5. Wait for the human's explicit start. Answered questions are not a start.
6. Build in ub-mode: make it work test-first, then make it right with crapper,
   dryer, and mutator scoped to the change.
7. Ship design, code, and docs in one PR. Move the design's rules into their owning
   specs and delete the design file before merge.

Integrate concurrent changes through a real three-way Git merge, never a whole
patch piped into `git apply`. Use short-lived branches, PR CI, and explicit human
merge. Servers consume only merged `main`.

## Verification

Run focused checks locally. Full suites run in PR CI. Broaden checks for failures,
shared-contract changes, or integration uncertainty.

- For Python, run `uv run pytest -n0 <affected test paths>` and
  `uv run ruff check <changed paths>`.
- For Web, run
  `node --experimental-strip-types --test web/tests/<affected>.test.mjs`.
  Run `npm --prefix web run build` when source or types change.
- Run `uv run pre-commit run --files <changed paths>`, including new files.
  All-file hooks see tracked files only.
- Pre-commit runs `basedpyright` against `.basedpyright/baseline.json`. Fix new
  errors. Never use `--writebaseline`. The baseline only shrinks.
- Before a push, read `uv run python scripts/quality_delta.py origin/main`. It never gates.
- Spec'd behavior changes run egm `codebase-details` on that spec. Fix confirmed drift.
- User-visible workflows, reported failures, and substantial route, background, or view
  changes need the affected served-app journey. Inspect network, console, and server
  logs. Report any verification gap.
- Check every UI change at 390px against the
  [phone layout, no-commentary, and smooth-motion rules](docs/specs/interface-and-visual-design.md).
  Avoid unnecessary secondary labels and descriptions; let the layout do the
  explaining. A truly needed note goes on hover. Motion never jumps or flickers.
- Remote behavior needs a reachable host. Migration and recovery checks use copies of
  real records, never the human's live data directory.
- Native changes require a Tauri rebuild and the [desktop checks](docs/desktop.md).
  Frozen-only behavior must be checked in the packaged backend.

Fresh-clone setup, browser dependencies, and full-suite commands live in
[install.md](docs/install.md). Use disposable data for served-app tests.
See [three desktop builds](docs/desktop.md#three-desktop-builds) for build naming
and packaged verification.
Runtime resources belong inside `rcp`. `packaging/rcp_backend.spec` collects and
inventories its source and data.

## Stable invariants

These permanent identifiers are cited from source and tests. Never renumber them
or reuse retired numbers. Module-local promises belong in their owner's docstring,
spec, and executable test, not this registry.

1. **Canonical Patch logs are append-only; materialized graph files are outputs.** Never
   edit or delete main or branch Patch history, and never hand-edit `graph.json`,
   `research.md`, glossary, Proposal, cursor, or scope-base files. Replay rebuilds the
   outputs and would change the past.
3. **Humans retain protected authority.** Only humans approve Proposals, change project
   truth membership, authorize episodes, or dispatch branch merges. A member's own page,
   including an agent the member runs there, counts as that human
   ([why](docs/specs/conversations-episodes-and-watchers.md#native-chat-context)).
   So does the member's ordinary Work chat starting an Experiment they asked for
   ([why](docs/specs/conversations-episodes-and-watchers.md#work-started-experiments)).
   The Auto-research orchestrator is the one explicit exception: on its branch and the
   forks its lineage owns it may choose Decisions, set ordinary belief standing, and
   merge a fork back into its owned source. A human-dispatched merge into main inherits
   that profile.
3b. **Existing ResearchQuestions and Hypotheses are protected beliefs.** Agents may
   create them. Structural or semantic changes to existing ones use a Proposal. Never
   infer protected intent from operation shape.
4. **Agent capability is fixed in code.** Configuration cannot widen it. Discuss has no
   graph authority. Every team launch carries its repositories' Git transport
   ([why](docs/specs/providers-and-containment.md#login-state-and-failure)). Work names
   exact write roots to provider enforcement, which bounds every file-editing tool and
   bounds the shell only where a provider can do so without disabling compute
   ([why](docs/specs/providers-and-containment.md#claude)). Ingestion writes only
   scratch. The paper coach writes only its own changes folder. RCP turns those
   changes into paper change cards. The per-chat browser grant is human consent to a code-defined
   tool ([why](docs/specs/providers-and-containment.md#browser-grants)).
4b. **The task stage is the only graph-change channel.** RCP collects the agent's Patch
   from its exact RCP-owned stage and nowhere else.
   `docs/specs/providers-and-containment.md` names the files each run kind collects.
   Never parse graph authority from answers, traces, artifacts, or repository edits.
6. **One canonical state repository.** Routes never write canonical files directly.
   `StateWorkspace` owns local and remote locking and publication.
6b. **One synchronous transition owns one semantic mutation.** Sync, Apply, branch
   Apply, and merge commit one expanded transition or nothing. Replay does not rerun
   historical rules.
8. **One RCP process owns one data directory.** OS advisory locks, not path existence,
   establish local and remote ownership.
10b. **Only a captured Work turn has conversation graph authority.** Message wording or
   a stray file cannot upgrade Discuss.
10e. **Agent HTML previews cannot act on RCP.** Keep the opaque sandbox, bounded
   artifact discovery, no popups, forms, or downloads, and no implied zero-network claim.
10g. **One episode, one graph target, one validated session and stage, one graceful
   Stop fence.** Never silently fall back to a fresh session or main graph.

Retired numbers stay with their owners. Invariant 2 is folded into 1.
Invariant 5 belongs to the module docstring of `agents/write_scope.py`.
Invariant 7 belongs to the `_atomic_write` and `_atomic_text` helpers.
Invariant 7b belongs to `materialize._fork_state`.
Invariant 9 belongs to `AppStore.complete_agent_task` for Patch text and the
`if applied` stage removal in `stream_graph_run` for scratch.
Invariant 10 belongs to `prepare_agent_patch`. An agent Patch carries no cursor
or coverage operation, so chat never advances ingestion.
Invariant 10f belongs to the watermark comment in `materialize._apply_patch`.
Invariant 10c belongs to `clear_turn_handoff_files`.
Invariant 10d belongs to the native chat context rule in the conversations spec.
Invariant 11 belongs to `ProviderStreamEvent`.

## Cross-cutting implementation rules

- Keep policy with its concrete owner. Do not add `kind`, `surface`, `patch_kind`,
  or equivalent selectors to shared plumbing merely to hide it.
- Permission contracts are code, not configuration. Each launch names its capability.
- Structured deliverables are file-backed. Provider prose is the labelled answer.
  Never duplicate it in a second answer file.
- Limits and timeouts belong in `limits.py`. Schema constants stay beside their model.
- Ship remote-executed code from its source module. Never copy it into command strings.
- Render enforcement prose from the same resolved object as enforcement, not a parallel
  allowlist.
- Resolve command verbs and read and write scopes once and pass them. Prompt sites
  never re-derive them or branch on their fields. Run-family builders own usage text
  and render it from handler verbs and parser option records. Context for each
  launch points to staged files. Scope-dependent filtering belongs to the context
  builder that takes that scope. Bump the policy version when stable contract prose changes.
- Kernel code uses `project_type_of(state)`. Only the research layer names research
  types, classes, and relations. The Python and Web project-type tests enforce a falling
  exception count ([owner](docs/specs/graph-history-and-transitions.md#product-ontology)).
- Persisted changes require old-record replay, equality between stored and restamped
  merges, cache upgrades, agent Patch input, and API and Web checks
  ([owner and check paths](docs/specs/graph-history-and-transitions.md#replay-and-compatibility)).
  Engine-written status, standing, and rule versions can be semantic. Exclude only
  explicitly irrelevant comparison fields and preserve recorded replay values.
  Add an old-writer workflow regression. Current-model fixtures are insufficient.
- Declare external programs in `src/rcp/dependencies.py` with purpose, roles,
  platforms, and tier. Runs refuse when a required program is absent. Optional
  programs name their feature and visible fallback. The feature owns its readiness checks.
  CI checks source and the server install command against the registry.

The backend is one coordinated process. Owners may call engine internals.
Experiment execution may reuse ordinary Work mechanics.
Keep recovery behind generic retryability guards because Experiment preflight
can write diagnostics or settle Stop. Moving recovery into HTTP handlers would
duplicate those guards or cause effects for ineligible tasks.
Keep task-row assembly shared unless a concrete collision justifies splitting it.
Keep `api/app.py` as the composition root for dispatch, ordered startup
reconciliation, and watcher wiring. Do not add registries, controllers, buses,
facades, or new owners without a measured maintenance collision, an independent
lifecycle, or a concrete testing need.

## Documentation lifecycle

- [Design](docs/design.md) owns product boundaries.
  The [invariant registry](#stable-invariants) owns cross-cutting promises.
  Applicable specs own current behavior and its short rationale. Report
  contradictions instead of silently choosing a source. Typo fixes need no
  design review.
- Operator guides own executable procedures and defer to specs on behavior. Keep rules
  in specs and commands in guides. Link to code types instead of duplicating API field
  lists.
- Keep docs and PR text free of real hostnames, account names, personal or backup paths,
  and private project or repository identities. Use neutral placeholders and check the
  full text before publishing.
- Docs describe current behavior only. Plans, status, unfinished journeys, and remaining
  work belong in GitHub issues and pull requests. Git history is the record.
- State each fact once and link to its owner. Delete stale instructions. Keep brief
  rationale beside the rule it protects.

## Conventions and local facts

- Python uses `uv`, `pyproject.toml`, Pydantic, and
  `from __future__ import annotations`. Read Ruff settings in `pyproject.toml`.
- `.research/`, `.recovery/`, and `web/dist/` remain outside formatting hooks.
- Never trust a piped test command's exit status unless `pipefail` is set.
- Use shared test wait helpers rather than copied polling loops. Derive expiry dates
  from the test clock.

## Maintaining this file

Keep cross-cutting rules and pointers only. There is no minimum length, and the
test ceiling is 230 lines. Move module-specific behavior and procedures to their
owners. Consolidate before adding rules. A new global invariant must name its
concrete code owner and executable test. Preserve existing ids.
