# Interface and visual design

This file owns Web layout and visual grammar. The
[API spec](api-web-and-desktop-projections.md) owns projections and shell behavior.
[Paper and artifacts](paper-artifacts-and-result-views.md) owns content interaction.

## Visual grammar

**Classic** borrows Margin Dev's paper, sheet, walnut, and oxblood grammar.
It uses warm rules and shadows, compatible typography, and tactile book materials.
Covers share an oxblood base. They differ only through restrained bookcloth or
paper textures. They never use decorative per-card colors, loud dye, mosaic,
or simulated wood. Semantic accents identify type or state.
RCP keeps its own architecture.

**Aqua**, the default, uses pearl-gray or dark-slate surfaces, rounded controls,
shallow bevels, and blue highlights. Upper-left light gives raised controls a
bright top edge and lower shadow. Fields and selected or pressed controls are inset.
Project tiles are plain raised surfaces. Clickable Overview rows, Runs headers,
space Runs rows, and available artifacts rise and press inward.
Expanded detail and unavailable artifacts do not.
Artifact preview targets span the row. Source chat is a separate target.
Type, status, warning, and focus remain distinct.

Display places Mode (System, Light, or Dark) above Theme (Classic or Aqua).
The settings are independent. They are remembered per origin, applied before
paint, and shared across projects. System follows the OS appearance.
The defaults are the Aqua theme and System mode.
Legacy mode choices retain their mode in Aqua. Soft Aqua becomes Aqua in Light mode.
The unified logo contains RCP once, without a redundant initial tile.

Every theme sets interface and display text in Figtree and code in JetBrains
Mono. Both are pinned packages bundled into the web build, so the desktop app
needs no font install.

## No commentary lines

Avoid unnecessary secondary labels and descriptions. Let the layout do the
explaining. Do not put muted explanatory subtitles beneath buttons, titles,
labels, or headings. Wording, hierarchy, shape, color, motion, and control state
carry meaning. A note that is truly needed appears on hover (a tooltip), not as
text on the page.
Errors, conflicts, required warnings, and accessibility labels stay explicit.
A failure recoverable with one action uses a visual mark and one icon-only action.
It does not use a stacked box or caption. An unsent turn has a red edge and
Resend at the top right.
More detail belongs in an explicit read-only inspector.
Chat and coaching have no sample prompts, slogans, instructional empty states,
or textarea placeholders. Empty conversations are empty.

## Smooth motion

State changes are smooth, never choppy. Loading, polling, and switching keep
their layout: no content jumps, no flicker between states, and no row that
resizes when its data arrives. Transitions are short and honor reduced motion.

## Phone layout

At phone width, pages and panels meet the screen edges.
Text wraps without horizontal page panning. Code, tables, graphs, and other wide
content scroll in their own containers. Screen-height surfaces use `dvh`.
Tall dialogs scroll to their last control without moving the page.
Open artifacts fill the screen. Pinch zoom magnifies the page without resizing
RCP windows. Verify at 390px.

## Project shell

No wordmark, logo, or revision label sits beside the project name.
The labelled Sync control sits beside Auto-research.
Group the icon-only History and Refresh controls together.
Agent tasks are icon-only and accessible. Inbox carries a colored count.
The DAG is under Research. Research has no page heading.
One View picker at the end of the tab strip switches between the drill-down and
the DAG. It also sets which records show. On phones, it sits in the ⋯ menu.
The DAG toolbar's type and layer filter is its Lens menu.
View and the branch picker stay pinned to the strip's right edge.
Tabs that overflow scroll under them.
Paper is under Artifacts' Files | Paper switch, retaining route `paper` and an
unsynced badge on Artifacts. The member's pinned pages follow Paper in that switch.
On phones, the switch scrolls within its own width and pin titles truncate.
Unpin has its own icon button. The destination order ends with Terminals, Agents,
and Settings.

At widths <=560px, one top bar holds Back, project name (opens the project dock),
Sync only for staged changes, the branch picker with Retry graph or Retry tasks
when stale, and a labelled-row ⋯ menu. Bottom tabs show Overview,
Inbox, Research, Runs, and More. More is active for its destinations.
Status banners and the Overview
revision use one line. Provider and compute notices expand from a summary while
keeping failures visible. Inbox counts share a row.

