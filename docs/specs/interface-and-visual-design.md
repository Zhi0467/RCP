# Interface and visual design

Current design decisions for the RCP web interface: what the surfaces are, how
they behave, and the visual grammar they share. This file owns those decisions;
[`api-web-and-desktop-projections.md`](api-web-and-desktop-projections.md) owns
the data those surfaces read and the shell that hosts them.

## Visual grammar

RCP's **Classic** theme uses Margin Dev's visual grammar: a
restrained paper, sheet, walnut, and oxblood system, warm rules and shadows,
compatible typography, and tactile book materials. Project covers share one
oxblood base and differ by texture only; decorative color is never assigned per
card. Their textures are restrained bookcloth and paper grain, never loud dye,
mosaic, or simulated wood. Semantic accents are reserved for meaningful type or
state. RCP keeps its own information architecture and behavior.

**Aqua** is the default theme with pearl-gray or dark slate surfaces, rounded
controls, shallow bevels, and restrained blue highlights. One upper-left light source gives raised
buttons a bright upper edge and a soft lower shadow; fields and selected controls
use inset shadows. Pressing a button changes its relief. Project tiles use plain
raised surfaces instead of book materials. Semantic type, status, warning, and
focus indicators remain distinct. Clickable Overview rows, Runs card headers,
space Runs rows, and available artifact rows also rise from the surface and
press inward while held. Expanded run detail and unavailable artifacts do not
use the same action cue. An available artifact's preview link spans its entire
row and opens the shared in-app viewer panel. Its **Source chat** link remains
a separate click target.

The space landing page's **Display** control places **Mode** above **Theme**.
Mode offers **System**, **Light**, and **Dark** independently of **Classic** or
**Aqua**. Both themes support all three modes, and changing theme preserves the
selected mode. Aqua with System mode is the default; saved theme choices are preserved.

Both choices are remembered on this browser origin and apply across projects,
including before the first paint. System follows the operating system's light
or dark preference. Legacy System, Light, and Dark choices become Aqua with
the same mode; legacy Soft Aqua becomes Aqua with Light mode. Display preferences
do not change project settings or research state.

The RCP mark is one unified logo. An initial tile beside the full acronym reads
as a duplicated letter, so the visible logo contains **RCP** exactly once.

## No commentary lines

Never place a smaller, muted, or more transparent explanatory line beneath a
button, title, large label, card heading, or other primary element. Helper
subtitles and descriptive microcopy are removed wherever they appear; primary
wording, hierarchy, shape, color, motion, and control state carry the meaning.

Actual errors, conflicts, required warnings, and accessibility labels stay
explicit. When an item genuinely has more to say, a card carries its name and
state and an explicit control opens a read-only inspector — never a caption under
the card.

Chat and coaching surfaces contain no sample prompts, slogans, instructional
empty-state copy, or textarea placeholder text. An empty conversation is simply
empty.

## Project shell

The shell is intentionally bare: no RCP wordmark, product logo, or revision label
beside the project name. Agent tasks and Refresh are icon-only accessible
controls; project chat is **Ask**. The attention destination is **Inbox** with a
colored count, and DAG is a subpanel of **Research** rather than a primary
destination. **Paper** is likewise a subpanel of **Artifacts**, reached from a
Files | Paper switch; its route view stays `paper`, and the paper's unsynced
badge shows on the Artifacts tab. Destination order ends Terminals, Agents,
Settings.

Group the header semantically — labeled **Sync / Ask** together, then icon-only
**History / Refresh** together. Do not space all four as unrelated peers.

Glossary definitions appear inline where terms are read. Glossary has no
navigation destination. Graph-writing agents add or revise the definitions through
`upsert_glossary` Patches.

A previously opened project feels immediate even when canonical state is remote:
render one rebuildable durable display snapshot first and refresh the
authoritative state in the background. That cache stays out of every history,
agent, Sync, paper-write, and other authority path. Canonical mutation controls
wait for reconciliation, and blocking remote refreshes run off the web event loop.

## Projections

The visible projections are **Research** and **Runs**.

