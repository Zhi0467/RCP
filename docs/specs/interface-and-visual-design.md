# Interface and visual design

Current design decisions for the RCP web interface: what the surfaces are, how
they behave, and the visual grammar they share. This file owns those decisions;
[`api-web-and-desktop-projections.md`](api-web-and-desktop-projections.md) owns
the data those surfaces read and the shell that hosts them.

## Visual grammar

RCP shares Margin Dev's visual grammar without copying its catalog literally: a
restrained paper, sheet, walnut, and oxblood system, warm rules and shadows,
compatible typography, and tactile book materials. Project covers share one
oxblood base and differ by texture only; decorative color is never assigned per
card. Semantic accents are reserved for meaningful type or state. RCP keeps its
own information architecture and behavior.

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
destination.

Group the header semantically — labeled **Sync / Ask** together, then icon-only
**History / Refresh** together. Do not space all four as unrelated peers.

Glossary definitions appear inline where terms are read. Glossary has no
navigation destination, and glossary authoring remains an open question.

A previously opened project feels immediate even when canonical state is remote:
render one rebuildable durable display snapshot first and refresh the
authoritative state in the background. That cache stays out of every history,
agent, Sync, paper-write, and other authority path. Canonical mutation controls
wait for reconciliation, and blocking remote refreshes run off the web event loop.

## Projections

The visible projections are **Research** and **Runs**.

Research shows question-centered paths with unconnected records separated. Its
DAG **Research flow** columns follow semantic stage rather than relation-arrow
direction.

Runs is the episode ledger for bounded Experiment loops and Auto-research. It
carries no page title and is ordered **Needs Action**, then **Completed**. Needs
Action is an unfolded reverse-chronological card list across both episode modes.
Completed is grouped into foldable **Experiment loop** and **Auto-research**
lists, in that order. The owning Experiment title or Auto-research identity is
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
change, and closes when the human enters Chats.

Node wording correction is a literal human edit, not an agent request. A direct
prose editor stages the change in the project draft and clears the draft standing
to asserted; node chat is never started merely to rewrite text. Canonical history
changes only when the human presses Sync.

## DAG controls

Boundary-aware page scroll chaining, brighten/dim-all, fullscreen with visible
node details, **Release all pins**, and per-node pin release. Repulsion must
visibly affect spacing, and the canvas must leave generous room for manual
dragging beyond auto-layout positions. Touchpad pinch zoom stays anchored at the
gesture focal point without turning ordinary two-finger scrolling into zoom or
disrupting other DAG interactions.

## Conversation composer

Discuss and Work are switchable on every node and project conversation. Discuss
is plum, Work is dark forest, `Shift+Tab` toggles while the composer is focused,
and every sent turn keeps an immutable visible mode label. A resumed task keeps
its original mode regardless of the current composer setting.

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
storage.

## Paper

The editor/coach split is human-resizable, and the editor begins with authored
content rather than a redundant canonical-file banner. The authored Markdown
switches between Write and Preview in the same pane, using the chat renderer so
unsaved text can be read without creating a second document.

## Auto-research and result views

Auto-research starts from the project header, beside Ask, because the action is
project-wide and belongs where project-wide actions live. Its budget is typed in
invocations with observed cost shown beside it: the enforced number stays exact,
the legible number stays honest. Its report allocation is hidden from that
operational budget. Every non-Stop ending produces the durable visual report;
Stop alone means no report.

Result views are revised by acting on the picture — box a region, underscore
items — not by describing it in the composer. A gesture writes a visible draft
and never dispatches a turn by itself.
