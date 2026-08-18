# Paper, artifacts, and result views

This specification owns the human paper draft, read-only coaching, temporary
artifacts, immutable episode reports, agent-authored result views, kept views,
and repository-file previews.

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
Bytes remain in temporary local or remote scratch and are proxied on demand;
descriptors may be durable with the task but do not copy bytes into chat or
canonical storage.

HTML previews run in an opaque sandbox. They cannot access or navigate the RCP
parent, open popups, submit forms, initiate downloads, or use ordinary network
resource APIs. Inline JavaScript remains useful and may navigate only its
isolated child frame, which can still cause a navigation request; RCP does not
claim literal zero network traffic.

Raster images and HTML have current bounded validation. An invalid artifact is
shown as an artifact error and does not erase the reply.

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

## Result views

An ordinary Work turn may create a **result view**: an agent-authored HTML page
for reading the run's own output inside the Runs task detail. It is not a global
navigation surface, dashboard, new agent capability, or canonical graph object.

RCP owns no chart vocabulary or data encoding. The useful property is that the
native Work session already knows the research and repository context. Views are
disposable by default.

### Stable conversation-owned path

A result view is not a per-turn artifact. It lives at one stable
`views/<view-id>/` path inside the conversation's exact reusable stage. A
revision is an ordinary Work turn resuming the same native session and stage and
editing that exact file. RCP never routes it through the current turn artifact
directory, changes cwd, copies, or symlinks it.

If the exact native session cannot resume, revision fails visibly. RCP never
silently starts a fresh session and redraws from scratch.

### Acting on the picture

A supported gesture such as boxing a region or underscoring items produces only
untrusted bounded text for a visible editable composer draft. It never dispatches
a turn automatically. The page may report what its own gesture selected, but
RCP exposes no application data or authority inward in return. A page without
gesture reporting remains revisable through typed prose.

The human reads and sends every revision request. A result view currently emits
no research action, graph Patch, Evidence record, Proposal, or Decision; whether
it ever may is an open question.

### Verified served bytes

The staged HTML is the agent's working copy, not the served copy. After a
successful turn, RCP validates it and atomically stores verified bytes with the
result-view record, digest, and size. Rendering always uses that stored copy.

An interrupted or invalid revision cannot corrupt the last readable view, a
remote view renders without rereading its stage over SSH, and expiry removes the
stored bytes together with the record.

### Shape boundary

RCP draws discrete, configural research objects for which no stronger
domain-native viewer owns the interaction. Current useful shapes include ordered
series, item grids, tables, distributions, matrices, projections, and structured
diffs. Series and item grids are the initial confirmed pair.

Node-link computation graphs, spatial fields/meshes, and performance traces stay
with Netron, ParaView/VisIt, Perfetto, or their domain-native equivalents. RCP
does not build per-domain connectors or a general monitoring dashboard under the
result-view name.

## Keeping a result view

**Keep** copies one verified view into a `views/` directory at the canonical
state repository root, outside `.research/`, through the normal state workspace
lock and explicit publication. The agent suggests a descriptive base name; RCP
owns project/date qualification and collision-free filename selection.

A kept view appends no Patch, spends no revision, creates no Proposal, changes
no attention count, and grants no graph authority. It is a useful repository
artifact beside the research record, not part of graph truth.

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
canonical graph provenance, and neither may silently become a result view.

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
[S114 result views](../acceptance/S114-see-your-results-without-leaving.md), and
[S120 episode reports](../acceptance/S120-episodes-wrap-up-with-a-visual-report.md).