Research shows question-centered paths with unconnected records separated. Its
DAG **Research flow** gives each node type its own column in research order,
as defined in the Research projection spec.

Runs is the episode ledger for bounded Experiment loops and Auto-research. It
carries no page title and is ordered **Needs Action**, **In progress**, then
**Completed**. Needs Action holds only runs that have stopped advancing and stay
stopped until a human acts; a run still moving on its own is In progress. Each of
those two is an unfolded reverse-chronological card list across both episode
modes. Completed is grouped into foldable **Experiment loop** and
**Auto-research** lists, in that order. The owning Experiment title or Auto-research identity is
the card's visual headline; start time is secondary metadata without an
`Episode` prefix. Completed groups name
the episode mode once; cards do not repeat it or add muted recommendation and
report commentary below the status. An Experiment contributes only the current
episode selected by its backend control; older episodes remain in History rather
than duplicating the same Experiment in Runs. That same control supplies the
card's Experiment health and section, so the summary cannot disagree with its
expanded detail.

Seed/Refresh, generic node chat, project chat, paper-coach tasks, and Blockers
live in their owning History or Inbox surfaces, never as Runs rows. Pressing an
Experiment's **Run** navigates to its episode card in Runs and opens its detail
rather than a floating node-chat window. That detail's only loop-level action is
**Stop loop**; invocation-level Pause, Resume, and Retry stay in the Agent task
inspector.

## Nodes and node detail

A node must be understandable when opened alone: ordinary language, enough
context-setting sentences, and inline explanations rather than terse project
jargon. Relation rows open a focused one-hop DAG view.

Node detail is a resizable floating inspection window. Its project-scoped size
survives minimize/restore and close/reopen, remains reachable after a viewport
change, and closes when the human enters Agents.

Node wording correction is a literal human edit, not an agent request. A direct
prose editor stages the change in the project draft and clears the draft standing
to asserted; node chat is never started merely to rewrite text. Canonical history
changes only when the human presses Sync.

**New node** creates any of the six built-in node types or an active custom type
in the same project draft. Graph connection controls select source, target and
relation; a node-to-node connection gesture opens the same editor. Explicit
select controls provide a keyboard alternative. Connections can be removed or
replaced through that draft. Backend preview validates the full batch before
Sync; the UI does not derive lifecycle effects or imply that staging committed.
Removing a node removes its current incident edges, not its history. Existing
accepted-node and active-Experiment safeguards still apply. Artifact selections
stay in the viewer for in-place editing; they are not graph-editing controls or
Evidence creation shortcuts.

## DAG controls

At viewport widths of 560px or less, DAG controls start closed behind a
chevron disclosure so the canvas is the primary content. Opening the disclosure
exposes the same controls; wider views keep them visible.

Boundary-aware page scroll chaining, brighten/dim-all, fullscreen with visible
node details, **Release all pins**, and per-node pin release. Repulsion must
visibly affect spacing, and the canvas must leave generous room for manual
dragging beyond auto-layout positions. Touchpad pinch zoom stays anchored at the
gesture focal point without turning ordinary two-finger scrolling into zoom or
disrupting other DAG interactions.

**Fit** fits and centers the graph horizontally without magnifying past authored
node size. Tall columns remain vertically scrollable, starting at the top; short
graphs are centered vertically. Adding rows must not shrink the fitted columns.
Fit preserves manual pins, and leaving and returning restores the zoom, scroll,
and centering offsets.

## Agent browser controls

Discuss and Work chats have a **Browser** switch in the header area. New chats
start with it off. Each chat loads and saves its own server preference. The
switch waits for a successful load and keeps the server value after a failed
save. Errors offer a check-again action. Paper coaching has no Browser switch.

The consent paragraph is primary content at normal reading size. It states
that the agent can use a headless browser on its execution machine, run
agent-written browser code, and write outside the chat's folders. It also
states that changes apply from the next turn and turning Browser off deletes
that chat's logins and cookies. Experiment launch controls in node detail and
Runs, and the Auto-research launch dialog, carry the same switch and consent.
Continuations keep the episode's existing grant.

