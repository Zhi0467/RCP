# Paper, artifacts, and viewing

This specification owns the human paper draft, read-only coaching, temporary
artifacts, versioned episode reports, the unified artifact viewer, kept
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
leave output files, but artifact discovery, validation, expiry,
rendering, SSH availability, or Download failure never changes the answer, task
verdict, Patch verdict, or graph.

For an ordinary turn, RCP discovers bounded direct regular children of the
exact RCP-created artifact directory, of any type. It ignores provider
directives, provider-owned paths, URLs in prose, nested files, and symlinks.
Discovery reads at most eight files, each at most 16 MiB, and attaches at most
32 MiB per turn; a size is checked before a read is spent. Files are considered
in filename order. An admitted edit reads its exact staged file separately;
discovery attaches only the other outputs as new artifacts.
Empty files and files past a bound get no card, but the turn reports how many
were left out and why, and says so when discovery itself failed. The report
comes from the durable discovery receipt; it carries counts, never paths.
Discovery copies bytes into RCP's data directory and attaches their identifiers
with the task. Chat, Experiment, and Auto-research child Work use this path,
including retries and recovered turns. Viewing and Download read only storage;
stage availability no longer decides whether newly captured outputs are viewable.
Existing stage-only and repository-kept outputs import in bounded background
passes per project, after startup effects are admitted. Local and SSH sources
retain their identities and supplier bindings. Temporary imports expire at the
stage's last-touch time plus its existing retention period; kept imports have no
expiry and leave the repository file in place. Unresolved candidates import as
ordinary artifacts on their own producing turns, without accepting a version of
the source. Import failures retain a durable reason shown by artifact routes;
transient failures retry with capped exponential backoff, while missing files,
expired stages, and invalid sources are terminal. Repeated or interrupted import
never replaces an existing artifact. Maintenance and shutdown drain active
passes before releasing the background owner.

HTML previews run in an opaque sandbox. Agent scripts cannot access or navigate
the RCP parent, open popups, submit forms, initiate downloads, or use ordinary
network resource APIs. A trusted human click on a sanitized HTTP(S) reference
may be relayed by the RCP wrapper over a private channel unavailable to the
artifact. Inline JavaScript remains useful and may navigate only its isolated
child frame, which can still cause a navigation request; RCP does not claim
literal zero network traffic.

Every card offers Download and Keep. The file's type decides how it is viewed:

- HTML opens in the sandboxed viewer. It has no inline thumbnail.
- Raster images and SVG open in the viewer, and a small one renders directly
  with the answer.
- Markdown opens as RCP-rendered, script-free HTML. Raw HTML in the source
  stays visible source, links show their address without being live, and
  images are not fetched.
- Text, data, notebook, and code files open as escaped text with line numbers.
  Valid JSON and notebooks are pretty-printed.
- Markdown and text previews render a bounded prefix and say when they are
  truncated; Download returns the whole file.
- A PDF opens in the system PDF viewer from the desktop app. In a browser it
  is Download only; RCP never loads a PDF into its own page or origin.
- Any other file, and any file whose bytes fail its type's recognition check,
  is Download and Keep only. Such a file stays download-only even if its bytes
  or RCP's supported types change later.

A broken preview never hides Download or Keep, and never erases the reply.
Keep refreshes the authoritative task projection after the mutation succeeds.
Omission counts remain visible even when the turn has no artifact cards.

## Live HTML pages

A live page is an HTML artifact for a source still changing after its producing
turn: a job, sweep, episode, or node gathering Evidence. Final results remain
ordinary pages. Episode reports are never live. Their creation owner records
`live_data_allowed: false` on the Artifact; shared live-data code enforces this
stored rule. Migration and legacy report import preserve it.

One `application/json` script with id `rcp-live` declares protocol `version: 1`
and a `needs` array. The code model in `rcp.live_artifacts` owns the declaration,
snapshot fields, and `rcp-live-data` message. Needs name a helper job's launch
key, a node id in the artifact's graph target, the artifact's own episode, or
an absolute file path on its execution host. Files select `tail` or `whole`
and `jsonl`, `csv`, or `text`. Registered project repository roots and the
artifact's bound worktree constrain file access; a path alone grants no access.

