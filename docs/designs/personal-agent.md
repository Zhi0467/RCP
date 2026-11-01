# Personal agent

Working design for one PR. Its rules move into the owning specs, and this file
is deleted before merge.

## Why

A member reaches RCP through many surfaces today: the space's project cards,
each project's views, Work chats, the Agents board, the Ask buttons and the
voice panel. The personal agent gives each member one place to talk to RCP.

It grows from the voice agent. Voice already acts as the member across
projects, but it is voice only, has no memory, and stops when the tab closes.

## Topology

- Each member has one personal agent per space. It runs in that space's RCP
  backend.
- Its threads are one **Space thread** plus one **project thread** per project
  the member can see. Each thread is private to the member and continues
  indefinitely.
- The space home has two views: project **Cards** and **Agent**. The Agent view
  has a left rail with the Space thread and every project thread, each with an
  unread dot. This is the main surface.
- Inside a project, the Agents view opens the member's project thread full
  width. The team's board conversations stay reachable there.
- The voice launcher, the desktop Ask button and the phone Ask button are
  removed. The agent replaces all three.

## Capability

Both thread kinds get full tooling. Handing work to another agent is a workflow
instruction, never a limit.

- **Project thread.** It runs on the project's base with Work-level capability,
  meaning files plus that project's Patch stage.
- **Space thread.** It runs where the backend lives and gets the union of the
  member's project read and write roots. It reaches projects on other bases
  through the existing machine verbs. Each run gets one Patch stage per project,
  and each project's Patch is collected and applied as its own transition.
  - A change across projects is therefore not atomic.
  - Invariant 4b becomes "one exact stage per graph target per run".
- **Browser.** On by default. A dropped browser connection blocks nothing.
- **MCP servers.** None, the same as Work today.
- **Provider sub-agents.** Allowed, as in Work.

### Verbs

- Every existing Work verb: `ask`, `episode`, `apply`, `bulletin`, `lesson`,
  `status`, graph log and diff, and the compute verbs.
- `ask` gains an optional action. A protected act becomes an approval card,
  and the member's tap runs it.
- `spawn` and `message` are reused from the orchestrator to start and message
  Agents-board conversations.
- New verb `projects`: read the member's projects and their state across the
  space.
- New verb `schedule`: create, list and cancel reminders and check-ins, in the
  cron style of Claude Code's scheduling.

Unseen agent messages already push, so there is no separate notify verb.

### Authority

- Protected acts wait for the member. These are approving Proposals, changing
  truth membership, authorizing Auto-research and dispatching a merge.
  - The card appears inline in the thread that asked, and as a push.
  - The tap runs the normal human route.
  - The card is a single-use record owned by the member. Any signed-in channel
    may resolve it for its owner.
- Ordinary member actions run without a tap and are recorded as "member, via
  agent", with a link back to the request.
- Replies link the nodes, runs, chats and Proposals they mention. The backend
  agent never drives the member's tab. Voice keeps its page tools.
- No agent-to-agent messaging. Agents share only through the Bulletin, whose
  mentions never wake anyone.

## Context and memory

These are two separate things.

- **Compaction belongs to the provider.** Each thread keeps one native session,
  and the provider auto-compacts it at its threshold.
  - RCP triggers compaction only on a provider switch. It compacts the old
    session and seeds the new provider with the result.
  - A lost session cannot be compacted. The fresh session starts from memory
    plus the transcript tail, and the thread shows that it started fresh.
- **Memory is files and prompt.** Lessons and memory are unified as one folder
  per project on its base, outside git:
  - `shared/` holds a `MEMORY.md` index plus note files. This is today's
    lessons.
  - `members/<member>/` holds the member's private notes for that project.
  - Agents edit these with ordinary file tools.
  - Every project agent reads `shared/`. The member's threads also read and
    write `members/<member>/`. The Space thread reads every project's memory.
  - There is no cross-project personal memory.
  - "Child agents only add lessons" becomes a prompt rule.
  - Note authorship and SQLite backup are given up.
  - Existing lesson rows migrate to files once. Consolidation prunes the files.
  - The Settings Lessons screen becomes a Memory screen over the files.
- Native CLI memory is off for personal-agent runs.

### Storage

- **Records** (messages, schedules, approval cards) live in the space's SQLite.
- **Files** live in persistent private workspaces:
  - one per Space agent on the backend's machine
  - one per project thread on that project's base

## Behaviour

- **Wake-ups.** The agent wakes on three things:
  - a member message
  - its handed-out work finishing or needing the member
  - a schedule the member set

  There is no turn limit; agents stop on their own. Schedules are visible and
  cancellable.
- **Voice.** A button inside the agent. A realtime voice model reads and writes
  the same thread and memory and hands work to the backend agent.
- **Provider and model.** The agent picker, per thread, billed through the
  member's sign-in order.
- **Recovery, Stop and sign-in failure.** As in Work chats. Missed schedules
  fire once, combined, when the backend returns.

## Slack seam

Slack follows the RCP version as a minimal bot on the same threads.

- **Now:** a message `source` field and channel-neutral delivery.
- **Later:** Slack approval uses Slack-signed interactions from a Slack user
  that the member linked from inside RCP.

## Open before build

- Rendered mockups: space Agent view, project Agents view, phone, Memory
  screen.
- Probe each provider CLI's headless or app-server compaction call, for the
  provider-switch case.
- egm review: `design`, `implementation-readiness`, `zoom-out`.
