# Graph rules render from the model

Confirmed by the human 2026-09-23. This record exists because the old shape,
hand-written prompt blocks beside the code they describe, is the easy thing to
drift back to.

## What changed

Agents learn what the research graph means from one place: descriptions that
live in code beside the fields and relations they describe. Every node field
carries a `description`, and every base relation carries one in
`RELATION_SPEC`. One renderer turns them, plus the relation endpoints, id
prefixes, and value vocabularies the code already holds, into the graph rules
every graph-reading or graph-writing contract includes. The same descriptions
fill the agent Patch JSON Schema.

This retires five hand-written blocks: the base authoring rules, the local
causal check and its "retained" copy, the graph reading rules, and the
auto-research node glossary. What stays hand-written is only method that spans
several nodes: the causal check and a few cross-cutting write habits.

## Why one source

The audit that preceded this found about twenty fields no prompt defined,
agents inferring meaning from field names, and the same rule restated in up to
seven places. It also found code facts retyped as prose: the relation endpoint
list, id prefixes, node and relation counts, and enum values. A new relation or
field changed the validator and silently left the prompt stale. The repository
rule that prompt prose must render the same object enforcement uses was not
being followed for the graph itself.

A description says three things and no more: what the field means, when it is
written (before any result exists, or after one does), and any method local to
that field, such as "`Hypothesis.scope` comes only from a cited excerpt".

## Why authority stays out of the descriptions

Who may write a field differs by profile. An ordinary agent never writes
`Decision.selected_option`, the Auto-research orchestrator may, and an episode
may not edit an Experiment's design at all. That policy already has concrete
owners: `core/authority.py`, the episode allowlist, and the human-editable
field registry. A "who writes this" line in each description would be a second,
hand-kept copy of that enforcement, the failure this change removes. Each call
site keeps its own authority block beside the shared graph rules.

The test for a sentence is whether it would change between call sites. If it
would, it is authority and belongs to the call site.

## Why continuations no longer repeat the rules

The rules carry a version digest. They first shipped repeated on every
continuation so a long session would not lose them. They now live in the
session master, and a continuation points to it instead
([decision](2026-09-27-continuations-point-to-their-master.md)). The digest is
part of every master key, so a release that changes the rules gives an existing
session a replacement master without a hand-bumped version number.

## What a later reviewer might restore

- **A per-contract copy of the field meanings.** Each surface wanting its own
  wording was how the drift began. Surface-specific policy goes in that
  surface's authority block.
- **A "who may write" column.** See above. Generating one from the enforcement
  sets is possible later; hand-writing one is not.
- **"Supersedes earlier instructions" on every continuation.** It is true only
  when the master key changed, and then the continuation says so.
