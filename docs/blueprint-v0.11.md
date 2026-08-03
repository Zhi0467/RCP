# Research Control Panel blueprint v0.11 amendment

This amendment supersedes the v0.9 statement that researchers configure the
ontology through Settings, the standalone Glossary destination in older shell
descriptions, conversation-local agent configuration, and older descriptions
that place every Agent task in Runs. Historical ontology data and operations
remain valid and replayable. D34 below records a proposed contract that is not
yet human-confirmed and does not authorize implementation.

## D31 — Reader-facing project surfaces

The six shipped node types are RCP's authoring product. Project Settings does
not expose ontology type, field, or relation authoring. Ontology extensions
already present in append-only history continue to materialize, validate,
render, and replay; this change removes an editing surface rather than changing
the historical schema contract.

Glossary definitions appear inline when an existing whole term is read in node
prose, a chat answer, or a Proposal. They are best-effort, supplementary aids:
matching never mutates the underlying text or creates graph authority. Glossary
has no primary navigation destination. Who authors future glossary entries
remains open in [`open-questions.md`](open-questions.md).

Paper uses one authored Markdown pane with Write and Preview modes. Preview
renders the current unsaved editor text through the same Markdown pipeline as
chat and does not alter saving, synchronization, or conflict handling. Node
detail remains a floating inspection window whose project-scoped size survives
minimize/restore and close/reopen, is clamped with its position to the viewport,
and closes when the human enters Chats.

## D32 — Project-owned execution profiles and truthful work surfaces

Provider, model, reasoning effort, and execution machine are owned by Project
Settings. Chat and paper coaching display a passive provider label rather than
conversation-local pickers. Chat retains Raw truth inputs because it chooses
the current turn's context, not its execution profile. Seed and Refresh retain
their deliberate launch controls. Changing provider without an explicit model
clears any inherited model so the new provider chooses its default; an explicit
new-provider model remains authoritative. Settings supplies fresh conversation
defaults; an existing native chat or coaching conversation retains the profile
it last ran with so continuation cannot silently switch provider, model, or
machine.

Inbox counts every open Blocker regardless of subtype. Runs contains research
execution: Seed/Refresh ingestion runs, experiments, and graph Blockers. Node
chat, project chat, and paper-coach tasks remain inspectable in project History
and the Agent task inspector, but never become research runs. Terminal tasks do
not display live progress or an ETA.

## D33 — Deterministic, reader-facing revision history

Overview shows the latest revision's plain-language change summary, and project
History shows the same summaries newest first alongside the complete Agent task
list. Patch producers are responsible for ordinary reader-facing prose: titles
rather than graph ids, no operation names, and no inventory-style counts.
Overview requests only the current revision after project state is available;
the complete projection is loaded when History opens and does not gate project
reconciliation.

Rendering is a deterministic safety net over append-only patch history. It
resolves only identifiers mapped to a title known at that revision, preserves
unknown slash-delimited text such as repository paths, derives a truthful
title-based fallback from operations when prose is absent, and quotes only
consequences already stored in a Proposal. Legacy authored inventory prose is
preserved rather than silently deleted. The projection is collected during the
existing canonical replay rather than applying accepted patches a second time.
It never authors a canonical summary file or invents cross-node scientific
causality.

## D34 — Proposed staged graph-audit skills (confirmation required)

RCP would ship independently versioned skill folders as package resources and
stage the registered folders only into initial Work, Seed, and Refresh scratch
workspaces. The prompt would carry compact id, version, when-to-use, and staged
path pointers rather than embedded bodies. Non-Python skill files must be
explicit wheel/package data, defended by built-wheel inspection and installed
staging without a source checkout. Ordinary repository sessions, `.research`,
Discuss, and paper coaching would not receive them. A central registry would
declare permitted recipients, while each Work, Seed, and Refresh caller would
request its skills explicitly; no shared helper would branch on a run kind or
surface.

The first `graph-scanner` skill would be required by the initial-run prompt
after `patch.json` is written and staging succeeds, but remain advisory and
prompt-enforced. Its report outcome would be exactly clean, findings, or
unavailable; a missing invocation would be recorded separately. A staging
failure would omit the advisory step without blocking the graph-writing launch.
Missing, transport, runtime, malformed-output, timeout, and oversized-report
cases would never change the semantic validator verdict or masquerade as graph
findings. Report bytes would be bounded by a central `src/rcp/limits.py`
tunable.

RCP would derive accepted-node and current human-authored literal-field
constraints from canonical append-only history and pass those protected
boundaries to the scanner. Advice could explain tension around protected
content but could not recommend removing, merging, rewriting, contesting, or
relocating it. Drop and merge advice would remain Proposal-shaped report
content, never an automatic graph operation or second patch channel. This is a
scanner constraint, not a change to S52's existing valid `remove_nodes`
authority.

Scanner-driven edits would remain in the original launch and `patch.json`,
without spending a correction round. Neither `work_patch_correction` nor a
generic Seed/Refresh correction relaunch would invoke the scanner again. Task
contracts and receipts would retain exact skill versions, invocation state,
outcome, bounded diagnostics, and the RCP-derived protection reasons needed to
reconstruct the run.

[`acceptance/S53-truthful-attention-and-run-surfaces.md`](acceptance/S53-truthful-attention-and-run-surfaces.md),
[`acceptance/S54-paper-preview-and-resizable-node-detail.md`](acceptance/S54-paper-preview-and-resizable-node-detail.md),
[`acceptance/S55-project-owned-agent-profile.md`](acceptance/S55-project-owned-agent-profile.md),
[`acceptance/S56-plain-language-revision-history.md`](acceptance/S56-plain-language-revision-history.md),
[`acceptance/S57-fixed-product-ontology.md`](acceptance/S57-fixed-product-ontology.md),
and
[`acceptance/S58-inline-glossary-definitions.md`](acceptance/S58-inline-glossary-definitions.md)
are the executable contracts for the implemented portions of this amendment.
[`acceptance/S59-staged-graph-audit-skills.md`](acceptance/S59-staged-graph-audit-skills.md)
is the proposed executable contract for D34 and remains pending until the human
confirms it.
