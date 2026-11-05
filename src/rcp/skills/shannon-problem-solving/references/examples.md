# Worked examples

Each example shows the situation, the strategies chosen, and the kind of thinking paragraph that follows the conclusion. The thinking is deliberately short; the conclusion and the rest of the answer are omitted or sketched.

## 1. Research — "How do we start researching whether LLM agents degrade on long tasks?"

Situation: blank page, topic too big.
Strategies: Simplification → Similar problems.

> The question is too broad to attack directly, so I simplified to the smallest claim that could be tested: does *one* agent on *one* task family get worse as the number of steps grows, holding the task fixed? That already forces the real decisions (which task family, what "worse" means). For the second move I looked for the nearest solved problem: this is the shape of a learning-curve or fatigue study, which gives a ready-made design — fixed task, vary length, measure a per-step error rate rather than end-to-end success.

Answer would then propose that concrete minimal experiment and the two or three bodies of prior work to pull from.

## 2. Debugging / analysis — "Our ranking model's A/B test keeps coming back flat and we don't know why."

Situation: can't tell whether it's working.
Strategies: Structural decomposition → Simplification.

> "Flat" could mean the model isn't different, the users don't notice, or the metric can't see it, and the current test can't distinguish those. So I split the pipeline into three independently checkable claims: (1) the new model produces measurably different rankings on the same queries, (2) users exposed to different rankings behave differently, (3) the metric moves when behavior changes. Then I simplified to the smallest check for each — offline rank overlap for (1), a deliberately bad "control" ranking for (2) and (3). If the bad control also comes back flat, the problem is the measurement, not the model.

## 3. Writing — "Help me storyboard a paper on our new caching scheme."

Situation: writing/storyboarding.
Strategies: Inversion → Structural decomposition.

> I started from the end: the one sentence a reader should be able to repeat after reading the paper. Draft: "Caching at the X layer instead of the Y layer cuts tail latency by N× at no accuracy cost." Working backward, that sentence needs the reader to already believe three things — tail latency matters here, the X layer is where the redundancy lives, and the accuracy cost is really zero. Those three become the sections, and each gets one figure that forces the belief.

Answer: section-by-section outline with the figure that each section exists to deliver.

## 4. Communication — "My explanation of why we need the migration isn't landing with leadership."

Situation: explanation not landing.
Strategies: Restatement → Similar problems.

> The current pitch is stated from the engineering side ("the schema can't support the new features"). Restating it from the listener's side gives "every new feature now costs 3× what it did a year ago, and that multiplier keeps growing." The second move was to find an analogy leadership already owns — this is the same shape as refinancing debt: a fixed cost now to stop a growing cost later — which gives them a familiar decision frame instead of a technical one.

## 5. Literature search — "Searching for 'agent memory consolidation' returns nothing useful."

Situation: search returning noise.
Strategies: Restatement → Generalization.

> The phrase is ours, not the field's. Restated in terms the literature would use, the question is about summarizing or compressing long interaction histories, which lives under "context compression," "long-context retrieval," and "episodic memory for agents." Generalizing one step further, it's an instance of the much older problem of lossy summarization under a fixed budget, which opens up the information-retrieval and summarization literature rather than just the agents literature.

## 6. Second round — when the first didn't move things

Situation: the user came back after example 2 saying the bad-control ranking *also* came back flat.
Strategies (new ones, not repeated): Inversion → Restatement.

> Since decomposition and simplification already showed the metric can't see a deliberately bad ranking, the measurement is the problem. Working backward from "a test that would detect the bad control": it needs enough exposures, enough difference per exposure, and a metric sensitive to that difference. Restating "flat" as "within noise" turned this from a model question into a power-analysis question — the answer below estimates the sample size the current metric would need, and proposes a more sensitive one.

## Calibration notes on length

- Example thinking above runs 80–130 words. That is the target.
- Two paragraphs is the ceiling, not the goal. One paragraph is often enough.
- The thinking may name a strategy once, or leave it implicit; it never defines it.
