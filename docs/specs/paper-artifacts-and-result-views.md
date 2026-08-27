# Paper, artifacts, and viewing

This specification owns the human paper draft, read-only coaching, temporary
artifacts, immutable episode reports, the unified artifact viewer, kept
artifacts, and repository-file previews.

## Human paper authorship

The canonical introduction and local paper draft are human-authored,
non-authoritative Markdown. The introduction covers the research question,
adjacent questions, literature, high-level methods, main results, and why the
work merits publication and communication.

Paper prose never becomes graph truth merely because it exists in the draft.
The graph and canonical research rendering remain separate inputs to writing.

The editor retains the canonical introduction content/revision against which its
draft was written. When canonical content moves, autosave preserves the human
draft and marks it behind rather than choosing a winner. The existing view
toggle exposes incoming canonical content in the preview pane, and one reversible
Apply action swaps it with the editor content. Only a later human edit re-pins
the draft and resumes canonical save.

No conflict strategy may discard either whole version or silently overwrite the
human draft.

## Read-only writing coach

The coach may read the draft and graph, identify unsupported claims, ask focused
questions, point to Evidence, and preserve native provider continuity. It may
not edit the draft, emit replacement prose as a write, write a Patch, approve a
Proposal, or turn paper text into graph state.

Provider-native session continuity does not make prior displayed RCP transcript
content a new authority source. The coach has its fixed read-only capability and
public-web behavior; a skill cannot widen it.

## Answer and artifacts are independent

The labelled final assistant message is the Markdown answer. A turn may also
leave supported preview files, but artifact discovery, validation, expiry,
rendering, SSH availability, or Download failure never changes the answer, task
verdict, Patch verdict, or graph.

For an ordinary turn, RCP discovers only bounded direct regular children of the
exact RCP-created artifact directory. It ignores provider directives,
provider-owned paths, URLs in prose, nested files, symlinks, and unknown types.
Bytes remain in temporary local or remote scratch and are proxied on demand
until the human keeps them. Descriptors are durable with the task but do not
copy bytes into chat or canonical graph storage.

HTML previews run in an opaque sandbox. They cannot access or navigate the RCP
parent, open popups, submit forms, initiate downloads, or use ordinary network
resource APIs. Inline JavaScript remains useful and may navigate only its
isolated child frame, which can still cause a navigation request; RCP does not
claim literal zero network traffic.

Raster images, SVG, and HTML have bounded validation. A small raster image or
SVG renders directly with the answer. HTML retains the ordinary **Open** link;
it does not need an inline thumbnail. An invalid artifact is shown as an
artifact error and does not erase the reply.

## Episode reports

