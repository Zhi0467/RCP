# Graph fields and rules render from the model

Date: 2026-09-23
Status: design confirmed by the human 2026-09-23; not yet implemented. Three
slices land sequentially on one pull request, none optional. The rationale is in
[graph rules render from the model](../decisions/2026-09-23-graph-rules-render-from-the-model.md);
current behavior after the change is in
[graph, history, and transitions](../specs/graph-history-and-transitions.md#product-ontology)
and [graph rules in task contracts](../specs/providers-and-containment.md#graph-rules-in-task-contracts).

Close this handoff when all three slices are merged and the checks below pass.

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
`core/research_md.py` if it renders Experiment design fields, `web/src/types.ts`,
`web/src/nodeEditing.ts`, `web/src/nodePresentation.ts`, the edge editor in
`web/src/components/GraphEditingControls.tsx`.

- Add `ExperimentProxy`, `Experiment.proxies`, `Experiment.limitations`,
  `Edge.expectation`, and their agent-schema counterparts. Old graphs load with
  empty defaults; no migration revision.
- `HUMAN_EDITABLE_NODE_FIELDS` gains both Experiment fields. The episode
  allowlist is already an allowlist and does not change. The episode receipt
  snapshots both fields beside `expected_outcomes`.
- Validation accepts `expectation` only on `produces`, and on human and agent
  edges alike.
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
- The chat `contract_key` includes the digest.
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
- Served-app journey on a throwaway data directory: edit an Experiment's proxies
  and limitations, add a `produces` edge with `expectation`, and read both back.
