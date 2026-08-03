# Research Control Panel blueprint v0.9 amendment

This amendment supersedes the generic agent-authority gate in v0.6 D20, the
proposal scope in v0.7 D26, and any older rule that makes accepted graph
structure itself proposal-only. D29 also supersedes the Work-specific permission,
patch-correction, and context-revision rules in v0.6 D19-D22 and the exact-scope
Work permission in v0.8. Discuss, Seed/Refresh, paper coaching, and preview
sandbox rules remain unchanged.

## D28 — Agent Proposals have exactly two semantic shapes

An agent-authored graph patch asserts research structure. Adding or removing an
edge, creating evidence or a blocker, editing ordinary node content, merging
nodes, and superseding nodes do not require a Proposal merely because an
affected node has accepted standing. A direct edit to accepted node content
returns that node to asserted standing for ordinary node review. A
Decision/Hypothesis merge or supersede that would also transition its semantic
status still follows the status boundary below; the structural relation itself
may be asserted directly.

An agent may create a Proposal only for one human-authoritative semantic
transition:

1. one Decision used as an Experiment input through an
   Experiment-to-Decision `governed_by` edge changes `selected_option` and/or
   `status`; or
2. one Hypothesis changes `status`.

The target Decision or Hypothesis may already exist in live state or be created
by an earlier operation in the same outer patch. The governing edge for a
Decision may already exist or be asserted by that outer patch. A Hypothesis
transition has exactly one `evidence_edge` cause naming a valid
Evidence-to-Hypothesis epistemic edge. No other belief-cause kind is
agent-authorized.

Each Proposal contains exactly one `update_nodes` operation for exactly one
node. No edge, content edit, ontology change, project-scope change, merge, or
supersede operation is an agent Proposal. Historical proposal records remain
replayable; this is an admission boundary for newly agent-authored patches.
Project configuration remains human-authored: ontology through Settings/Sync,
and project truth-scope changes through human patches.

Agent-created Decisions begin open and unselected. Agent-created Hypotheses
begin proposed. Agents never set standing, resolve or withdraw Proposals, or
authorize an Experiment Run. A belief Proposal's Evidence edge is part of its
live dependency set: removing or changing that cause makes the Proposal stale
rather than leaving an Inbox item that cannot be approved.

An experiment loop may propose a transition only for a pinned governing
Decision or for the status of a Hypothesis the Experiment tests. The latter is
grounded by an Evidence-to-Hypothesis epistemic edge asserted in the same patch,
and that edge is recorded as the belief transition's cause. Proposal approval
does not launch or resume an experiment; the human must press **Run** again.

The executable policy and the exact model-facing authority block share one source
in [`src/rcp/core/authority.py`](../src/rcp/core/authority.py). That module owns
the inspectable policy version and digest rendered into every graph-capable agent
task contract; admission validators consume the same semantic constants.

[`acceptance/S50-minimal-agent-proposal-boundary.md`](acceptance/S50-minimal-agent-proposal-boundary.md)
is the executable contract for this amendment.

## D29 — Work validates semantic patches against live state

Work is unrestricted non-interactive execution for both providers. Codex uses
`--dangerously-bypass-approvals-and-sandbox`; Claude uses
`--permission-mode bypassPermissions`. RCP imposes no repository-root or tooling
allowlist on a Work turn. The exact run scope remains contextual input, not a
permission boundary. Fresh, resumed, and retried Work launches plus Work graph
and watcher corrections retain the same Work capability. Direct writes to
canonical `.research` remain forbidden only by the Work task contract. This
prompt-enforced boundary is a known accepted limitation for both providers and
must never be reported as an OS sandbox guarantee.

The agent-facing patch is semantic only. It contains the graph operations and
human-readable change summary, while RCP supplies patch kind, author, revision,
run scope, Proposal dependencies and base revisions, object lifecycle
revisions, and admission metadata. Provider output never becomes a second
authority source for RCP bookkeeping.

Each Work stage contains an RCP-staged Python validator client and its exact
invocation in the task contract. The client reads that workspace's `patch.json`,
writes a uniquely named request file atomically, and waits for the matching
response file. While the provider runs, RCP polls the writable workspace locally
or through the existing SSH run-stage transport, reads the live canonical state,
prepares the candidate in process, and invokes the same semantic validator used
by Apply. Client exit `0` means valid, `1` means semantically invalid, and `2`
means the validator was unavailable. Request size, wait time, and checks per Work
turn are bounded; every answered check and any mailbox failure are recorded in
the durable task event and receipt stream. Unavailability never masquerades as
invalidity or starts a semantic-correction loop.

An invalid patch or watcher handoff is corrected in the same native Work session,
with the same repository, tooling, network, and provider permission access. Only
the instruction changes: correction asks for a revised semantic `patch.json` or
watcher request and explicitly forbids repeating completed operational side
effects. It does not switch to `scratch_patch`, clear repository directories, or
launch a second operational Work turn.

Apply does not trust a prior self-check or the graph revision used to assemble
the Work context. Under the canonical append lock, RCP reloads live state,
re-prepares all bookkeeping, reruns the same semantic validation, and appends
only the resulting valid candidate. There is no context-revision pin, Resume
ancestor walk, or rejection solely because the graph moved. A concurrent change
may still make the patch semantically invalid on current state, in which case
the current validation diagnostic is returned.

Validation stages operations in their written order against a temporary state
that includes every earlier valid operation. Whole-patch node and edge lookup
remains available for legal forward references, but lookup never authorizes
operation reordering. Thus create-then-update and create-then-resolve sequences
work, a Proposal may target a node created earlier in the same outer patch, and
duplicate or otherwise invalid ordered operations still fail.

This amendment does not change Discuss, Seed/Refresh and their generic
scratch-only patch-correction profile, the paper coach, or preview discovery and
sandboxing.

[`acceptance/S51-live-agent-patch-validation.md`](acceptance/S51-live-agent-patch-validation.md)
is the executable contract for D29.
