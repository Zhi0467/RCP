# One chat mode

Extend the private-thread rule ("threads have no Discuss mode") to every human
chat. Every new human chat turn is a Work turn. The composer's Discuss/Work
toggle goes away. Historical Discuss tasks keep their records, recovery and
display through a closed Discuss runner that admits nothing new. A later
release deletes that runner once no store holds an unsettled Discuss task.

## Goals and non-goals

- Goal: one kind of human chat turn, with no mode for the member to choose.
- Goal: old Discuss records keep decoding, displaying and settling.
- Non-goal: changing Work's capability, the merge fence, or provider
  containment for Work.
- Non-goal: removing the scratch-only `artifact_edit` kind used where a chat
  refuses human turns.

## Alternatives considered

- **Keep Discuss, fix agy alone.** Leaves the toggle and its second contract.
- **Keep the toggle as a prompt hint only** (Work capability, "answer only"
  wording). Keeps a control whose effect the member cannot verify.
- **Delete Discuss outright now.** Strands remote passes, owed retries and
  pending questions recorded as Discuss. Rejected for a one-release closed
  runner.

## Why

- Private project and Space threads and the viewer's Send are already
  Work-only. The Paper coach runs `paper_suggest` with Work-like access. Discuss
  survives only as a toggle on node and board chats.
- Discuss is an intent signal for "answer only". The agent can read that intent
  from the message, as it already does in threads.
- Discuss costs a second prompt contract, a second launch profile per provider,
  a second readiness path, and a mode the member must remember to flip.

## What a chat loses, stated plainly

- **Operational containment of a question.** A Discuss turn cannot run a
  general shell or write outside its scratch. A Work turn can, within its write
  scope, and Claude and OpenCode Work shells can write outside the file-tool
  roots. An accidental or injected command in a question turn now has Work
  reach. This is the accepted tradeoff under the generic-attacker model: the
  same member's next Work turn already has that reach, and the graph still
  changes only through the stage (invariant 4b) and Proposals (invariant 3b).
- **Cost.** Every question carries the `outcome.json` contract and its one
  correction turn.
- **Chat while a provider fails the Work probe or is below the Work floor.** The
  picker shows the provider as unavailable, as it already does for Work. There
  is no Discuss fallback.
- **Chat on a branch that is the source of a running merge.** Work admission
  on the branch being merged is refused with 409 while the merge fence holds (`api/graph_changes.py:183`,
  `storage/agent_tasks.py:1487`), and a live chat turn blocks a merge from
  starting (`merge_source_has_live_writer`). Accepted (D1).

## The rule

1. The chat task routes (`node_chat`, `project_chat`) admit only `mode: "work"`.
   A missing mode at the HTTP boundary becomes `work`, as agent threads already
   do (`api/tasks.py:432`). A request naming `discuss` is refused with 422.
2. `RunRequest.mode` keeps `discuss` as its model default, because the same
   model decodes stored rows and an omitted mode means the legacy Discuss
   (`storage/request_compat.py:54-60`). Only the route sets Work.
3. The `discuss` capability literal stays in `AgentCapability` and
   `AgentDispatchAuthority`, because stored authorities decode strictly
   (`core/authority.py:125`, `storage/rows.py:179`). No capability is renamed.
4. The composer has no mode toggle. The stored `rcp:chat-mode:*` preference is
   ignored.
