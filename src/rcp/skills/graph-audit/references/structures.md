# Common graph structures and how they break

Projects differ, so none of these shapes is required. Use them to read what a node is trying to
be, then check whether its connections deliver that. A missing link is a finding only when the
node's own content says it belongs in one of these structures.

## Question decomposition

```text
ResearchQuestion -has_subquestion-> ResearchQuestion
```

A broad question splits into parts that can be pursued separately. Hypotheses and Decisions hang
off the narrowest question they serve.

Flag:
- a sub-question that restates its parent instead of narrowing it;
- Hypotheses attached to the broad parent when a sub-question already names their narrower claim;
- a sub-question whose answer would not help answer the parent.

## A claim under test

```text
ResearchQuestion -has_hypothesis-> Hypothesis
Experiment -tests-> Hypothesis
Experiment -produces-> Evidence
Evidence -supports|weakens|refutes|inconclusive-> Hypothesis
```

The loop closes when the Evidence an Experiment produced bears back on the Hypothesis it tested.

Flag:
- a Hypothesis with no question it serves;
- a Hypothesis that only answers its question yes or no;
- an Experiment that `tests` a Hypothesis but whose design cannot tell the outcomes apart;
- produced Evidence with no edge back to the tested Hypothesis, even when the result settled nothing
  (`inconclusive` is the honest edge then);
- a completed Experiment that tests a Hypothesis but produced no Evidence;
- a Hypothesis `status` change that no Evidence edge explains.

## Rival answers

```text
ResearchQuestion -has_hypothesis-> Hypothesis A
ResearchQuestion -has_hypothesis-> Hypothesis B
Hypothesis A -contradicts-> Hypothesis B
Experiment -tests-> Hypothesis A, Hypothesis B
```

Two claims compete for one question, and one Experiment can discriminate between them.

Flag:
- rivals that are not marked `contradicts` although both cannot hold;
- a discriminating Experiment that tests only one rival;
- Evidence that bears on both rivals but connects to only one, or carries one assessment copied to
  both.

## One observation, several claims

```text
Evidence -supports-> Hypothesis A
Evidence -inconclusive-> Hypothesis B
```

One observation often bears on more than the Hypothesis its Experiment targeted, each with its own
assessment.

Flag:
- a relation to a Hypothesis the Evidence plainly bears on but does not connect to;
- identical assessments on claims the observation bears on differently.

## A choice gated by a measurement

```text
ResearchQuestion -has_decision-> Decision
main Experiment -governed_by-> Decision
precursor Experiment -produces-> Evidence -informs-> Decision
```

A Decision is an input to the main Experiment, and a precursor measurement informs it.

Flag:
- a Decision with neither a question it belongs to nor an Experiment it governs;
- the Decision governing the precursor that exists to inform it (reversed);
- Evidence that bears on a choice but connects only to its producing Experiment.

## A blocked path

```text
Experiment|Decision|ResearchQuestion -blocked_by-> Blocker
Blocker -requires_decision-> Decision            (a choice clears it)
precursor Experiment -produces-> Evidence -addresses-> Blocker   (an observation clears it)
```

Flag:
- a Blocker that blocks nothing;
- a Blocker whose `resolution_condition` is a choice but which has no `requires_decision` edge, or is
  an observation but which no Evidence `addresses` once that Evidence exists;
- an Experiment blocked by the very condition its own Evidence is meant to address.

## External and analytic support

```text
Evidence (external_publication | external_instance | analytic) -supports|weakens|...-> Hypothesis
```

This Evidence has no producing Experiment and needs none.

Flag:
- external Evidence given an invented Experiment or conversation source;
- external Evidence connected to nothing.

## Replacement

```text
new node -supersedes-> old node
duplicate -duplicate_of-> canonical
```

Flag:
- a superseded node whose replacement lost its connections, such as a new Hypothesis with no question
  or a new Experiment that no longer tests anything;
- two live nodes for one entity that should use `duplicate_of`.
