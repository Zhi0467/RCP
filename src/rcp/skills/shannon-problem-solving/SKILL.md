---
id: shannon-problem-solving
kind: skill
label: Shannon problem-solving
version: 1.0.0
description: Reshape a hard, stalled, or open-ended problem with Shannon's six strategies before attacking it.
dependencies:
---

# Shannon problem-solving

Shannon's 1952 Bell Labs talk "Creative Thinking" argued that good problem-solvers share a trait he called **constructive dissatisfaction**: a mild, productive itch that the current approach is not as clean as it could be. The six strategies below are what he did with that itch. This skill turns them into a short diagnostic loop: read the situation, pick one or two strategies, apply them, and only then answer.

The point is not to recite the six. It is to change the problem before attacking it.

## Workflow

1. **Name the situation** in one line. Which of these is it?
2. **Pick 1–2 strategies** from the routing table. Never all six; the discipline is choosing.
3. **Apply them to the problem itself** — actually simplify it, actually restate it — not just describe doing so.
4. **Write the notes**: at most two paragraphs covering which strategies you chose, why, and what they changed about how you see the problem.
5. **Give the normal answer** below the notes, in whatever form the task calls for.
6. **If the answer is still unsatisfying** (or the user comes back stuck), run another round with *different* strategies. Up to three rounds total. If three rounds haven't moved things, say so and recommend what information or experiment would.

## Routing: situation → strategy

| Situation | Reach for | Because |
|---|---|---|
| "How do we even start?" / blank page / topic too big | **Simplification**, then **Similar problems** | Find the smallest version you can actually solve; find who solved something like it |
| "Our approach isn't working" | **Restatement**, then **Inversion** | The framing may be the bug; working backward from the goal exposes which step is missing |
| "We can't tell whether it's working" | **Structural decomposition**, then **Simplification** | Split into pieces that each have a checkable outcome; build a minimal case where success is unambiguous |
| "We have an answer but it feels narrow or lucky" | **Generalization**, then **Restatement** | Test whether the trick is a principle; a second framing shows whether it survives |
| "Too many moving parts" / can't hold it in your head | **Structural decomposition**, then **Simplification** | Build the ladder of intermediate steps; drop variables until it fits |
| Writing or storyboarding (paper, talk, doc) | **Inversion**, then **Structural decomposition** | Start from the one sentence the reader must leave with, then work backward to the sections that force it |
| Explaining something that isn't landing | **Restatement**, then **Similar problems** | Change representation (words → picture → example); find the analogy the listener already owns |
| Search / literature review returning noise | **Restatement**, then **Generalization** | Rename the thing the way the field names it; search for the broader class, not your specific instance |

When the situation doesn't match a row, pick by asking: *is the problem unclear (Restatement, Simplification), too large (Decomposition), unfamiliar (Similar problems), stuck at the last step (Inversion), or solved-but-fragile (Generalization)?*

## The six strategies

### 1. Simplification
Strip away every detail that isn't essential until the problem is almost trivial, even if it no longer resembles the original. Solve that. Then add constraints back one at a time, watching for the one that makes it hard — that is the real problem.

Moves: set every parameter to 1, 0, or ∞; drop to one variable, one user, one data point, one sentence; ask "what is the toy version of this?"

Wrong pick if: the problem is already minimal and the difficulty is in the detail you'd be removing.

### 2. Similar problems
Don't jump from your problem P straight to a solution. Find a solved problem P′ that rhymes with it, note how P maps onto P′, and carry P′'s solution across the same mapping. Two small steps instead of one big one.

Moves: ask "what field has already dealt with this shape of problem?"; look for the textbook case, the classic paper, the standard architecture; name the mapping explicitly so you can see where it breaks.

Wrong pick if: you can't articulate the mapping — then you're borrowing vocabulary, not a solution.

### 3. Restatement
Say the problem differently: change the vocabulary, the representation, or the viewpoint. Algebra to geometry, prose to diagram, system view to user view, "how do I X" to "what stops X from happening on its own."

Moves: rewrite the problem in three unrelated ways; explain it to an imagined outsider; swap the subject and the object; state it as a question the field would recognize.

Wrong pick if: every restatement collapses back to the same sentence — the problem is well-posed and the block is elsewhere.

### 4. Structural decomposition
When the jump from start to goal is too big, don't jump. Insert intermediate waypoints A → I₁ → I₂ → … → B, each small enough to solve or verify on its own. The global path becomes a sequence of local, checkable steps.

Moves: write the chain of claims that would have to be true; find the step you can't yet justify; give every piece its own success criterion so "is it working?" has an answer per piece.

Wrong pick if: the pieces only make sense together and can't be checked separately — then simplify first.

### 5. Inversion
Assume the goal is already achieved and work backward. What must have been true just before? And before that? Backward search often has far fewer branches than forward search, and it exposes the precondition you've been skipping.

Moves: write the final sentence of the paper first; write the passing test before the code; state the ideal result and ask what it would take; ask "if this were already solved, what would the solution obviously have to contain?"

Wrong pick if: the goal itself is undefined — restate first.

### 6. Generalization
Once something works, push it as far as it goes. Is it a one-off trick or an instance of a broader principle? Finding the widest range where it holds tells you why it works and shows the edge where it stops.

Moves: relax one assumption and see if the result survives; ask what the result says about the class of problems, not just this one; look for the boundary case that breaks it.

Wrong pick if: nothing has been solved yet — this strategy needs a foothold.

## Output format

ALWAYS use this shape:

```
**Notes** — one or two short paragraphs:
Situation read, strategies chosen and why, what they changed. Plain prose, no bullet-per-strategy.

[Normal answer, in whatever form the task needs]
```

Keep the notes genuinely short. They are a window into the reasoning, not a second answer. If a strategy was tried and didn't help, one sentence saying so is useful; a paragraph defending it is not.

## Things to avoid

- Applying all six, or listing them as a checklist. The value is in the choice.
- Describing a strategy instead of doing it ("one could simplify this by…" with no simplified version shown).
- Repeating a strategy in a later round. Each round should bring a new angle.
- Letting the notes swallow the answer. Notes ≤ 2 paragraphs; the answer gets the space.

## Worked examples

See `references/examples.md` for short before/after examples across research, debugging, writing, communication, and literature search. Read it when the routing table doesn't obviously fit the task at hand, or when calibrating how long the notes should be.