Every non-Stop Experiment or Auto-research ending receives one hidden visual
report lifecycle described in
[Conversations, episodes, and watchers](conversations-episodes-and-watchers.md#visual-wrap-up).

The provider writes one exact `episode-report.html`. RCP validates and captures
immutable bounded bytes before serving them through the opaque sandbox. The
versioned official report skill requires a visual retrospective; runtime safety
validation does not mechanically score visual quality.

Experiment reports emphasize objective, method/configuration, attempts,
observations, Evidence, failures, limitations, and the resulting human pause or
next step. Auto-research reports additionally cover epistemic movement,
Decisions, delegation, failures, and the briefing needed for the human to resume
control.

The report is retrospective only. It has no Patch, watcher, command, Proposal,
or graph channel and never determines the episode verdict. A final generation
error remains visible and nonblocking.

## Unified artifact viewer

There is no separate result-view kind. A task that draws a custom HTML result
produces an ordinary task artifact, through the same artifact directory,
descriptor, chat card, viewer route, and lifecycle as any other HTML artifact.
The artifact is available in its originating Node or Project chat; it is not
limited to Experiment Runs and is never shown across chats.

Previously stored result-view rows remain readable through their legacy backend
routes only for compatibility. The current web client exposes no result-view
type, selector, card, or authoring request, and the task API rejects new legacy
create or revise intents.

Every supported task artifact opens through one viewer shell. The shell owns
**Keep** and transient selection-to-prompt interaction. The artifact remains the
dominant visual object. The shell adds only a narrow selection rail and the
controls needed to add the selections to the originating chat. Episode reports
use the same shell and selection vocabulary while retaining their immutable
episode-report lifecycle.

Small raster images and SVGs render inline in the chat and may also open in the
viewer. HTML keeps its current link behavior and opens directly into the full
viewer; it has no chat thumbnail. Repository-file previews are explicitly out
of this contract.

The viewer entrance is backend-owned. `/viewer` is the current explicit shell
URL, while the former `/preview` URL remains a compatibility alias to that same
shell for a retained desktop binary after a source pull. Raw sandboxed rendering
lives at `/content` and is embedded by the shell or used for a small inline
image; it is not a second user-facing viewer. This split applies equally to task
artifacts and episode reports. A retained client that still embeds a small PNG
or SVG from `/preview` continues to receive image bytes for an explicit browser
image request; ordinary navigation to that URL receives the shell.

### Selection-to-prompt, not annotation

Selections are temporary prompt inputs, not persistent annotations. The human
may select text or draw a box over the rendered artifact, add one comment or
question per selection, review the assembled draft, and add it to the ordinary
chat composer. Nothing is sent until the human sends that composer turn.

RCP carries selected text with limited surrounding text. A box carries bounded
viewport-relative coordinates and the intersecting visible text or SVG labels;
an implementation may additionally attach a screenshot crop. The selection
payload, comments, and final question are bounded and treated as untrusted input.
The current artifact bytes are staged as a read-only turn input so the resumed
agent can inspect what the human saw.

The turn resumes the artifact's originating native session and chat. Failure to
resume is visible, with a separate explicit fresh-session action; RCP never
silently changes sessions. The default mode is Discuss. The prompt asks the
agent to address every comment and question, not to edit the artifact. An
artifact edit is allowed only when the human explicitly requests one and sends
the turn as Work.

### In-place revision

An accepted revision replaces the same artifact's bytes and updates the same
card and identity. It does not create a second artifact file or descriptor. For
an unkept artifact, the task-stage file is the working file. For a kept artifact,
the repository file is the working file. RCP validates a proposed replacement
before atomic publication, but does not compare it with a previous digest or
guard against external edits. Humans and tools may edit kept files normally;
the viewer and each prompt read the current bytes.

### Shape boundary

RCP owns no chart vocabulary or data encoding. Agents may draw bounded custom
HTML for discrete, configural research objects such as series, item grids,
tables, distributions, matrices, projections, and structured diffs. Established
domain viewers continue to own node-link computation graphs, spatial fields and
meshes, performance traces, and other specialist formats.

## Keeping an artifact

**Keep** writes the current artifact into an `artifacts/` directory at the
canonical state repository root, outside `.research/`, through the normal state
workspace lock and explicit publication. If `artifacts/` already exists as a
real directory, RCP reuses it and preserves every existing file. A file or
symlink at that path makes Keep fail visibly. Initial Keep chooses a safe,
collision-free filename and never overwrites an existing entry.

Keep records the artifact's stable repository filename. It does not freeze the
file: later human, tool, or explicit Work revisions update that same file in
place. No digest precondition rejects an update merely because another editor
changed it first.

A kept artifact appends no Patch, spends no revision, creates no Proposal,
changes no attention count, and grants no graph authority. It is a live
repository artifact beside the research record, not part of graph truth.

## Repository-file previews

A repository-file Markdown link in an answer never navigates the main RCP
WebView. RCP resolves the absolute execution-host path against configured
project repository roots. Exactly one match opens a bounded escaped read-only
source page through the secondary preview window.

A remote match is read on demand through that repository's configured SSH host
and is not retained locally. No match, several matching/nested roots, unavailable
host, nonregular or nontext file, or over-bound file produces a visible
nonnavigating error. RCP never chooses the longest root or guesses a machine
from path text.

## Temporary chat inputs versus outputs

Human input attachments and agent output artifacts have separate contracts.
Input bytes are claimed for one turn and never offered for later download;
output artifacts are discovered after a task in its exact directory. Neither is
canonical graph provenance. Keep changes an output artifact's storage lifecycle,
not its authority or type.

## Glossary presentation

Glossary entries already in canonical history render as best-effort whole-term
inline definitions in node prose, answers, and Proposal cards. There is no
standalone Glossary surface or current creation/edit/delete path until the open
authorship question is decided.

## Verification contracts

The durable journeys include [S11 paper coach](../acceptance/S11-paper-coach.md),
[S17 live preview](../acceptance/S17-real-agent-preview.md),
[S18 remote preview](../acceptance/S18-remote-artifact-preview.md),
[S32 desktop artifacts](../acceptance/S32-artifacts-in-the-desktop-window.md),
[S110 paper draft preservation](../acceptance/S110-paper-draft-survives-a-canonical-change.md),
[S114 unified artifact viewing](../acceptance/S114-see-your-results-without-leaving.md), and
[S120 episode reports](../acceptance/S120-episodes-wrap-up-with-a-visual-report.md).
