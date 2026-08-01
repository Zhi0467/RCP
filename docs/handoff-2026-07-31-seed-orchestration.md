# Handoff — seed/refresh orchestration recovery (S33)

**Date:** 2026-07-31
**State:** design settled and confirmed by the human. **No code has been
written.** `docs/acceptance/S33-a-seed-corrects-itself.md` is the specification;
this file is the implementation brief for it.

Read `AGENTS.md` first, then S33, then this file. Where this file and S33
disagree, S33 wins — it is the promise, this is only how to get there.

---

## 1. What went wrong, concretely

A real seed run on a large project failed across two attempts. Both failures
were RCP's, not the provider's.

**Attempt 3.** Codex assembled an 89-session context, exited once without a
patch, then on the first correction round produced a substantial patch (33
nodes, 40 edges). RCP rejected all 83 coverage keys as unknown. A second
correction round was cut off by a server restart.

**Attempt 4.** Resume reattached the Codex session and its stage, but RCP
silently assembled a *different* 92-session context behind it. Codex fixed the
83 coverage keys; RCP then rejected three asserted scientific blockers that
should have been Proposals, and terminated. The human was asked to press Retry
for something the agent could have corrected in-session — and a same-provider
Retry at that point would have resumed the session **without telling it what was
rejected**, so it could have resubmitted the same patch.

Net effect: two expensive attempts, ~6.7M input-token accounting across 61 model
steps in the native session, a 96KB patch stranded, zero nodes in the graph, and
a rejected patch occupying canonical revision 1.

---

## 2. Root cause

Four distinct defects, but three of them share one design flaw: the
orchestration conflates three separate questions into the single boolean
`AgentTaskExecution.resumed`.

1. **What to reuse** — native session, stage, prepared context (*resource*)
2. **Why we are continuing** — interrupt, provider error, invalid patch, graph
   rejection, quota (*cause*)
3. **Who decides the next step** — RCP or the human (*authority*)

Cause is currently inferred from resource state. `resume()` and `retry()` both
call `_create_and_spawn(..., resumed=True)`
([background.py:128](../src/rcp/background.py:128),
[background.py:187](../src/rcp/background.py:187)), so downstream nothing can
tell an interruption from a rejection. That is why attempt 4 reused the session
and was told "Continue the interrupted task."

---

## 3. Decisions already made — do not relitigate

The human settled these. Build to them.

| Decision | Ruling |
|---|---|
| Rejected agent patches in the log | **Validate under the append lock; append only on success.** A rejected agent patch never enters the log. A seed that took three rounds is still revision 1. |
| Sync / human batch semantics | **Unchanged.** `append_batch` and every human path keep today's behavior exactly. The new mode is agent-graph-run only. |
| Resume against a moved graph revision | **Fail visibly and name Retry.** No automatic rebase in this change. Auto-rebase is a later phase. |
| Scope | **All five patches in one pass.** Splitting leaves the orchestration half-converted between the boolean and the continuation cause. |

The existing rejected revision 1 in the human's project **stays on disk**.
Invariant 1 is append-only; do not delete or rewrite it. The fix is
forward-looking only.

---

## 4. The five patches

Land in this order. P0 is a shared contract — serial, no fan-out across it.

### P0 (serial) — continuation cause replaces the `resumed` boolean

Files: `src/rcp/background.py`, and the `AgentTaskExecution` consumers in
`src/rcp/api/app.py`.

- Replace `AgentTaskExecution.resumed: bool`
  ([background.py:29](../src/rcp/background.py:29)) with a continuation cause:
  `"fresh" | "resume" | "correction" | "handoff"`.
- `resume()` ([background.py:114](../src/rcp/background.py:114)) → `resume`.
- `retry()`'s `resume_same_provider` branch
  ([background.py:178](../src/rcp/background.py:178)) → `correction`. It keeps
  reusing the native session and stage; only the *instruction* changes.
