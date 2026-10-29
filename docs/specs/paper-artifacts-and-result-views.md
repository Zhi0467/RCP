# Paper, artifacts, and viewing

This spec owns the shared paper, coaching, artifacts, report versions, interaction,
storage, and repository previews. [Interface design](interface-and-visual-design.md#artifact-viewer)
owns the panel layout. [API projections](api-web-and-desktop-projections.md#artifact-viewer-and-run-inventory)
owns viewer and run payloads. Models live in [paper](../../src/rcp/paper/models.py),
[artifacts](../../src/rcp/storage/artifact_models.py), and
[live artifacts](../../src/rcp/live_artifacts.py). Limits live in `limits.py`.

## Shared paper

Paper is a member-authored file tree containing Markdown, LaTeX, Typst,
bibliographies, figures, and other files. It is non-authoritative and separate
from graph and research inputs. Paper text never becomes graph state. There is
no main file. Preview begins with the file being read. Each text file is a shared
live document. Concurrent edits merge without personal drafts or concurrency
refusals. The owning RCP process holds the live documents, including those for
personal projects with SSH state roots.

Saving and publishing are separate operations. An edit, its paper revision, and
its publication intent commit in SQLite before broadcast or acknowledgment.
Only acknowledgment means an edit is saved. Disconnected browser edits wait.
Publication replaces `paper/` as one staged tree after idle editing and
periodically during continuous editing. Renames and edits therefore never appear
apart. Failed or unreachable publication backs off while saving continues.
Paper is never committed to Git. Workers reconstruct documents and perform SQLite
work under the project lock. After commit, the owner installs and broadcasts the
result even if callers cancel. The paper badge means that saved revisions still
await publication.
**Why:** acknowledged keystrokes must survive crashes. Multi-file builds must
never see half a published tree.

The editor locally refuses edits that exceed the advertised per-document limit.
It flashes red and stays editable. A server refusal ends that connection and
shows a red edge with Retry. A refusal caused only by rate limits retries
automatically. Deletion, maintenance, and transfer pause live Paper.
[Socket admission](../../src/rcp/api/paper_live.py) owns retryable and permanent
close codes. It accepts the connection before closing so browsers see those codes.
A failed stop or ended maintenance resumes live Paper. Backup marks saved Paper
as unpublished. Restore republishes SQLite text instead of trusting copied folders.
Import publishes after activation without requiring a visit.

Stable file ids survive renames. File, folder, and root menus offer the applicable
Create, Upload, Rename, Download, and Delete actions. Folders exist through file
paths, so empty folders are browser-local. Renaming or deleting a folder creates
one revision. Downloading a folder produces one zip. Desktop uses native save
dialogs. New and uploaded files open immediately. Replacement uploads require
confirmation. Splits retain a usable width and the browser's preference. They
support keyboard resizing and omit the divider when stacked.

Deleting a file with unsent peer text keeps that editor read-only with Recreate.
Otherwise, deletion selects another file. Hidden deleted files with unsent text
remain behind a red icon across reloads until recreated. Restart pauses the editor
without unmounting it or losing typing. Refused or inaccessible unsent text remains
downloadable. Leaving with unsent text asks first. An early connection close never
discards it. One path rule reserves `.git` and `.publish` for editing, backup, and
transfer. Uploaded binaries deduplicate by content hash. SVG, HTML, and unknown
types download. Only raster images display inline. Paper files never execute as
same-origin documents.

A database without Paper adopts the existing published tree. Local revisions
continue after its marker. SSH mirrors omit that marker, so publication waits
until new revisions pass it. Paper from before the file tree becomes
`introduction.md`. Both divergent old drafts and saved text are retained. The draft
becomes live, and publication waits for a member to choose. The unchosen text
remains in History.

History checkpoints editing bursts and explicit requests. Before deletion,
replacement upload, or restore, it also checkpoints current revisions that are
not yet covered. Restore and text upload change only the differing span to
preserve peer positions. Identical text is a no-op. Per-file restore is a new
live edit. Automatic versions form a bounded ring. Named versions have count and
byte quotas. They refuse new names instead of evicting old versions or blocking
edits. Members may delete named versions. Deleting automatic versions is refused.
Timing rows prune only after publication and retained History both cover them.

### Preview

Markdown renders in the browser with images from the paper tree. Paper links open
the editor. HTTP(S) links open a new tab. Other links are inert. Large files render
after typing pauses. Typst runs in the browser with a compiler and fonts downloaded
by the backend and pinned by hash. LaTeX runs hash-pinned Tectonic on the backend
with `--untrusted` and bounded time, output, and concurrency. Compilation changes
no state and is available to members. Identical concurrent requests join. A newer,
different request cancels the project's older request and shows Retry. Previous
PDFs remain readable within retention.

Preview selects the one compile root that reaches the open file. If no root or
multiple roots qualify, it shows candidates. A chosen root is accepted only while
it reaches that file. PDF scroll is remembered per root. The preview's actions
(engine install, root choice, compile log, Download) sit in the panel header
beside the Preview and Coach switch, so the PDF starts under it. Typst errors identify
the path, line, and message. Tectonic has no OS sandbox. Its `--untrusted` option
disables shell escape, but absolute inputs may expose any server-user-readable
file in the member-only PDF, including agent-hidden files. This follows the
[team trust model](server-and-machine-operations.md). Agent launches cannot read
the preview.

## Writing coach

The coach reads saved paper, the graph, research, and context to question claims
or propose file edits, creation, renames, and deletion. Each turn reads an immutable
saved snapshot. It writes complete files only under `changes/files/` and records
moves and deletions in `changes/moves.json`. RCP validates the whole output tree
against paper limits or rejects it all. It then diffs the output against the
snapshot to create cards. A rename and its dependent edits form one group. The
coach writes no diffs or line numbers.

Execution follows [Local and SSH execution](providers-and-containment.md#local-and-ssh-execution).
Remote snapshots are fixed stage inputs, never the live publication directory.
Only changes are writable. Bounded read-back refuses symlinks and hardlinks and
uses the same card collection. Shared supervised passes, process receipts, and
quiescence prevent overlapping providers. Collection stores the task and round
receipt with the cards. Replay therefore duplicates neither cards nor refreshes.
Detached turns retain history mailboxes and their captured read scope through
restart until settlement. Checkpointed recovery keeps stages. Completed stages
survive for continuations until the sweeper removes them. Saved off-base sessions
reopen fresh on base. Their visible history remains, but their native memory is
lost. Model and effort changes retain the native session. Provider changes reopen
it under ordinary profile compatibility.

Uploads and references use ordinary attachment admission and a per-draft
`attachment_scope_id`. They are staged as immutable readable inputs locally or
remotely. The shared renderer separates uploads from references. It includes the
ordinary graph read scope and named branches in the master and continuation deltas.

Cards apply only against their source text. Settlement checks the live anchors.
Changed text gets up to two refresh turns in the same session. After that, the
card becomes outdated and offers Refresh or Reject. Refresh preserves the master,
sends changed paths as a delta, and saves an immutable prompt for each round.
Default mode waits for the member to Accept or Reject. Apply-directly mode applies
each fresh card at turn end. Failures remain reviewable without blocking other cards.

Apply rechecks the source text and runs once per card. Its durable receipt holds
the prepared operation, inverse, and committed result. The receipt replays before
precondition checks. A refused Accept or Undo drops preparation so retry prepares
again against live text. Guarded Undo requires the changed regions to still hold
the coach's text but tolerates edits elsewhere. Undo all reverses a turn's applied
cards. Undoing deletion retains CRDT identity so cached editors do not duplicate
restored text. Pending cards and active turns retain base revisions. Pending and
outdated binary cards retain both blobs through backup and restore. Card batches
read live paper only to check pending anchors.
[Change models](../../src/rcp/paper/changes.py) own anchor and receipt shapes.

The saved mode normalizes the request's apply-directly choice. Rewrite Compare
fetches the full proposed text through the member-only plain-text attachment
route with `nosniff`. From RCP attaches all saved paper or files selected by stable
id. The coach shares chat's composer, transcript, attachments, comments, and
editable agent picker. Provider changes announce a fresh session. History and
New chat sit at the end of Paper's view-switch row without adding a phone row.
The coach has no direct paper, Patch, approval, or graph-promotion authority.
[Containment](providers-and-containment.md) remains fixed regardless of transcripts
or skills.

## Answer and artifacts are independent

The labelled Markdown assistant message is the answer. Failures in discovery,
validation, expiry, rendering, SSH, or Download never change the verdict for the
answer, task, Patch, or graph. Discovery visits direct regular children of the
exact turn artifact directory in filename order. It enforces the count,
per-file, and total-byte caps in `limits.py` before reading. It ignores nested
files, symlinks, provider paths and directives, and prose URLs. Empty or oversized
files get no card. Durable omitted counts and discovery failures expose no paths.
They survive even when there are no cards or answer. In-place edits to old files
use versioning.

RCP captures bytes with origin task ids across chat, Experiments, workers, retry,
and recovery. Viewing therefore needs no live stage. Bounded background import
preserves the origin and supplier for both stage-only and repository-kept outputs.
Temporary expiry uses the stage's last-touch time. Repository-kept originals never
expire. Unresolved candidates import independently with full retention. Errors
distinguish retryable backoff from terminal missing, expired, or invalid sources.
Import cannot replace artifacts. Maintenance and shutdown drain active passes.

HTML follows [invariant 10e](../../AGENTS.md#stable-invariants). It uses an opaque
sandbox with no RCP access or navigation, popups, forms, downloads, or ordinary
network resource APIs. Sanitized trusted HTTP(S) clicks may relay privately.
Child self-navigation can still send data, so there is no zero-network promise.
Browser-granted turns may use `rcp-artifact-preview <dir> --port 0` on the host.
It uses the production sanitizer and CSP, reports removed attributes, and scopes
access to the exact artifact folder. It refuses symlinks, uses authored fallback
colors, and applies the launcher's idle timeout. Other turns use logic checks.

| Type | Presentation |
| --- | --- |
| HTML | It opens in the sandboxed viewer without a thumbnail. |
| Raster or SVG | It opens in the viewer and as an inline picture. |
| Video | It uses the native player in chat and the viewer, with whole-file comments. |
| Markdown | It renders without scripts. Raw HTML is visible, links are inert, and remote images are disabled. |
| Text, data, code, or notebook | It displays as escaped numbered text. Valid JSON and notebooks are formatted. |
| PDF | It uses the desktop system viewer or browser Download. |
| Other | It offers only Download and Keep. Recognition never changes retroactively. |

[Byte classification](../../src/rcp/artifacts.py) owns the recognized formats.
Play downloads video into a blob URL. This avoids WebKit's Range probes against
the whole-file byte route. Leaving aborts the download and revokes the URL.
Bounded text prefixes are labelled, while Download returns the full bytes.
Broken previews retain Download, Keep, and the reply. Backend offers to view,
open, download, keep, or discuss are independent. `can_open` means the RCP viewer,
not system PDF opening. Keep refreshes the task projection.

## Live HTML pages

One JSON script with `id="rcp-live"` declares needs through
[LiveTag](../../src/rcp/live_artifacts.py). Reports explicitly forbid live data.
Needs can identify helper keys, target nodes, the artifact's episode, or files
and folders on the execution host. Read roots are registered repositories and
ready conversation or episode worktrees. Stages and artifact folders are never
read roots. Paths alone grant nothing.

Folder patterns have bounded literal or fnmatch segments. Absolute, recursive,
parent, empty, and dot segments are forbidden. Descriptor walks reject symlinks
and nonregular files. They rediscover sorted matches on each read. Empty matches
succeed. Missing or unreadable folders fail. Bounded files, bytes, and rows, along
with per-file errors, preserve partial success. Remote folders use one SSH call
that runs the same reader. Models and limits own the exact pattern, row, and byte caps.

Each artifact version resolves its needs once, including edited versions. Job
keys bind to durable receipts and recovery lineage. Invalid declarations remain
static with stored reasons. Bindings, diagnostics, and final snapshots belong to
the version, so Undo cannot retarget them. Authenticated reads accept only artifact
and version ids. They recheck membership and target, then use the stored bounded
bindings. Episode budgets remain invocations. Visible viewers poll more slowly
over SSH and relay data one-way into opaque HTML.

Server reconciliation captures final data after all watched jobs and episodes
end. Failures use bounded backoff and attempts, and exhausted errors are retained.
Expired artifacts that were not kept are skipped. Invalid ownership, invalid
lineage, and history-only sources terminate reconciliation. Reads occur outside
the artifact lock. Saving rechecks state inside it. Only complete final snapshots
become immutable and survive source loss. Pages that need only nodes, graphs,
files, or folders never finalize. Snapshot bytes follow the version's backup,
transfer, and retention rules.
**Why:** the wrapper supplies declared data without giving HTML a credential or
executing agent code outside a launch. Child navigation still prevents a
zero-network promise.

### Graph views

A parameterless graph need returns the bound target's lean `GraphSnapshot`, never
node bodies. Schema bounds truncate titles with flags, never ids. Page code
filters and draws the snapshot. There is no query language. Only one graph need
is allowed, and it cannot accompany job or episode needs.

Snapshots are full, unchanged, or errors. They carry an opaque target head that
restart or restore invalidates. The shell sends its last complete head. An
unchanged response omits the graph. Graph and message caps also apply after shell
cache reconstruction. If that result is too large, the shell drops the head and
requests a full read. Reads over the cap retain the last complete graph as stale
and continue polling. Nodes and edges are never silently omitted. Oversized
immutable final snapshots or identities produce static errors.

`rcp-open-node` crosses only exact-child frame hops and accepts only ids in the
last delivered graph. Pinned views open trusted RCP detail. Other contexts ignore
it, and HTML gets no reply. Personal HTML pins appear after Files and Paper only
on their selected main or branch target. They are bounded per target and always
show the current version. Pin atomically keeps the artifact. Unpin removes only
the pin. Transfer omits pins. Viewer target selection defaults to the origin and
requires explicit Pin. It opens an existing pin and never changes the live binding.
**Why:** custom views inherit the member's graph access and trusted node UI, not
another request channel. Personal pins for each target can retain useful branch
views. Revision heads do not reconstruct change history.

## Episode reports

Endings other than Stop use [visual wrap-up](conversations-episodes-and-watchers.md#visual-wrap-up)
to write the exact file `episode-report.html`. RCP validates and captures bounded,
immutable bytes. The lifecycle binds the first version permanently without storing
HTML. Viewing cannot resettle the episode. The official skill asks for visual
retrospectives, rather than runtime aesthetic scoring. Experiment reports cover
the method, configuration, attempts, observations, Evidence, failures, limits, and
next step. Auto-research adds belief movement, Decisions, delegation, and handback.
Reports have no mutation, watcher, command, or approval channel. They cannot decide
verdicts. Generation failure is visible and nonblocking. The viewer shows the
current version and Download without repository copies.

## Artifacts panel

Artifacts inventories kept outputs and undeleted reports across all history,
including archives. Temporary outputs remain in chats until kept. Auto-research
child Experiment reports also need Keep. Chain reports summarize them. Revisits
paint the cache first. Rows group by branch, with the branch having the newest
output first. Device folds are shared with From RCP. Archived branches sit in a
closed Archived fold. A single branch omits its header but retains row tags.

Each branch is one raised box without row rules. It shows the five newest outputs
and Show more. Rows lead with the title. The author, verified Source chat, and
node link form one quiet line. Clicking a row opens it. Copy reference, Download,
and Delete sit on the right as icons. Stored identities, times, and paths are
data rather than repetitive labels. Captured HTML metadata supplies titles
without rereading the artifact or changing its digest or outcome. A missing title
appears as Report. One origin resolver verifies the transcript and exact target
for the inventory and viewer, including edits and children. Orchestrator outputs
link to Runs. Unverifiable origins retain viewing without source links.
Unsupported files download. Desktop PDFs can open in the system viewer.

## Unified artifact viewer

Result HTML is an ordinary origin artifact. It is available in its chat or run
and, after Keep, in Artifacts. It never appears in unrelated chats. Stored
result-view rows migrate, and old URLs redirect. All viewable content uses the
shared panel. Its chrome is the sole action bar. The same-origin shell owns
notices, selection, comments, and Send while HTML stays opaque. Repository
previews are script-free. PDF and download handling follow the file type.

Visible panels poll even static artifacts for others' edits and Undo. Disposal
aborts polling. Editing stays until the server clears it. Permanent HTTP failures
stop automatic reads until Retry. Transient failures keep polling. Versions and
Undo reload frames. The HTML doctype stays before policy and bootstrap code.
An absent doctype preserves quirks mode. Uncaught errors and rejections produce
bounded plain text for the current channel. They do not hide content, live data,
or Download. Panel notices sit beneath the frame. Inline errors use a red edge,
an icon, and a tooltip. The first error appears immediately. Later summaries are
rate-limited. Reloading or changing versions clears them. Ask to fix prefills a
comment for the human to send.

### Artifacts inside a reply

Markdown images and path links resolve registered artifacts in any turn folder
of that chat. Resolution uses that turn's descriptors or the citing edit's new
or forked version. Episode-folder report paths resolve only for publishing turns,
including deletion history. Paths from another chat and unregistered paths never
navigate. Older turns fetch the exact task before declaring an artifact absent.
They retain it across list refreshes. HTML, images, video, Markdown, and text embed
with Comment, Expand, Keep, and Download actions. Surrounding links are dropped.
Unembedded video uses the same player. PDF and unknown embeds point to cards.
`/viewer` is the explicit shell. `/preview` is its compatibility alias, including
old image byte requests. `/content` renders raw sandboxed content and is not
another entrance.

Inline content uses the shared shell without chrome, a transparent background,
the reply's color scheme, and the `--rcp-*` palette before CSS loads. Theme changes
reload it. Expand and Download use authored fallback colors. Markdown and text
run no script. The fixed embed height persists through loading, refreshes, and
version changes. Content scrolls internally, and images are centered. The caption
stays on one row until an action, avoiding chat motion. Frames mount near the
reader in the chat scroll box. They refresh when turns settle, the version or
visibility changes, and on a slow live interval. Live relay is one-way.

Comment mode defaults off to preserve interaction. Readers can select with a
mouse or pen. Touch readers use Expand for whole-artifact comments. Add stages
one artifact and version in the composer. Admission refuses moved versions.
Cancel, Escape, a new selection, or leaving comment mode closes the window.
Version changes clear stale regions with a notice. Composer comments follow the
chat's mode and profile. They can combine attachments and references, including
in Experiments. Read-only lineage children allow only Discuss comments without
references. Both paths can publish tracked next versions. Keep follows viewer state.

### Selection and comments

HTML, raster images, and SVG support gestures immediately in the panel and by
opt-in inline. Other viewable types allow whole-file comments. PDFs and types
available only for download refuse comments. Text selection highlights text.
Dragging on figures or blank space selects a boxed region. Add saves a comment
in the tray.
Edit now sends only the comment being written without changing the tray. Cancel
or Escape discards it. Clicking elsewhere closes the window but retains the text
for the next selection. The tray offers Remove, a whole-file comment, and Send to
original chat for all filed comments. These are prompt inputs, never graph annotations.

Browser-profile storage retains filed and unsent comments per artifact. Both
send paths retain refused comments with a reason. A refused Edit now files the
comment in the tray or returns it to the box if the tray is full. Success removes
only sent comments and reports the operation. Fresh-session admission is labelled
where the send resumes the origin. Ordinary Send and inline comments need a
readable artifact and a chat that accepts human turns. The origin master and
session gate Edit now. A later failure remains in `edit_failure` until replaced.
The theme follows the app or the standalone preference.

`ArtifactContextRequest` in [service models](../../src/rcp/service.py) owns bounded
selection inputs. [Comment requests](../../src/rcp/api/artifacts.py) own sends.
HTML areas identify elements by CSS path, label, text, and relative position.
Image areas use displayed fractions corrected for orientation. Raster crops
decode once from the staged version and survive recovery. Animation uses the
first frame and labels it. SVG and oversized images carry only positions. Legacy
viewer areas are never cropped. Both routes number human comments and regions
identically. Edit now adds only the immediate request. Human bubbles name the
artifact and selected text, elements, or image area. Editable context provides a
URL-encoded embed of the tracked path. All input is untrusted.

### Editing and versions

Admission freezes the version and route. Composer comments are ordinary turns
with that chat's mode, profile, session, scope, graph, compute, and worktree.
Viewer Send uses the same Work path with the latest profile when the chat accepts
human turns. This includes ordinary chats, Experiments, and ended orchestrators.
Edit now resumes the recorded master. Send also resumes that master for a running
orchestrator or read-only child. A chat or child master uses Discuss with the
origin profile. An Experiment or orchestrator master uses a scratch-only edit
that revokes graph, watcher, master, and repository rights.
[Edit admission](../../src/rcp/runs/artifact_edit_admission.py) owns routing.
**Why:** the recorded master defines session compatibility. After a revoking edit,
the next operational turn must reopen its master. Versions need no Accept because
Undo is reversible and never overwrites human repository files.

Admission atomically reserves the session and stage across owners. A busy owner
refuses the edit rather than queuing or steering it. Edits spend no episode
invocation. They do not change health, Stop, or operational membership. The durable
origin routes replies. Chat and child replies go to chat. Experiment replies go
to node chat. Orchestrator replies go to Runs as delivered mail with a labelled
answer. There is no new-chat routing, current-view routing, or implicit master reopening.

Artifacts track their origin-stage path and last recorded digest, using the first
version for legacy artifacts. Before turns, unchanged tracked files synchronize
to current versions after Undo or peer edits. Locally changed or missing files
remain as they are. A new comment restores its selected version even over missing
text or text from a failed turn. Retrying or resuming the same comment keeps changes.
It restores current bytes only when the file is missing. Different stages get a
turn-relative copy that publishes at settlement but is not tracked later. Read-only
crops remain inputs. Question answers need no extant artifact. Other tracked-file
errors warn and defer. Only failure of the commented file refuses that comment.

Settlement digests session files. Remote settlement uses one SSH call. Under the
artifact lock, it publishes changed bytes against the retained version matching the
recorded digest. A moved or pruned base forks into a new artifact and tracked
file with an id prefix. Invalid types, sizes, or reads produce failures with
reason codes. Ordinary absence produces nothing. A commented file that is
unchanged or missing records that outcome. Reads stop beyond the cap. Every
outcome updates the digest, including invalid markers, so later synchronization
restores rejected or forked files. Replay retains receipts and already-published
versions. Outcomes project as published, unchanged, failed, or forked, adapting
older receipts. Replies state when publication did not occur. Artifacts deleted
during a turn drop edits without an outcome. Publication failure leaves bytes for
later settlement and cannot fail the answer. Failed turns retain their stage
without publishing. Other new files remain ordinary outputs.

Undo goes back one retained version and preserves the original. There is no redo.
Undo may run during an edit while active bases remain retained. Publish, Undo,
Keep, expiry, and pruning share a lock. Versions remain outside provider scratch.
Missing or history-only origins report unavailability. A new session and scratch
in the same thread require both an identified resumability failure and an explicit
fresh-session flag. They do not retain old authority. Viewer offers use the
durable origin, master, and reservations without SSH or hash probes. POST performs
full admission and integrity checks.

### Shape boundary

RCP defines no chart vocabulary or data encoding. Agents draw bounded HTML.
Specialist viewers retain domain computation graphs, spatial fields and meshes,
traces, and other formats. See [current exclusions](../design.md).

## Keeping an artifact

Keep stops expiry without writing to the canonical repository. SQLite owns the
metadata. Immutable digest files for each artifact write atomically in RCP data.
Original and recent versions are bounded. Publication checks the base and
idempotency under the shared lock. Capture guards delay pruning and unlinking.
They do not delay reads, creation, Keep, or publication that does not prune.
Backup uses a typed SQLite inventory. Transfer reads metadata, versions, and files
from one transaction. Report migration writes bytes before binding them and
removing inline HTML. Keep, publication, and Undo create no graph authority,
Proposal, or revision.
**Why:** shared RCP storage supplies version history without managed files beside
human code. Artifacts intentionally do not travel with repository clones.

### Deleting an artifact

Any member may Delete with second-click confirmation. Delete and expiry share
removal under the artifact lock and capture guard. They clear all versions, pins,
and kept chat bindings. They leave an import tombstone: `missing` for expiry or
`deleted` for Delete. A deleted identity never recreates through import, discovery,
replay, or fork. Repeated Delete succeeds. Queued, running, or pausing readers and
editors refuse deletion. So do publishing episode reports, unreviewed consolidation
reports, and pending legacy imports. These refusals use the same route and offer
rule. Backup and transfer file copying refuse Delete, while expiry waits. Paused
or interrupted edits hold nothing, and retry refuses after deletion. Report rows
retain lineage with a deletion marker in the same transaction. Older Keep copies
in the state workspace remain. Delete only unbinds them.
**Why:** tombstones prevent import resurrection. Cleared bindings keep the backup
inventory coherent.

## Repository-file previews

Absolute host paths with an optional line resolve against configured repository
roots. Exactly one match opens escaped, numbered, script-free text that only RCP
can frame. Remote reads use that repository's SSH host without local retention.
Missing, ambiguous, or nested-root matches produce a nonnavigating error. So do
unavailable hosts and files that are nonregular or nontext. RCP never guesses the
host or selects the longest root. Large files show bounded windows. They preserve
the cited line by dropping leading context first. Without a line, they show the
beginning. Real line numbers, whole-byte size, and truncation labels identify
partial reads. Registered turn-output citations open artifacts or focus download
cards. Unregistered paths fail. Prompts render this same contract.

## Temporary chat inputs versus outputs

Inputs are claimed by a turn and never offered for later download. Project
artifact and paper references are immutable admission copies. Their sources
remain unchanged. Outputs come only from the exact turn directory. Keep changes
retention, not type or authority. Neither inputs nor outputs are canonical provenance.

## Glossary presentation

[Typed graph operations](graph-history-and-transitions.md#typed-graph-operations)
own glossary authoring. Best-effort definitions of whole terms appear inline in
node prose, answers, and Proposal cards. There is no separate destination or human editor.
