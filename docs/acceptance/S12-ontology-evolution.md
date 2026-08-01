---
id: S12-ontology-evolution
status: implemented
tier: hermetic
driver: pytest + browser
covered_by: tests/test_ontology_evolution.py, tests/test_sync.py, tests/test_proposals.py, web/tests/ontologyEditing.test.mjs, browser 2026-07-30
last_passed: 2026-07-30
invariants: [1, 3]
blueprint: v0.5 §5.6, §5.7
---

# Change the ontology without breaking old work

Implemented and driven on 2026-07-30.

A team's ontology grows as they do research. Work recorded under the old shape
must keep opening, keep meaning what it meant, and keep replaying identically.

---

## UI path — confirmed 2026-07-30

### Where it lives

A new **Ontology** section in Project Settings, alongside Truth scope and Agent
defaults. **Settings is the sole entry point** — there is no ontology editing
from the graph views, a node detail, or a chat. This is configuration, not
research.

The **base ontology is not editable.** It is the projection target every
extension declares a mapping into; making it movable would remove the thing
extensions are defined against.

### What you can do

- **Add a node type.** Name, plain-language definition, and its fields.
- **Add a field** to a type, existing or new. Name, kind, optional or required,
  and whether an agent may write it or only a human.
- **Add a relation.** Name, which types may be its source, which may be its
  target, and which layer it belongs to — epistemic or action.
- **Declare the mapping.** A new type says how it projects onto the base six
  (§5.7). Without that, the project stops being transferable, so this is
  required rather than optional.

### Who can change it

**Both paths.** A human authors directly in Settings. An agent may also *propose*
an extension — it can notice the graph wants a type the ontology lacks — which
lands in the judgment queue as an ordinary proposal for a human to approve. That
keeps invariant 3 intact: the agent proposes, the human decides. There is no
path by which an agent's proposal takes effect on its own.

### Deprecation and removal

- **Deprecating a type or field** stops it appearing in pickers; existing nodes
  keep rendering.
- **A deprecated type can then be removed outright**, from Settings, as a
  deliberate human action.

Removal is safe for replay, and the reason matters: the ontology is itself
materialized from the log, so validating the patch at revision N uses the
ontology **as of revision N**. A type defined at revision 5, used at revision 6,
and removed at revision 20 still replays — revision 6 is checked against an
ontology that still has it. Removal only ever affects what you can author next.

This is the same log-is-authoritative principle as everything else, and it is
what makes the whole promise work rather than a special case.

### What is still refused

The editor may not express a change that would make the log fail to replay:

- **Making an optional field required**, retroactively — every node authored
  before it would fail. Allowed only for nodes created after the change.
- **Narrowing a relation's allowed types** below what the graph already
  contains.

### How it commits — and the one real constraint

Like every other human authority action: staged in the project draft, committed
on **Sync**, appended to the log as an operation. Not a code constant, not a
config file.

An ontology change rides in a normal Sync batch. `Patch.ops` is an open list
validated by name against
[`OP_RULES`](../../src/rcp/core/validation/registry.py:43), so this needs a new
entry there and **no envelope change** — `set_project_truth_scope` is the
existing precedent for a config-shaped op sitting alongside graph ops.

But there is a constraint that falls out of how validation works, and the UI has
to respect it:

> **An ontology change and the first use of what it defines cannot be in the
> same patch.**

[`_validate_operations`](../../src/rcp/core/validation/patch.py:152) runs every
op against `ctx.state` — the state as it was **before** the patch, not updated
incrementally. So a batch that defines type X *and* creates a node of type X
fails: the `create_nodes` validator never sees X.

So Sync must either split such a batch into two patches, ontology first, or
refuse it with that explanation. Silently failing the whole Sync is the outcome
to avoid, and it is the default if nobody handles this.

---

## Drive

1. Open Project Settings → Ontology. Add a node type, a field on an existing
   type, and a relation. Sync.
2. Reopen the project. The old graph renders.
3. Compare it against the recorded `graph.json`.
4. Add a node of the new type. Sync. Reopen.
5. Open a node created under the old schema and edit it.
6. Try to make an optional field required retroactively.
7. Deprecate the new type, then remove it from Settings. Reopen.
8. In one draft, define a second new type **and** create a node of it. Sync.
9. Confirm there is no ontology control anywhere outside Settings.
10. Have an agent propose an extension; approve it from the judgment queue.

## Assert — pytest

- `old_project_opens_under_new_schema`
- `replay_is_identical` — every field of every node and edge matches the
  recorded `graph.json`. Not "no errors" — identical.
- `no_patch_rejected_by_the_new_schema` — tightening must not retroactively
  invalidate history
- `ontology_change_is_in_the_log` — an operation, not a code constant; the log
  stays the single answer to "what does this mean"
- `validation_uses_the_ontology_as_of_that_revision` — the property the whole
  promise rests on
- `removed_type_still_replays` — old patches using it validate against the
  ontology they were written under
- `base_ontology_mapping_declared`
- `base_ontology_not_writable` — no operation can change it
- `narrowing_change_refused`
- `agent_proposal_requires_human_approval` — an agent-proposed extension has no
  effect until approved

## Assert — browser

- `ontology_editor_reachable_from_settings`
- `no_ontology_control_outside_settings` — not in graph views, node details, or
  chat
- `new_type_appears_in_node_creation`
- `old_node_still_editable`
- `deprecated_type_hidden_from_pickers_but_still_renders`
- `deprecated_type_removable_from_settings`
- `define_and_use_in_one_batch_is_handled` — split into two patches or refused
  with the reason, never a silent Sync failure
- `refused_change_explains_why` — naming the nodes that block it, not a generic
  error
- `agent_proposed_extension_appears_in_judgment_queue`

## Failure means

Someone's year of recorded research became unreadable because the schema moved.
This is the failure that ends trust in the format permanently, and it stays
invisible until it happens to real data.