- Cross-provider / clean retry → `handoff` (today's behavior).
- Delete the diagnostic-discarding line
  `retry_feedback=() if resumed else self._retry_feedback(record)`
  ([background.py:362](../src/rcp/background.py:362)). Feedback is dropped only
  for `resume`, never for `correction`.

`_session_is_rcp_owned` and the lineage walk stay as they are — they answer the
*resource* question, which is already correct.

### P1 — coverage keys become staged data

Files: `src/rcp/agents/prompts.py`, the staging helpers in `src/rcp/api/app.py`.

The contract currently tells the agent to derive coverage keys from the
projected directory layout
([prompts.py:124-127](../src/rcp/agents/prompts.py:124)). That is unrecoverable:

- canonical key — `<repository>/<machine>/<provider>/<session_id>`
  ([indexer.py:160](../src/rcp/sources/indexer.py:160))
- projected path — `<provider>/<repository>/<machine>/<session_id>.jsonl`, and
  every segment URL-quoted
  ([app.py:3691](../src/rcp/api/app.py:3691) `_session_bundle_relative_path`)

So the path is both reordered *and* lossy. Reversing it is guesswork, and the
agent guessed wrong for all 83 sessions.

- Stage `inputs/authorized-session-keys.json` alongside the conversation
  projection: a list of `{key, path}` built directly from `context.sessions`.
  Use the existing `_stage_json_task_input` so local and remote both work.
- In `graph_task_contract`, replace the derive-from-layout sentence with a
  pointer to that file, and state that coverage keys are exactly its `key`
  values and are never derived from a path.
- The path must be stable across correction rounds and retries, because
  `base_contract_content` is reused verbatim from a prior attempt
  ([app.py:1827](../src/rcp/api/app.py:1827)).
- The chat contract sets no coverage — leave `chat_task_contract` alone.

### P2 — resume uses the saved prepared context, or refuses

File: `src/rcp/api/app.py`, `_stream_graph_run`.

`_retry_lineage` returns `[]` when the execution is resumed
([app.py:1081](../src/rcp/api/app.py:1081)), so `_try_reuse_graph_context`
returns `None` ([app.py:1215](../src/rcp/api/app.py:1215)) and control falls
into `service.assemble_run` ([app.py:1691](../src/rcp/api/app.py:1691)). The
resumed run then rebinds that *fresh* context onto the *old* staged files
([app.py:1769](../src/rcp/api/app.py:1769)). That is the 89-vs-92 split-brain.

The machinery to fix it already exists and is simply not wired up:
`_PreparedGraphContext` carries the full `RunContext` **and**
`previous_coverage` ([app.py:110](../src/rcp/api/app.py:110)), and
`_read_prepared_graph_context` ([app.py:1065](../src/rcp/api/app.py:1065))
reads it from a record's stage. On resume the parent's stage *is* the current
stage, so the file is already there.

- For continuation `resume` and `correction`, load the prepared context and use
  its `context`, `source_snapshot_digest`, `graph_revision`, and
  `previous_coverage`. Never call `assemble_run` on those paths.
- Verify `prepared.graph_revision == service.graph_snapshot()["revision"]`. On
  mismatch, fail with a message that names Retry. No rebase.
- On failure to load, fail the same way. Do not fall through.
- Lift the `if not resuming` guard on `_stage_prepared_graph_context`
  ([app.py:1949](../src/rcp/api/app.py:1949)) so a third attempt can chain off
  the second. **Watch out:** the resumed attempt reuses the same stage, so
  `prepared-context.json` already exists in `inputs/`. Decide overwrite vs.
  skip-if-identical explicitly and record a receipt either way — do not let it
  silently raise the way `_stage_local_graph_conversations` does for an existing
  projection ([app.py:3620](../src/rcp/api/app.py:3620)).

### P3 — a graph rejection is correctable

File: `src/rcp/api/app.py`, `_stream_graph_run`.

[app.py:2111-2116](../src/rcp/api/app.py:2111) lumps `PatchRejected` in with
`ReplayHalted` and `StateUnavailable` and terminates, on the reasoning that a
rejection is a semantic disagreement. It is not: `PatchRejected` carries a
`ValidationReport` of authoring-level messages
([manager.py:62](../src/rcp/history/manager.py:62)) — exactly what the session
still holding the analysis can fix. "These blockers must be Proposals" is a
deterministic authoring correction.

- Route `PatchRejected` into the existing correction ladder at
  [app.py:2131](../src/rcp/api/app.py:2131) by setting `problem` from the
  reject-level messages.
- `ReplayHalted` and `StateUnavailable` remain terminal.
- `_MAX_CORRECTION_ROUNDS = 2` ([app.py:814](../src/rcp/api/app.py:814)) still
  bounds it. An agent that never converges fails with the last refusal as its
  visible error.
- Record the rejection as a receipt on every round; the retained patch text is
  already persisted before validation
  ([app.py:2073](../src/rcp/api/app.py:2073)) and must stay that way.

### P4 — a rejected agent patch consumes no revision

File: `src/rcp/history/manager.py`.

Today `append` writes the patch file *before* the caller sees the rejection —
deliberately, per its docstring
([manager.py:170](../src/rcp/history/manager.py:170)). With P3 looping twice
more, a messy seed would land at revision 3.

Add `discard_on_reject: bool = False` to `append`
([manager.py:161](../src/rcp/history/manager.py:161)). When set and
`report.rejected`, raise `PatchRejected(report)` **before** the write at
[manager.py:239](../src/rcp/history/manager.py:239) — before `_atomic_text`,
before `materialize`, before `publish_committed_patch`.

This is clean because `_next_revision` derives the number from files on disk
([manager.py:475](../src/rcp/history/manager.py:475)): not writing means nothing
to roll back. Keep it inside the same `_append_lock` so the check stays atomic —
a validate-then-append across two lock acquisitions reintroduces exactly the
race that `expected_revision` exists to close.

Call site: [app.py:2107](../src/rcp/api/app.py:2107) switches from
`raise_on_reject=False` plus a manual `report.rejected` check to
`discard_on_reject=True`, catching `PatchRejected` into `problem`.

`append_batch` and every human/Sync path are **untouched**.

### P5 — provider exit evidence

File: `src/rcp/agents/launcher.py`.

Attempt 3's first silent exit cannot be explained after the fact: a zero return
code with no `error` event yields a bare `done`
([launcher.py:343-347](../src/rcp/agents/launcher.py:343)).

Record a diagnostic receipt on the clean-exit path too: return code, event
counts by kind, whether a patch file existed at collection time. Small, but it
is the difference between diagnosing the next silent exit and guessing at it.

---

## 5. Contracts and invariants at risk

- **Invariant 1** — append-only. P4 makes a rejected agent patch never *enter*
  the log; it never edits or deletes one. Existing rejected patches stay.
- **Invariant 8/9** — a failed run keeps its scratch folder and patch text. P2
  and P3 both depend on the stage surviving; do not add cleanup on the
  correction path. Cleanup stays gated on `applied`
  ([app.py:2177](../src/rcp/api/app.py:2177)).
- **Invariant 10** — chat and graph runs stay separate. No shared helper may
  take a `kind` / `is_chat` / `surface` discriminator. The continuation cause is
  *not* such a discriminator — it describes why one run is continuing, not which
  surface it serves — but keep it out of `_stream_chat_run` unless chat needs it
  on its own terms.
- `_parent_task_contract_path` ([app.py:988](../src/rcp/api/app.py:988))
  already refuses a contract path outside the saved stage. Correction-mode
  retry must go through the same check.
- `continuation_task_contract` already supports both `resume` and
  `patch_correction` modes ([prompts.py:307](../src/rcp/agents/prompts.py:307)).
  Correction-mode retry reuses `patch_correction` and adds the retained patch
  pointer; no new mode string is needed.

---

## 6. Fan-out

`AGENTS.md` module boundaries. P0 is serial and lands first — it touches a shared
contract. After that:

| Agent | Files | Patch |
|---|---|---|
| Agent I/O | `src/rcp/agents/prompts.py`, `src/rcp/agents/launcher.py` | P1 contract wording, P5 |
| Service/API | `src/rcp/api/app.py` | P1 staging, P2, P3 |
| History | `src/rcp/history/manager.py` | P4 |

P1 spans two agents (contract text and staging helper) — settle the file name
and JSON shape in P0 so they do not collide.

---

## 7. Definition of done

Baseline is a precondition, not the verification:

```bash
uv run pytest && uv run ruff check src tests
```

Done is **S33 passing** — `docs/acceptance/S33-a-seed-corrects-itself.md`, whose
setup is a scripted three-act provider needing no real provider or quota. Plus
the one assertion amended into S27 under **Assert — lands with S33**:
`same_provider_retry_carries_the_prior_failure_diagnostic`.

S33's browser assertions are real: the inspector must name each launch's
continuation cause, and a correction round must read as progress rather than as
a warning. Serve the app and drive it
(`.claude/launch.json`, name `rcp`, port 8421 — probe
`http://127.0.0.1:8421/api/health` first and reuse a running instance). If the
browser cannot be driven, say so plainly and hand over exact steps; do not
report S33 as passing.

End with the staleness sweep described in `AGENTS.md`, and add the
coverage-key-inference failure to **Repeated failures** — the one-line form is
*an identifier is named by RCP and staged as data, never derived by the agent
from a transport path.*

---

## 8. Do not

- Do not delete the existing rejected revision 1 from the human's project.
- Do not change `append_batch` or any human Sync path.
- Do not add an automatic rebase when the graph moved under a resume.
- Do not reintroduce `assemble_run` on any continuation path.
- Do not treat "reuses the native session" as "repeats the previous
  instruction". That equivalence is the bug.
