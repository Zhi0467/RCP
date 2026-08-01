# RCP — Blueprint v0.6

This document is the v0.6 amendment to
[`research-control-panel-blueprint-v0.5.md`](archive/research-control-panel-blueprint-v0.5.md).
Everything in v0.5 remains authoritative except where this amendment explicitly
replaces it. In particular, this amendment supersedes v0.5 D10–D12 wherever
they require every chat turn to be a graph-update primitive or prohibit project
execution from chat.

## 0. What changed in v0.6

RCP gains a minimum research-control vertical slice without becoming a job
scheduler. Every node and project conversation supports two per-turn modes:

- **Discuss** reasons without project or canonical graph mutation.
- **Work** may execute against the exact run-scope repositories and may emit one
  optional validated graph patch describing what changed or was learned.

This closes `decision → agent execution → durable result → optional graph
reflection → next decision` while leaving external process and scheduler state
agent-mediated. `ExperimentAttempt` remains a graph record, not a first-class
Slurm/process controller.

## D17 — Conversation mode is per turn, not per conversation

`ConversationMode = Literal["discuss", "work"]` is captured when a turn is
submitted. The same conversation and native provider session can alternate
between modes. A running, paused, resumed, or correction continuation retains
the original turn's mode and run scope; changing the composer affects only the
next ordinary turn.

Every new human and assistant transcript record stores the immutable mode. The
last selected mode is the next-turn default for that conversation. Historical
records created before v0.6 remain unlabelled.

The UI uses a labelled plum Discuss state and labelled dark-forest Work state.
`Shift+Tab` toggles only while the composer is focused. Color is a semantic
accent, never the only label or a tint over the reading surface.

## D18 — Discuss has no graph authority

Discuss receives the full graph and scoped source context needed to reason, but
the prompt contains no patch path or graph output schema. It never takes the
canonical append lock. A stray `patch.json` is a diagnostic receipt only and is
cleared before the next turn; it cannot grant its author authority.

Discuss may use writable conversation scratch for temporary preview artifacts.
Canonical state and repository inputs remain read-only. Codex enforces this with
its task permission profile. Claude's `--add-dir` behavior remains a prompt
contract rather than an OS sandbox guarantee; receipts and UI must not claim
otherwise.

## D19 — Work is non-interactive execution with optional graph reflection

Choosing Work is the human's per-turn authorization for both operational work
and an optional graph reflection. There is no separate `allow_graph_change`
switch and no RCP approval-event lifecycle.

Work may:

- edit exact on-machine repositories in the selected run scope;
- run Bash, builds, tests, network tools, and SSH;
- act on off-machine repositories through their exact host/path pointer without
  copying them;
- write temporary preview artifacts into the RCP-created artifact directory;
- write at most one optional graph patch, exactly at `patch.json`.

Work may complete with no patch. A missing or valid empty patch spends no graph
revision. The Markdown final answer remains the reply; `patch.json` is never
parsed from messages or stdout.

The canonical state repository's `.research` tree is never a Work output root.
Codex uses a task-scoped permission profile that grants scratch and exact
run-scope roots while keeping canonical `.research` read-only or denied. Claude
cannot provide the same OS-enforced nested boundary; its prompt forbids direct
canonical writes and the limitation remains visible in the launch receipt.

## D20 — A Work patch is not a universal Proposal

New Work patches use `kind="work"`; old accepted `kind="chat"` records remain
replayable. Work is not ingestion and may not write cursors or coverage.

The ordinary agent-authoring rules remain unchanged: legal additions and
updates land as `standing="asserted"`. Only operations already protected by the
narrow human-authority gate are encoded as current `Proposal` objects and sent
to Inbox. The graph patch itself is never wrapped in a second proposal.

The patch is pinned to the run scope and graph revision used to build the first
attempt's context. `HistoryManager.append(expected_revision=...)` performs the
freshness check under the append lock. RCP never silently rebases a stale Work
patch. A graph rejection cannot erase the answer, artifacts, or external work.

## D21 — Graph correction never repeats operational work

After the provider's operational turn is complete, RCP persists any exact
`patch.json` before validation. An agent-correctable failure starts a bounded
same-session continuation whose only job is to rewrite that file:

- writable scratch, no writable project repositories;
- exact bounded validator diagnostic;
- original expected revision and scope;
- explicit prohibition on project commands, networked operations, or repeating
  the original task.

This is an internal graph-correction continuation, not Seed/Refresh Retry and
not a second Work turn. The existing configured correction bound applies. If it
still fails, the agent task completes with `graph_update.status="rejected"` and
the original result survives. A later **Repair graph update** may continue only
while the retained stage, native session, and original revision are valid. A
moved graph requires a new Work turn; no automatic rebase occurs.

## D22 — Provider permission profiles are fixed by capability

The manifest still cannot widen or narrow agent permissions. RCP selects one
fixed capability profile:

| Capability | Codex | Claude | Writable state |
|---|---|---|---|
| Discuss | bounded workspace, `approval_policy="never"`, network | scratch-capable non-interactive mode | conversation scratch only |
| Work | `approval_policy="on-request"`, automatic reviewer, network | `acceptEdits` | scratch + exact run-scope repositories |
| Seed / Refresh | bounded workspace, `approval_policy="never"`, network | `acceptEdits` | run scratch only |
| Graph correction | bounded workspace, `approval_policy="never"`, network | `acceptEdits` | run scratch only |
| Paper coach | existing read-only contract | existing read-only contract | none |

`approval_policy="never"` means Codex never asks for escalation; it does not
mean unrestricted access. In Work, the automatic reviewer answers escalation
requests without an RCP approval surface. A denial is an ordinary provider/tool
event: the agent adapts or the task fails with the exact diagnostic.

Claude's `auto` argument is not used: the real non-interactive acceptance probe
normalized it to `default` and denied both scratch and authorized-repository
writes. `acceptEdits` is the bounded non-interactive mode that completed the
same probe. Its `.research` boundary remains prompt-enforced, as described in
D19, and is recorded explicitly in the launch receipt.

Network access is enabled for these graph and conversation capabilities. Once
network is part of the sandbox baseline, an SSH or HTTP call is not individually
reviewed merely because it uses the network. `danger-full-access` is not used.

## D23 — Work and graph outcomes are independently inspectable

The final task result contains:

```json
{
  "graph_update": {
    "status": "none | applied | rejected",
    "applied_revision": null,
    "change_summary": [],
    "proposal_ids": [],
    "validation_messages": [],
    "correction_rounds": 0,
    "repairable": false
  }
}
```

The task remains `succeeded` when operational work succeeds but graph reflection
is rejected. Runs and the transcript show the exact distinction. The reply may
show **Graph updated · rN**, **Proposal sent to Inbox**, or **Graph update
rejected**. No graph receipt is shown when no patch was produced.

Ordinary provider failure remains part of the durable task lifecycle. RCP never
automatically retries Work because operational effects may already exist.

## v0.6 acceptance

[`acceptance/S40-discuss-and-work.md`](acceptance/S40-discuss-and-work.md) is the
authoritative acceptance path for this amendment.
