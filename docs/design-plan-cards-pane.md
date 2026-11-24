# Design: plan cards edited in place, parked plans, and the agent pane

Status: design for review. This file is deleted before merge, and its rules move to
the owning specs (`paper-artifacts-and-result-views.md` for the pane,
`conversations-episodes-and-watchers.md` for plan cards,
`api-web-and-desktop-projections.md` for the route).

The design extends the existing Experiment and Auto-research plan cards (the
`approval_cards` table, `EpisodePlanCard.tsx`) and the existing agent Artifacts pane
(`ChatArtifactsPane.tsx`). The pane lives in `AgentChatView`, so every agent view gets
it: project chats, node chats, agent threads, and the Paper Coach.

It ships as one PR. Part 1 is the card in its turn and in-place editing. Part 2 is
parking, in-place revision, and the pane.

## Problem

1. The pencil beside Design and Done when sends the member to the node editor. The
   member leaves the agent window, and the edit becomes a staged change that blocks Start
   until the member Syncs.
2. A pending plan card is moved to the dock at the bottom of the chat, away from the
   turn that raised it.
3. "Don't run this now" declines the card. The plan is lost, and the only way back is
   to ask the agent again. A card also expires after 72 hours.
4. When the agent revises a plan, RCP raises a new card and marks the old one stale.
   Nobody can edit a live card.
5. Running runs show as a strip above the composer ("1 run in progress"). Artifacts show
   in a pane that exists only once the chat has an artifact.

## Part 1: the card in its turn, edited in place

### Placement

A plan card that is open (pending or parked) renders inside the answer of
the turn that raised it, or that last revised it, above that turn's Done chip. It no
longer goes to the dock. Asked cards that are not plans keep their dock placement.

### Editing Design and Done when

Design and Done when get the same inline editor that Goal uses. The pencil opens a text
box on the card, and nothing leaves the agent window. The node chip still opens the node
for reading. Done when is edited as one criterion per line.

The draft lives in the card's draft store (`usePlanDraft`). The store's provider moves
from the dock stack up to `AgentChatView`, and it keeps drafts for every open plan card
the view shows (transcript and dock), so a draft survives the card moving or folding. A Design or Done when draft records the node's `updated_rev` it started from.

Start runs these steps in order:

1. **Staged-change check.** The existing staged-change check runs first. If the member
   has staged changes on this graph target, Start stops and asks for a Sync, and
   nothing is written.
2. **Save the edit.** When Design or Done when changed, Start sends one node change
   through the existing Sync route, `POST /api/projects/{id}/sync`. The request carries
   the card's `branch_id` when the target is a branch. `base_updated_rev` is the
   revision the draft started from, and `base_revision` is the graph revision of a fresh
   read taken right before the Sync. A 409 from the graph revision alone (another edit
   landed in between) is retried once with a new read. A 409 because the node itself
   changed keeps the card pending and drops the conflicting fields from the draft, as in
   "agent wins" below.
3. **Approve.** Start approves with the node's new `updated_rev`, read from the graph
   that Sync returns.

When Sync succeeds and the approval is refused, the edit stays on the node as ordinary
graph history, and the card shows the refusal. A line on the card, shown while a field
is being edited, says the edits are saved to the node on Start.

## Part 2: parked plans, in-place revision, the agent pane

### Park for later

"Don't run this now" becomes **Park for later**. Internally the state is `shelved`,
because `parked` already names a question that waits on a settled turn
(`api/questions.py`).

- **Draft saving.** Park first saves the current draft:
  - Design and Done when go to the node through Sync, as on Start.
  - Goal, turn limit, and agent go into the card's action as a member revision.
- **Recording.** The draft step and setting `shelved_at` are one transaction,
  conditioned on the card being open (`result_json IS NULL`, `running_since IS NULL`)
  and still carrying the hash the member saw before the draft step. Park never takes the Start claim, so crash recovery never sees it. A
  failed condition returns 409 `plan_revised`. The question stays pending.
- **Attention.** Park appends an inactive `question_attention` event for the question.
  `append_question_attention` and every attention and digest count treat a card with
  `shelved_at` as inactive.
- **Fold and animate.** The card folds in its turn into a one-line stub with the title
  and "Parked". Clicking the stub only expands the card in place; the card stays parked.
  A copy of the card animates into the pane button, and the count on the button goes up.
  With reduced motion, the card only fades.
- **No message.** Park does not wake or message the agent.

A parked card never expires:

- **Expiry.** The expiry sweep and the claim's expiry clause both skip rows where
  `shelved_at` is set. `expires_at` keeps its stored value. Nothing un-parks a card:
  `shelved_at` stays set until Start or Delete settles it, so the stale expiry never
  applies again.