5. Artifact comments:
   - In a chat that accepts human turns (Experiment chats included), comments
     go through the existing Send route (`artifact_send_chat_turn` in
     `runs/artifact_edit_admission.py`): a Work turn in that chat, on its
     session, queued or steered exactly as Send is. The agent decides from each
     comment whether to edit. No new dispatcher.
   - Edit now is deleted for these chats: the `edit_now` request field and its
     short-circuit in the viewer's handler in `api/artifacts.py`, `can_edit_now`,
     the panel's Edit now gate, and the prompt line that acts on `edit_now`.
     `fresh_session_required` remains only for refused chats.
   - Viewer buttons (`src/rcp/artifact_comment_panel.js`): today's "Edit now",
     "Edit now in a new session", "Send to original chat" and "Send in a new
     session" become one "Send to agent". It first saves a typed, unqueued
     comment as a comment, then sends every queued comment. In a refused chat
     it posts the scratch edit and reads "Send to agent in a new session" when
     `fresh_session_required` holds. The shared artifact comment design owns
     the panel's look; this design owns only the routing.
   - No new task is admitted with `launch_kind="discuss"`. Refused chats always
     use `launch_kind="revoking"` (`_launch_kind_for_master_owner` and
     `artifact_edit_admission.py:342` change to match).
   - In a chat that refuses human turns (a running orchestrator or Auto-research
     child, `storage/agent_tasks.py:1039`), the scratch-only `artifact_edit`
     kind (today's `launch_kind="revoking"`) handles comments unchanged. It
     keeps `mode: "discuss"` and capability `discuss` internally, set by
     `admit_artifact_edit`. A comment typed in a refused chat's composer arrives
     at the chat route as `work`; `admit_chat_artifact_comment` drops its
     "only in Discuss" refusal (`artifact_edit_admission.py:275-276`) and hands
     it to `admit_artifact_edit` whatever the mode.
   - Episode-report replies follow the same split.
6. The chat master is unchanged in this release. It already carries both the
   Discuss and Work contracts and names the turn's mode, so the closed runner
   and new Work turns share it as today. The release that deletes the runner
   removes the Discuss contract and bumps `CHAT_MASTER_CONTEXT_VERSION`.

## Historical Discuss tasks

The closed Discuss runner (`runs/tasks/discuss.py` and its finalizers in
`runs/remote_finalization.py`) stays for one release. It runs only for tasks
already recorded as Discuss:

- recorded remote results and live remote passes, keeping their fences
  (`storage/agent_tasks.py:4539,6258`);
- automatic transport retries and provider-login parked retries owed across
  restart (`background.py:3121,3194`, `runs/provider_login.py:243`);
- Pause and Resume of a running Discuss task;
- answers to a pending question whose origin is Discuss, which keep Discuss
  authority as today (`storage/agent_tasks.py:349,397`). This is the one new
  Discuss task the runner admits;
- legacy private-edit stage proofs (`discuss.py:609-768`).

A human Retry of a settled Discuss chat turn is not a recovery.
- `POST /tasks/{id}/retry` refuses a Discuss chat task with 409 and the reason
  "Send it again as an ordinary turn".
- The web Retry button on such a turn (the transcript's retry action in
  `web/src/agents/agentTasks.ts`) puts the turn's message text into the chat
  composer instead of calling the route. Attachments and references are not
  restored.
- Sending that composer text carries `supersedes: <old operation id>`. The chat
  admission in `api/tasks.py` checks that the old task is a settled Discuss
  task in the same chat and records `transport_auto_retry_superseded` on it in
  the admission transaction.
- New code honours that receipt: `_transport_retry_superseded` in
  `background.py` (the in-process timer) and the parked retry in
  `runs/provider_login.py` stand down when it exists, as the restart path
  already does. Each gets a test.
- The old turn keeps its record. The Discuss-to-Work continuation guard in the
  store is unchanged.

Recorded `artifact_edit` tasks are not chat turns: their Resume and Retry keep
`launch_kind="discuss"` or `"revoking"` through the closed runner and the
scratch kind as today. Rule 5's "no new `launch_kind=\"discuss\"`" covers new
admissions only. Steering a resumed historical Discuss turn keeps today's
rules.

The runner can be deleted when a store has no non-terminal Discuss task, no
owed Discuss retry and no pending Discuss question. The later release checks
this at startup and expires any leftover Discuss question with a visible note
before it removes the runner. Display keeps the "Discuss" label on old
turns and on their question cards.

## Config and readiness

- `agent.node_chat` and `agent.project_chat` permission tables in `rcp.toml`
  that equal the old Discuss table load as the Work table, like the existing
  `legacy_chat` rule. Any other table keeps today's refusal.
- `rcp server provider check` maps chat profiles to `work_auto`.

## Voice and WebMCP

The WebMCP send tool drops its `mode` input; a call that still passes it is
refused by the tool schema. Its inspection output and description stop naming
Discuss. `VoiceActionReceipt.mode` (`service_connections.py`, strict model)
becomes optional: saved receipts still decode, new receipts omit it, and voice watches every send
(today it watches only `mode === "work"` in `voiceExecutor.ts`). Today the
send tool asks for confirmation only for Work, and only voice reads
that (`webmcp/shared.ts:28-46` strips it before host registration). Since every
send is Work, the send tool's `confirm` becomes always true. Voice keeps
honouring the member's `tap` or `none` setting. WebMCP stays unguarded, as
today. The voice playbook line that hands questions to a Discuss turn hands
them to an ordinary send.

## Decisions

- **D1 (decided): chat on the source branch of a running merge is refused.**
  Merges take seconds to minutes. The composer shows the existing merge-fence
  message and the member sends again when the merge ends. No new mechanism.

## Code changed

| Area | Change |
| --- | --- |
| `api/tasks.py` chat admission | Work-only for `node_chat` and `project_chat`; 422 for `discuss` |
| `agents/prompts.py` | New masters are Work-only; Discuss contract kept for the closed runner; `turn_mode="paper"` unchanged |
| `artifact_edit_admission.py`, `api/artifacts.py` | Ordinary-chat Edit now uses the Send route; refused chats keep the scratch kind |
| `config.py`, `setup.py`, `server_ops/provider_readiness.py` | Work table, legacy load rule, `work_auto` mapping |
| `chat/QuestionCard.tsx` | Old Discuss origins keep their label; new questions are Work |
| Web: `NodeChat.tsx` toggle, `chatWorkspace.ts` mode store, `webmcp/conversations.ts`, `webmcp/playbook.ts`, `voice/voiceExecutor.ts` | Remove toggle and mode state; confirm always |
| Paper `mode="discuss"` styling props | Style by component |

Retained for one release: `runs/tasks/discuss.py`, Discuss finalizers, Discuss
provider profiles (also used by the scratch `artifact_edit` kind), Discuss
question binding.

## Specs to update

- `conversations-episodes-and-watchers.md`: replace "Discuss and Work turns"
  with one chat-turn section, the historical-Discuss rules above, and the
  question rule at 613-655.
- `providers-and-containment.md`: capability table (Discuss row describes
  historical and scratch-edit runs only), probe rule.
- `authority-and-proposals.md`, `api-web-and-desktop-projections.md`,
  `interface-and-visual-design.md`, `auto-research-and-branch-merge.md`
  (merge fence and chats), `docs/design.md`.
- AGENTS.md invariant 10b: "Only a captured Work turn has conversation graph
  authority. Message wording or a stray file cannot grant it." Invariant 4
  keeps "Discuss has no graph authority", which still holds for historical and
  scratch runs.

## Tests

- Admission: chat routes refuse `discuss` and default an omitted mode to Work.
- Old-writer store (a real SQLite store written by the current release) with a
  finished Discuss turn, a running remote Discuss pass, a transport-retry-owed
  Discuss task, an auth-parked Discuss task and a pending Discuss question.
  After upgrade: rows decode, the remote pass settles through the closed
  finalizer, both retries run as Discuss, the question answer runs as Discuss,
  and a human Retry admits Work.
- A stored request with no mode still decodes as Discuss.
- A transfer import shows old Discuss turns with their label (display only).
- Artifact comment in an ordinary chat runs a Work turn via Send; in a refused
  chat it runs the scratch kind.
- Config legacy load of the Discuss chat tables.
- Voice send asks per the member's setting for every send.
- Remove tests of the Discuss toggle and new Discuss admission. Keep the
  Discuss runner tests while the runner remains.

## Delivery

One PR in slices, each green on its own:
1. Route admission (the 422 lives in the task route's chat admission in
   `api/tasks.py`) together with the web composer, voice and WebMCP sends, and
   the removal of the refused-chat Work refusal in
   `admit_chat_artifact_comment`, so no slice sends a mode a route refuses.
   Config, readiness, old-writer regression, Retry-to-composer with the
   superseded receipt and both retry paths honouring it.
2. Artifact-comment routing (Send for ordinary and Experiment chats, the
   revoking kind for refused chats) and the viewer's one button.
3. Web cleanup: Paper styling props, question card label.
4. Specs and AGENTS.md.

This design file is deleted before merge once its rules live in the specs.
