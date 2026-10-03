---
id: graph-consolidation
kind: workflow
label: Graph consolidation
version: 1.1.0
description: Consolidate a project's main research graph and operational lessons while nobody is watching, apply the result inside the turn, and leave one short visual report of every change for the human to read in the morning.
dependencies:
- graph-audit@3.4.0
- experiment-causality@1.4.0
- evidence-triage@3.4.0
---

# Graph consolidation

The graph is this project's memory. Your job tonight is to make it easier to
read and act on without changing what it claims. Tidy; do not research. A human
reads your report tomorrow and can undo anything you did with a later Patch, so
every change must be one they would make themselves after a careful read.

## Pass 1: audit

Apply Graph audit to main, reading `research.md` before `graph.json`. Use
Experiment causality and Evidence triage only on the nodes the audit flags.
Keep the findings as working notes; they feed passes 2 and 4.

## Pass 2: consolidate

Fix only what the audit proved, smallest change first:

- merge nodes that split one identity, keeping the clearer title and every
  relation;
- supersede Evidence or Experiments that a later node fully replaces, and say
  which in the content;
- rewrite a title or content that misleads a first-time reader, without
  changing its claim, scope, or qualifications;
- refresh a stale `current_summary` or `next_action` from what the graph now
  shows;
- add or sharpen glossary entries for terms a reader would trip on;
- add a missing relation only when the graph already states its reason.

You may also add what the graph already implies but never wrote down:

- a ResearchQuestion or Hypothesis that several existing nodes plainly serve or
  test. It enters as unreviewed, so the human decides whether it belongs;
- a Decision queued as `ready` for a choice the graph shows is open and
  unrecorded, with at least two options written with equal care;
- a decided Decision moved to `revisit` when evidence recorded since it was
  decided undermines it. Restate the ballot so the card describes the choice
  the human now faces, and say in `rationale` which evidence reopened it;
- a restated ballot on a Decision already queued, only when new evidence adds
  or removes an option.

Never choose a Decision, never create Experiments, and never change standing.
A change to an existing ResearchQuestion or Hypothesis is a Proposal; raise one
only when the audit shows a reader would otherwise be misled, and prefer one
clear Proposal to many small ones. When in doubt, leave the graph alone and
put the idea under Suggestions in the report.

Validate, then `apply` with a key. Read the returned revision. If validation
rejects the Patch, fix it or drop the offending operation; never broaden the
change to make it pass.

## Pass 3: lessons

Read `lessons.md`. Merge agent-written lessons that say the same thing, rewrite
unclear ones, and delete ones the graph or later lessons show are wrong or no
longer apply, using `lesson update` and `lesson delete`. Never touch a
human-owned lesson. Add a lesson only for operational know-how you met tonight.

## Pass 4: report

Write one self-contained HTML file named `consolidation-report.html` in the
turn's artifact folder. Give it a `<title>` naming what changed most, such as
"Merged three duplicate caching Hypotheses" or "No consolidation needed".

Lead with one sentence on the state of the graph. Then list every change you
applied, grouped as merged, superseded, rewritten, refreshed, glossary, and
relations, each with the node titles before and after and one line of reason.
Then list the nodes you added, every Decision you queued or reopened, every
Proposal you raised and why, and the lesson changes. End with **Suggestions**:
next steps, possible experiments, and audit findings you chose not to act on,
each one line the human can act on. Use a small before-and-after diagram when a
merge or supersession changes the shape of a question's path. Keep it to a few
screens, plain language, short sentences, and no task ids or paths outside one
collapsed `<details>` appendix.

If the audit finds nothing worth changing, apply nothing and write a short
report that says so.
