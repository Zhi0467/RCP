# ML pack

Status: design. Implementation runs in this PR's branch after the design is
reviewed.

## Problem

RCP's ontology and official skills are domain-general. An agent running ML
experiments gets no method for the things that most often make an ML result
wrong: unequal tuning between arms, one seed, leaked test data, runs cut short,
missing provenance. Researchers keep this knowledge by hand today, in repository
instruction files and long handoff documents.

A live check on 2026-10-14 showed the tracker side needs no new machinery: a
Codex Work turn, asked in plain words, wrapped three TensorBoard-only training
runs with an exporter, built a live dashboard from the exported files, and the
member kept and pinned it. Its one flaw was operational: each run rewrote one
growing JSON file that the page read whole, which would pass the live-file read
caps the contract already states on any long run.

## What already exists

- Official packages live in `src/rcp/skills/` and are listed in
  `_OFFICIAL_PACKAGE_SPECS` (`src/rcp/skill_registry.py`). Every official skill
  is enabled by default; `tests/test_skill_registry.py` holds that rule.
  Launches receive compact pointers and read bodies on demand.
- `evidence-triage` already owns observation versus interpretation, field
  choice, claim-relative assessments, and citation checks. The new rigor skill
  builds on it and does not restate it.
- `live-pages` owns live HTML sources. The artifact contract renders the read
  caps from `limits.py` (`src/rcp/agents/artifact_contract.py`).
- `graph-consolidation` is the one nightly Work turn on main. Its charter is
  "Tidy; do not research", and it already writes a morning report.

## Design

1. **`ml-run-preflight`** (new skill). Before a launch that is longer than a
   smoke run: write a provenance record next to the run's outputs (commit and a
   hash of any uncommitted diff, config, model and dataset revisions as the code
   reports them, seeds, hardware, environment lock), run a reduced smoke that
   proves metrics are being written, and point the attempt's `configuration` at
   the run folder. For live dashboards: keep metrics append-only and read them
   from the tail, or maintain a small downsampled summary file, so a long run
   never outgrows the read caps the contract lists.
2. **`ml-result-rigor`** (new skill). One general checklist of traps that make
   a result look better or worse than it is, each with the check that rules it
   out, plus how a comparison becomes Evidence: arms, seeds per arm, spread, the
   shared eval set, and the tuning budget each arm received. A single-seed or
   unequal-budget comparison may still be Evidence, but its edge weight and
   qualifications say so. It applies Evidence triage by name; the v1 registry
   gives skills no dependencies.
3. **`live-pages`** (version bump). One short section on growing sources: append
   and tail, or downsample; never one ever-growing file read whole. No numbers;
   the contract owns them.
4. **`graph-consolidation`** (version bump). A literature pass after the audit:
   search for recent work bearing on the open ResearchQuestions and Hypotheses.
   Prefer editing, tidying, and flagging existing nodes (a qualification on an
   existing Evidence edge, a Suggestion, or a Proposal when a paper undermines a
   protected belief). New `external_publication` Evidence may be added, linked
   with assessed edges. Every literature change and new paper is listed in the
   report under its own heading. The charter line changes from "do not
   research" to "do not run experiments". Its dependencies stay as they are:
   the pass judges papers with Evidence triage, and a general workflow takes no
   ML-only dependency.

Nothing here adds an ontology field, a setting, a dependency, or graph
authority. Skills supply method only.

## Settled decisions

- **One general trap checklist**, not one per research area (human,
  2026-10-14).
- **No tracker connectors.** Agents write exporters; the pack teaches the
  file pattern only.
- **Literature writes report and graph**, preferring edits and flags to new
  nodes (human, 2026-10-14).
- **On by default**, like every official skill.

## Sources

A 2026-10-14 survey read the main open skill packs (K-Dense scientific skills,
Orchestra AI-research skills, NVIDIA, Hugging Face, Anthropic) and the research
loops of autoresearch, AI Scientist, and AIDE. None ships a general ML rigor
method; the closest content is K-Dense's critique method and NVIDIA's run
preflight. The pack is original prose grounded in the rigor literature (NeurIPS
checklist; Dodge et al. 2019; Agarwal et al. 2021; Bouthillier et al. 2021;
Henderson et al. 2018; Lipton and Steinhardt 2018; recent contamination work).
No text is copied, so no third-party notices are needed.

Borrowed ideas: a smoke run validates plumbing and never tests the hypothesis;
search-time acceptance (validation-driven keep or discard) is candidate
generation, not confirmation; a negative contamination check is not evidence
of a clean benchmark; tuning budget is part of a comparison's identity. Avoided:
fixed seed minima, numeric rigor scores, autonomous loop or publishing
instructions.

## Slices

- **A.** `ml-run-preflight` and `ml-result-rigor`, each with a short
  references file.
- **B.** `live-pages` growing-sources section and the `graph-consolidation`
  literature pass, with version bumps.
- **Integration (Claude).** Registry entries, version pins in tests,
  and the spec line in `providers-and-containment.md` that names the optional
  packages.

## Checks

- Registry: the two new skills resolve, stage, and appear in default skill ids;
  bumped versions match (`tests/test_skill_registry.py`).
- No wording tests. Prose is reviewed directly.
- Live: one real Codex Work turn on throwaway data with a TensorBoard toy repo,
  asked for three runs and a live board, writes a provenance record, smoke-runs
  first, and keeps its metrics file within the caps.
