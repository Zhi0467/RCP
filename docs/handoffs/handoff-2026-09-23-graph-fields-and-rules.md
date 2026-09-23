# Graph fields and rules render from the model

Date: 2026-09-23
Status: design confirmed by the human 2026-09-23. Slice 1 is implemented and
verified: focused and full Python suites, web tests and build, and a served-app
journey that edited proxies and limitations, staged a `diverged` `produces`
edge, reloaded before Sync, synced, and read both back. Slices 2 and 3 remain.
Three slices land sequentially on one pull request, none optional. The rationale is in
[graph rules render from the model](../decisions/2026-09-23-graph-rules-render-from-the-model.md);
current behavior after the change is in
[graph, history, and transitions](../specs/graph-history-and-transitions.md#product-ontology)
and [graph rules in task contracts](../specs/providers-and-containment.md#graph-rules-in-task-contracts).

Delete this handoff when all three slices are merged and the checks below pass.

## Design review, 2026-09-23

A read-only Codex xhigh review of this plan found one blocker and three major
gaps. Each is folded into the slices below:

1. **Blocker.** Custom-field collision checks reserve every core field name, and
   they run on replay. A project whose history defines a custom field named
   `proxies` or `limitations` would stop replaying. Replay keeps accepting
   those two names; only new ontology changes are refused.
2. Canonical `core/operations.py` `NewEdge` forbids extra fields, so
   `expectation` must be added there too, not only on `Edge` and the agent
   schema. Human Sync, agent preparation, branch merge, and replay need
   round-trip coverage.
3. Web draft persistence (`humanDraft.ts`) rebuilds edges from an explicit field
   list and has no draft value type for proxy records. The node editor, the
   detail drawer, and the relation map need a proxy editor and readable
   presentation.
4. The chat key is checked in a third place (`runs/chat.py`, retained Patch
   reuse), and the master context embeds both Discuss and Work, so the digest
   must cover every rendering and not depend on the turn's mode. Continuation
   builders and their callers are enumerated below, including ontology-flag
   propagation.
5. Minor: relation layers are derived per edge from endpoint types. The table
   renders the derived layer, keeps `same_type`, and states assessment
   applicability per endpoint pair.

## Version bumps this change needs

- `CHAT_MASTER_CONTEXT_VERSION`, once, in slice 1. From slice 2 the key also
  carries the rules digest, so later rule edits need no manual bump.
- `AGENT_GRAPH_AUTHORITY_POLICY_VERSION` in slice 3 if the authority body text
  changes; its digest already follows the text.
- Each edited skill's `version`, and the workflow's dependency pins, in slice 3.
- No server upgrade fixture: the new fields are optional with defaults, so no
  earlier persisted shape stops being current. The existing boundary fixtures
  are run to prove it.
- No team-shell protocol change: the native entrance does not carry graph
  shapes. No release version bump; that is its own pull request.

## Settled decisions

1. Experiment gains `proxies: list[{stands_for, measure}]` and one
   `limitations: list[str]`. No limitations inside a proxy.
2. The `produces` edge gains `expectation: matched | diverged | no_expectation`.
   It is not an `EvidenceAssessment` and not a field on Evidence.
3. Descriptions live in code: `Field(description=...)` on every agent-visible
   node field in `core/models.py`, and a `description` on every `RelationSpec`.
   A description states meaning, timing, and field-local method only. It never
   states who may write the field.
4. One renderer, `rcp/agents/graph_rules.py`, produces the graph-rules block
   from those descriptions and the code facts. `graph_rules(*, edits: bool,
   ontology_extensions: bool)` is the only entry point. It is a capability flag,
   not a surface selector.
5. Authority blocks stay per call site and unchanged in role. Authority lines
   that leaked into shared rules move back to their owners.
6. The block carries a digest. Continuations repeat it and say "replaces" only
   for a differing digest. The chat contract key includes it.
7. `invocation_ceiling` stays agent-writable on create and gets a description.

## Audit findings and their fixes

| # | Finding | Fix | Slice |
|---|---|---|---|
| A | About twenty node fields have no definition in any prompt | Description on every field | 1 |
| B | No prompt says which fields are written before results exist | Timing in each description | 1 |
| C1 | Authority lines inside shared rules (for example the `set_ontology` ban in the ontology-gap bullet) | Move to the owning authority block | 3 |
| C2 | Experiment-loop authority narrows the generic write rules that follow it | Keep its narrowing sentence; accepted as intended | 3 |
| C3 | `invocation_ceiling` agent-writable and unexplained | Describe it | 1 |
| D | The same rule restated in up to seven places (action-edge semantics, assessment shape, smoke-test rule, `Hypothesis.scope`) | Skills and worked examples keep method only; bump versions and workflow pins | 3 |
| E | Relation endpoints, id prefixes, counts, and enum values typed by hand | Render from `RELATION_SPEC`, `ASSESSMENT_REQUIRED_FOR`, `validation/constants.py`, and model `Literal`s | 1 |
| — | Node fields declared twice (`core/models.py` and `Agent*` in `agents/schema.py`) | Agent JSON Schema takes each description from the core field | 1 |
| F | Reading rules reach only chat contracts | Every graph-reading contract includes `graph_rules(...)` | 1 |
| G1 | Chat re-sends its master context only on a hand-bumped version | Graph-rules digest in `contract_key` | 2 |
| G2 | Human-started graph repair reads only its original contract | Render a current contract for it | 2 |
| G3 | Resume, Retry, and retry handoff send the causal check twice | Delete `_RETAINED_LOCAL_CAUSAL_CHECK` | 1 |
| G4 | Auto-research continuations lack the node glossary | Covered by F | 1 |
| H | Continuations say "supersede" when nothing changed; a stale migration preface is still sent | Repeat plainly; "replaces" only on a differing digest; delete the preface | 2 |

## Slice 1: model, descriptions, one renderer

Files: `core/models.py`, `agents/schema.py`, new `agents/graph_rules.py`,
`agents/prompts.py`, `agents/auto_research_prompt.py`,
`agents/experiment_loop_prompt.py`, `agents/branch_merge_prompt.py`,
`core/validation/ops.py`, `runs/experiment_loop.py` (episode receipt),
`core/operations.py`, `core/ontology.py`, `core/research_md.py`,
`history/delta.py`, and the web files listed below.

- Add `ExperimentProxy`, `Experiment.proxies`, `Experiment.limitations`,
  `Edge.expectation`, and their agent-schema counterparts. Old graphs load with
  empty defaults; no migration revision.
- `HUMAN_EDITABLE_NODE_FIELDS` gains both Experiment fields. The episode
  allowlist is already an allowlist and does not change. The episode receipt
  snapshots both fields beside `expected_outcomes`.
- Validation accepts `expectation` only on `produces`, and on human and agent
  edges alike. It is added to `Edge`, canonical `NewEdge`, and agent `NewEdge`,
  and omitted from serialized historical operations that lack it.
- Ontology collision checks accept historical custom fields named `proxies` or
  `limitations` on replay and refuse them for new ontology changes.
- Web: `types.ts`, `humanDraft.ts` (edge fields and a proxy draft value),
  `nodeEditing.ts` and `DetailDrawer.tsx` (a proxy-pair editor),
  `nodePresentation.ts` and `RelationMap.tsx` (readable proxies and edge
  expectation), and the edge editor in `GraphEditingControls.tsx`.
- `graph_rules()` renders, per node type, each field with its description;
  the relation table with endpoints, layer, description, and whether an
  assessment is required; id prefixes; the ontology extension rules when
  requested; with `edits=True`, the cross-cutting write habits and the causal
  check.
- Replace every `_authoring_rules(...)` call and `_GRAPH_READING_RULES` with
  `graph_rules(...)`. Add it to graph-reading contracts that lack it (Seed/Refresh
  already writes; the loop, Auto-research, and branch merge already write).
  Delete `_BASE_AUTHORING_RULES`, `_LOCAL_CAUSAL_CHECK`,
  `_RETAINED_LOCAL_CAUSAL_CHECK`, `_GRAPH_READING_RULES`, and `_NODE_ONTOLOGY`.
  Hand-written method that survives moves into `graph_rules.py`.
- Bump `CHAT_MASTER_CONTEXT_VERSION` once for this slice's content change.

## Slice 2: continuations repeat, and say when rules changed

Files: `agents/graph_rules.py`, `agents/prompts.py` (`continuation_task_contract`,
`retry_handoff_task_contract`), `agents/experiment_loop_prompt.py` corrections,
`runs/tasks/discuss.py`, `runs/tasks/work.py` (contract key and manual repair),
`runs/chat.py` if the key is built there.

- The block's header names its version digest.
- Continuation wording: this attempt's paths, outputs, and authority apply now;
  the graph rules below repeat the session's rules and replace them only if the
  digest differs. Remove the stale migration preface.
- Corrections that carry a Patch repeat the block rather than only the causal
  check.
- The chat `contract_key` includes one digest over every graph-rules
  rendering, independent of the turn's mode. `discuss.py`, `work.py`, and the
  retained-Patch check in `chat.py` build it from one function.
- Continuation builders that carry a Patch and repeat the block:
  `continuation_task_contract` (resume, retry, both Patch corrections),
  `retry_handoff_task_contract`, `branch_merge_correction_contract`, the
  Experiment-loop Patch correction, and the child Work wake in
  `runs/tasks/auto_research_child_work.py`. Their callers pass the ontology
  extension flag (`runs/tasks/graph.py` retry handoff and correction included).
- Human-started graph repair renders a current contract.

## Slice 3: authority cleanup and skills

Files: `agents/graph_rules.py`, the authority blocks in `core/authority.py`,
`auto_research_prompt.py`, and `experiment_loop_prompt.py`, the four skills
under `skills/`, their `references/worked-examples.md`, and
`skills/workflows/research-graph-audit/WORKFLOW.md`.

- Move each authority sentence out of shared rules into the owners that need
  it. A sentence that differs between call sites is authority.
- Skills drop restated definitions and relation rules and keep method. Bump each
  changed skill's version and the workflow's dependency pins.
- Worked examples show `proxies`, `limitations`, and a `produces` edge with
  `expectation`.

## Invariants that must hold

- 1, 2: no history rewrite; old graphs replay and load unchanged.
- 3, 3b: no widened authority. Agents still cannot write `selected_option` in
  ordinary profiles or edit existing Hypotheses directly.
- 4: capability stays in code; the `edits` flag only changes rendered text.
- 10d: chat transcripts are still not task input; only the master context is
  re-sent.
- Prompt prose describing enforcement renders from the enforcing object.

## Checks

- `uv run pytest -n0` over the touched prompt, schema, validation, chat, and
  experiment-loop tests; `uv run ruff check` on changed paths.
- Tests check data, not wording: every agent-visible node field and every base
  relation has a description; every graph contract includes the rendered
  block; the rendered relation table matches `RELATION_SPEC`; a changed digest
  changes the chat contract key; `expectation` is refused off `produces`; an old
  graph without the new fields loads and replays.
- Web: the affected `web/tests/*.test.mjs` and `npm --prefix web run build`.
- The server upgrade fixture tests pass unchanged.
- Served-app journey on a throwaway data directory: edit an Experiment's proxies
  and limitations, add a `produces` edge with `expectation`, reload before Sync,
  then Sync, and read both back on the node and the edge.
