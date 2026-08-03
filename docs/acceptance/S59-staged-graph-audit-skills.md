---
id: S59-staged-graph-audit-skills
status: pending
tier: hermetic
driver: pytest + browser
covered_by: none
invariants: [3, 4, 4b, 9, 10, 10b]
reported_by: human, 2026-08-03
---

# An agent audits the graph patch it is about to finish

This scenario is a proposal and is **not yet human-confirmed**. It does not
authorize implementation until the human confirms it. The delivery decisions
come from
[`docs/handoff-ui-fixes-and-graph-skills.md`](../handoff-ui-fixes-and-graph-skills.md);
the behavioral choices below answer the questions that handoff deliberately
left open, but those answers remain proposed rather than settled.

RCP ships independently versioned skill folders as application resources. For
each initial graph-writing launch, RCP stages the applicable folders into that
run's scratch workspace and records their id, version, one-line use case, and
exact path in the immutable task contract. Non-Python skill files are explicit
wheel/package data: a built-wheel test proves the installed package contains the
same folders and can stage them without a source checkout. Skills never enter
`.research`, a project repository, or an ordinary agent session.

The first skill is `graph-scanner`. If Work, Seed, or Refresh writes
`patch.json` during its initial launch and the skill staged successfully, its
prompt contract requires the agent to invoke the scanner before finishing. This
is a known prompt-enforced contract, not an OS or validator boundary: a missing
invocation is visible, but the existing validator alone decides whether RCP may
accept the patch. A staging failure is recorded as unavailable and the advisory
step is omitted rather than blocking the graph-writing launch. Discuss and paper
coaching receive no graph scanner.

## UI path (proposal — confirmation required)

1. Start a Work, Seed, or Refresh task and inspect its contract in Agent tasks.
   The staged skill id and version are visible with the compact pointer, not the
   skill body.
2. The agent writes `patch.json`, invokes the scanner, reads its report, and may
   revise the same file before returning. This consumes no correction round;
   later semantic rejection still has the unchanged bounded correction budget.
   RCP does not request another scan during `work_patch_correction` or a generic
   Seed/Refresh correction relaunch.
3. Open the task receipt. It records the exact staged skill version, whether it
   ran, and, when it ran, one outcome: **clean**, **findings**, or
   **unavailable** with a bounded diagnostic. Missing execution and unavailable
   execution are distinct and visible, but neither turns an otherwise legal
   patch into an RCP validation failure.
4. Inspect a past task to reconstruct the skill version it received.

## Scanner contract (proposal)

- It checks the candidate graph for empty research questions whose apparent
  answers are attached elsewhere, unusually flat sibling piles, likely
  misattachments, near-duplicate nodes, and prose that depends on unexplained
  private jargon or missing context.
- A report outcome is exactly `clean`, `findings`, or `unavailable`. Transport,
  staging, process, malformed-output, timeout, and oversized-report failures are
  recorded by RCP as `unavailable`, never graph findings. A successfully staged
  required invocation that produced no report is recorded separately as missing
  rather than synthesized as any of those outcomes.
- Advice to drop or merge is expressed as a suggested Proposal-shaped change in
  the report; it is never automatically converted into removal, merge, or
  standing operations, and the skill never creates a second graph-change
  channel. This scanner constraint does not revoke S52's existing authority for
  a graph-capable agent to author an independently chosen legal `remove_nodes`
  operation.
- RCP derives protection constraints from canonical append-only history rather
  than asking the scanner to infer authorship. Every accepted node is protected
  as a whole. Every current literal node field last authored by a human patch is
  protected at the exact node-and-field boundary. The scanner input and receipt
  call out those RCP-derived protected fields and their reason without copying
  their full contents into task metadata.
- The scanner may explain structural tension around protected content, but it
  never recommends removing, merging, rewriting, contesting, or relocating an
  accepted node or a protected human-authored literal. It may recommend changes
  to surrounding unprotected structure that preserve those literals exactly.
- The scanner writes only its scratch report. Any graph response is authored in
  the same `patch.json` under the surface's existing authority.
- RCP bounds the report it reads and records. When implemented, that bound lives
  with the other tunables in `src/rcp/limits.py`; an oversized report becomes
  `unavailable` with a bounded diagnostic rather than being truncated into a
  possibly misleading finding.

## Drive (after confirmation and implementation)

1. Build and inspect the wheel, install it without the source checkout, and
   stage the registered skill for one local and one fake remote run.
2. Exercise initial Work, Seed, and Refresh launches with clean, findings,
   unavailable, and missing scanner receipts. Submit validator-valid and
   validator-invalid patches in each advisory state.
3. Exercise a scanner-driven same-launch patch edit, then trigger both Work and
   Seed/Refresh correction relaunches.
4. Scan a graph containing an accepted node, an asserted node with one current
   human-authored literal field, and nearby unprotected agent-authored content.
5. In Agent tasks, inspect the current receipt and a historical task contract.

## Assert

- Skill definitions live as real folders under the RCP package, with a
  `SKILL.md`, version, and locally testable scripts where needed.
- Wheel/package-data configuration explicitly includes every non-Python skill
  file; a built-wheel test fails if any registered folder or file is absent.
- One registry owns id, label, version, when-to-use text, source folder, and
  permitted receiving surfaces. Work, Seed, and Refresh callers each request
  their skills explicitly; no shared staging helper branches on a surface or
  `kind` discriminator.
- Local and remote runs stage byte-identical folders through the existing run
  stage directory transport.
- Contracts carry compact pointers, never embedded skill bodies.
- Every task records the exact staged skill versions, invocation state, and
  advisory outcome; old receipts remain reconstructable after a skill upgrade.
- Ordinary repository sessions cannot discover the staged skills.
- The scanner reports only clean, findings, or unavailable; runtime and
  transport failure cannot masquerade as a graph finding.
- Missing or unavailable scanning is visible but never changes the validator
  verdict or correction budget.
- Work, Seed, and Refresh receive the scanner; Discuss and coach do not.
- Scanner-driven edits use the original `patch.json` and do not spend a
  correction round.
- `work_patch_correction` and generic Seed/Refresh correction relaunches do not
  rerun the scanner.
- Accepted nodes and RCP-derived protected human-authored fields constrain
  advice exactly as declared in the receipt.
- The report bound is a central `src/rcp/limits.py` tunable, not a per-module
  constant.
- Human standing and existing per-surface permissions remain authoritative;
  scanner output never becomes a second patch channel.

## Failure means

Advisory quality becomes an undeclared hard gate, an unavailable scanner is
reported as scientific criticism, protected human wording is rewritten, a
correction relaunch silently scans again, a skill creates a second patch
channel, version history is unreconstructable, packaged installs omit the
skill, or an RCP-only skill leaks into the research repository.
