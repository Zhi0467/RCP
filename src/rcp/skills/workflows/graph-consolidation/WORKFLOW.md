---
id: graph-consolidation
kind: workflow
label: Graph consolidation
version: 1.6.1
description: Consolidate a project's main research graph and operational lessons while nobody is watching, apply the result inside the turn, and leave one short visual report of every change for the human to read in the morning.
dependencies:
- graph-audit@3.4.0
- experiment-causality@1.4.0
- evidence-triage@3.4.0
---

# Graph consolidation

The graph is this project's memory. Your job tonight is to make it easier to
read and act on, to check its Evidence against its sources and correct what
they plainly show, and to check recent literature against its open claims,
without changing what the project believes on your own authority. Tidy and check sources; do not run experiments. A human
reads your report tomorrow and can undo anything you did with a later Patch, so
every change must be one they would make themselves after a careful read.

## Working in parallel

When your provider offers native subagents, use them for work that splits into
independent pieces, and wait for all of them before the step that needs their
results. The audit can split by ResearchQuestion, the fact-check by batches of
Evidence, and the literature search by question. Keep the sequential steps
yourself: combining findings, the shared Patch, `apply`, the re-read, and the
report.

## Pass 1: audit

Apply Graph audit to main, reading `research.md` before `graph.json`. Use
Experiment causality and Evidence triage only on the nodes the audit flags.
Keep the findings as working notes; they feed passes 2, 3, 4, and 6.

## Pass 2: fact-check and literature

Check Evidence created or changed on main since the covered head supplied for
this run; if there is no covered head yet, check all current Evidence. Read
the cited sources themselves: artifact refs, producing Experiment job logs, and
repository files on the execution host, and papers, web pages, or datasets
through your web and network tools. Conversation transcripts and earlier agent
summaries are pointers, not substitutes for reading sources.

Correct what you are sure of; flag only what needs a human. When you read the
full source, and the relevant code, logs, or results where they apply, and the
fix is unambiguous, correct the Evidence directly in your Patch: a wrong number,
a result the source reports that the Evidence omits, or a claim broader than the
source states. For example, if a paper's main results are A, B, and C and the
Evidence states only A, add B and C. Keep every qualification the source
supports and cite it. Correcting accepted Evidence returns it to `asserted`,
which puts it back in front of the human; that is expected. Fix prose problems
and missing or wrong citations the same way.

Flag what you cannot settle yourself: you read only an abstract or a summary,
the source is ambiguous, or the fix would change what the Evidence means for a
question or Hypothesis in a way a careful reader could dispute. The same
literature Evidence you would correct after reading the full paper and its code
is a flag when all you have is its abstract. Each flag costs the human a check,
so raise at most five a night, counting Pass 4's check; when more qualify, flag
the ones most likely to mislead a reader and list the rest under Suggestions.
Use the command client's keyed `flag`:

- `overclaim`: the Evidence claims more than the source supports;
- `underclaim`: the Evidence omits or understates a result the source supports;
- `no_source`: you read the sources you could find and none contains the claim;
- `unverified`: a source is named but you could not read it, for example a
  paywalled paper, a dead link, or an unreachable host.

Give a one-line finding and, when available, the source: an absolute
execution-host path or a repository alias followed by its relative path, with
an optional line number, or an `http(s)` URL. Do not edit the flagged claim,
validity, or standing to resolve the finding, including in later passes. A
human reviews it from Inbox; only a human checks an `unverified` source. Do
not treat inability to read a source as proof the claim is false; state what
you could not verify in the finding and use `unverified`, not `no_source`.

Then use the turn's web search to find recent work bearing on open ResearchQuestions
and Hypotheses. For each search, record the query, date, each source and its version (preprint or published),
and whether you read full text or only an abstract. Mark unread hits as unread;
search snippets alone do not establish Evidence. Deduplicate preprint and
published versions, including papers already in the graph. Finding nothing
new is a valid result; report it. If search or source access fails, report that
gap rather than claiming there was nothing new.

Prefer improving existing records. An edge's assessment cannot be edited in
place, and replacing an edge to an existing ResearchQuestion or Hypothesis is a
protected change. So when a paper limits what an existing Evidence edge shows,
raise one protected-relation Proposal that replaces the edge with the added
qualification. Otherwise tidy within the rules below, or leave a Suggestion in
the report. When a paper
undermines a protected belief, propose the needed change rather than editing
the ResearchQuestion or Hypothesis directly. The authority rules in Pass 3
and the accepted-node rule in Pass 4 still apply.

Apply Evidence triage to each literature change. You may add Evidence with
`origin: external_publication`, linked with assessed edges where required by
the graph rules. Cite the paper and keep the observation within what you read.
An abstract-only observation may state what the abstract reports; it cannot
claim you checked the full methods or results. Carry that limit into the
edge's qualifications. RCP holds every Evidence node you add for human review.
Keep these changes for the shared Patch in Pass 4.