Unavailable or lost browser access appears as a notice on the affected chat
turn and in episode timeline turn details. The notice gives the reason, the
server detail when present, and a machine-card fix when applicable. Unknown
codes show a generic failure with the server detail. Unrequested and granted
turns add no notice.

Each saved machine card loads its Browser row independently after mounting;
readiness never delays the rest of the card. The row shows status and diagnostic
detail, offers **Install** when the browser is absent, and tells the human to
install Node 18+ and npm first when either is missing or too old. Missing system
libraries show a selectable command and **Copy command**. Install shows progress
and disables repeat requests until it returns the new readiness. Failed checks
and installs leave **Check again** available.

## Conversation composer

Discuss and Work are switchable on every node and project conversation. Discuss
is plum, Work is dark forest, `Shift+Tab` toggles while the composer is focused
and is not addressing a running attempt, and every sent turn keeps an immutable
visible mode label. A resumed task keeps its original mode regardless of the
current composer setting.

In the Agents workspace, a conversation has one header band: title and meta on
the left, New session and repository scope on the right. The meta names the
umbrella provider (`provider_label`, such as Claude or Codex), never its
runtime; the task inspector keeps the runtime. It shows the latest turn's
model and effort, and the chat kind. Neither the header nor an agent card
names Discuss or Work; each turn's own label already carries it. The agent list has no title
band: a sidebar icon beside its search folds it, a folded list leaves that
icon at the chat band's top left, and a hairline separates list from chat.

Cards share one fixed size: a one-line title and one secondary line of
meta; the group they sit in names their state. A three-dot menu on
the card removes an unsent draft, which exists only in the browser, or renames,
pins, or archives a conversation with turns. Rename and archive are project
display choices shared by every member; a pin belongs to the member who made it
(`POST /api/projects/{project_id}/chats/{chat_id}/title`, `.../pin`, and
`.../archive`, read together from `GET /api/projects/{project_id}/chat-display`,
which returns the acting user's pins);
a blank name returns the derived one. Rename edits the title in place on the
card. An Archived filter appears when any exist, counts every archived chat
including unloaded pages, and offers Restore. A revisit keeps the last loaded
display set until its reload answers, so archived chats never flash back into
the list. Card metadata shows a provider's monochrome logo (Claude in its brand
orange) when RCP ships one in `web/public/providers`, otherwise its label.
Transcripts and tasks are never changed or deleted.

Chat uses one wide readable column. A human request is a quiet paper card;
assistant prose is unboxed. Current task activity folds behind a muted Activity
row when its underlying status can already be inspected, while failures and
recovery controls stay explicit. The composer is a calm contained writing
surface rather than a full-width control bar.

The **Agents** destination (formerly Chats; the route view is still `chats`) lists
conversations as an agent-hub panel. Each conversation belongs to exactly one
group, and each group lists its conversations in recency order. The groups come
from the backend's answers on the latest loaded turn, in this order:

- **New reply**: the turn succeeded after the human last viewed this
  conversation (the read markers in `api-web-and-desktop-projections.md`).
- **Failed**: the turn failed.
- **Stopped**: the turn was paused or interrupted. Both resume the same way.
- **Working**: the turn is queued, running, or pausing.
- **Done**: the turn succeeded and was read. An unsent draft, and a
  conversation whose turns are not loaded, are also Done.

Pinned conversations leave their group for a **Pinned** section above the
groups, newest pin first, in the All view only; each pinned row carries its own
status mark. Filters and their counts ignore pins.

The group header carries the state: a mark and a label in the state's colour,
plus a count. Rows carry no state dot. Each row is a raised card with a one-line
title and a provider · repository line. Failed and Stopped rows add Retry or
Resume when the task offers it, and a working row shows its live phase and
elapsed time. An unread turn is the most prominent thing in the list in any
group: a tinted card, an accent border, a bold title, and a New pill. A failed
or stopped turn that is unread stays in its own group. Search runs in the browser over what each card
already holds: its name, node, chat kind, latest message, and every loaded
turn's prompt, provider, model, effort, and repositories; every word must match.
Chips filter to All or Working. Above the conversation, a header
names the title, provider, model, effort, repository, and chat scope; a failed or stopped turn adds a banner
with the backend label and Resume or Retry when the task offers it.

