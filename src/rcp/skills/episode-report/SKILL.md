---
id: episode-report
kind: skill
label: Episode report
version: 1.5.0
description: Create the required durable visual HTML wrap-up for an RCP episode, explaining its work, evidence, limits, ending, and next human decision without changing project or graph state.
dependencies:
---

# Episode report

Produce one self-contained, valid HTML report at the exact output path in the
episode wrap-up instruction. This is a retrospective for a researcher taking
over from the episode, not another operational research turn.

Give the HTML document a concise `<title>` naming the research subject and its
main finding or unresolved outcome. RCP uses this title on the artifact card,
so it must make sense without opening the report. For example, “Compaction
fidelity passes retrieval checks” or “Reset–stream comparison awaits complete
measurements.” Keep internal identifiers out of the title.

Write it as scientific communication, not an operations log. Start with the
research outcome in plain language: what question was pursued, what was found,
what evidence supports it, and what the human needs to decide next. A reader
should understand that opening in under a minute without knowing the task ids,
internal protocol, or preceding conversation. Use concrete node titles and
research terms, and explain an unfamiliar term when it first matters.

## Page skeleton

RCP shows every report as a long-form article: a collapsible contents rail on
the left built from your section headings, one focused reading column in the
centre, and side notes in the right margin. RCP supplies that layout, the
typography, figure numbering, and the phone layout. Write the structure below
and nothing that positions the page: no page-level layout, column, font, or
colour CSS, and no contents list of your own. Style only inside a figure.
Without that article, RCP shows the page exactly as written, with none of the reader layout.

```html
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Subject and main finding</title></head>
<body>
<article class="report">
  <header>
    <h1>Same title as the page title</h1>
    <p class="dek">One sentence: the finding, or why there is none yet.</p>
    <p class="meta">Experiment loop, 14 turns, ended when the budget ran out</p>
  </header>
  <section class="summary">
    <h2>Summary</h2>
    <ul><li>Three to five plain-language takeaways, the decision last.</li></ul>
  </section>
  <section>
    <h2>A heading that states the finding</h2>
    <p>The argument, in short paragraphs.</p>
    <aside>A definition, caveat, or number that would interrupt the sentence.</aside>
    <figure>
      <svg viewBox="0 0 640 280" role="img" aria-label="What the chart shows">…</svg>
      <figcaption>What to see in the figure and how it was measured.</figcaption>
    </figure>
  </section>
  <details class="appendix"><summary>Operational details</summary>…</details>
</article>
</body>
</html>
```

- Order the sections as question, what was done, one section per main
  finding, limits, and the next decision. Each `<h2>` is a short sentence a
  reader could skim in the contents rail; use `<h3>` only for real subparts.
- Put an `<aside>` right after the paragraph it annotates. Use side notes for
  what a careful reader wants but the argument does not need: definitions,
  configuration values, caveats, provenance, and pointers to other nodes. Keep
  each one under about forty words.
- Give each `<figure>` a `<figcaption>` that tells the reader what to see. RCP
  numbers figures, so do not write "Figure 1". Add `class="wide"` to a figure
  that needs the margin's width too.
- Keep paragraphs short and lead with the claim. Use bold sparingly, for the
  one phrase a skimmer must not miss.

Keep it short. The main body is the question, the findings with their evidence,
the limits, and the next decision; a reader should get through it in a few
screens. Leave out what does not change the reader's understanding: commands,
job ids, retries, paths, and step-by-step chronology. Put any operational detail
the human needs to check the work in the one collapsed `details.appendix` at the
end.

Make the report visual first. Give each main finding a figure, such as a chart
of the measured values, a before-and-after comparison, a diagram of how the
Evidence bears on the Hypotheses and Decisions, or a timeline of attempts when
their order matters. A figure replaces prose and tables rather than decorating
them. Draw figures with inline SVG or HTML and CSS, using `currentColor` or the
`--rcp-*` colour variables with a fallback so they read in light and dark
pages; keep every one honest about missing evidence and uncertainty. The HTML
must remain useful in RCP's opaque sandbox without network resources.

State why the episode ended and distinguish observations from interpretation.
If it exhausted its operational ceiling, failed, or paused for human authority,
make the partial boundary conspicuous and never imply unfinished work happened.
A continuation episode, one a human started by adding turns to an ended episode,
gets its own report: cover only this episode's turns and name the episode it
continued when the receipt says so.
Use only the compact immutable episode receipt supplied for this continuation
and the native session's existing context. Do not seek or rebuild graph,
research, transcript, or repository context during wrap-up.

## Experiment-loop guide

For an `experiment_loop` episode, emphasize the objective, method and relevant
configuration, what was measured and what it stood for, scientifically
meaningful attempts, observations, evidence, failure analysis, limitations, and
the exact completion or human-authority pause.
End with the next falsifying test or human decision that the evidence supports.

## Auto-research guide

For an `auto_research` episode, also explain epistemic movement across the
research graph, Decisions made or awaiting authority, delegated-agent and worker
orchestration, what progressed or failed, unresolved uncertainty, and a concise
briefing that lets the researcher resume control without reconstructing the
episode chronology.

Show the meaningful before/after research changes when the supplied context
supports them, with evidence and unresolved questions beside each conclusion.
Distinguish results recorded on the episode branch from changes already merged
to main. Use a small task/dependency diagram only when known relationships help
explain the result; do not invent an execution trace from a list of task ids.

## Authority boundary

The report is descriptive only. It has no Patch, watcher, command, Proposal, or
graph-authority output. Do not write or modify any file except the exact report
output. The current graph and Patch history remain the sources of project truth.