## Pass 3: consolidate

Fix only what the audit proved, smallest change first:

- merge nodes that split one identity, keeping the clearer title and every
  relation;
- supersede Evidence or Experiments that a later node fully replaces, and say
  which in the content;
- rewrite a title or content that misleads a first-time reader, without
  changing its claim, scope, or qualifications;
- refresh a stale `current_summary` or `next_action` from what the graph now
  shows;
- add a missing relation only when the graph already states its reason;
- add Evidence for a result that a source you read states and the graph lacks,
  citing that source. RCP holds every Evidence node you add for human review.

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
only when the audit or a paper you read shows a reader would otherwise be
misled. Prefer one clear Proposal to many small ones. When in doubt, leave the
graph alone and put the idea under Suggestions in the report.

## Pass 4: readability and glossary

Read the graph as a newcomer would, against the "Writing for a reader" rules
in your graph rules.

- Rewrite the title and prose of nodes that fall short, keeping every claim,
  scope, qualification, and relation. Rewrite only `asserted` nodes, except
  Experiments: editing an accepted node returns it to `asserted`, so list other
  accepted nodes that need it under Suggestions instead. ResearchQuestions and Hypotheses are protected
  beliefs: carry their rewrites in a Proposal, never a direct edit.
- Read every Experiment as someone deciding whether to run it, and check it
  against the Experiment rules in your graph rules. Check every `asserted`
  Experiment's prose closely. Rewrite an accepted one only when a reader could
  not tell what it runs: the rewrite returns it to `asserted` and costs the
  human another review, so leave small flaws in accepted Experiments alone.
  Rewrite its objective and
  design as the operational plan when they are compressed, narrate the record's
  own history, or use internal names they never define: what runs, under which
  conditions, the run matrix and run count, and what is logged. Recover those
  facts from the attempt ledger, job logs, configs, and repository; never
  invent a level or a count. Refresh a `current_summary` that contradicts the
  status or the attempts. When an Experiment mixes two studies, when its fields
  contradict in a way you cannot fix, or when the sources cannot tell you the
  operation, list it under Suggestions with what a human must split, settle, or
  supply.
- Make one concept use the same words across nodes.
- Build the glossary: add an entry for each term that several nodes use
  without defining it and sharpen entries that are vague or out of date. List
  glossary entries for terms the graph no longer uses under Suggestions.

Put the changes from passes 2 through 4 in one Patch. Validate, then `apply`
with a key. Read the returned revision. If validation rejects the Patch, fix it or
drop the offending operation; never broaden the change to make it pass.

Then re-read the graph. A merge, supersession, or new relation can leave an
Experiment's `current_summary_stale` or `next_action_stale` set, and a rewrite
inside the same Patch stays stale. Rewrite each such field from what the graph
now shows and `apply` that as one more validated Patch. When you cannot tell
what is now true, leave the field and list the Experiment under Suggestions.

Last, fact-check every Evidence node your Patches added, exactly as in Pass 2,
correcting what you are sure of in one more validated Patch and flagging the
rest within the same nightly limit.

## Pass 5: lessons

Read `lessons.md`. Merge agent-written lessons that say the same thing, rewrite
unclear ones, and delete ones the graph or later lessons show are wrong or no
longer apply, using `lesson update` and `lesson delete`. Never touch a
human-owned lesson. Add a lesson only for operational know-how you met tonight.

## Pass 6: report

Write one self-contained HTML file named `consolidation-report.html` in the
turn's artifact folder. Give it a `<title>` naming what changed most, such as
"Merged three duplicate caching Hypotheses" or "No consolidation needed".

Lead with one sentence on the state of the graph. Then list every change you
applied, grouped as corrected Evidence, merged, superseded, rewritten, refreshed, readability,
glossary, and relations, each with the node titles before and after and one line of reason.
Then list the nodes you added, Evidence first, every Decision you queued or
reopened, every Proposal you raised and why, and the lesson changes. List the
Evidence flags you raised with each Evidence title, flag kind, and finding.
Under **Literature**, list every literature change and new paper, with its
source, reading depth, and the claim it bears on. Include the search records
and any access gaps; say when the search found nothing new. End with **Suggestions**:
next steps, possible experiments, and audit findings you chose not to act on,
each one line the human can act on. Use a small before-and-after diagram when a
merge or supersession changes the shape of a question's path. Keep it to a few
screens, plain language, short sentences, and no task ids or paths outside one
collapsed `<details>` appendix.

If the audit and literature find nothing worth changing, apply nothing and
write a short report that says so.
