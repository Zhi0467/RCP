# Interface and visual design

This file owns Web layout and visual grammar. The
[API spec](api-web-and-desktop-projections.md) owns projections and shell behavior;
[paper and artifacts](paper-artifacts-and-result-views.md) owns content interaction.

## Visual grammar

**Classic** borrows Margin Dev's paper, sheet, walnut, and oxblood grammar:
warm rules/shadows, compatible typography, and tactile book materials. Covers
share an oxblood base and differ only by restrained bookcloth/paper texture,
never decorative per-card colors, loud dye, mosaic, or simulated wood.
Semantic accents identify type or state. RCP keeps its own architecture.

**Aqua**, the default, uses pearl-gray/dark-slate surfaces, rounded controls,
shallow bevels, and blue highlights. Upper-left light gives raised controls a
bright top edge and lower shadow; fields and selected/pressed controls are inset.
Project tiles are plain raised surfaces. Clickable Overview rows, Runs headers,
space Runs rows, and available artifacts rise and press inward; expanded detail
and unavailable artifacts do not. Artifact preview targets span the row, with
Source chat a separate target. Type, status, warning, and focus remain distinct.

Display places Mode (System/Light/Dark) above Theme (Classic/Aqua). They are
independent, remembered per origin, applied before paint, and shared across
projects. System follows OS appearance. Defaults are Aqua/System; legacy mode
choices retain their mode in Aqua, and Soft Aqua becomes Aqua/Light.
The unified logo contains RCP once, without a redundant initial tile.

## No commentary lines

Do not put muted explanatory subtitles beneath buttons, titles, labels, or
headings. Wording, hierarchy, shape, color, motion, and control state carry meaning.
Errors, conflicts, required warnings, and accessibility labels stay explicit.
A failure recoverable with one action uses a visual mark and one icon-only action,
not a stacked box/caption: an unsent turn has a red edge and top-right Resend.
More detail belongs in an explicit read-only inspector.
Chat and coaching have no sample prompts, slogans, instructional empty states,
or textarea placeholders. Empty conversations are empty.

## Phone layout

At phone width, pages/panels meet screen edges and text wraps without horizontal
page panning. Code, tables, graphs, and other wide content scroll in their own
containers. Screen-height surfaces use `dvh`; tall dialogs scroll to their last
control without moving the page. Open artifacts fill the screen. Pinch zoom
magnifies the page without resizing RCP windows. Verify at 390px.

## Project shell

No wordmark, logo, or revision label sits beside the project name. Group labelled
Sync/Ask together and icon-only History/Refresh together. Agent tasks are
icon-only and accessible. Inbox carries a colored count; DAG is under Research. Research has no page
heading: one View picker at the end of the tab strip switches between the
drill-down and the DAG and sets which records show; on phones it sits in the ⋯
menu. The DAG toolbar's type and layer filter is its Lens menu. View and the
branch picker stay pinned to the strip's right edge; tabs that overflow scroll
under them.
Paper is under Artifacts' Files | Paper switch, retaining route `paper` and an
unsynced badge on Artifacts. The member's pinned pages follow Paper in that
switch; on phones it scrolls within its own width, pin titles truncate, and
unpin is its own icon button. Destination order ends Terminals, Agents, Settings.

At widths <=560px, one top bar holds Back, project name (opens the project dock),
Sync only for staged changes, and a labelled-row ⋯ menu. Bottom tabs show Overview,
Inbox, Research, Runs, and More; More is active for its destinations. Ask floats
above the bar except in Agents. Status banners and Overview revision use one
line. Provider/compute notices expand from a summary while keeping failures
visible; Inbox counts share a row.