The Agents tab opens on a **board**; a link that names a chat (Ask, a source-chat
link, or a chat route) opens that chat instead, and the list's board icon returns
to the board. The board has four columns: **Needs you** (New reply, Failed,
Stopped), **Working**, **Done**, and **Archived**. Each card shows the provider
logo, with a spinner ring while the agent works, the title, a provider · model ·
effort · task-type line, and its state or age with Retry or Resume when offered.
Clicking a card opens its chat with the list folded and the composer focused.
Leaving Agents for another view resets it to the board. A mouse drag reorders
cards within a column, and that order is the viewer's own, kept in browser
storage per project; pins still lead their column. Dropping on Archived archives
the chat, and dragging out of Archived restores it. A drop on another state
column is refused, because the run decides the state, and a working agent cannot
be archived. A branch episode card opens in Runs and does not move. Touch scrolls
the board rather than dragging, so archiving there goes through the card's menu;
Alt+Up and Alt+Down reorder a focused card from the keyboard. The board's
columns go to two at 920px or less and to one at 560px or less.

At viewport widths of 560px or less, Agents uses a single column. The conversation
list starts closed behind an **Agents** disclosure above the conversation and
closes after selecting a chat. Wider views retain the resizable list and its
saved collapse preference; changing viewport size does not overwrite that
preference or the saved list width.

Selecting packages is `/` or `$` in the composer, and it is keyboard-first:
arrows highlight, Enter selects the highlight instead of sending, and Escape
dismisses. Project Settings holds the defaults; a composer selection applies to
that turn only.

Agent configuration is owned by Project Settings. Chat and coaching show one
non-expandable provider-name box only — no model, reasoning, machine, permission
summary, or locked/editable label. Seed/Refresh keeps its explicit launch
controls, and chat keeps Raw truth inputs because those select context rather
than execution configuration. Settings supplies fresh conversation defaults; an
existing native conversation retains the profile it last ran with, so
continuation never silently moves providers or machines.

The composer also has one compact **Compute** menu. It lists only the project's
configured compute connections, shows green reachable or red unavailable status
for the conversation's actual execution machine, and attaches or detaches each
resource without changing the provider or `run_on`. The active count and checked
items recover from the newest persisted turn. Sending keeps the active set for
the next turn; settings removal reconciles a stale selection away.

Project Settings owns compute connections beside provider executables. A local
entry needs a name; an SSH entry adds `user@host`; either may carry one optional
non-secret access hint. There is no password or private-key input. Settings shows
one probe result per agent execution machine and distinguishes unreachable,
authentication, and host-key failures. Credential repair text names the exact
agent machine rather than inviting credentials into RCP. The explicit **Probe**
action refreshes those results; ordinary project polling reuses the last matrix
and does not launch SSH work. The floating connection list uses native checkbox
semantics, and unsaved connection edits are not copied into local settings-draft
storage. Editing a connection masks its saved probe result and disables **Probe**
with a concise save-first label until the metadata is saved. A compute-settings
save also invalidates older in-flight readiness responses, so a late old-target
success cannot replace the empty state left by a failed new-target probe.

Project Settings shows its machines as small tiles (name, host, and status dots
for each provider, the jobs route, and the writable-path count) with a dashed
**Add machine** tile; one machine's card is open below at a time, the first
until another tile is clicked, grouping
provider executables, **Long-running jobs**, and the machine's writable paths.
The same writable-path record appears in space Settings, so the project card
says the list applies to every project on that machine. **Add machine** opens
a labelled box of the space's other machines as the same tiles; one click adds
that machine under a name derived from its card and opens it. Setup picks from
the same tiles. Both end with a dashed **New machine** tile. Writable paths are picked with a small folder picker (breadcrumbs, one
level, name filter, **Load more**, locked protected folders, **Use this
folder**) rather than typed. **Use Slurm** opts into direct scheduler submission; **Jobs root**
configures helper storage. RCP exposes no scheduler resource settings. **Reset
compute** removes the optional block through the normal Settings **Save**.
Readiness uses the same label, tone, and pending presentation as compute
connections, with one row per offered route (scheduler and helper). There is
no Probe control: a save that changes the block checks it in the background,
and editing masks the saved result until then. A route that is not ready
shows on Runs with its fix and **Check again**.

