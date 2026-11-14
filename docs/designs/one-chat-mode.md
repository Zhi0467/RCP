# One chat mode

Extend the private-thread rule ("threads have no Discuss mode") to every human
chat. Every new human chat turn is a Work turn. The composer's Discuss/Work
toggle goes away. Historical Discuss tasks keep their records, recovery and
display through a closed Discuss runner that admits nothing new. A later
release deletes that runner once no store holds an unsettled Discuss task.

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
- **Chat on a branch that is the target of a running merge.** Work admission is
  refused with 409 while the merge fence holds (`api/graph_changes.py:183`,
  `storage/agent_tasks.py:1487`), and a live chat turn blocks a merge from
  starting (`merge_source_has_live_writer`). See D1.

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
   - In a chat that accepts human turns, Edit now becomes the viewer's existing
     Send route (`artifact_send_chat_turn`, `api/artifacts.py:291-323`): a Work
     turn in that chat carrying the comments. No new dispatcher. The chat-master
     `launch_kind="discuss"` edit is no longer admitted.
   - In a chat that refuses human turns (a running orchestrator or Auto-research
     child, `storage/agent_tasks.py:1039`), the scratch-only `artifact_edit`
     kind (today's `launch_kind="revoking"`) handles comments unchanged. It
     keeps `mode: "discuss"` and capability `discuss` internally. Rule 1 does
     not reach it, because it is admitted by `admit_artifact_edit`, not the chat
     route.
   - Episode-report replies follow the same split.
6. The Work chat master is the only master for new turns.
   `CHAT_MASTER_CONTEXT_VERSION` bumps, so a resumed session gets the Work-only
   master through the existing master-replacement rule.

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
  authority as today (`storage/agent_tasks.py:349,397`);
- legacy private-edit stage proofs (`discuss.py:609-768`).

A human Retry of a settled Discuss turn is not a recovery: the button reads
"Retry" and admits a fresh Work turn with the same message through the chat
route. The old turn keeps its record. Display keeps the "Discuss" label on old
turns and on their question cards.

## Config and readiness

- `agent.node_chat` and `agent.project_chat` permission tables in `rcp.toml`
  that equal the old Discuss table load as the Work table, like the existing
  `legacy_chat` rule. Any other table keeps today's refusal.
- `rcp server provider check` maps chat profiles to `work_auto`.

## Voice and WebMCP

Today the send tool asks for confirmation only for Work, and only voice reads
that (`webmcp/shared.ts:28-46` strips it before host registration). Since every
send is Work, the send tool's `confirm` becomes always true. Voice keeps
honouring the member's `tap` or `none` setting. WebMCP stays unguarded, as
today. The voice playbook line that hands questions to a Discuss turn hands
them to an ordinary send.

## Decisions

### D1. Chat on the target branch of a running merge

- **(Recommended) Accept the refusal.** Merges take seconds to minutes. The
  composer shows the existing merge-fence message and the send waits for the
  human.
- Admit the Work turn and hold its Patch at Apply until the merge ends. Chat
  never waits, but the fence gains a second shape.

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
1. Route admission, config, readiness, old-writer regression.
2. Artifact-comment routing.
3. Web: toggle, voice and WebMCP, Paper styling, question card.
4. Specs and AGENTS.md.

This design file is deleted before merge once its rules live in the specs.