- **Closed threads.** Closing an agent thread still settles its parked cards as expired.
- **Shared and node chats.** These have no thread, so their parked cards stay until
  someone starts or deletes them.

A parked card is left out of the dock and out of `dockedPlans`. It appears in the transcript and in the pane.

A card is one stored record. Every view that shows it refers to the same question id:
the chat that asked it, Agents home and Pip's relayed dock, and a parent agent's dock
for a child run's cards. A park, revision, or Start therefore changes the card in every
view. Relayed docks also leave parked cards out. A park tapped in a relayed view animates
into that view's pane button. The parked plan is listed in the pane of the chat that
asked it.

A parked card offers three buttons:

- **Start**: the same approval as on a pending card, with the same steps as Part 1.
- **Delete**: declines the card, which removes it from the pane. The transcript keeps
  the collapsed "declined" card.
- **Save**: shown only when the card has unsaved edits. It saves them the same way Park
  does (a member revision), so a parked plan's edits persist across reloads and devices.

The agent sees parked plans through its existing question snapshot. In
`question_snapshots._card_entry`, a parked card carries `shelved: true`. Parked cards
sort after every other pending item, so a backlog of them never pushes a newer question
out under `QUESTION_SNAPSHOT_MAX_RECORDS`.

Auto-research plan cards park the same way.

### Revising a card in place

When the agent asks again for a plan whose subject matches an open card in the same
chat, RCP updates that card in place. The subject is the experiment node, or
Auto-research. The card can be pending or parked. If several open cards match (an old
chat), the newest is revised and the rest settle as stale. `raise_plan_cards` no longer
calls `_settle_replaced_cards` for this case. RCP does not raise a new card.

**Agent revision.** One store method, `revise_approval_card`, runs one transaction.
Its condition is that the card is open: `result_json IS NULL` and
`running_since IS NULL`. It then:

- rewrites the action and recomputes the pins, and `pinned_sha256` with `card_sha256`
  from the new action and pins;
- rewrites the question text;
- sets `revised_operation_id` to the revising turn, plus `revised_at` and `revised_by`;
- renews `expires_at` on a pending card;
- issues a new `request_id`, so a Start never returns an episode started for the plan
  before the revision;
- writes the turn's raised receipt.

**Member revision (Save, and Park's draft step).** The member revision uses the same
method in member mode. Its condition also requires the hash the member's draft started
from, and a mismatch returns 409 `plan_revised`. The member revision:

- rewrites only the member's run fields (goal, turn limit, agent) in the action;
- keeps the stored pins and recomputes the hash from them;
- re-checks the live pins first, and a difference settles the card as `stale`, as Start
  does;
- leaves `revised_operation_id` unchanged, and issues a new `request_id`;
- returns the new hash, which the saving tab adopts as its own, so its draft is not
  dropped and it shows no "changed this plan" note. Other tabs and devices do.

**Claimed cards.** A card already claimed for Start is not revised. The agent's request
raises a new card, and the claimed card settles as it does today.

**Newer-card check.** `_newer_card_for` compares against the turn that revised the card,
falling back to the turn that raised it. Crash recovery therefore cannot overwrite a newer
revision with an older one.

**Start uses the latest turn.** Start runs from `revised_operation_id`, falling back to
the origin turn. The run then uses the truth scope and graph target of the plan the card
shows.

**Out-of-date taps.** The Start claim becomes conditional on `pinned_sha256`, so a tap
that sends an old hash cannot claim a revised card. It gets 409 `plan_revised`, and the
card stays open and refreshes. `_resolve` re-reads the card after the claim. This applies
to approve, decline, park, and save. A difference in live pins still settles the card as
`stale`, as today.

**Dropping the member's draft ("agent wins").** The web keeps the last `pinned_sha256`
it saw for each card. When the hash changes, it drops the member's unsaved draft for that
card. When the node's `updated_rev` moves past the revision a Design or Done when draft
started from, it drops those fields. In both cases the card shows one line: "<name>
changed this plan" with the time. For an agent revision, `<name>` is the agent; for a
member revision it is the member. The card moves to the turn that revised it.

**Agent instructions.** The Experiment usage text in `work_experiment_commands.py` and
the Auto-research text in `work_episode_controls.py` change. They now say that running
the command again for the same node (or Auto-research), with a new key as today, revises
the open card. `WORK_POLICY_VERSION` is bumped.

### One card for the plan's life

The plan card and the run stay separate stored records: the approval card, and the
episode it starts. The web shows them as **one card component** from proposal to result:
Needs you → Parked → Running → Needs you (the run asks) → Ended.

- **Join.** The card finds its run through the episode id in the card's result. One shared
  projection, `experimentCardView(card?, run?)`, gives the phase, the title, the state
  line, and the actions. Views never compare statuses themselves.
- **Replacements.** The component is built on `ApprovalCardShell`. It replaces the plan
  card's settled line, `EpisodeRunCard`, and `RunningRunRow`. Run controls (Stop, task
  actions, Continue) come from the existing `EpisodeControls`.