Chat and Experiment show one external job row per shell watcher. Its log path,
observation status, last check, and diagnostic remain visible with Cancel
requester and time when recorded. A completed watcher does not assert scientific
success; **Cancel requested** does not assert the process was stopped. There is
no second job-ID-based display path.

**Stop watching** keeps its existing meaning and never cancels external work.
A **Cancel** control appears only while the backend's `can_cancel` permits it,
including on a stopped watcher that may still describe live work. It is
independent of graph read-only mode or Experiment action locks because the API
owns project write admission. The action response updates the displayed receipt;
the existing watcher refresh owns observation. The browser does not infer
cancellation availability from watcher status.

Watcher actions sit at the right of the row. Completed shell and graph-condition
watchers offer **Hide**, which removes the full watcher row or detail card from Chat
and Runs on this device and browser origin, scoped to the project. **Show hidden watchers** restores
them. This display preference preserves the watcher record and other viewers'
lists; a watcher that becomes active again is visible regardless of the preference.

## Terminals

**Terminals** is a project destination beside Overview, Inbox, Research, Runs,
Artifacts, Agents, and Settings when at least one project machine can host
a session. Remote pending and failed probes also keep it visible so their
status and recovery control remain reachable. It is hidden for empty projects
or only unavailable local machines. Settings has no terminal control. The empty
state consists of repository controls showing their starting paths. Unavailable
repositories show their capability reason and cannot open a terminal. Remote
rows distinguish pending checks, unreachable hosts, authentication failures,
host-key failures, and missing prerequisites. The tab polls while a probe is
pending; ordinary refresh reads cached results. The terminal Refresh control
explicitly retries machine probes. Space kind does not affect eligibility.

A left rail lists open sessions with Live or Idle state, selection, and an
individual End control. One session exists per repository in each project.
The right pane holds the selected interactive shell. A running Work turn does
not block opening a session: its title appears above the active terminal, and
a Work running mark appears on its rail row. Repository paths are data, not
instructional helper copy.

Leaving the destination detaches the viewer without ending the shell. Returning
lists the existing sessions; ending a session, losing membership, or idle expiry
ends it. SSH link loss ends the session with an explicit link-drop diagnostic
that survives removal from the open-session list. It offers no reconnect into a
new shell; opening the repository again is a new session. Theme and mode changes
repaint the terminal using the same surface and text tokens as the project UI.

The shell runs as the service account and inherits the Work turn's trust
boundary. A mirrored session's mount namespace provides accident resistance to
wrong-directory mistakes; it is not isolation from the service account or a
boundary against deliberate action.
Mirrored sessions refuse canonical-state writes in that filesystem view.
Cooperative sessions show a visible, explicit warning in the terminal pane:
canonical-state protection is unavailable on this machine, and there is no
canonical-state fence. This required warning uses semantic warning colors,
border, and surface tokens, following the Aqua treatment and remaining explicit
in Classic and Aqua across System, Light, and Dark modes. It is not a smaller,
muted commentary line beneath a heading. Terminal setup failures remain explicit
in the view; a failed mirrored launch never offers a cooperative downgrade.

## Paper

The editor/coach split is human-resizable, and the editor begins with authored
content rather than a redundant canonical-file banner. The authored Markdown
switches between Write and Preview in the same pane, using the chat renderer so
unsaved text can be read without creating a second document.

## Auto-research and episode history

Auto-research starts from the project header, beside Ask, because the action is
project-wide and belongs where project-wide actions live. Its budget is typed in
invocations with observed cost shown beside it: the enforced number stays exact,
the legible number stays honest. Its report allocation is hidden from that
operational budget. Every non-Stop ending produces the durable visual report;
Stop alone means no report.

