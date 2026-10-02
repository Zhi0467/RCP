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

Opening Paper reads the Paper itself before showing an editor, because the
project snapshot's copy can be older than the saved file; until that read
answers, the view shows neither an empty editor nor Create introduction. Create
and save also refresh the project snapshot's copy.

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
the source, and receive at least a full retention period from import time. Import
failures retain a durable reason shown by artifact routes;
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
retries with bounded backoff and a bounded attempt count, retaining its error
after exhaustion. Expired unkept artifacts are skipped; invalid ownership,
lineage, and history-only sources are terminal. Source reads happen outside the
artifact lock; saving rechecks the version under the lock. Only a complete
capture becomes the immutable final snapshot, served after source files
disappear. Node/file-only pages have no final snapshot. Snapshot bytes live beside version bytes and share their typed backup,
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

A report opens in the in-app panel through its stored-artifact viewer, which
shows its current version and offers **Download**. The lifecycle record stays
bound to the immutable first version. RCP never writes a report copy into the
state repository.

## Artifacts panel

**Artifacts** is a project panel beside Research, Runs, and Inbox. It lists kept
chat artifacts and durable episode reports across project history, including
archived episodes, without depending on the recent task or episode window.
Temporary outputs remain in their originating chats until kept.

Reports appear as soon as their immutable bytes are captured. Cards show the
artifact title and a **Source chat** link
when its originating conversation is available. An output from a graph node
(its Experiment's node, else its node chat) also links that node by title when
the presented graph holds it; the link opens the node's detail in place. Actions
sit at each card's right end. A revisit shows the last loaded list at once and
refreshes it in place. Outputs originating in an
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
and a PDF also opens in the system viewer from the desktop app. Listing grants
no new filesystem or graph authority.

## Unified artifact viewer

There is no separate result-view kind. A task that draws a custom HTML result
produces an ordinary task artifact, through the same artifact directory,
descriptor, chat card, viewer route, and lifecycle as any other HTML artifact.
The artifact is available in its originating Node or Project chat, on its
run's Runs card when an episode produced it, and after Keep in the project's
Artifacts panel. It is not shown in unrelated chats.

Previously stored result-view rows migrate into Artifact records, and their
legacy URLs redirect to artifact routes.

Every viewable artifact, including a report, opens in the RCP window's viewer
panel. Its RCP chrome owns the title, version, Live or Finished status, Undo,
reply-thread control, dock, and close. The embedded shell keeps Keep and notices,
selection gestures, a comment box, and Send. The shell allows framing only by
the same RCP origin. Agent HTML remains inside its unchanged opaque sandbox.
Small raster images and SVGs may also render inline in chat; HTML has no thumbnail.
PDFs use the system viewer on desktop, and unsupported files remain download-only.
Repository-file previews use the same panel with their script-free content route.

The Runs card's list comes from the run artifact endpoint described in
[the API spec](api-web-and-desktop-projections.md#artifact-viewer-and-run-inventory).

The viewer entrance is backend-owned. `/viewer` is the current explicit shell
URL, while the former `/preview` URL remains a compatibility alias to that same
shell for a retained desktop binary after a source pull. Raw sandboxed rendering
lives at `/content` and is embedded by the shell or used for a small inline
image; it is not a second user-facing viewer. This split applies equally to task
artifacts and episode reports. A retained client that still embeds a small PNG
or SVG from `/preview` continues to receive image bytes for an explicit browser
image request; ordinary navigation to that URL receives the shell.

### Selection and comments

HTML, raster images, and SVG support selection gestures. Every type the viewer
shows accepts an edit comment, including Markdown, text, data, and code; these
other types accept comments without selections. PDF and download-only files
refuse editing.

Every comment is one object: its text and the selection it anchors to, or none
for the whole artifact. HTML selection gestures activate when the surrounding
shell opts in through the private preview bridge. A text or area selection opens
a floating comment window over the artifact; Cancel or Escape discards it.
Dragging from a figure or blank space selects an area, while starting on text
preserves ordinary highlighting. The window offers **Add comment**, which files
the comment, and **Edit now**, which adds it and sends every filed comment at
once. Filed comments stay behind one corner comment icon with a count badge;
clicking it opens the tray, which lists them with Remove, offers a
comment on the whole artifact (the only kind for types without selection
gestures), and a one-off **Send to original chat** for everything filed. These
are prompt inputs, never graph annotations.

The shell saves filed comments and the unsent draft per artifact in the current
browser profile. Both actions post the same comment list to the stored
artifact's comments endpoint; Edit now also sets `edit_now`. A 409 displays the
server's reason and keeps the comments filed. Success clears them and notifies
the containing RCP panel of the admitted edit operation. When admission requires
an explicit fresh session, both actions say so and supply the fresh-session
flag. An unavailable origin never prevents viewing; its reason appears beside
the disabled actions. The shell paints with the containing app's theme and
color mode and follows a change live; opened on its own, it uses the remembered
appearance.

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
carries at most 50 annotations. The viewer and a chat draft reach the agent in
one shape: admission writes the human message as any free text followed by
`Comment N: <text>` for each anchored comment, and the prompt's artifact item
lists the same numbers with what each covers, saying the file may be edited in
place when a comment needs it. `edit_now` adds one line asking for that edit in
this turn; nothing else differs between the routes. No markup is added. The selection payload, comments, and final
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
Stop or episode health. Edit tasks have no operational episode membership; their
admission snapshot retains episode provenance for replies and display. The next
operational launch reopens its master after a revoking edit; an edit finishing
does not clear that requirement.

The reply destination comes from durable origin and episode records. Chat and
child Work artifacts use their own chat. Experiment artifacts and reports use
the episode's Experiment node chat. Orchestrator artifacts and reports use the
Runs orchestrator thread: the human comment is recorded as already-delivered
mail, and the edit task's labelled answer is its reply. No new chat is created,
and the open view has no routing authority.

RCP stages the admitted base bytes at a writable file in turn scratch, preserving
its filename. Recovery and Retry reuse that exact staged file and operation key.
Unchanged bytes do not publish or fork. Once publication is recorded, retries
retain that result; publication failures retain the answer and an error receipt.
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
reason. Only after exact-session admission identifies a resumability failure
does the explicit fresh-session flag authorize a new provider session and
scratch while retaining the same reply thread; it never restores the old
execution authority. Legacy candidate rows are read only for background import
and archive capture. The read-only viewer state checks durable origin, master presence, and session
reservations without SSH or content hashing. Its offers never replace full
admission and integrity enforcement by the comments POST.

### Shape boundary

RCP owns no chart vocabulary or data encoding. Agents may draw bounded custom
HTML for discrete, configural research objects such as series, item grids,
tables, distributions, matrices, projections, and structured diffs. Established
domain viewers continue to own node-link computation graphs, spatial fields and
meshes, performance traces, and other specialist formats.

## Keeping an artifact

**Keep** stops the artifact's expiry and refreshes its task projection. It does
not write to the state repository.

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
source page in the in-app viewer panel. Its script-free response allows only
same-origin framing.

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
