# Paper, artifacts, and viewing

This specification owns the shared paper, the writing coach, artifacts,
versioned reports, content interaction, storage, and repository-file previews.
[Interface design](interface-and-visual-design.md#artifact-viewer) owns panel layout;
[API projections](api-web-and-desktop-projections.md#artifact-viewer-and-run-inventory)
owns viewer state and run inventory payloads.

## Shared paper

The paper is a file tree of Markdown, LaTeX, Typst, BibTeX, figures and any
other files, written by the project's members. It is non-authoritative: the
graph and research rendering remain separate writing inputs, and no paper text
becomes graph state. There is no main file; each preview starts from the file
being read.

Every text file is one shared live document. Members edit it at once and their
edits merge; there is no per-person draft and no edit is refused for being
concurrent. The RCP process that owns the project holds the live documents: the
team server for a team project, the member's own RCP for a personal one, even
when its state root is on an SSH host.

**Saved and published are separate.** An edit is saved once it, the new paper
revision, and the intent to publish commit in one SQLite transaction; only then
is it broadcast and acknowledged, and only an acknowledgement shows the editor
as saved. Edits made while disconnected wait in the browser until a server
acknowledges them. Publication writes the saved tree to the state root's
`paper/` some seconds after editing pauses, and at least every few minutes
during continuous editing, as one staged tree that replaces the previous one in
a single step, so a rename and the edit that follows it never appear apart. An
unreachable state root leaves publication pending; saving continues. Failed
publication retries after a backoff. Paper is never committed to Git. Candidate document reconstruction
and SQLite work run on workers under the project lock; the owner loop installs
and broadcasts only after commit, including when a caller is cancelled. The
project snapshot exposes `paper` as a boolean badge: true means saved revisions
still need publication, false means they are published.

**Why:** a paper co-written live must not lose an acknowledged keystroke to a
crash, and a half-published tree breaks every build that includes the missing
half.

The editor refuses a local edit that would push its file past `max_doc_bytes`
and flashes a red edge, leaving the text editable. An edit the server refuses,
such as an invalid one, ends that file's connection for good and the editor
shows a red edge with one retry action; an edit refused only by the update rate retries on its own. Deletion,
maintenance, and transfer pause the live paper; paused admission closes with
retryable socket code 4503, while lost membership or a deleted file closes with
4403. Refused connections are accepted before closing so browsers receive the
code. A stop that fails, or maintenance that ends, resumes it. The backup SQLite
snapshot holds all saved Paper state and marks it unpublished, so a restore
republishes the saved text rather than trusting the copied folder. An imported
project's saved Paper publishes once the transfer activates it, without a visit.

Files keep a stable identity across renames. A new or uploaded file opens at
once; uploads go to the chosen folder, and an upload over an existing path asks
first. When a member deletes a file another member has open with unsent text,
that member's editor keeps it read-only and offers to recreate it as a new file;
with nothing unsent the editor moves to another file. A hidden file deleted that
way stays reachable from one red icon while it holds unsent text, also after a
reload, until Recreate moves the text to a new file. A backend
restart pauses every document without unmounting the editor, so typing carries
on; text refused by the server, or held when access is lost, can be downloaded,
and a close before the server answered (such as a room limit) never discards it.
While the open file has unsent text, leaving the page asks first. One path rule serves editing,
backup and transfer; `.git` and `.publish` are reserved path components. Uploaded figures are stored once by
content hash. Paper files are served as files, never as same-origin documents:
SVG, HTML and unknown types download; only raster images show inline.

A database with no Paper for a project adopts the tree already published in its
state root, every file and folder, so a fresh install pointed at existing state
never publishes over it. On a local state root its revisions continue after the
published one; an SSH mirror omits that marker, so publication waits until the
new revisions pass it.
A project that still holds a pre-tree paper keeps it as `introduction.md`. When
its old draft had diverged from the saved introduction, both are kept, the draft
becomes the live text, and nothing publishes until a member picks which to keep.
The text not picked stays in History.

History records a version after each burst of editing and on request. Deleting a
file, replacing its uploaded content, or restoring History first checkpoints the
current revision if no version covers it. Restore and text uploads replace only
the changed span, preserving peer positions in unchanged text; identical content
creates no edit. History rows include the member display name when available,
and the tree advertises the per-document byte limit as `max_doc_bytes`. Automatic
versions form a bounded ring; named versions have a quota, and a full quota
refuses new names rather than evicting old ones. A history byte budget filled by named
versions refuses only new names; edits keep saving. Members may delete named versions
through `DELETE /api/projects/{project_id}/paper/history/{version_id}` to free quota;
automatic versions refuse deletion with 409. A version is restored per file
as a new live edit. Revision timing rows are pruned once both publication and
the newest retained version cover them; unpublished or unversioned edits keep
their timing records.

### Preview

Markdown renders in the browser, with image paths resolved inside the paper
tree. A link to another paper file opens it in the editor, an `http(s)` link
opens a new tab, and any other link is inert. Large Markdown files re-render only
after typing pauses. Typst compiles in the browser from compiler and font files that the RCP
backend downloads once, checks against pinned hashes, and serves from its own
origin. LaTeX compiles on the RCP backend with Tectonic, downloaded and checked
the same way on first use, run with `--untrusted` and bounded in time, output and
concurrency. A compile changes no state, so any member may start one; a newer
compile cancels an older one for the same project, and earlier PDFs stay
readable until the result limit ages them out. A
compile starts from the one root that reaches the open file; with none or
several, the preview lists the candidates, and the chosen one is sent as the
compile root, accepted only while it still reaches the open file. The PDF keeps
its scroll position per
compile root across file switches, and Typst errors show as path, line and
message.

Tectonic runs as the RCP server user with no operating-system sandbox.
`--untrusted` turns off shell escape only, so `\input` of an absolute path
can print any file that user can read into the preview PDF, including files
that agent launches hide. This is accepted: members are one trusted lab, the
PDF goes only to members, and no agent can read the preview.

## Writing coach

The coach reads the paper, graph and research rendering, flags unsupported
claims, asks questions, and may change the paper: edit, create, rename or delete
any paper file. It reads a read-only snapshot of the saved paper taken when its
turn starts, and writes whole files into its own changes folder
(`changes/files/…`, with renames and deletions in `changes/moves.json`). RCP
collects that folder alone, validates all of it or none of it, and turns it into
change cards by diffing against the snapshot; the coach never writes diffs or
line numbers. Validation applies the live paper's own limits to the whole
proposed tree. A rename and the edits that depend on it form one card group.

A card applies only to the text it was made from. At the end of a turn RCP
checks every card against the live paper; a card whose text has since changed is
refreshed by up to two short follow-up turns of the same coach session, then
shown as outdated with refresh and reject actions. These follow-ups retain the delivered
master, send changed snapshot/output paths as a delta, and record a separate
immutable prompt for each refresh round.

By default each card waits for a member to accept or reject it. In a chat set to
apply directly, every fresh card is applied when the turn ends, and the cards
become a log. Either way, applying re-checks the original text, happens once per
card, and records a receipt that a guarded Undo reverses while the regions it
changed still hold what the coach wrote; edits elsewhere in the file keep it. Undo all reverses every accepted or applied card of a turn. A card that fails to apply stays reviewable; in a chat set
to apply directly, the other cards still apply. The same durable receipt holds the prepared operation,
its inverse, and the core's committed result; replay returns that result before
checking preconditions again. Outstanding cards and active coach turns keep their
base revisions available even beyond the ordinary snapshot retention window.
Pending and outdated binary cards retain both original and proposed blobs through
backup and restore. Card lists load payloads together and read the live paper only
when pending cards need their anchors checked. Each mapped text anchor exposes
`current` with live UTF-16 start/end offsets, its 1-based start `line`, revision,
and anchored text.
Delete receipts also retain the text document state so Undo restores its CRDT
identity; reconnecting a cached editor cannot duplicate the restored text.

The coach request's `paper_apply_directly` boolean is normalized into its saved
`mode`. Rewrite Compare fetches the full proposed text from
`GET /api/projects/{project_id}/paper/changes/{change_id}/proposed`; this
membership-protected response is plain text, served as an attachment with
`nosniff`. From RCP can attach the whole saved paper or any file by stable id.

The coach cannot write the paper directly, emit a Patch, approve Proposals, or
promote paper text to graph state. Prior transcripts and skills cannot widen its
capability or public-web contract; see [provider containment](providers-and-containment.md).

## Answer and artifacts are independent

The labelled assistant message is the Markdown answer. Artifact discovery,
validation, expiry, rendering, SSH, or Download failure cannot change the answer,
task/Patch verdict, or graph.

Discovery reads direct regular children of the exact turn artifact directory,
in filename order: at most eight files, 16 MiB each, 32 MiB total. Size is checked
before reading. Nested files, symlinks, provider paths/directives, and prose URLs
are ignored. Empty/over-bound files get no card; durable omission counts and
`discovery_failed` survive even without cards or answer, never exposing paths.
Earlier files edited in place follow [Editing and versions](#editing-and-versions).

Bytes are captured into RCP storage with task identifiers for chat, Experiment,
child Work, retries, and recovery. Viewing/Download needs no live stage/session.
Bounded background import covers stage-only/repository-kept outputs, preserving
origin and supplier. Temporary imports expire from stage last-touch; kept files
remain in the repository without expiry. Unresolved candidates import separately
with a full retention period. Durable errors distinguish retryable backoff from
terminal missing/expired/invalid sources. Import never replaces existing artifacts;
maintenance/shutdown drains active passes.

HTML uses the opaque sandbox required by
[AGENTS invariant 10e](../../AGENTS.md#stable-invariants). Inline scripts cannot
reach/navigate RCP, open popups, submit forms, download, or use ordinary network
resource APIs. Trusted clicks on sanitized HTTP(S) references may relay through
a wrapper-private channel. Child-frame self-navigation can still send data;
there is no zero-network promise.

Browser-granted turns can run `rcp-artifact-preview <dir> --port 0` on their host.
It uses the production sanitizer/CSP, logs removed loading attributes, and serves
only the granted turn workspace's artifact folder, refusing symlinks/other paths.
It sets no palette, showing Expand/download fallback colors. The launcher supplies
`ARTIFACT_PREVIEW_IDLE_SECONDS`; inactivity ends it. Other turns use logic checks.

Type recognition fixes each artifact's presentation:

| Type | Presentation |
| --- | --- |
| HTML | Sandboxed viewer, no card thumbnail. |
| Raster/SVG | Viewer and small inline picture. |
| MP4/M4V, MOV, WebM | Native video in chat and the viewer; whole-file comments. |
| Markdown | Script-free rendered HTML; raw HTML visible, links inert, images not fetched. |
| Text/data/code/notebook | Escaped numbered text; valid JSON/notebooks pretty-printed. |
| PDF | Desktop system viewer; browser Download only. |
| Other/unrecognized bytes | Download and Keep only, even if bytes/support later change. |

Video downloads on Play, then uses a blob URL in the native player. The byte
route returns the whole file without Range support: WebKit refuses full-body 200
responses to media range probes, and blobs avoid those probes within the 16 MiB
artifact cap. Leaving the viewer aborts downloads and releases the blob.

Text/Markdown previews label bounded prefixes; Download returns full bytes.
A broken preview never hides Download/Keep or the reply. Keep refreshes the task
projection. `view` and independent open/download/keep/discuss offers are backend
answers; `can_open` always means an RCP viewer, not system PDF handling.

## Live HTML pages

Live HTML watches changing job, sweep, episode, node, graph, or file data. Reports are
static: their creation/import owner records `live_data_allowed: false`, enforced
by shared code. One JSON script `id="rcp-live"` declares version 1 and `needs`;
`rcp.live_artifacts` owns schemas and `rcp-live-data` messages.

Needs identify helper launch keys, target node ids, the artifact's own episode,
or absolute execution-host files/folders. File sources select tail/whole and
jsonl/csv/text. Readable roots are prompt-listed registered repositories and the
artifact's ready conversation/episode worktree, never task stages or artifact
folders. Paths alone grant nothing.

A folder need names absolute `dir` and relative segmented `pattern`. Up to eight
literal/fnmatch segments allow `*`, `?`, `[...]`, but no absolute pattern, `**`,
parent/empty/dot segments. Descriptor-based walks refuse symlinks and nonregular
files. Each read rediscovers sorted matches, including new files. Empty matches
succeed; missing/unreadable folders fail. File entries carry relative path, rows,
truncation/error. Folder caps are 128 files and 1 MiB total, plus per-file/row caps;
one file's failure does not hide others. One SSH call handles each remote folder;
local and remote execute the same reader.

Each version resolves needs once, including edits. Job keys bind stable receipt/
recovery lineage; invalid declarations stay static with stored reasons. Bindings,
diagnostics, and final snapshots belong to that version, so Undo cannot retarget it.
Authenticated reads take only artifact/version ids, recheck membership/target,
and use stored bindings with bounded regular-file readers (`limits.py`). Episode
budget values remain invocations.

The visible viewer polls (slower for SSH) and relays one-way into opaque HTML.
Server reconciliation independently captures final data once all watched jobs/
episodes end. Failures retry with bounded backoff/attempts, preserving exhausted
errors. Expired unkept artifacts skip; invalid ownership/lineage/history-only
sources terminate. Reads happen outside the artifact lock; save rechecks under it.
Only complete snapshots become immutable and survive source loss. Node/graph/file/
folder-only pages never finalize. Snapshot bytes share version backup/transfer/retention.

### Graph views

A `graph` need takes no parameters and delivers a lean projection of the
artifact's bound graph target: each node's id, type, title, standing, and status,
and each edge's source, relation, and target. Titles are cut to a schema limit
with a flag; ids never are. Bodies are never included. The page's own script
filters and draws it, so no query language exists. A declaration has at most one
`graph` need and cannot combine it with `job` or `episode` needs.

Each graph snapshot is full, unchanged, or an error. It carries an opaque head
for its target that also changes across restart and restore. The shell sends the
head of the last complete graph it cached, and the server omits graph data when
it is unchanged. Byte caps in `limits.py` bound the graph and the whole message;
the shell re-measures a delivery rebuilt from its cache and, if it would exceed
the message cap, drops the head so a full read decides.
Over a cap, the read is an error: the shell keeps its last complete graph, marks
it stale, and keeps polling until the graph fits again. Nodes and edges are never
silently dropped. An oversized saved final, or declared identities that alone
exceed the message cap, can never shrink, so that error is static.

A page may post `rcp-open-node` with a node id. Each frame hop accepts it only
from its exact child, and the shell forwards only ids in the last delivered
graph. A pinned view opens RCP's own node panel; elsewhere it is ignored. The
page never receives a reply.

A member may pin an HTML artifact into the Artifacts switch, after Files | Paper,
for main or an existing branch in the project. Pins are personal, shown only
while that target is selected, bounded by `RESEARCH_VIEW_PIN_LIMIT` per target,
and always show the current version. Pinning keeps
the artifact in the same transaction; unpinning deletes only the pin. Pins are
not carried by cross-space transfer. The viewer's compact branch picker defaults
to the origin task's bound target; choosing a target does not pin until Pin is
clicked. If already pinned on the selected target, the control opens that pin.
The artifact's live-data binding remains unchanged.

**Why:** members can keep a useful branch artifact accessible after merging its
branch, while personal pins and their limits remain separate for each target.

**Why:** RCP owns every graph read, so a custom view gains no data path the
member lacks and no new request channel. Opening a node uses trusted UI, so the
page cannot style or intercept protected content. Change history is not part of
this projection; a revision head cannot reconstruct what changed.

**Why:** The authenticated wrapper can relay declared data without giving agent
HTML an RCP credential. Refresh never executes agent code outside a launch;
the producing code shapes its own data. Relayed data can still leave through
child-frame navigation.

## Episode reports

Non-Stop endings use the hidden [visual wrap-up](conversations-episodes-and-watchers.md#visual-wrap-up).
The provider writes exact `episode-report.html`; RCP validates/captures bounded
immutable bytes into an Artifact. The lifecycle binds its first version permanently
and stores no HTML. Viewing never resettles an episode. The official report skill
requires a visual retrospective; runtime validation does not score aesthetics.

Experiment reports cover objective, method/configuration, attempts, observations,
Evidence, failures, limits, and pause/next step. Auto-research adds belief movement,
Decisions, delegation, and handback. Reports have no graph, watcher, command, or
Proposal channel and cannot decide verdicts. Generation failure stays visible and
nonblocking. The panel shows current version and Download, without repository copies.

## Artifacts panel

Artifacts lists kept outputs and undeleted reports across history, including
archives, independent of task/episode windows. Temporary outputs stay in their
chats until kept. Reports appear when captured, except an Auto-research child
Experiment's report, which offers Keep like a temporary output and is listed once
kept. Revisits paint cached lists first.

**Why:** a chain can start many child Experiments, and listing every child report
buried the outputs a member chose to keep; the chain's own report summarizes them.

Rows group under their branch, the branch with the newest output first; each
header folds, and the device remembers which branches are folded. Archived
branches sit together in one Archived fold after the live ones, closed until the
member opens it; the server marks each row's branch as archived. With one branch
there is no header and each row keeps its branch tag. The composer's From RCP
picker groups its Reports and Artifacts folders the same way, sharing the folds.

Each branch is one raised box holding its rows, with no rules between them; an
archived branch is the same full-width box under the Archived fold. A branch
lists its five newest rows, then one Show more control for the rest. A row leads with the title; one quiet line
below holds the author, a Source chat link when verified, and a node title link
when the displayed graph holds the origin node. Clicking the row opens it. Copy
reference, Download, and Delete are icon buttons at the right. Stored
identity/time/path remain data, not repetitive row labels.

Report titles come from captured/imported HTML metadata without rereading bytes;
missing titles use Report. Metadata never changes the immutable digest/outcome.
One resolver gives inventory and viewer the same exact-target source link after
checking transcript and origin, including children/edits. Orchestrator outputs
link to Runs. Unverifiable origins have no source link but remain viewable.
Unsupported files offer Download; desktop PDFs also offer system opening.

## Unified artifact viewer

Custom result HTML is an ordinary artifact, available in its origin chat, producing
run, and after Keep in Artifacts, never unrelated chats. Stored result-view rows
migrate to artifacts; old URLs redirect.

All viewable content uses the shared panel. Panel chrome is the one action bar:
title, version, live state, Keep, Undo, Source chat, Delete, dock, and close. The
embedded shell owns notices, selection, comments, and Send. No guidance line repeats these paths.
The shell is same-origin-frameable; agent HTML remains opaque. Repository previews
are script-free; PDFs/download-only formats follow the type table.

HTML doctype stays before policy/bootstrap; a missing doctype retains browser
quirks behavior. Uncaught errors/rejections produce a bounded plain-text notice
beneath the panel frame, or inside a reply a red edge and one icon action whose
title holds the summary, without hiding content or blocking live data or Download. First error appears immediately, later summaries
at most once a second, only from the current private channel. Reload/version
change clears it. Ask to fix prefills a comment for the human to send.

### Artifacts inside a reply

Markdown image syntax embeds a registered artifact by its absolute file path in
any turn folder of the same chat; a cited link opens it the same way. A turn
folder is named by its turn, so a path resolves through that turn's descriptors,
or through the citing turn's when it published a new version of that file. An
episode report's folder is named by its episode, so only a turn that published a
version of the report resolves it, even after a member deletes it. When the edit
forked because the artifact moved past the version it started from, the turn's
fork answers to the path it cites. A path into another chat's turn resolves to
nothing. HTML, images, video, Markdown, and text run/render in the inline viewer;
the card becomes Comment/Expand/Keep/Download actions beneath it. Surrounding
links are dropped. Unembedded videos show the same player below the reply; other
artifacts keep cards. PDFs/download-only embeds point to their cards. Unknown
names report unavailable without navigating. Old turns fetch their exact task
before declaring absence and retain it across list refreshes.

Inline uses the same shell without chrome, with transparent background. Every
frame pins the reply color-scheme; content receives `--rcp-*` palette before its
CSS. Theme/mode changes reload it. Only inline supplies palette; Expand/download
use authored fallbacks. Markdown/text run no script. Every embed holds one box of
`limits.py`'s fixed height from mount, while its turn loads, and through refreshes
and versions; taller content scrolls inside it, and an image is centered in it.
Its caption stays one row until the reader acts (comment mode, a failed Keep), so
an embed loading never moves the chat. Frames mount within 800px of the reader in
the chat's own scroll box and refresh on settled turns, viewer version changes,
page visibility, and a slow live interval. Live data remains one-way.

Comment mode starts off, preserving chart/game input. Mouse/pen readers can enable
selection; touch-only readers use Expand for whole-artifact comments. Selection
opens the chat comment window; Add comment stages one artifact's comments and
version in the composer. Admission rejects a moved version. Cancel/Escape/new
selection or leaving mode closes the window. Version changes also clear old
staged regions with a notice. Keep follows viewer state.

Composer comments use that chat's Discuss/Work mode and execution profile,
Experiment chats included, and share a turn with attachments and project
references. Read-only Auto-research children compose comments only, in Discuss,
without references. Both can
publish the same artifact's next version through its tracked file.

`/viewer` is the explicit shell; `/preview` remains its compatibility alias.
An explicit legacy image request there still receives PNG/SVG bytes. `/content`
is raw sandboxed rendering, never a separate viewer entrance. These routes serve
both turn artifacts and reports; [run inventory](api-web-and-desktop-projections.md#artifact-viewer-and-run-inventory)
supplies Runs cards.

### Selection and comments

HTML/raster/SVG support gestures (on immediately in panel, opt-in inline).
Other viewable types accept whole-artifact comments; PDF/download-only refuse.
Each comment is text plus optional selection. Starting on text highlights it;
figure/blank-space drags select area. The floating window offers Add comment or
Edit now, which sends only the comment being written and leaves the tray as it is.
Cancel/Escape discards it; a plain click elsewhere in the artifact only closes it,
and its text returns with the next selection. A corner
count icon opens the tray with Remove, whole-artifact comment, and Send to original
chat, which sends every filed comment as a Work turn there. These are prompt inputs,
never graph annotations.

The shell saves filed comments/unsent text per artifact in the browser profile.
Both send paths post a comment list, with `edit_now` for immediate editing. A refusal
keeps the comments with the reason (a refused Edit now files its comment in the tray,
or returns it to the comment box when the tray is full);
success removes only the comments it sent and reports the operation to the parent.
Explicit fresh-session admission is labelled on Edit now, and on Send only where it
too resumes the origin. Send, and inline comment mode in a chat, need only a readable
artifact in a chat that takes human turns; the origin's master and session gate only
Edit now (`can_edit_now`). A later edit failure comes from `edit_failure` until replaced.
Theme follows the containing app, or remembered appearance when standalone.

Selections carry bounded text/context. HTML boxes name up to eight elements by
CSS path, own/chart label, text, and relative position within a containing element.
Image boxes use orientation-correct displayed fractions. Raster crops are decoded
once from the staged version and retained for recovery; animation uses its first
frame and says so. SVG/over-pixel-limit images carry positions only. Legacy viewer-
area boxes keep sampled text and are never cropped. Turns allow 50 annotations.
Both routes number human `Comment N:` text and prompt regions identically;
`edit_now` adds only the immediate-edit request. The chat route joins each human
turn's selections to its message, so the bubble names the artifact and quotes what
each numbered comment was drawn on: the text, the boxed elements' names, or a plain
area for an image. The editable item also gives the
URL-encoded Markdown embed of its tracked path, so a reply that changed the file in
place can show it there. All input is bounded/untrusted.

### Editing and versions

Admission captures artifact/version. A composer comment must belong to that chat
and is an ordinary turn of it: its `artifact_context` names the artifact and the
version it was drawn on, and nothing else binds it, so it follows the chat's mode,
profile, session, write scope, group context, graph admission, compute, and worktree
exactly as a plain follow-up does. A viewer Send is that same composer turn in Work, on the chat's latest
profile, whenever the artifact's chat takes human turns (ordinary and Experiment
chats, and an ended chain's orchestrator conversation). Edit now, and a Send whose
origin is a running orchestrator or a read-only child, resume the recorded master: chat/child masters get Discuss with the origin
profile; Experiment/orchestrator masters get a revoking scratch-only edit, without
graph, watcher, master, or repository authority. Admission freezes this choice.

**Why:** The recorded master defines which turns its session understands. A
revoking edit avoids substituting an incompatible chat contract; the next
operational turn reopens its own master. RCP-owned versions need no Accept step:
publication is reversible through Undo and cannot overwrite a human repository file.

Admission atomically reserves session/stage across launch owners. Busy returns
409; comments are not queued/steered. Edit tasks consume no episode invocation or
health/Stop state; provenance remains for display/replies, not operational membership.
Completion never clears the need to reopen a revoked master.

Durable origin decides replies: chat/child outputs use their chat, Experiment
outputs use its node chat, orchestrator outputs use Runs. Orchestrator comments
are already-delivered mail with the labelled answer as reply. No new chat or
current-view routing is involved.

Each turn artifact tracks `turns/<turn>/artifacts/<name>` on its origin stage and
the digest last recorded there (legacy records use the first version). Session
artifacts are live outputs sharing stage host/root. Correcting/enriching an old
file in place makes a version; the agent decides when to create a new file.
Before a turn, files still matching the recorded digest are rewritten to current
versions, reflecting Undo/other edits. Changed files remain; missing files skip.

A new comment restores its selected version at the tracked path, even over failed-
turn changes or absence. Retrying or resuming that same comment preserves changes;
if missing, it restores current bytes, not a possibly pruned base. A different stage
receives only the commented artifact at a turn-relative path and publishes at turn
end; no later session tracks that copy. A question answer is an ordinary turn with
no comment, admitted whether or not the artifact still exists. Read-only selection
crops remain alongside turn inputs. Turn start never refuses a turn over another
tracked file: one it cannot check or read is a warning event and waits for a later
turn; only the commented file's own failure refuses its comment.

Settlement digests session files (one SSH call remotely). Changed bytes publish
against the retained version matching their recorded digest, under the artifact
lock. If current moved or that base was pruned, bytes fork into a separate artifact
with an id-prefixed name and its own tracked file. Type changes, oversize, or
unreadable files produce reason-coded failure; ordinary missing files produce
nothing. Commented unchanged/missing files record `unchanged`/`missing`.
Digest reads stop one byte beyond the limit. Every outcome updates the tracked
digest, including invalid-file markers, so next start restores rejected/forked
files to current content.

Replay keeps receipts and recovers an already-published operation's version.
`result.artifact_edit_outcomes` lists published/unchanged/failed/forked per artifact,
including projected older single receipts. Replies state non-published outcomes;
published versions show themselves. An edit to an artifact a member deleted during
the turn is dropped without an outcome. Their later cards accept comments and tracked-
file citations. Unreachable publication never fails the answer: bytes remain for
a later settlement. Failed turns retain stages without publishing; other new files
are ordinary outputs. Version storage stays outside provider-writable scratch.

Undo moves back one retained version, with no redo and original preserved. It may
run during an edit; active base versions stay retained. Publication, Undo, Keep,
expiry, and pruning share the artifact lock. History-only/missing session/stage
origins report unavailability. Only an identified resumability failure plus an
explicit fresh-session flag permits a new session/scratch in the same reply thread,
without restoring old authority. Viewer offers use durable origin/master/reservations
without SSH/hash work; POST still performs full admission/integrity checks.

### Shape boundary

RCP defines no chart vocabulary/data encoding. Agents can draw bounded HTML for
series, tables, distributions, matrices, projections, grids, and structured diffs.
Specialist viewers retain computation graphs, spatial fields/meshes, traces, and
other domain formats.

## Keeping an artifact

Keep stops expiry and refreshes the task, without writing the state repository.
SQLite owns metadata; digest-addressed immutable files live per artifact under
RCP data, written atomically. Original plus bounded recent versions obey
`limits.py`. Publication checks base and operation idempotency under the shared
lock. Capture guards delay unlinking until captures finish, not ordinary reads,
creation, Keep, or non-pruning publication. Backup uses the SQLite snapshot's typed
inventory; transfer derives metadata/versions/files from one read transaction.
Report migration writes digest files before bindings and removal of inline HTML.

**Why:** RCP storage gives every format version history without adding managed
files beside human code. Team storage is already shared; artifacts intentionally
do not travel with repository clones. Keep, publication, and Undo grant no graph
authority, create no Proposal, and spend no graph revision.

### Deleting an artifact

Any project member may delete an artifact from the viewer or the Artifacts list,
with a second click to confirm. Delete and expiry share one removal under the
artifact lock and capture guard. It removes every version and every member's pin,
clears kept bindings on chat results, and leaves an import tombstone: `missing`
after expiry, `deleted` after Delete. Nothing recreates a `deleted` identity; the
importer skips it and `create_artifact` refuses it, so neither turn discovery nor
a replayed fork restores it. Deleting it again succeeds.

Delete is the human's call. It refuses only while a queued, running, or pausing
turn reads or edits the artifact, an episode report is still publishing, a
consolidation report awaits review, or a legacy import is pending; `can_delete`
and the route apply that same rule. It also refuses, rather than waits, while a
backup or transfer is copying artifact files; expiry waits for the copy instead. Unlike expiry, a paused or interrupted edit holds nothing;
its retry is refused once the artifact is gone. Deleting an episode report also
marks its report row deleted, in the same transaction, and keeps the row as lineage
([episode reports](conversations-episodes-and-watchers.md#visual-wrap-up)). A file
an older Keep copied into the state workspace stays; Delete only unbinds it.

**Why:** without the tombstone, import restores a deleted chat output from its
stage; without clearing kept bindings, backup inventory refuses the project.

## Repository-file previews

Absolute host-path citations, optionally with a line number, resolve against
configured project repository roots. Exactly one match opens escaped, numbered,
script-free text in the panel, frameable only by RCP. Remote reads use that
repository's SSH host on demand without local retention. No/ambiguous/nested-root
match, unavailable host, or nonregular/nontext file gives a nonnavigating error;
RCP never guesses a host or chooses the longest root.

Large files show a bounded window around the cited line, retaining that line by
dropping leading context first. Without a line, show the beginning. Real line
numbers, whole byte size, and window/truncation labels distinguish partial reads.
Turn-written citations resolve registered artifacts (or focus download-only cards);
unregistered names fail without navigation. Task prompts state this same contract.

## Temporary chat inputs versus outputs

Inputs are claimed for one turn and never offered for later download. Project
artifact/paper references become read-only admission-time copies without changing
the stored source. Outputs are discovered only in the exact turn directory;
Keep changes their retention, not type or authority. Neither is canonical provenance.

## Glossary presentation

Graph-writing agents use `upsert_glossary` for thin project-wide definitions.
Best-effort whole-term definitions appear inline in node prose, answers, and
Proposal cards. There is no standalone Glossary or human glossary editor.