Every banner uses the shared `ui/Banner` component.
It has one icon-only Dismiss in its corner and uses one per-device store.
Its key names one occurrence: a release, a sign-out, a failure, or a turn.
A new occurrence shows again. A banner whose key cannot name its
occurrence, such as a compute route failure, forgets its dismissal once the
condition clears.
**Why:** separate banner implementations developed inconsistent dismiss controls.
Some had none, and the gaps recurred.

Glossary definitions are inline, without a destination. Cached projects paint
immediately and reconcile in the background.
Mutation availability and cache boundaries follow
[revision
observation](api-web-and-desktop-projections.md#revision-observation-and-drafts).

## Projections

Research is a drill-down with one level per screen and a trail back up.
Question cards show the question and its motivation.
They skip motivation text that only restates the title.
They count only what is one relation away: sub-questions and Hypotheses, plus
undecided Decisions and open Blockers on retained legacy question links.
Transitive work does not inflate these one-hop counts.

Hypothesis cards show the statement, then three rows in page order.
The inputs row includes attached or Experiment-governing undecided Decisions and
open Experiment Blockers. The Experiments row includes active Experiments.
The Evidence row includes the verdict tally.
A question page lists sub-questions, Hypotheses, and work that tests no Hypothesis.
A Hypothesis page puts its Experiments in the middle, with one collapsed Inputs
box before them and one Evidence box after each Experiment.
The boxes and verdict tally open as their own trail step.
They never stack a box under the row.
At phone width, each Experiment keeps its Evidence box beside it.
Records that no question reaches stay explicit.
Research flow places types in the order defined by the
[Research projection](api-web-and-desktop-projections.md#research).

Runs has no page title. Needs Action and In progress are unfolded lists that mix
modes in reverse chronological order. Only runs stopped pending human action belong
in Needs Action. Completed has folded groups for Experiment loop, then Auto-research.
The owning Experiment or Auto-research name leads each card.
The start time is secondary, without “Episode”. Groups name the mode once.
Collapsed cards have no muted recommendation or report commentary. Expanded
Auto-research detail shows the parent recommendation described by the
[Runs projection](api-web-and-desktop-projections.md#runs).
Backend controls select the current Experiment episode, its section, and its health.
Older episodes remain in History. Seed, Refresh, chat, and coaching are not Runs rows.
Blockers stay in Inbox.

Run opens episode detail in Runs. Stop loop is the loop action.
Task-level Pause, Resume, and Retry are available in the task inspector.
Expanded Auto-research detail also exposes the current task control offered by
the parent projection, including a provider switch when that recovery permits it.

## Nodes and node detail

Nodes use ordinary language and enough context to stand alone. Relation rows
open focused one-hop DAGs. Node detail floats and resizes.
Its size is remembered per project across closing and minimizing.
It remains reachable after viewport changes. It closes on entry to Agents.

Literal prose edits stage in the project draft and reset draft standing to
asserted. They do not start chat. Artifact selections are not graph editing
or Evidence shortcuts.

A [stale assessment](graph-history-and-transitions.md#stale-assessments) has a
red relation pill, and its peer box in node detail has a red left edge. Its DAG
line and label are red. On main, one icon-only Refresh beside the pill opens the
re-judging chat. A failure turns the icon red and puts the error on hover.

Open or addressing Evidence flags mark their current nodes on the selected graph.
The graph and research cards show a red left edge. Node detail adds one icon-only
flag action that opens the Inbox. A flag on retired or absent Evidence keeps its
row, titled from the recorded node or id. It offers only Open chat, Open source,
and Dismiss, as they apply. It marks no node.

## Canvas editing

The DAG canvas is the only graph-editing surface. There are no side forms.
Every gesture makes one human-draft update and one undo step.
Undo (⌘Z) and redo (⇧⌘Z) are session-only.
Sync or a rebasing reconcile clears their history.
A node-detail edit is also one step. Its provisional standing reset folds into
Done or Cancel. Backend preview validates the batch. Sync commits it.

- To create a node, double-click empty canvas, choose `+ Node`, press N, or
  long-press. This places a staged card with a title field and type dots. The
  id, the type's main text, and an
  explicit `unknown` origin derive from the title. Refine them in node detail.
  An unknown origin is a red card edge with one icon action. Custom types with
  required fields add one field row.
- To link nodes, drag a card's handle. The wire starts at the handle.
  Eligible cards and handles light up. The wire snaps to a nearby eligible handle.
  One valid relation stages at once. Several valid relations open one inline menu.
  A relation that needs an Evidence assessment or accepts an expectation opens
  a details step in that menu. It contains the assessment and its
  qualifications, expectation, and an
  explanation. The explanation is required for matched or diverged expectations.
  Picking a link's current relation opens the same step to edit its details.
  Unchanged details stage nothing. Dropping on empty canvas creates a
  linked card.
  Clicking the handle (or L) enters pick mode for touch and keyboard.
- Links are selectable. One chip shows the relation and Unlink.
  The relation opens in place, never as a second box.
- To merge nodes, hold a card over one of the same type, or select two and press M.
  The kept node receives copies of the duplicate's links and the duplicate is
  removed, in one draft update.
- Delete and rename work on the selection. Node removal removes incident edges,
  not history. Accepted-node, staged-edit, and active-Experiment guards refuse
  by shaking or disabling, never by caption.
- Edits never refit or recenter the view. New cards stay pinned where placed.

**Why:** forms detached editing from the graph and made every structural edit a
multi-field round trip. One update per gesture keeps preview, undo, and Sync
on the existing draft path.

## Evidence to check

Inbox includes the selected target's flags in its open count. Rows show kind,
night, current Evidence title, and finding. Address keeps the member in Inbox.
Open chat enters the bound conversation. Address all appears for at least two
addressable findings. Eligibility and settlement belong to
[consolidation](graph-consolidation-and-lessons.md#evidence-to-check).

An addressing row is one strip with a spinner, kind, title, and icon-only Open chat.
Successful completed work removes it. Another ending restores the full row with
a red edge, icon-only retry, and Open chat. Open Evidence enters node detail.
Open source appears only for a URL or a configured repository file, opening a
new tab or existing file preview respectively. Dismiss is icon-only. Failed
actions mark the same row or control red and add one icon-only retry.
Controls wrap with phone-sized targets at 390px.

## DAG controls

One floating toolbar inside the canvas holds every control.
It contains + Node, Undo, Redo, Layout, Lens, Fit, and full screen.
The Layout menu holds layout mode, repulsion, Release all pins, and Reset layout.
The Lens menu holds projection, per-type brightness and hiding, and controls to
brighten or dim all nodes. A graph opens dim. Each node's bulb lights that node
and its own
edges, never its neighbours. A hidden type hides its edges too. Lit nodes and
hidden types are remembered per project. Dim cards stay opaque above the edges.
Menus close on an outside click or Escape.
At <=560px, the menu buttons show icons only.
Floating edit menus keep their screen size at every zoom.
Provide boundary-aware scroll chaining, fullscreen with node detail, and individual pin
release. Repulsion visibly changes spacing.
Manual dragging has room beyond auto-layout. Touchpad pinch anchors at its focal
point without turning ordinary two-finger scroll into zoom.

Fit centers horizontally without exceeding authored node size. Tall graphs start
at the top and scroll vertically. Short ones center vertically. Added rows do not
shrink columns. Fit preserves pins. Returning restores zoom, scroll, and offsets.

## Agent browser controls

Chat Options holds Browser use beside Work in a worktree. Its unlabelled sliders
chip counts enabled options. On phones, the menu spans the screen-height composer.
An outside click, Escape, or a chat change closes it.
Browser use starts off in new chats. Failed saves retain the server value and
offer Check again. Coaching has no Browser switch.

Episode options sit inside the agent picker below the provider fields.
The chip matches Start's height and counts enabled options.
Auto-research's branch name stays outside. Browser use has no caption.
Affected turns show unavailable or lost access with its reason, server detail,
and machine fix. Unknown codes retain the detail.
Granted or unrequested access adds no notice.

Machine Browser rows load independently. A missing browser offers Install.
Missing runtime prerequisites name the fix. Missing libraries offer Copy command.
A linger refusal shows the administrator command. Install prevents repeats.
Failure leaves Check again. Readiness meanings and continuation grants follow
[browser API consumers](api-web-and-desktop-projections.md#agent-browser-web-consumers).

## Conversation composer

Discuss is plum. Work is forest. Shift+Tab toggles an idle focused composer.
Each turn retains its mode. The Agents header has a title, a picker for provider,
model, and effort, a context ring, and one menu. The menu holds Rename, Archive,
New session, Branch this chat, and repository scope.
Runtime information stays in the inspector. The list has search and fold controls
and a hairline divider. It has no title band or row for metadata or mode.

Cards have fixed size, one-line title, and secondary metadata. Their menus remove
unsent drafts or rename, pin, or archive chats. Rename is inline.
A blank name keeps the current name.
Archived counts include unloaded chats and offer Restore.
An archived composer shows only Restore. Revisits retain loaded rows until refresh.
Display ownership and admission follow
[Agents](api-web-and-desktop-projections.md#agents).
Use the shipped provider logos, with Claude in orange and others in monochrome.
Use labels when a logo is unavailable.

Human requests are quiet paper cards in one readable column.
Assistant prose is unboxed. Progress folds behind Activity.
Failures and recovery stay explicit. The composer is a contained writing surface.
The list groups appear in this order: New reply, Failed, Stopped, Working, and Done.
New reply contains unread successes. Stopped contains paused or interrupted turns.
Done contains read successes, unsent drafts, or unloaded turns.
Pins lead only the All view, newest first. Filters and counts ignore pins.
Group headings carry color and counts, without redundant row dots.
Unread cards have a tint, an accent edge, a bold title, and New.
Failed or stopped unread cards retain their group.
Retry or Resume remains visible. Working rows show phase and elapsed time.
Search matches every word against loaded card and turn content.
The filters are All and Working, plus Archived while any chat is archived.

Recovery uses the backend-labelled banner, except plain Retry is one icon action
on the failed turn's error row. Only the latest eligible turn offers recovery.
Artifact edits and conversation turns are compared within their own families.
Agents shows the selected graph plus listed unarchived branch orchestrators on
main. It opens orchestrators in place. There is no graph filter or tags for branch
or role. User tags remain. Children in the active lineage show Working or Done.
Attention belongs to the orchestrator. Posting and ownership follow the API spec.

Entry opens the board unless a link selects a chat or a chat was left open in
this project. Inside Agents, its tab returns to the board. The columns are Needs you, Working, Done, and Archived. Archived starts folded;
folding is per viewer.
Cards show a logo or spinner, title, provider, model, effort, type, state, age,
and offered recovery. Branch cards also show their source chat.
Setup uses the newer of the loaded turns and the chat summary's newest task or
history turn. An artifact edit never replaces it. Draft means no message.
Clicking a card opens chat with the list folded and the composer focused.
Mouse dragging, Alt+Up, or Alt+Down reorders cards within columns, with pins first.
The order is stored per project in the browser.
Cross-column moves only archive or restore eligible chats.
Working chats cannot be archived.
Touch scrolls the board. Menus provide the archive action.
The layout has two columns at <=920px and one at <=560px.

At <=560px, the header is one row of picker, ring, and menu without chat name.
More → Agents opens the board unless a chat was left open. Wider layouts retain
resizable-list preferences independently of viewport changes. Only code, tables,
and display math scroll sideways. Paths and URLs wrap.
Code Copy is icon-only and copies the exact text. It has 44px phone targets.
New chats open at the bottom and follow new content.
Scrolled-up chats retain their exact session-memory position despite later loads.
On touch phones, Return inserts a newline. Only Send sends.

`/` or `$` selects packages. Arrow keys highlight a package, Enter chooses it,
and Escape dismisses the picker. Selections are per turn.
Settings supplies fresh-chat defaults. Sessions retain their profile.
Seed and Refresh have launch controls. Raw truth inputs choose the context.
The + menu holds Upload file and From RCP.
Start episode's picker takes an optional starting message.

Machine tiles show the name, host, provider status, writable-path count, and
job status where a jobs route exists: Slurm on any machine, the helper on the base.
Aliases for one machine account share a tile and card.
One card opens below the tiles. Dashed Add machine selects another space machine,
derives its alias, and opens it. Setup uses the same tiles and New machine control.
Space and Project Settings share the card. Project Settings offers unlink only
when no repository or profile uses the machine.
Each card is a raised box with rounded wells for Compute, provider executables
and limits, writable paths, and hidden folders.
Folder pickers have breadcrumbs, filtering and paging within one level, protected
entries, and Use this folder.

Compute has no launch picker or resource form. Its Slurm and Helper rows show
status and route checks. The Helper row appears only on a machine agents run on.
The remote Helper job folder and nonsecret Access hint
use placeholders, not labels above. Fields save independently. Reach checks
distinguish reachability, authentication, and host-key failures.
They name the machine needing repair. Reads use stored results.
Edits mask those results. Saving runs the checks.
Runs offers fixes and Check again.
Compute policy belongs to [machines](machines-and-compute.md).

Card and server sections use the same fold, initially closed, with + and − controls.
Each has a bold title and short status or count. Actions stay inside.
Browser and Dependencies failures are red.
Team server keeps title, overall badge, refresh, and folded Release, Protected
backup, Restore drill, Execution readiness, and Needs attention.

Hidden folders sits beside writable paths. Immutable defaults and editable
folder-picker additions show Enforced or Unhidden with a reason.
They never imply protection. Remote status says Checked at launch.
Gaps appear here and in doctor, rather than in per-turn badges.
See [hidden-folder
projections](api-web-and-desktop-projections.md#machine-hidden-folders).

External jobs have one watcher row with logs, observations, checks, diagnostics,
and a cancellation receipt. Actions sit on the right.
Cancel requested never means stopped. Stop watching never means cancel.
Backend offers govern availability independently of graph read-only state.
Hide in the browser or project affects completed rows only.
Show hidden watchers restores them. Renewed activity overrides hiding.

Work replies show a neutral Done, Partial · reason, Couldn't do it · reason, or
No outcome stated chip beside the graph receipt, even without graph updates.
Other modes without outcome metadata show no chip.
Long reasons wrap without failure coloring.

## Agent view

The space home switches between Cards and Agent. A personal space remembers the
last tab per member on that browser, defaulting to Agent on the first visit.
Explicit Cards, Agent thread, project, and project-group links take precedence.
The Cards tab opens `#/cards` in every space, so it stays an explicit link.
Agent has a left rail with the member's Space thread first, then each project
thread. Rows show the agent name,
the project or Space, and an unread dot or a needs-you mark. The rail folds like
the Agents conversation list and remembers its fold per device. At phone width
the rail is the list, and a chosen thread fills the screen.

Inside a project, Agents opens the member's own project thread, which leads the
conversation list. The team's board conversations stay in that list. The thread
header holds a schedules list with Cancel and re-enable; the composer holds the
voice button. Board chats and threads
share the agent window transcript, scrolling, artifact rendering, comments, and
composer parts. Project threads support node references, saved artifacts, and
artifact-selection comments. Space threads render their own turn artifacts and
accept files and answer comments; they offer no node references, Keep, or
artifact-selection comments. Both open artifacts in the shared in-app viewer,
including on desktop. Voice and handoff source icons sit inside the human message
bubble. Answer comments join the next message in both.
Approval cards stay where the agent raised them, before later transcript lines,
and show the action with Approve and Decline. Their outcome replaces the need
for a separate member answer bubble. There is no separate Ask or voice launcher.
A turn that starts a fresh session shows a quiet chip alongside its first message,
except for the thread's first turn. Its recorded reason appears only on hover.
Revision history names an attributed member as "<member>, via <agent name>" when
the change came through their thread. The name stays plain text when the view
cannot establish that the viewer owns the private thread.

## Terminals

[Terminal API](api-web-and-desktop-projections.md#member-terminal-api) owns
eligibility, probe, session, and disconnect behavior. Settings has no terminal
control. Repository controls show paths and unavailable reasons.
Pending probes refresh. Explicit Refresh retries them.
The left rail lists Live or Idle sessions, their selection, and End.
The right pane holds the shell. Running Work appears above it
and in the rail without blocking use. Theme changes repaint with project tokens.

SSH loss and setup failures stay explicit. Cooperative sessions visibly warn
that canonical-state protection is unavailable using semantic warning tokens in
every theme. Mirrored launch failure offers no cooperative downgrade. Trust
belongs to [Providers and containment](providers-and-containment.md#member-terminals).

## Paper

The file tree folds beside a live editor and a pane for Preview or Coach.
Files retain their identity across renames. Copy links open that file.
The editor shows durable save coverage separately from publication. Markdown
previews use the
chat renderer. LaTeX and Typst previews show a PDF.
At phone width, Write, Preview, and Coach occupy one pane selected by the segmented
control. Files and History open as panels.

## Auto-research and episode history

Auto-research starts in the project header beside Sync. Its exact invocation budget
has its observed cost beside it. Hidden report allocation stays outside that meter.
Report lifecycle belongs to
[episode reports](paper-artifacts-and-result-views.md#episode-reports).

History is one read-only SVG roster and timeline.
Fixed labels group People, Orchestrator or Experiment agent, Workers, and
Experiments or Watchers by row key. Date and time rows sit above colored blocks
for turns, attempts, and reports. Episodes have light rails.
Failed worker attempts have red slivers. Stop is a square.
Live runs have a now line. There is no vertical grid or commentary caption.

Solid lines connect recorded starts with clickable handoff icons. Message arrows
run from envelopes to delivery. Failed delivery has a red ring.
Undelivered messages have a dashed cross. Notice diamonds and watcher dots open
payloads. Wake signals use dashed lines to turn starts.
Harvested or acknowledged signals use dotted lines to landing time.
Sources and Stop attribution use recorded provenance. Delivery never means read.

One popover shows the selected text, signal, or details for an actor or span.
Text shows loading and error states. Orchestrator turns show the cause, incoming
work, actions, and headline. Turns and attempts link to the inspector.
Children link to Experiment detail. Reports do not open task inspection.
Close, Escape, or an outside click dismisses the popover.
Selection outlines or highlights related items and dims others.
No extra panel or table sits below the chart. Counts explicitly identify truncation.

Pinch, Ctrl-wheel, and Cmd-wheel zoom at the cursor.
Buttons and double-click zoom. Shift-double-click zooms out.
Dragging, sideways scrolling, and Shift-scroll pan the timeline.
Vertical scrolling moves the page. Whole run and First 90 min presets have a
ten-minute minimum window. Dragging never selects. Overlapping numbers hide.
Keyboard focus, reduced motion, theme tokens, and container scrolling at narrow
widths apply. Watcher and task controls keep their owners.

## Artifact viewer

One shell-mounted panel survives branches for loading and identity.
It closes on project change. Artifact, report, and repository entrances use it
for supported previews. PDF system viewing and downloads follow the
[file-type rules](paper-artifacts-and-result-views.md#answer-and-artifacts-are-independent).
It starts docked on the right at full height. Dragging its left edge resizes it.
Dragging its title floats it. Double-clicking the title or pressing focused Enter
or Space toggles fullscreen and restores placement.
Dock places it beside node windows. Restoring the panel docks it.
Settings and setup hide it until the user returns to the project.
Placement is remembered per origin and stays reachable after resizing.
There is no row of mode buttons.

Inline artifacts share the reply's width and palette without a frame.
They have a loading shimmer and a quiet caption with name, version, live state,
and action. Comment mode adds a dashed selection outline.
[Artifact interaction](paper-artifacts-and-result-views.md#artifacts-inside-a-reply)
owns content, comments, versions, polling, and recovery.

The title holds the name, live state, version, offered Undo or reply link, dock,
and close controls. The same-origin shell owns comments and availability without
reading opaque HTML. Selection alone sends or stages nothing.
Runs preserves server order, with the report first, and labels kind, time, and worker.
Timeline popovers filter to the exact operation.