Discovery resolves needs once per version. Edited-version publishers call the
same resolver. Job keys bind to stable ids through helper-command receipts and
their existing recovery lineage. An invalid declaration remains static with a
stored reason. Live resolution and final-capture diagnostics belong to the
version, so Undo or a later edit cannot silently retarget an earlier page.

The authenticated viewer requests a snapshot for an artifact and version only.
Every read rechecks project membership and the graph target. The server reads
only stored bindings, through readers that refuse symlinks and nonregular
files. Local and SSH reads, rows, needs, and log tails are bounded by `limits.py`.
Episode budget fields measure invocations, matching the existing episode meter.

The shell polls while visible, more slowly for SSH file needs, and relays
`rcp-live-data` over the existing private artifact channel. HTML remains opaque
and receives no new RCP request capability. Data received by the page can leave
through scripts navigating their own frame; this is not a zero-network promise.

Server reconciliation captures final data after every watched job and episode
has ended, without requiring an open viewer. A failed read stays incomplete and
retries with bounded backoff. Only a complete capture becomes the immutable final
snapshot, served after source files disappear. Node/file-only pages have no final
snapshot. Snapshot bytes live beside version bytes and share their typed backup,
transfer, retention, and integrity inventory.

## Episode reports

Every non-Stop Experiment or Auto-research ending receives one hidden visual
report lifecycle described in
[Conversations, episodes, and watchers](conversations-episodes-and-watchers.md#visual-wrap-up).

The provider writes one exact `episode-report.html`. RCP validates and captures
immutable bounded bytes into an Artifact before serving them through the opaque
sandbox. The lifecycle record binds the first version permanently; it stores no
HTML. Viewing that report does not resettle the episode. The
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

The report viewer offers **Save copy**, including when its originating chat is
unavailable. It writes the captured HTML into the state repository's `artifacts/`
directory through the existing artifact publication path, requires project write
admission, and shows the repository-relative saved path. Each explicit save
creates a collision-free copy and preserves existing files. A failed save is
visible and retryable. The stored report, its preview, and the episode lifecycle
remain unchanged; saving does not create a graph Patch. The viewer labels these
durably stored reports as **report**, rather than **temporary**.

## Artifacts panel

**Artifacts** is a project panel beside Research, Runs, and Inbox. It lists kept
chat artifacts and durable episode reports across project history, including
archived episodes, without depending on the recent task or episode window.
Temporary outputs remain in their originating chats until kept.

Reports appear as soon as their immutable bytes are captured; **Save copy** is
not required to make them discoverable. Saving a repository copy does not add a
duplicate report entry. Cards show the artifact title and a **Source chat** link
when its originating conversation is available. Outputs originating in an
episode also carry one compact **Experiment** or **Auto-research** tag. Ordinary
chat artifacts have no episode tag. The listing retains episode
identity, creation time, and saved path in its data without displaying repeated
type labels, timestamps, paths, or opaque episode IDs on each card.

Each new report's HTML document title names its research subject and main
finding or unresolved outcome. RCP extracts that title when capturing or
importing the report and stores it as display metadata. Existing reports receive
the same extraction during storage migration. The inventory reads this metadata
without loading report HTML. A missing document title uses **Report** as its
label; the tag supplies its episode type without repetition. This display
metadata does not alter the immutable report bytes, digest, or episode outcome.

Source chat preserves the exact project and graph target. Episode-owned branch
conversations open through their existing Runs transcript, preserving its
read-only boundary; Work that an Auto-research episode spawned opens that
episode's Runs entry, which lists the worker turn and inspects its transcript.
Missing or non-chat origins have no source link; their
artifact preview remains available. The source link is independent of the
card's preview click target. Opening an entry uses the existing bounded artifact
viewer in both browser and desktop; an entry with no viewer offers Download,
and a PDF also opens in the system viewer from the desktop app. Listing grants no new filesystem or graph
authority.

## Unified artifact viewer

There is no separate result-view kind. A task that draws a custom HTML result
produces an ordinary task artifact, through the same artifact directory,
descriptor, chat card, viewer route, and lifecycle as any other HTML artifact.
The artifact is available in its originating Node or Project chat, and after
Keep in the project's Artifacts panel. It is not shown in unrelated chats.

Previously stored result-view rows migrate into Artifact records. Their legacy
URLs redirect to artifact routes for compatibility. The current web client exposes no result-view
type, selector, card, or authoring request, and the task API rejects new legacy
create or revise intents.

Every viewable task artifact opens through one viewer shell. The shell owns
the preview and **Keep**. Selection-to-prompt is a separate layer that the
shell hosts only for a type that supports it, and only when the originating
chat can receive it. The artifact remains the dominant visual object. That
layer adds only a narrow selection rail and the controls needed to add the
selections to the originating chat. A missing chat removes the rail; it never
prevents viewing. Episode reports
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

### Selection-to-prompt, not artifact annotation

HTML, raster images, and SVG support selection gestures. Every type the viewer
shows accepts an edit comment, including Markdown, text, data, and code; these
other types accept comments without selections. PDF and download-only files
refuse editing.

HTML selection gestures activate only when the surrounding confirmation shell
opts in through the private preview bridge. A viewer without an originating chat,
whether a task artifact or an episode report whose concluding task is not a chat
turn, keeps ordinary browser gestures and never draws a selection rail; it keeps
Keep or Save copy.

Selections and comments are saved per artifact in the current browser or desktop
profile. Closing and reopening the viewer restores them, including comments
already added to a chat draft. **Remove** deletes an individual saved selection.
They remain prompt inputs, separate from the artifact and graph. Highlighting
text remains an ordinary browser selection. Dragging from a figure or blank
space draws an area immediately, without a separate Box mode; starting on text
preserves native highlighting, and ordinary controls keep their own gestures.
Both text and area selections are pending until the human chooses **Comment**;
**Cancel** or Escape discards the pending selection. Clicking or dragging alone
never adds prompt context. The human may add one comment or question per
confirmed selection and add them to the chat. Each selection becomes a composer
annotation, the same object as a comment on answer text, with the artifact
selection as what it is about; the comment stays editable there and the
annotation is removable. Nothing is sent until the human sends that composer
turn. Re-adding selections replaces the staged artifact annotations and leaves
typed text and answer comments alone. Artifact annotations need a new turn, like
files; they block steering a running turn.
After adding selections, **Open chat** opens that exact conversation and graph
target with the annotations ready to review. In the desktop it brings the existing RCP
window forward; in a browser it follows the chat link in the current tab.
An expired desktop navigation cannot later select the chat or focus the window.

RCP carries selected text with limited surrounding text. A box on HTML names
up to eight elements it covers the way a reader of the source finds them: a CSS
path, the element's own or its chart's label, and its bounded text. When the box
sits inside one element, such as a canvas or chart, it also says where within
that element. A box on an image is a fraction of the image as displayed, with
its orientation applied. On a raster image (PNG, JPEG, GIF, WebP) the server
also crops that region from the staged copy, decoding it once, and stages the
crop beside it, so a recovery restages the same crop. An animated image is
cropped from its first frame and the prompt says so; SVG and an image over the
crop pixel bound travel as positions only. A box saved by the viewer before
elements were named measured the viewer area, so it is described by its old
sampled text and never cropped. A turn
carries at most 50 annotations. On send, each artifact annotation adds
`Selection N: <what it covers>` and its comment to the human message, numbered in
order, and the prompt lists the same numbers with what each selection covers; no
markup is added. The selection payload, comments, and final
question are bounded and treated as untrusted input.
### Editing and versions

Comment admission records the current artifact version and the origin session's
recorded master. A chat master, including a child Work boundary, receives an
ordinary Discuss turn. An Experiment or orchestrator master receives a separate
revoking artifact-edit task with no master, graph contract, watcher commands,
or repository write authority. Admission decides this once; prompt construction
uses the frozen decision. Comments never become Work turns.

Admission atomically reserves the native session and stage against every launch
owner. A busy session returns 409 with its unavailable reason; comments are
neither queued nor steered. Edits spend no episode invocation and do not change
Stop or episode health. The next operational launch reopens its master after a
revoking edit; an edit finishing does not clear that requirement.

The reply destination comes from durable origin and episode records. Chat and
child Work artifacts use their own chat. Experiment artifacts and reports use
the episode's Experiment node chat. Orchestrator artifacts and reports use the
Runs orchestrator thread: the human comment is recorded as already-delivered
mail, and the edit task's labelled answer is its reply. No new chat is created,
and the open view has no routing authority.

RCP stages the admitted base bytes at a writable file in turn scratch, preserving
its filename. Recovery and Retry reuse that exact staged file and operation key.
RCP reads it after settlement and publishes under the artifact lock only if the
base is still current. If Undo moved the pointer, the edited bytes become an
ordinary new artifact on that turn. Other files in the artifact directory are
ordinary outputs. Artifact storage remains outside the provider's writable
scratch. A failed turn retains its stage and does not publish a version.

Undo moves back one retained version under the same lock as publication, Keep,
expiry, and pruning. The original remains; there is no redo. Undo is permitted
while an edit is admitted. Version bases awaiting staging are retained so
concurrent publication cannot remove the admitted bytes.

A history-only origin or missing native session or stage returns an unavailable
reason. The explicit fresh-session flag authorizes a new provider session and
scratch while retaining the same reply thread; it never restores the old
execution authority. Candidate creation, comparison, Accept, and Reject are
removed. Legacy rows are read only for background import and archive capture.
The docked viewer and its direct comment controls remain the later viewer slice;
existing selection-to-composer controls remain until then.

### Shape boundary

RCP owns no chart vocabulary or data encoding. Agents may draw bounded custom
HTML for discrete, configural research objects such as series, item grids,
tables, distributions, matrices, projections, and structured diffs. Established
domain viewers continue to own node-link computation graphs, spatial fields and
meshes, performance traces, and other specialist formats.

## Keeping an artifact

**Keep** stops the artifact's expiry and refreshes its task projection. It does
not write to the state repository. The report viewer's explicit **Save copy**
continues to publish a separate repository copy from the immutable first version.

Artifact metadata is in SQLite. Version files live in one folder per artifact
under the data directory, referenced by relative digest identifiers. Writes use
temporary files and atomic replacement. Original bytes are retained with the
last bounded number of versions, subject to the byte cap in `limits.py`.
Publishing checks the base version under the same artifact lock used by Keep,
expiry, and pruning, and records an operation idempotency key. Captures share an
in-process guard that delays file deletion until all captures finish. Reads,
creation, Keep, and version publication do not take that guard; only publication
that prunes files waits for captures before unlinking. Backup copies exactly the
typed inventory from its SQLite snapshot. Project transfer reads artifact
metadata and versions in one SQLite read transaction and derives the file
inventory from those same versions. The report migration writes digest
files before committing bindings and removing inline HTML; migration checks use
a throwaway file root.

Publication, Undo, and Keep append no Patch, spend no graph revision, create no
Proposal, and grant no graph authority.

## Repository-file previews

A repository-file Markdown link in an answer never navigates the main RCP
WebView. RCP resolves the absolute execution-host path against configured
project repository roots. Exactly one match opens a bounded escaped read-only
source page through the secondary preview window.

A remote match is read on demand through that repository's configured SSH host
and is not retained locally. No match, several matching/nested roots, unavailable
host, or nonregular or nontext file produces a visible nonnavigating error. RCP
never chooses the longest root or guesses a machine from path text.

A file larger than the preview bound is not refused. The reader returns the
lines around the cited line, numbered by their real positions and labelled with
the whole file's byte size, so a cited line in a multi-gigabyte log stays
readable evidence. When those lines exceed the byte bound, leading context gives
way first and the cited line itself is always retained. Without a cited line the
reader returns the file's first lines. A window is always labelled as one; RCP
never presents it as the whole file.

An answer may also cite a file the turn itself wrote. That path lies outside
every repository root, so RCP opens the artifact the task already registered
under that name through its viewer, or focuses its card when it is download-only.
An unregistered name keeps the ordinary nonnavigating error.

Task prompts state this citation contract to the agent: an absolute path on the
file's host, optionally suffixed with a line, naming either an authorized
repository file or a file written in the turn's artifact directory.

## Temporary chat inputs versus outputs

Human input attachments and agent output artifacts have separate contracts.
Input bytes are claimed for one turn and never offered for later download;
output artifacts are discovered after a task in its exact directory. Neither is
canonical graph provenance. Keep changes an output artifact's storage lifecycle,
not its authority or type.

## Glossary presentation

Graph-writing agents add or revise thin project-wide definitions through
`upsert_glossary` Patches. Canonical glossary entries render as best-effort
whole-term inline definitions in node prose, answers, and Proposal cards. There
is no standalone Glossary surface or human glossary editor.