Glossary definitions are inline, without a destination. Cached projects paint
immediately and reconcile in the background; mutation availability and cache
boundaries follow [revision observation](api-web-and-desktop-projections.md#revision-observation-and-drafts).

## Projections

Research is a drill-down, one level per screen, with a trail back up. Question
cards show the question and its motivation (text that only restates the title
is skipped), and count only what is one relation
away: sub-questions and Hypotheses, plus undecided Decisions and open Blockers
on retained legacy question links. Transitive work does not inflate these
one-hop counts. Hypothesis cards show the statement, then three rows in page
order: inputs (with attached or Experiment-governing undecided Decisions and
open Experiment Blockers), Experiments (with active ones), and Evidence (with
the verdict tally). A question page lists sub-questions, Hypotheses, and work
that tests no Hypothesis. A Hypothesis page puts its Experiments in the middle, one collapsed
Inputs box before them, and one Evidence box after each; boxes and the verdict
tally open as their own trail step, never a box stacked under the row. At phone
width each Experiment keeps its Evidence box beside it. Records no question
reaches stay explicit. Research flow places types in the order defined by the
[Research projection](api-web-and-desktop-projections.md#research).

Runs has no page title. Needs Action and In progress are unfolded, mixed-mode,
reverse-chronological lists. Only runs stopped pending human action belong in
Needs Action. Completed folds Experiment loop, then Auto-research. The owning
Experiment/Auto-research name leads; start time is secondary, without “Episode”.
Groups name the mode once. Cards have no muted recommendation/report commentary.
Backend controls select the current Experiment episode and its section/health;
older episodes remain in History. Seed/Refresh, chat, and coaching are not Runs
rows; Blockers stay in Inbox.

Run opens episode detail in Runs. Stop loop is the loop action; task-level
Pause/Resume/Retry remain in the task inspector.

## Nodes and node detail

Nodes use ordinary language and enough context to stand alone. Relation rows
open focused one-hop DAGs. Node detail floats, resizes, retains project-scoped
size across close/minimize, remains reachable after viewport changes, and closes
on entry to Agents.

Literal prose edits stage in the project draft and reset draft standing to
asserted; they do not start chat. Artifact selections are not graph editing
or Evidence shortcuts.

Open or addressing Evidence flags mark their current main-graph nodes with a red
left edge in graph and research cards. Node detail adds one icon-only flag action
that opens the Inbox. A flag on retired or absent Evidence keeps its row, titled from
the recorded node or id, with only Open chat, Open source, and Dismiss as they apply;
it marks no node.

## Canvas editing

The DAG canvas is the only graph-editing surface; there are no side forms. Every
gesture is one human-draft update, so one undo step (⌘Z / ⇧⌘Z, session-only,
cleared by Sync or a rebasing reconcile). A node-detail edit is one step too:
its provisional standing reset folds into Done or Cancel. Backend preview
validates the batch; Sync commits.

- Create: double-click empty canvas, `+ Node`, N, or long-press places a staged
  card with a title field and type dots. The id, the type's main text, and an
  explicit `unknown` origin derive from the title; refine them in node detail.
  An unknown origin is a red card edge with one icon action. Custom types with
  required fields add one field row.
- Link: drag a card's handle. The wire starts at the handle, eligible cards and
  handles light up, and the wire snaps to an eligible handle nearby. One valid
  relation stages at once; several open one inline menu. A relation that needs an
  Evidence assessment or accepts an expectation opens a details step in that
  menu: assessment with its qualifications, expectation, and an explanation
  (required for matched or diverged). Picking a link's current relation opens the same step to edit its
  details; unchanged details stage nothing. Dropping on empty canvas creates a
  linked card.
  Clicking the handle (or L) enters pick mode for touch and keyboard.
- Links are selectable. One chip shows the relation and Unlink; the relation
  opens in place, never as a second box.
- Merge: hold a card over one of the same type, or select two and press M.
  The kept node receives copies of the duplicate's links and the duplicate is
  removed, in one draft update.
- Delete and rename work on the selection. Node removal removes incident edges,
  not history. Accepted-node, staged-edit, and active-Experiment guards refuse
  by shaking or disabling, never by caption.
- Edits never refit or recenter the view; new cards stay pinned where placed.

**Why:** forms detached editing from the graph and made every structural edit a
multi-field round trip. One update per gesture keeps preview, undo, and Sync
on the existing draft path.

## Evidence to check

The Inbox uses the consolidation load for an **Evidence to check** section and
includes its rows in the open total. Each row shows the kind, night, current
Evidence title, and finding. **Address** starts the backend-authorized node Work
chat and keeps the member where they are. When two or more findings offer Address,
the header's **Address all** starts one shared Work chat for the ten oldest.
A failed batch marks that control with a red edge and one icon-only retry.
While the turn runs each row collapses to
one strip: a spinner, the kind, the title, and an icon-only **Open chat**. A
turn that finishes the work removes the row. Any other end brings the full row back
with a red edge, an icon-only retry in place of Address, and **Open chat**.
Otherwise the row stays until its icon-only **Dismiss** action succeeds.
**Open Evidence** opens node detail. **Open source** opens a URL source in a new
tab, or a repository alias or absolute path in the existing file preview, and is
offered only for a URL or a path that resolves to a configured repository file.
The section and its count appear on every graph target. On a branch, rows read
their Evidence from the branch's graph, every open flag offers Address because
main's endpoint decides it, and Open chat switches to main, where the flag's chat
is bound.
A failed action marks the row with a red edge and replaces that action with one
icon-only retry. Controls wrap and retain phone-sized targets at 390px.

## DAG controls

One floating toolbar inside the canvas holds every control: + Node, Undo,
Redo, a Layout menu (layout mode, repulsion, Release all pins, Reset layout), a
View menu (projection, per-type brightness and hiding, brighten/dim-all), Fit,
and full screen. A graph opens dim. Each node's bulb lights that node and its own
edges, never its neighbours; a hidden type hides its edges too. Lit nodes and
hidden types are remembered per project. Dim cards stay opaque above the edges. Menus close on an outside click or Escape; at <=560px the menu buttons
show icons only. Floating edit menus keep screen size at every zoom. Provide
boundary-aware scroll chaining, fullscreen with node detail, and individual pin
release. Repulsion visibly changes spacing;
manual dragging has room beyond auto-layout. Touchpad pinch anchors at its focal
point without turning ordinary two-finger scroll into zoom.

Fit centers horizontally without exceeding authored node size. Tall graphs start
at the top and scroll vertically; short ones center vertically. Added rows do not
shrink columns. Fit preserves pins; returning restores zoom, scroll, and offsets.

## Agent browser controls

Chat Options holds Browser use beside Work in a worktree. Its sliders chip has no
label and counts enabled options. The phone menu spans the composer in a
screen-height chat; outside click, Escape, or changing chat closes it. New chats
start off. Loading/saving follows server preferences; failed saves retain the
server value and offer Check again. Coaching has no Browser switch.

Experiment/Auto-research launch options sit inside the agent picker beneath
provider fields; its chip counts enabled options and matches Start's height.
The Auto-research branch name stays outside. Browser use has no caption.
Continuations retain their grant. Unavailable/lost access appears on the affected
turn/timeline with reason, server detail, and applicable machine fix; unknown
codes retain detail. Granted/unrequested access adds no notice.

Machine Browser rows load independently. Missing browser offers Install;
missing/old Node or npm asks for Node 20+ and npm. Missing libraries show Copy
command. `linger_disabled` offers Allow background processes; refusal shows the
administrator command. Install disables repeats while running; failure leaves
Check again. See [browser API consumers](api-web-and-desktop-projections.md#agent-browser-web-consumers).

## Conversation composer

Discuss is plum; Work is forest. Shift+Tab toggles in an idle focused composer.
Every turn retains its mode, including resumed tasks. The Agents header has title,
then agent picker, New session, and repository scope. The picker names provider,
model, and effort; runtime stays in the inspector. No extra metadata/mode row.
The list has search and a fold icon, not a title band; a hairline divides chat.

Cards have a fixed size, one-line title, and secondary metadata. Their menu
removes local unsent drafts or renames/pins/archives saved chats. Rename/archive
are shared display choices; pins are member-owned. Blank names restore derived
names; rename is inline. Archived counts include unloaded chats and offer Restore.
Revisits retain the loaded set until refresh, preventing archived-card flashes.
Use shipped provider logos (Claude orange, others monochrome), otherwise labels.
Display changes never alter transcripts/tasks.

One wide readable column shows human requests as quiet paper cards and unboxed
assistant prose. Inspectable progress folds behind Activity; failure/recovery
stays explicit. The composer is a contained writing surface. List groups, in order:

- New reply: successful and unread.
- Failed: latest turn failed.
- Stopped: paused/interrupted, with the same resume path.
- Working: queued/running/pausing.
- Done: read success, unsent draft, or unloaded turns.

Pinned leads only All, newest pin first, with per-row status; filters/counts ignore
pins. Group headings carry colored state/counts, rows no redundant state dots.
Unread cards have tint, accent edge, bold title, and New; unread failed/stopped
cards keep their group. Offered Retry/Resume stays visible; working rows show
phase/elapsed time. Search requires every word to match loaded card/turn content.
Filters are All/Working. A failed/stopped conversation has a backend-labelled
recovery banner above it, except a plain Retry: that sits as one icon-only
action on the failed turn's error row. Only a turn with no later turn offers it;
artifact edits and conversation turns are compared only among themselves.

Agents shows the selected graph, plus unarchived listed branch orchestrators on
main. Orchestrators open in place without linking branch ids to main nodes.
No graph filter or branch/role tags; user tags remain. Active-lineage children
show Working/Done, with attention on their orchestrator. Posting and target
ownership follow [Agents](api-web-and-desktop-projections.md#agents).

Entry opens a board unless a link selects a chat; the board icon returns there.
Leaving Agents resets it. Columns are Needs you (New reply/Failed/Stopped),
Working, Done, Archived. Cards show logo/spinner, title, provider/model/effort/task
type, state/age, and offered recovery. Click opens chat with list folded and
composer focused. Mouse dragging reorders within columns in per-project browser
storage, with pins first. Dropping into/out of Archived archives/restores; other
state moves and working-agent archive are refused. Branch episode cards open Runs
and cannot move. Touch scrolls; menus archive. Alt+Up/Down reorders focused cards.
Columns become two at <=920px and one at <=560px.

At <=560px, the chat list starts behind an Agents disclosure and closes on selection.
Wider layouts retain resizable-list preferences; viewport changes never overwrite
them. Only code, tables, and display math scroll sideways in transcripts; paths
and URLs wrap. Fenced blocks have icon-only exact-text Copy, with 44px phone targets.

`/` or `$` selects packages: arrows highlight, Enter chooses instead of sending,
Escape dismisses. Settings owns defaults; selections apply to one turn. Fresh
conversations use Settings defaults; existing sessions retain their last profile.
Seed/Refresh has launch controls; Raw truth inputs choose context.

The + menu holds Upload file, From RCP, and configured Compute connections with
execution-machine reachability. Selection does not change provider or `run_on`,
restores from the latest persisted turn, survives Send, and drops removed settings.
Compute Settings takes names, optional SSH targets, and nonsecret hints, never
keys/passwords. Its matrix distinguishes reachability/auth/host-key failures and
names the machine needing repair. Explicit Probe runs checks; routine refresh
uses cache. Unsaved edits mask results, disable Probe with Save first, and are
not stored in settings drafts. Saves fence older in-flight probe responses.

Machine tiles show name, host, provider/job status, and writable-path count.
One card opens at a time below them. Dashed Add machine opens other space machines;
selection derives an alias and opens it. Setup shares these tiles and New machine.
Cards group provider paths, Long-running jobs, and shared writable paths. Folder
pickers have breadcrumbs, one-level/filter/paging, protected entries, and Use this
folder. Use Slurm opts into scheduler submission; Jobs root sets helper storage;
Reset compute removes the block through Save. No scheduler resource form exists.

Hidden folders sits beside writable paths: code defaults cannot be removed;
user additions use the folder picker and save immediately for all projects.
Show Enforced/Unhidden and reason, never imply enforcement without a projection.
Remote status says Checked at launch without probing. Gaps appear here/doctor,
not as per-turn badges. API validation/status belongs to
[machine hidden folders](api-web-and-desktop-projections.md#machine-hidden-folders).
Job-route readiness uses compute labels/tones; saving changed config checks it,
editing masks it, and Runs offers fixes/Check again.

External jobs show one watcher row with log, observation, checks, diagnostics,
and cancellation receipt. Completion is not scientific success; Cancel requested
is not proof of stopping. Stop watching does not cancel. Backend offers decide
Cancel independently of graph read-only state; watcher refresh owns observation.
Actions sit at the right. Hide removes completed watcher rows/cards only for this
browser/project; Show hidden watchers restores them, and renewed activity overrides
hiding. Records and other viewers are unaffected.

Work replies show the recorded turn outcome as a neutral chip beside the graph
receipt, including replies without a graph update: **Done**, **Partial · reason**,
**Couldn't do it · reason**, or **No outcome stated**. Missing outcome metadata on
non-Work turns produces no chip. Outcome chips never use failure coloring and
wrap long reasons within the reply width.

## Terminals

Destination eligibility and probe behavior follow the
[terminal API](api-web-and-desktop-projections.md#member-terminal-api). Settings has
no terminal control. Repository controls show starting paths and unavailable
reasons. Pending remote probes refresh; explicit Refresh retries probes.
A left rail lists sessions with Live/Idle, selection, and End. The right pane
holds the shell. Running Work appears above it and on the rail without blocking
terminal use. Theme changes repaint using project tokens.

Leaving detaches without ending the shell; return lists it again. End, lost
membership, or expiry closes it. SSH loss shows a diagnostic, never a reconnect
that silently starts another shell. Cooperative sessions visibly warn that
canonical-state protection is unavailable, using semantic warning surface/border/
color in every theme/mode, not muted microcopy. Setup failures stay explicit;
mirrored launch failure offers no cooperative downgrade. Trust rules belong to
[Providers and containment](providers-and-containment.md#member-terminals).

## Paper

The editor/coach split resizes. Authored content leads without a canonical-file
banner. Write/Preview share one pane and the chat Markdown renderer, so unsaved
text can be previewed without creating another document.

## Auto-research and episode history

Auto-research starts in the project header beside Ask. Its exact invocation budget
has observed cost beside it; hidden report allocation stays outside that meter.
Report lifecycle belongs to [episode reports](paper-artifacts-and-result-views.md#episode-reports).

History is one read-only SVG roster/timeline. Fixed labels group People,
Orchestrator/Experiment agent, Workers, and Experiments/Watchers by row key.
Date/time rows sit above colored turn/attempt/report blocks. Episodes have light
rails, failed worker attempts red slivers, Stop a square, and live runs a now line.
No vertical grid or commentary captions.

Solid lines connect recorded starts with clickable handoff icons. Message arrows
run from envelopes to delivery; failed delivery has a red ring, undelivered a
dashed cross. Notice diamonds/watcher dots open payloads. Wake signals dash to
turn starts; harvested/acknowledged signals dot to landing time. Sources and Stop
attribution use recorded provenance; delivery never means read.

One popover shows selected text (with loading/error), signal, or actor/span detail.
Orchestrator turns show cause, incoming work, actions, and headline. Turns/attempts
link to the inspector, children to Experiment detail; reports do not open task
inspection. Close/Escape/outside click dismiss. Selection outlines/highlights
related items and dims others. No extra panel/table sits below the chart.
Counts explicitly identify truncation.

Pinch/Ctrl/Cmd-wheel zooms at cursor; buttons/double-click zoom, Shift-double-click
zooms out. Drag/sideways/Shift-scroll pan; vertical scroll moves the page. Whole run
and First 90 min presets have a ten-minute minimum window. Drag never selects;
overlapping numbers hide. Keyboard focus, reduced motion, theme tokens, and
container scrolling at narrow widths apply. Watcher/task controls keep their owners.

## Artifact viewer

One shell-mounted panel survives loading/identity branches and closes on project
change. All artifact/report/repository entrances use it. It starts right-docked,
full-height; left-edge drag resizes and title drag floats. Title double-click or
focused Enter/Space toggles fullscreen and restores placement. Dock moves it beside
node windows; restoring docks it. Settings/setup hide it until project return.
No mode-button row. Origin-scoped size/placement stays reachable after resizing.

Inline artifacts share reply width/palette, with no frame, a loading shimmer,
and a quiet name/version/live-state/action caption. Comment mode adds a dashed
selection outline. Content and interaction follow
[artifacts inside a reply](paper-artifacts-and-result-views.md#artifacts-inside-a-reply).

The title holds name, live state, version, offered Undo/reply link, dock, and close.
Editing persists until cleared by the server. Visible panels poll even static
artifacts for other members' edits/Undo; disposal aborts. Permanent HTTP errors
stop automatic reads until Retry; transient failures keep polling. Version changes
and Undo reload the frame. Errors remain explicit.

The same-origin shell owns comments and availability; the panel never reads opaque
agent HTML. Selection alone sends/stages nothing. Repository previews are script-free.
PDFs open via desktop system viewer or browser Download. Runs preserves server
order (report first, then outputs), with kind/time/worker labels. Timeline popovers
filter that list to the exact operation; all previews use this panel.