- **Running.** The card shows the run's health ("Running · turn 4 of 30") with **Open
  agent**. Its menu holds the run controls.
- **Ended.** The card shows the outcome ("Ended: completed") and the turns used, with
  **Open report**. A start that fails keeps today's failed line.
- **Runs without a plan card.** A run started from the node page has no plan card. It
  gets the same component from Running onward.
- **Where it appears.** A card-started run stays in its turn, and the pane shows the same
  card. A node-started run has no turn, so it appears only in the pane.

Proposed phases on episodes were considered and rejected for now. Many readers take an
existing episode to mean an admitted run: Start's retry, recovery, latest-run selection,
health, transfer, and power. Merging the records would move the plan logic rather than
remove it.

**No second plan while a run is live.** An Experiment allows one live episode per graph
target. When the agent asks for a plan for a node whose run is live, the `episode`
command refuses with a reason the agent can act on: the run is live, so wait for it to
end or ask the member to stop it. No card is raised. Auto-research keeps its own rule.

### Agent pane

One header button is always shown. Its count is the number of running runs, parked
plans, and artifacts. It opens one pane with up to four sections in this order:
**Running**, **Parked**, **Ended**, and **Artifacts**. Empty sections are hidden, and
an empty pane shows only its header. Each section header folds its section and shows the
count while folded. The fold state is remembered per section on this device.

- **Running** lists the chat's live runs as the experiment card in its compact form:
  one line with the title, the state, and a menu. Clicking the line expands the card in
  place. It replaces the dock's "N runs in progress" strip. Runs that need the member
  stay in the dock as well, as today.
- **Parked** lists the chat's parked plan cards, newest first, in the same compact form.
- **Ended** lists the chat's ended runs, newest first, each with Open report.
- **Artifacts** is the existing list, with Copy link and Download on each row.

A compact card that has a turn offers "Show in chat", which scrolls to it.
`useArtifactsPaneLayout` becomes the pane's layout hook, and it no longer depends on the
chat having artifacts. The pane sits beside the chat on a wide window and covers the
chat on a narrow one, full width at 390px.

## Data and routes

- **Migration.** `approval_cards` gains the nullable columns `shelved_at`,
  `revised_operation_id`, `revised_at`, and `revised_by_json`. Old rows read as open and
  never revised.
- **Decision route.** `POST /api/approval-cards/{question_id}`:
  - `decision` accepts `park`, and `start` carries the member's run fields;
  - an old hash gets 409 `plan_revised`;
  - approve and decline accept parked cards.
- **Card view.** The card view adds `shelved_at`, `revised_at`, the reviser's name, and
  `revised_operation_id`. Its state precedence is `expired`, then `running`, then
  `parked`, then `pending`.

## Not changed

- Invariant 3 and approval semantics. Only a member's Start starts a run. The agent can
  revise a card but cannot start or delete one.
- The pin checks, the refusal codes other than `plan_revised`, and the staged-change rule.
- Asked cards that are not plans.

## Verification

**Part 1, web browser tests:**
- The card renders in its turn above Done.
- A Design edit saves through Sync with its base revision, then Start sends the new
  `node_rev`.
- The staged-change check refuses before any Sync.
- A node conflict keeps the card pending.
- The layout holds at 390px.

**Part 2, Python tests:**
- Park, then Start from a parked card. Park after a pin change.
- A parked card skips expiry. A closed thread settles a parked card. A parked card in a
  shared chat persists.
- An agent revision updates the card in place and keeps the question id.
- A claimed card is not revised.
- A tap with an old hash gets 409 and the card stays open.
- Start after a revision uses the revising turn's scope.
- Crash recovery leaves a newer revision alone.
- Old rows read correctly.

**Part 2, web browser tests:**
- Park folds the card and the pane lists it. Delete removes it from the pane.
- An agent revision drops the draft and shows the note.
- The pane button shows with no artifacts.
- The pane holds at 390px. Reduced motion is respected.

- A started card shows the live run, then its outcome. A node-started run shows in
  the pane. Pane sections fold. A plan request for a node with
  a live run is refused.

**Served-app drive:** a real Codex Work turn raises a plan. The member parks it, the
agent revises it, and the member edits Design and Starts.
