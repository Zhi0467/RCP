# Documentation model and archive cleanup implementation handoff

Date: 2026-08-17
Status: confirmed and ready to implement last

## Purpose

Replace the current single-blueprint plus ever-growing acceptance-file model with a small active documentation surface:

- one central design document;
- full current specifications by module;
- a small set of active decision records;
- active implementation handoffs only; and
- a deliberately small active acceptance suite.

Most implemented or superseded handoffs, decisions, and acceptance scenarios should be moved intact into `docs/archive/`. Do not compress thousands of lines of old scenario prose into module specifications. Git moves preserve the record; the active tree should contain only material needed to understand or implement the current product.

Implement this handoff after the code and behavior in the other handoffs are complete so the new current specifications describe the final system once.

## Authority and directory model

Adopt this current layout:

```text
docs/
  design.md
  specs/
  decisions/
  handoffs/
  acceptance/
  open-questions.md
  archive/
    design/
    decisions/
    handoffs/
    acceptance/
```

### `docs/design.md`

This is the concise central design authority. Keep it readable as one document. It should contain:

- product purpose and boundary;
- the core graph/history, authority, execution, episode, and projection invariants that cut across modules;
- the documentation precedence rules;
- current terminology;
- a module index with a short description and link to each full specification; and
- only genuinely cross-cutting open constraints.

It is not a restatement of every module, acceptance scenario, implementation seam, test, or historical decision.

### `docs/specs/`

These are the full current normative specifications, one coherent module per file. Migrate the still-current content from the blueprint and `docs/design/*.md` into this directory, removing duplicate authority.

Choose module boundaries that follow the product and code, not one file per old handoff. At minimum, the current specifications must clearly cover:

- graph schema, claim-relative Evidence assessments, append-only history, typed operations, transition preparation, replay, and branches;
- human/agent authority and Proposals;
- providers, task stages, project write containment, and remote execution;
- conversations, Work, Experiment loops, watchers, and episodes;
- Auto-research and agent-native branch merge;
- project identity, spaces, membership, and server operations;
- API/project projections and desktop/Web behavior; and
- papers, artifacts, result views, or other current product modules that remain implemented.

A specification states current behavior and invariants. It may link to tests and active acceptance scenarios, but it does not absorb old scenario narratives or implementation chronology.

### `docs/decisions/`

Decision records explain a still-relevant choice and its consequences. They do not override `design.md` or a current specification.

Keep a decision active only when its rationale is still materially useful for an ongoing migration, a live tradeoff, or an easy-to-regress architectural boundary. Once the decision is fully incorporated into current design/specification and no longer needed for active implementation, move it intact to `docs/archive/decisions/`.

Use a small consistent header containing date, status, decision, and the current specification that incorporates it. Superseded decisions are archived, not kept beside current authority.

### `docs/handoffs/`

This directory contains only confirmed work that is not yet implemented or verified.

A handoff is moved to `docs/archive/handoffs/` when it is:

- implemented and verified;
- superseded by a later handoff;
- abandoned; or
- no longer actionable.

Do not leave completed handoffs active as informal design modules. The current specification is the durable description after implementation.

### `docs/acceptance/`

This directory contains only active product-level acceptance contracts. Keep scenarios when they are one of:

- pending or not yet verified;
- a core cross-module end-to-end user journey;
- a durable authority, append-only-history, recovery, remote, or data-loss boundary;
- a browser/desktop interaction whose human path adds information that unit/API tests cannot; or
- a live external integration contract that still requires an explicit drive.

Move a scenario intact to `docs/archive/acceptance/` when it is implemented and primarily:

- a minor regression;
- a unit/module-local behavior;
- an API shape already exhaustively represented by tests and a current specification;
- an implementation detail rather than a user promise;
- a moderately obvious consequence of a stronger retained scenario; or
- redundant with another active cross-module scenario.

Do not merge archived scenario text into a specification. Do not renumber or reuse scenario ids. Active and archived ids remain globally unique.

End the rule that every reported bug or substantial module change automatically creates a new scenario. The new rule is:

> Create or retain an acceptance scenario only when the work introduces or reveals a durable product-level promise that is not already covered by an active scenario. Otherwise add regression tests and update the relevant current specification only when semantics changed.

A major new cross-module journey, such as Auto-research branch isolation and agent-native merge, still merits one scenario. Typed schema refactors and individual transition rules do not.

### Acceptance indexes

Keep `docs/acceptance/README.md` short: policy, how to run active tiers, and an index of active scenarios only. Generate the index from frontmatter if a small existing/new script can do so reliably; do not maintain another hundred-row table by hand.

`docs/archive/acceptance/README.md` may contain a generated archive index or simple instructions for searching by scenario id. The archive must not pollute the active index.

### `docs/open-questions.md`

Keep only unresolved questions that cut across modules or whose ownership is not yet clear. Put a module-local unresolved question in the corresponding specification. Remove questions that the implemented handoffs settle; archive historical discussion when useful rather than leaving a decided question open.

