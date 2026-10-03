# Standby voice agent

Date: 2026-10-02
Status: design settled with the human on 2026-10-02 (issue #229, part 2), then
revised the same day after an astra xhigh review and a Claude review.
Implementation started on this PR on 2026-10-02. Done: slice 1 (shared tool
catalog, Auto-research authorization and Stop, `rcp_open_view`, stoppable
Auto-research ids in the overview), slice 2 (voice purposes, voice settings,
the stateless session route), and slice 3 (voice panel and executor, driven
headless against a fake transport), voice-only terminal tools, and the spec
and `AGENTS.md` updates. On 2026-10-03 the human ran a live session in a
browser on a throwaway server: connect, choosing the voice connection,
session creation, spoken answers, project reads, and opening Settings worked. A headless drive
of the same served app, with only the OpenAI exchange faked, proved the Work
card, Confirm, and an accepted Work turn, and a confirmed terminal command
read back its output. Remaining: a live Work turn and terminal command spoken
end to end, and the real-hardware checks below. This builds on the
model-backed dictation handoff (part 1, its own PR): it reuses that PR's
member service connections, member settings file, and microphone owner.

Close this handoff when all of these hold on real hardware:

- on the desktop app, a member opens a voice session, asks about a project,
  hears a correct answer, and asks it to open a node, which opens;
- by voice, the member sends a Work message, starts an Experiment, and
  authorizes Auto-research, once with "Tap to confirm" and once with "Run
  without confirming", and hears when each one finishes, including after
  moving to another project;
- by voice, the member gracefully stops a running Experiment episode and a
  running Auto-research episode;
- the same session works from a team member's phone web app;
- a forgotten session ends at the idle limit, and closing or suspending the page
  ends the paid session.

## Settled

- **It acts as the member.** Its actions are the authenticated member's own,
  exactly like today's WebMCP tools. No new actor type. History shows the
  member. See the
  [decision record](../decisions/2026-10-02-agents-in-a-member-page-act-as-that-member.md).
- **Its tools are the shared WebMCP tool list.** One list serves both WebMCP
  host agents and voice. Additions, for both: Auto-research authorization,
  Auto-research Stop, and one `rcp_open_view` navigation tool.
- **What it can do:** read projects, nodes, conversations, and Experiments;
  list and open artifacts and reports; open views; send Discuss and Work
  messages; start an Experiment; authorize Auto-research with any budget, like
  the visible form; gracefully stop an exact Experiment or Auto-research
  episode; list the project's terminals, and type one command line into a
  repository's terminal and hear its recent output. A terminal command always
  waits for a tap, even with **Run without confirming**. The terminal tools are
  voice-only; WebMCP does not register them.
- **It does not read artifact contents.** Existing artifact tools return
  metadata and open the viewer; the voice model cannot see the viewer. A
  question about an artifact's content goes through a Discuss message.
- **What stays tap-only, outside the list:** Proposal judgment, Decision
  choice, node standing, Hypothesis status, graph editing and Sync, truth
  membership, branch-merge dispatch, settings, membership, project creation and
  deletion, and artifact retention.
- **Confirmation toggle** in the voice panel: **Tap to confirm** (default) or
  **Run without confirming**, remembered per member. It governs Work messages,
  Experiment Start, and Auto-research authorization. WebMCP host agents keep
  running without an RCP confirmation.
- **Click to open, click to end.** No always-listening microphone, no
  background session. A session also ends after a few silent minutes, at a hard
  cap, and on reload, logout, or leaving the space. The panel shows the elapsed
  time. GPT-Live costs $0.05 per minute of open session.
- **It speaks first only about its own work.** When a Work turn, Experiment, or
  Auto-research episode it started in this session finishes or needs the
  member, it says so, wherever the member has navigated since.
- **No memory across sessions.** The panel shows the live transcript, and RCP
  never stores it. The durable records its actions create are ordinary RCP
  records.
- **No audio passes through RCP.** Audio goes between the browser and OpenAI.
- **Billing.** The member's own OpenAI connection, chosen under **Standby
  voice agent → Runs on** separately from transcription, so a
  transcription-only key is never used for voice. The delegation model
  defaults to `gpt-6-luna` and can be changed in the voice settings.
- **Clients:** desktop app, team browser app, team phone web app. A personal
  paired phone stays notify-only.
- **Disclosure.** The panel says that audio, and the project content the agent
  reads, go to OpenAI.
- **Not a ChatGPT plugin.** That needs public HTTPS and OAuth, which RCP does
  not offer.

## How a session works

GPT-Live (`gpt-live-1`) is a full-duplex voice model that calls no tools
itself. RCP uses its **Responses delegation**: GPT-Live hands work to a
Responses model, that model picks tools, and the app executes them.

1. The member clicks the voice button. The page takes the microphone through
   the shared owner and creates a WebRTC offer.
2. The page posts the offer and the catalog's tool schemas to
   `POST /api/voice/sessions`.
3. The backend reads the member's voice-enabled OpenAI key, calls
   `POST /v1/live/sessions` with the offer, the tools, Responses delegation
   with `parallel_tool_calls: false`, the member's delegation model, and RCP's
   fixed instructions, and returns the answer SDP. The browser never sees the
   key. The backend keeps no session state.
4. Audio flows between the browser and OpenAI. A WebRTC data channel carries
   events.
5. When the Responses model calls a function, the page's executor receives it
   on the data channel, runs it, and returns `response.item.create`
   (`function_call_output`) then `response.create`.
6. The page ends the session: on the member's click, the idle limit, the hard
   cap, reload, logout, leaving the space, loss of identity, or the page going
   hidden. It sends `session.close`, waits a bounded time for the close to
   finish, then closes the peer connection.

**The page owns the session's lifetime.** A frozen page runs no timers, so the
page does not wait for its own idle timer there: it ends the session on
`visibilitychange` to hidden, `pagehide`, and `freeze`. On the desktop that
means hiding or minimizing the window ends voice. The Live session config has
no duration limit (checked against the API reference on 2026-10-02), so the
page's hard cap and transport loss are the only bounds. The probe checks real suspension on iOS Safari and a hidden desktop
window, and how fast billing stops after the transport drops. There is no
sideband: a sideband receives reflected audio, which would break "no audio
passes through RCP".

**Identity loss ends it at once.** The voice session follows the same verified
identity and team-session state that retires WebMCP's surface on a reconnect
screen. Logout, a revoked session, member removal, or any 401 or 403 on the
page's polling ends the session immediately. The executor checks that state
before every call, so reads from the page's cached snapshot stop too.

Two tabs can each open their own session; each is a deliberate click and its
own bill.

### The shared tool catalog (slice 1 owns it)

Today the tool definitions exist only inside App's WebMCP memo, change with page
state, and get a registry only when a WebMCP host exists
(`createWebMcpToolRegistry` returns null without `document.modelContext`).
Slice 1 adds a host-independent catalog:

- `catalog(): {name, description, inputSchema, confirm}[]`: a fixed list of
  every tool, independent of page state. `confirm(args)` is a predicate on the
  tool's own definition. Conversation Send returns true only for
  `mode: "work"`; Experiment Start and Auto-research authorization return true;
  every other tool returns false. There is no separate confirmation list.
- `catalogAsFunctionTools()`: the one serializer to the Responses function
  format, `{type: "function", name, description, parameters}`. It leaves out
  `confirm` and any other local metadata.
- `resolve(name)`: the current executable definition built from the latest page
  state, or a refusal that says why (wrong screen, nothing to stop, and so on).
- WebMCP keeps registering conditionally from the same definitions.

### The executor

- It runs only names in the catalog, through `resolve`, with the same id
  revalidation WebMCP already does. Anything else returns an error result.
- With `parallel_tool_calls: false`, each response carries at most one function
  call. A declined or timed-out confirmation returns "not confirmed".
- It deduplicates `call_id`s. It also keeps a per-session list of calls whose
  outcome is unknown, for example after a dropped request, and refuses an
  identical repeat under a new `call_id`. It reports "unknown, check the chat"
  instead.
- **Confirmation cards pin the exact action.** A card holds project, graph
  target, the resolved arguments, the budget, and for a message its mode and
  provider profile. It shows all of them, including the full Auto-research
  starting instruction. Confirm re-reads current state and refuses if any pinned
  value changed. Experiment Start sends the confirmed `invocation_ceiling`
  explicitly; the Run route already accepts it. One card is pending at a time.
  GPT-Live keeps talking meanwhile.

### Tools added in slice 1

- **Auto-research authorization.** Today `authorizeAutoResearch` in App.tsx
  returns nothing, writes errors into dialog state, and changes the view, and
  its fences are split between the button's `disabled` expression and the
  function body. Slice 1 extracts one start function that throws and returns
  the Episode, and one refusal check. The button and the tool both use them.
  Inputs match the form: `invocation_ceiling` (any value of at least 1),
  `starting_instruction`, `code_worktree`.
- **Auto-research Stop.** `rcp_stop_episode` also accepts an Auto-research
  episode, through the existing `POST .../episodes/{id}/stop`.
- **`rcp_open_view({kind, id})`** with kind `node`, `conversation`, `run`,
  `artifact`, or `tab`. A `tab` id is a project tab by its visible name
  (overview, inbox, research, runs, artifacts, terminals, agents, settings);
  opening Settings changes nothing. It calls the in-page owners (`openNodeById`,
  `openChats`, `showWebMcpArtifactViewer`, the view switch) within the current project and
  graph target. A `run` id is an episode id. It opens through the same exact
  episode route tokens as `episodeNotificationHash` (an Auto-research route, or
  the Experiment's board entry), not `showExperiment`, which takes a node id and
  clears the exact episode. It never switches project or
  branch; only `rcp_open_project` changes project.

### Speaking first

The voice module remembers the task and episode ids it started this session,
each with the project it started in, and polls those exact ids through that
project's task and episode routes. This keeps working after navigation; the
page's own polling covers only the open project. On a change to
finished or needs-you, it sends one `session.commentary.append` with
`delegation_id: null`. The text comes from a fixed template of kind, project
name, and status. It never includes provider answers or other authored text,
which could carry injected instructions. A refused poll (membership lost)
drops the id.

## Contracts

`POST /api/voice/sessions`, JSON (keeps the team origin check):

- request `{sdp_offer, tools}`; `tools` is `catalogAsFunctionTools()`, capped
  in size. The backend forwards it unchanged. If the probe shows the data channel
  accepts `session.update` with tools, the page sends them there instead and
  the field goes away.
- response `{sdp_answer, limits: {idle_seconds, hard_cap_seconds,
  confirm_timeout_seconds, commentary_max_chars}}`. The values live in
  `limits.py`; the page enforces them.
- errors: `voice_not_connected` (409) when the member has no voice-enabled
  connection; `voice_upstream_failed` (502) with a bounded message that never
  echoes OpenAI's raw error, which can carry key fragments.
- RCP's fixed instructions state identity and rules (act only through tools,
  never claim an action ran until its result says so). They do not list
  capabilities; the catalog does.

`GET` and `PUT /api/voice/settings`: `{delegation_model, confirm: "tap" |
"none"}`, stored in the member's settings file from the dictation PR under the
same per-member lock. A `PUT` carries only the fields it changes, so the panel's
toggle and the Settings model field never undo each other. A restore drops the
file, so settings fall back to
`gpt-6-luna` and tap.

Service connections gain `purposes`, a subset of `transcription` and `voice`.
A record without the field means transcription only. Connect takes the
purposes to enable, and each purpose has its own check: transcription keeps the
clip check, and voice makes one authenticated request for `gpt-live-1`. So a
voice-only key can be saved. `PUT /api/service-connections/{id}/purposes` takes
`{purposes}` and runs the check for each newly added purpose with the stored
key, so an existing OpenAI connection gains voice without pasting its key
again. Removing every purpose is refused; Disconnect does that. Voice applies only to the OpenAI preset, and at
most one connection has it: enabling voice on a connection moves it off any
other. That connection pays for every voice session.

## Native

The microphone usage string in `Info.plist` and `Info.dev.template.plist` says
the microphone is used only for dictation. It must also name voice, so this PR
needs a native rebuild and the desktop checks.

## Slices

0. **Live probe** with a real OpenAI key, on a throwaway server. The human
   connects the key; agents never type it. Prove:
   - one WebRTC session with Responses delegation where the page receives a
     function call on the data channel, returns the result there, and the model
     continues and speaks; then a second call in the same session;
   - WebRTC and microphone capture in the desktop app's WKWebView and in iOS
     Safari;
   - how fast billing stops after the transport drops;
   - whether session config has a duration limit, and whether the data channel
     accepts `session.update` with tools;
   - a `delegation_id: null` commentary.
   Record the results here.
1. **Shared catalog and tools** (web): catalog and `resolve`, the extracted
   Auto-research start and refusal check, Auto-research Stop, `rcp_open_view`,
   in WebMCP too. Starts now from `main`.
2. **Session backend** (Python): `purposes` and the voice check, the session
   route, voice settings. Starts once the dictation connection store lands;
   merge that branch in first.
3. **Voice panel and executor** (web, plus the plist string). Starts after
   slice 1's catalog interface is committed, against the contracts above.

## Tests

One test per invariant, no wording assertions:

- the executor refuses a name outside the catalog;
- in tap mode, a call whose `confirm(args)` is true runs only after Confirm,
  and Confirm refuses when a pinned value changed; in the other mode it runs at
  once; a Discuss Send and reads never wait;
- identity loss ends the session and refuses the next call, including a cached
  read;
- a repeated `call_id`, or an identical repeat of an unknown-outcome call,
  does not run again;
- the Auto-research tool and the button refuse in the same states;
- `rcp_open_view` never changes project or graph target;
- the session route refuses without a voice-enabled connection, and no response
  contains the key or OpenAI's raw error;
- completion commentary is built only from kind, project name, and status.