An episode's history is a read-only agent roster on one SVG timeline. A fixed
label column groups People, Orchestrator or Experiment agent, Workers, and
Experiments or Watchers; actors with the same row key share a row. Date and time
rows sit above status-colored turn, attempt, and report blocks. Experiment
episodes have light rails, failed worker attempts are red slivers, and a Stop
request is a square mark. Only live runs show a vertical now line; there are no
vertical grid lines or commentary captions.

Solid lines connect recorded starting spans to actors, with clickable hand-off
icons. Message arrows begin at envelopes and end at delivery time; failed
attempt delivery has a red ring, while undelivered messages use a dashed line
and cross. Notice diamonds and watcher ringed dots open their payloads. A wake
signal is dashed to its landed turn's start; harvested or acknowledged signals
are dotted to the recorded landing time. Watcher lines originate from their
recorded node row, and missing stop attribution stays unattributed. Delivery
labels never claim a message was read.

One popover beneath the selected item shows its details: fetched hand-off or
message text, including loading and error states, the inline signal payload,
or an actor or span summary. For an orchestrator turn it shows the cause, what
landed in the turn, what the turn did, and its headline. It links turn and
attempt spans to the task inspector and child episodes to their existing
Experiment detail; report spans do not open the task inspector. Close, Escape,
and outside click dismiss it. Selection draws a thin outline on the selected
block, highlights related actors, spans, and items, and dims others. There is no
panel or table below the chart.

Summary counts cover returned actors, hand-offs, and message
dispositions and explicitly say when only truncated items are counted.

Pinch or Ctrl/Cmd+wheel zooms at the cursor; buttons and double-click zoom, with
Shift+double-click zooming out. Dragging, sideways scroll, and Shift+scroll pan;
vertical scrolling remains page scrolling. Whole run and First 90 min presets
share a minimum zoom window of ten minutes. Dragging never selects an item and
overlapping turn numbers are omitted. Interactive elements have keyboard focus,
motion respects reduced-motion preferences, and the chart scrolls inside its
own container at narrow widths. Colors use existing theme tokens in Classic,
Aqua, and dark mode. Watcher controls and the task inspector retain their own
surfaces.

## Artifact viewer

One viewer panel is mounted beside the application shell, surviving loading and
identity branches. Changing the open project closes it. Chat artifacts, Artifacts,
History reports, Runs, repository-file links, and WebMCP all open it. Its default is
right-docked and full height. Dragging its left edge resizes it; dragging the
title bar floats it. Double-clicking the title bar enters full screen and
repeats to restore the prior placement. Enter or Space on the focused title bar
does the same. The dock control collapses it to a slim tab on the right edge,
just below the project header; the tab restores it in docked mode. There is no mode button row.
Size and placement persist on this browser origin and remain reachable after
viewport changes.

The title bar contains the name, Live or Finished when supplied, version,
Undo when offered, the reply-thread control when supplied, dock, and close.
An admitted edit shows Editing until the server clears it; the panel refreshes
the iframe when the current version changes. State polling runs only while
open and visible, including static artifacts so other members' edits and Undo
appear. Disposing the viewer request aborts it. Permanent HTTP errors stop
automatic requests until Retry; network and transient HTTP failures remain polled.
Undo reloads the current version. Errors remain explicit and retryable.

The iframe hosts the same-origin viewer shell, which owns selection, comments,
Send, and the reason Send is unavailable. Agent HTML stays in the shell's
opaque sandbox; the panel never reads that inner frame. A selection alone
never dispatches an edit or stages a chat draft. Repository files use their
script-free preview. PDFs open in the desktop system viewer; in the browser
they offer Download only, with no Open action.

Each Runs card preserves the server order: its report first, followed by every
turn and worker artifact, with name, kind, time, and the worker label when
present. Worker and turn timeline popovers show artifacts from their exact producing operation,
using the same fetched run list. Every preview opens the shared panel.