## Precedence and conflict handling

Write this hierarchy into `AGENTS.md` and `docs/design.md`:

1. `docs/design.md` owns repository-wide product boundaries and cross-cutting invariants.
2. The applicable file in `docs/specs/` owns current module behavior.
3. Active acceptance scenarios state selected observable promises and must agree with current design/specifications.
4. Active decision records explain rationale but do not override current design/specifications.
5. Active handoffs authorize and scope implementation; they may refine an explicitly open implementation detail but may not silently change current design.
6. `docs/archive/` is historical and non-authoritative.

A contradiction between current sources is a documentation defect. Do not silently choose one or treat the newest timestamp as authority.

## Blueprint migration

The current `docs/research-control-panel-blueprint.md` must stop being duplicate live authority.

- Extract its current normative content into `docs/design.md` and `docs/specs/`.
- Reconcile it with the implemented behavior from the other handoffs, including orchestrator Decision authority, provider-native project containment, graph branches, typed operations, claim-relative Evidence assessments, and the transition manager.
- Move the old blueprint intact or as a clearly labelled pre-modular snapshot to `docs/archive/design/` after current content is represented.
- Update links so no active file instructs agents to edit or version-bump the old blueprint.

Do not preserve two competing current specifications for convenience.

## `AGENTS.md` changes

Rewrite only the documentation/acceptance workflow rules necessary for the new model. Preserve unrelated engineering and product instructions.

Required changes:

- replace “single canonical blueprint” with the new design/specification hierarchy;
- remove the prohibition on archived design snapshots where it conflicts with the confirmed archive model;
- replace mandatory acceptance-first-for-every-bug language with the durable-product-promise criterion;
- remove “every bug becomes a permanent scenario” and equivalent wording;
- state that a handoff explicitly marked human-confirmed and ready to implement does not require the coding agent to reopen the design interview;
- require active handoffs to be archived when their implementation closes;
- make relevant tests and any applicable active acceptance scenario the completion proof; and
- direct agents to ignore archived documents as current authority unless researching history.

Do not broadly rewrite the rest of `AGENTS.md` or remove hard-earned operational guidance unrelated to documentation.

## Archive pass

Perform one deliberate inventory, then use `git mv` so history is preserved.

### Handoffs

Archive every implemented/superseded handoff currently in the active directory, including the old graph-transition design checkpoint and all handoffs implemented by this dispatch. Leave only genuinely unimplemented work active.

### Decisions/design modules

Move superseded or fully absorbed design/decision files into the appropriate archive directory. Current module specifications replace them; do not leave aliases that look normative.

### Acceptance scenarios

Classify every existing scenario using the criteria above. The expected outcome is that most implemented single-module and regression scenarios move to the archive, while a much smaller set of pending and important cross-module journeys remains active.

Do not rewrite scenario bodies merely to justify classification. Produce a concise implementation report listing retained ids and archived id ranges/counts with the applied criterion; do not add that report as another permanent active design file unless it is needed for review.

Preserve current uncommitted edits in acceptance files before moving them. An archive move must include the person's latest content, not the version at `HEAD`.

## Link and tooling cleanup

Update links in current source files, README files, tests, and scripts. Archived files may retain historical links when rewriting them would falsify their context, but current navigation must not lead to missing paths.

Add or update a lightweight documentation check that proves:

- every current design/spec/decision/handoff/acceptance link resolves;
- no scenario id is duplicated across active and archive;
- active handoff frontmatter/status identifies actionable work;
- the active acceptance index matches active files; and
- current files do not name the archived blueprint as canonical authority.

Do not build a documentation application or database.

## Non-goals

Do not:

- combine all old scenarios into one large specification;
- summarize every archived handoff into `design.md`;
- keep implemented files active merely because they contain useful history;
- delete historical documents instead of archiving them;
- renumber acceptance scenarios;
- require a new scenario for every bug, test, or module change;
- make decision records higher authority than current specs; or
- rewrite unrelated engineering rules in `AGENTS.md`.

## Verification

The documentation pass is complete when:

1. `docs/design.md` is concise, current, and points to every full module specification.
2. `docs/specs/` contains the complete current normative design with no live duplicate blueprint.
3. `docs/handoffs/` contains only unimplemented confirmed work.
4. Most old implemented/minor acceptance scenarios are under `docs/archive/acceptance/`, unchanged except necessary archival metadata/links.
5. Active acceptance README/index is short and contains only active scenarios.
6. `AGENTS.md` no longer mandates a permanent scenario for every bug and no longer treats the old blueprint as sole authority.
7. Current links resolve, ids remain unique, and documentation checks pass.
8. The implemented behavior from all preceding handoffs is accurately reflected.
9. No unrelated uncommitted user/concurrent change was lost.

## Completion

This handoff is implemented last. After its own verification, archive it too. The final active documentation surface should explain the current system without requiring readers to traverse archived handoffs or one hundred implemented regression scenarios.
