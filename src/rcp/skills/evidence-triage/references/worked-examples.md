# Worked examples

Five Evidence nodes from a live continual-learning project, with the triage reasoning behind each
field. Identifiers, hosts, paths, and dataset names have been abstracted; the epistemic shape is
unchanged.

The project studies whether measurable properties of a policy during training predict how well it
can still learn a *new* task afterwards. It runs a pipeline of jobs on a cluster, measures a fixed
panel of probe tasks at checkpoints, and compares consolidation strategies.

---

## 1. A completed measurement that settles a choice

> **Title** Specialist curves froze the training-budget and teacher vector
> **observation** Three jobs completed the original 50-update specialist protocol, and an extension
> job carried one domain to 100 updates; that domain's pass-at-1 at step 70 was 0.461, step 70
> captured 93.7% of the 0-to-100 gain, and the mean over steps 70, 80, 90, and 100 equaled step 70.
> **interpretation** The observed plateau justifies a 70-update budget for that domain, while the
> other two keep the one-epoch denominator contract; these three identities are now both retention
> denominators and teacher sources.
> **strength** confirmatory  **validity** valid  **origin** internal_run
> **source_refs** (a) the analysis record reporting the 93.7% figure; (b) a record of the human
> confirming the budget after reviewing the curves
> **artifact_refs** the project state document, the study design document, two configs

**Why `confirmatory`.** The runs completed, the plateau was checked two ways (fraction of gain
captured, and the mean over later steps equaling step 70), and the claim being settled is narrow:
this budget for this domain. Confirmatory does not mean "important" — it means the result closes the
specific question it is attached to.

**Why two source refs of different kinds.** The number comes from the analysis record. The *decision
to freeze the budget* comes from the human. These are on different rungs of the ladder and are doing
different jobs: rung 2 supports the measurement, rung 3 supports the framing. Collapsing them —
citing only the human, or only the analysis — would lose either the provenance of the number or the
authority for the choice.

**What would have been wrong.** Writing the interpretation as "70 updates is the right budget" with
no mention of the other two domains. The observation covers one domain at 100 updates; the other two
were never extended. The interpretation names that asymmetry instead of smoothing it.

---

## 2. Apparatus evidence that could have been read as a finding

> **Title** The smoke gate validated machinery and probe identities
> **observation** The repaired 24-job small-model graph produced 30 stream rows, 21 checkpoint
> records, 140 probe rows, 14 probe summaries, and 10 analysis rows; neither held-out domain
> triggered transfer contamination, the entropy feature's pooled R-squared was 0.781, one optional
> component stayed off, and no fallback path was invoked.
> **interpretation** The smoke establishes runtime and measurement validity decisions, but its
> six-update curves are machinery evidence and do not establish the main scientific mechanism
> effects.
> **strength** supporting  **validity** qualified  **origin** internal_run

**The trap.** This node contains a real number attached to the project's central mechanism — a
pooled R-squared of 0.781 for the entropy feature. Read quickly, that is a headline result: the
mechanism predicts. It is not. The runs were six updates long, on a small model, in a pass whose
purpose was to prove the pipeline runs end to end.

**Why `qualified` rather than `valid`.** The measurement is real; its reach is bounded by the
configuration it ran under. That is exactly what `qualified` is for. `valid` here would have been
technically defensible and practically misleading.

**Why `supporting` rather than `confirmatory`.** It confirms things about the apparatus — probe
identities are clean, no contamination, no fallback triggered — and those are genuine findings. But
the node is attached to hypotheses about the mechanism, and for those it is not close to
confirmatory.

**The sentence that saved it.** "Its curves are machinery evidence and do not establish the main
scientific mechanism effects." An interpretation that says what a result does *not* license is worth
more than one that says what it does, because the reader would have supplied the optimistic reading
themselves.

---

## 3. A snapshot, not a result

> **Title** The first reference probe was healthy near completion
> **observation** At the last observed record, the job was running without traceback,
> out-of-memory, file-descriptor, or router failure at step 117 of 120; peak host memory was 238 GiB
> of 320 and peak GPU memory was within budget, while the fixed fit and eval curves had reached the
> values recorded in the experiment node.
> **interpretation** This is preliminary runtime and baseline-curve evidence only; the final
> fixed-panel evaluation, committed checkpoint, cleanup, and the remaining three runs are still
> required before normalization is complete.
> **strength** preliminary  **validity** qualified  **origin** internal_run

