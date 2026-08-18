# Typed graph operations implementation handoff

Date: 2026-08-17
Status: confirmed and ready to implement

## Purpose

Replace the untyped operation dictionaries carried by `Patch.ops` and `Proposal.ops` with one strict, discriminated Pydantic operation model shared by parsing, validation, authority checks, materialization, transition preparation, and replay.

This is a contract cleanup before the Evidence-assessment, graph-transition, and Auto-research branch work. It must preserve the current persisted JSON shapes and current graph semantics.

## Confirmed contract

- `Patch.ops` is no longer `list[dict[str, Any]]`; it is `list[GraphOperation]`.
- `Proposal.ops` is no longer `list[dict[str, Any]]`; it is the appropriately restricted typed proposal-operation union.
- The discriminator remains the existing `op` field. Do not wrap operations in a new envelope or rename existing payload keys.
- Every operation model is strict and rejects unknown fields.
- Existing append-only Patch bytes remain untouched. Old valid projects replay to the same graph.
- The typed model does not change authority, operation ordering, Proposal policy, or materialization behavior.
- Invalid operations fail at the decoding or validation boundary with a deterministic, attributable error before canonical admission.

## Required implementation

### One core operation module

Create one core module, such as `src/rcp/core/operations.py`, that owns:

- the complete persisted operation vocabulary handled by materialization;
- strict payload models for each operation;
- the discriminated `GraphOperation` union;
- the restricted Proposal operation union;
- shared operation helpers needed by validation, authority, and materialization; and
- the centralized compatibility decoder for older valid operation shapes.

Cover every operation currently handled by `src/rcp/core/materialize.py`, including human- and system-only operations. Do not define only the narrower agent vocabulary.

Where agent-facing types are already stricter than the core type, keep that restriction. Prefer importing shared payload models into `src/rcp/agents/schema.py` and composing a narrower agent union rather than maintaining two structurally divergent copies.

### Preserve serialized form

A model-dumped operation must retain the current JSON object shape. In particular:

- `op` remains at the top level;
- operation payload keys remain unchanged;
- Proposal operations remain nested where they are today;
- backend-owned bookkeeping remains backend-owned; and
- no migration revision is emitted merely because the current RCP reads an older Patch.

Do not normalize or rewrite stored Patch files. Compatibility adaptation happens only while decoding into the current in-memory model.

### Refactor consumers to typed dispatch

Remove ad hoc `.get(...)`, stringly typed key access, and repeated shape interpretation from the core consumers. Refactor at least:

- `src/rcp/core/materialize.py`;
- `src/rcp/core/validation/patch.py`;
- `src/rcp/core/validation/proposals.py`;
- graph authority classification and admission;
- Proposal dependency and bookkeeping logic;
- history delta and revision-summary code that inspects operations;
- agent Patch parsing and correction diagnostics; and
- tests/helpers that construct raw operation dictionaries where a typed builder is more appropriate.

Typed pattern matching or `isinstance` dispatch is preferred. A temporary conversion back to dictionaries at one narrow serialization boundary is acceptable; continuing to reinterpret dictionaries throughout the core is not.

### Compatibility boundary

Keep legacy adaptation centralized. The decoder may accept a prior valid operation shape and return the current typed operation, but current live parsing must stay strict.

Representative older Patch fixtures must remain byte-identical before and after opening. A current RCP may materialize them through adapters; it must not append a migration Patch or rewrite history.

### Error contract

Malformed or unknown operations must produce an ordinary validation or decoding failure that identifies:

- the operation index;
- the discriminator or field that failed; and
- the containing Proposal id when the failure is nested in a Proposal.

No malformed operation may enter canonical history or degrade later replay.

## Non-goals

Do not use this handoff to:

- add, remove, or rename graph operations;
- change agent or human authority;
- redesign the ontology;
- implement transition-manager rules;
- implement graph branching;
- rewrite historical Patch files; or
- introduce a general command or rule DSL.

## Important seams

Expect the shared-contract work to touch:

- `src/rcp/core/models.py`;
- a new core operation module;
- `src/rcp/core/materialize.py`;
- `src/rcp/core/validation/`;
- `src/rcp/core/authority.py` and related admission helpers;
- `src/rcp/agents/schema.py`;
- `src/rcp/history/delta.py` and `src/rcp/history/manager.py`; and
- focused tests and helpers.

Land this as one serial contract change before any Evidence, transition-manager, or branch worker starts. Those later changes must consume the typed union rather than adding new dictionary interpretation.

## Acceptance and verification

This is an internal schema refactor, not a new user journey. Do not create a standalone acceptance scenario solely for it. Update the existing boundary-input contract only if its wording needs to name the now-central typed decoder.

Required proof:

1. Every currently supported operation round-trips through Pydantic without changing its JSON shape.
2. Existing accepted Patch fixtures materialize to exactly the same graph, Proposal state, coverage state, and revision summaries.
3. Existing older-generation fixtures open without canonical writes or changed Patch bytes.
4. Unknown operations, extra fields, wrong payload types, and malformed nested Proposal operations fail before history admission.
5. Agent and orchestrator schemas remain narrower where authority requires it.
6. Replay and current Patch application use the same typed operation contract.
7. The focused core, history, authority, agent-schema, and Proposal test suites pass before the next handoff begins.

## Completion

Once implemented and verified, update the current graph/history specification, record any compatibility decision that is not obvious from the specification, and move this handoff to `docs/archive/handoffs/` during the final documentation pass.