**"At the last observed record."** This phrase is the whole node. The job was at step 117 of 120
*when the record was written* — not when the Evidence was authored, and certainly not now. A
collector dump, a status line, a monitoring message: each is an observation at its own timestamp,
never live state. Write the timestamp qualifier into the observation itself, because the node will
be read months later by someone who has no idea when it was written.

**Why not wait for the run to finish.** Because the graph is a record of what is known, and "the
reference run reached step 117 healthy" is genuinely known. The honest move is to record it at
`preliminary` with the remaining work named, not to omit it and not to round it up to a result.

**What `preliminary` buys you.** When the run finishes, this node gets superseded or updated rather
than contradicted. A node that claimed completion would have to be corrected, and corrections are
where graphs lose their credibility.

---

## 4. A citation that does not carry the claim

> **Title** The metric and archive transaction gate completed
> **observation** A job completed the repaired large-model canonical metric and archive path with a
> specific peak VRAM figure and a bounded host-memory ceiling, receipt-verified payloads, local
> receipt stubs, and no terminal failure state.
> **source_refs** one record, excerpt: "The verified launch summary states the sequential boundary
> contract, the fixed-panel probe contract, the route-level contract, and the pending seed-0
> launch."
> **artifact_refs** the project state document, the integration document

**The defect.** The observation is made of specific quantities: peak VRAM, host-memory ceiling,
verified payloads, absence of a failure state. The excerpt contains none of them. It is a summary of
what a *different* document asserted, from the conversation where this was discussed.

The claim is probably true — the numbers most likely came from the state document in
`artifact_refs`. But a reader who wants to check where "peak VRAM" came from follows the source ref
and lands on a sentence about contracts and a pending launch.

**The fix.** Either cite the record where the memory figures actually appear, or keep the artifact
refs as the support and write an interpretation that says the quantities come from the state
document. Provenance you cannot follow is not provenance.

---

## 5. The reuse tell

> **Title** Stream pools and probe identities were frozen
> **observation** The final data freeze selected one procedurally generated domain, one hard-band
> code domain, and one structured function-calling domain, with 1,600 train rows, 200 never-trained
> eval rows, and 32 anchors per domain; the two held-out probe identities were fixed.
> **source_refs** one record, excerpt: "The verified launch summary states the sequential boundary
> contract, the fixed-panel probe contract, the route-level contract, and the pending seed-0
> launch."

**Read that excerpt again.** It is the same excerpt, from the same record, as example 4 — under a
node about a completely different thing. One is about memory and archive transactions; the other is
about dataset selection and row counts. A single sentence cannot establish both.

**Why it happens.** When you triage a long conversation, one summarizing message often looks like it
covers everything you read, so it gets attached to everything you write. It is the path of least
resistance and it silently voids the citation on every node it touches.

**The check.** Before finishing a batch of Evidence nodes, list the excerpts you used. Any excerpt
appearing under two unrelated observations needs to be replaced on at least one of them — and
usually on both, because the sentence that fits everywhere fits nothing.

---

## 6. What the hypotheses in this project show about scope

Every hypothesis in this graph has an empty `scope`, and that is correct. Consider:

> Greater normalized displacement of the current policy from the untouched initial weights predicts
> lower future held-out plasticity after controlling for reward and update count.

It is obvious what the implied scope is — this model size, these domains, this probe panel, this
number of seeds. Writing that in would feel like diligence. It would be invention: no cited excerpt
states it, and the moment it is in the graph it becomes a boundary the project is held to, including
in the paper introduction.

The correct move when the boundary genuinely matters is an Ambiguity naming what is missing. This
project's ambiguities do exactly that — "what exact permutation, replicated arms, and replicate
checkpoints should be frozen before the main manifest is generated" — and they name the nodes they
block, so the question reaches the human as a decision rather than sitting as a silent assumption
inside a hypothesis.
