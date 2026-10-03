# Standby voice agent

Date: 2026-10-02
Status: design settled with the human on 2026-10-02 (issue #229, part 2).
Nothing is implemented yet. This builds on the model-backed dictation handoff
(part 1, its own PR): it reuses that PR's member service connections and its
microphone owner. A live GPT-Live probe gates the session work below.

Close this handoff when all of these hold on real hardware:

- on the desktop app, a member opens a voice session, asks about a project,
  hears a correct answer, and asks it to open a node, which opens;
- by voice, the member sends a Work message, starts an Experiment, and
  authorizes Auto-research, once with "Tap to confirm" and once with "Run
  without confirming", and hears when each one finishes;
- by voice, the member gracefully stops a running episode;
- the same session works from a team member's phone web app;
- a forgotten session ends at the idle limit, and at the hard cap even while
  the page is suspended.

## Settled

- **It acts as the member.** Its actions are the authenticated member's own,
  exactly like today's WebMCP tools. No new actor type. History shows the
  member. See the
  [decision record](../decisions/2026-10-02-agents-in-a-member-page-act-as-that-member.md).
- **Its tools are the shared WebMCP tool list.** One list serves both WebMCP
  host agents and voice. It gains two things, for both:
  - an **Auto-research authorization** tool;
  - **navigation** tools that open a node, a conversation, a run, or the Inbox.
- **What it can do:** read projects, nodes, conversations, artifacts, and
  Experiments; open views; send Discuss and Work messages; start an
  Experiment; authorize Auto-research; gracefully stop an exact episode.
- **What stays tap-only, outside the list:** Proposal judgment, Decision
  choice, node standing, Hypothesis status, graph editing and Sync, truth
  membership, branch-merge dispatch, settings, membership, project creation and
  deletion, and artifact retention.
- **Confirmation toggle** in the voice panel: **Tap to confirm** (default) or
  **Run without confirming**. RCP remembers the choice per member. It governs
  Work messages, Experiment Start, and Auto-research authorization. In tap mode
  the agent fills in the action and shows a card with the exact message, target,
  and budget; it runs only on Confirm. WebMCP host agents keep running without
  an RCP confirmation.
- **Click to open, click to end.** No always-listening microphone, no
  background session. A session also ends after a few silent minutes, at a hard
  cap, and on reload, logout, or leaving the space. The panel shows the elapsed
  time. This keeps the bill bounded: GPT-Live costs $0.05 per minute of open
  session.
- **It speaks first only about its own work.** When a Work turn, Experiment, or
  Auto-research episode it started in this session finishes or needs the
  member, it says so.
- **No memory across sessions.** The panel shows the live transcript, and RCP
  never stores it. The durable records its actions create are ordinary RCP
  records.
- **Billing.** The member's own OpenAI connection, with **Use for voice**
  checked separately from transcription, so a transcription-only key is never
  used for voice. The delegation model defaults to `gpt-6-luna` and can be
  changed in Settings.
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
2. The page posts the offer to `POST /api/voice/sessions`, with the shared
   tool definitions.
3. The backend reads the member's voice-enabled OpenAI key, calls
   `POST /v1/live/sessions` with the offer, the tools, Responses delegation,
   the chosen model, and RCP's fixed instructions, and returns the answer SDP
   and session id. The browser never sees the key.
4. Audio flows between the browser and OpenAI. A WebRTC data channel carries
   events.
5. When the Responses model calls a function, the page's executor receives it
   on the data channel, runs it, and returns `response.item.create`
   (`function_call_output`) then `response.create`.
6. The member clicks to end, or a limit ends it. `DELETE
   /api/voice/sessions/{id}` tells the backend.

### The executor

- It runs only functions in the shared tool list, against the current page
  state, with the same id revalidation WebMCP already does. Anything else
  returns an error result. A tool that is unavailable on the current screen
  returns a refusal; the session keeps one fixed tool schema list.
- It deduplicates `call_id`s, so a repeated call runs once.
- It never replays a Send or Start whose outcome is unknown, for example after a
  dropped connection. It reports "unknown, check the chat" instead.
- In tap mode, confirm-required calls wait for the card. GPT-Live keeps talking
  meanwhile. Decline or timeout returns "not confirmed".

### Speaking first

The page already polls task and episode state. The voice module remembers the
ids it started this session. When one finishes or needs the member, it sends
one bounded `session.commentary.append` with `delegation_id: null`, and
GPT-Live says it.

### Limits

New values in `limits.py`: idle end (start at 3 minutes without speech), hard
cap (start at 30 minutes), confirmation timeout, and commentary length.
The page ends a session on idle. The backend owns the hard cap so it holds
while a page is suspended: it uses the session's own duration limit if the
Live API has one, otherwise it attaches a sideband
(`wss://api.openai.com/v1/live/sessions/{session_id}/attach`) and sends
`session.close` at the deadline.

## Backend changes

- Service connections gain a **voice** purpose, only for the OpenAI preset.
  Turning it on runs one real authenticated request for the live model, and
  saves the purpose only if it succeeds.
- `POST /api/voice/sessions` and `DELETE /api/voice/sessions/{id}`, acting for
  the authenticated member. One open session per member.
- The member's voice settings: delegation model and the confirmation toggle.
- No audio passes through RCP.

## Web changes

- Shared tool list: the Auto-research authorization tool composes the same
  owner and fences as the visible control (readiness, budget, single start).
  Navigation tools use the existing deep links (`notificationLinks.ts`).
- Voice panel: open and end button, elapsed time, live transcript, the
  confirmation toggle, confirmation cards, and the disclosure line.
- Executor, as above.
- The microphone owner from dictation refuses a voice session while dictating,
  and the reverse.

## Slices

Three slices run in parallel; the probe gates merging the session slices, not
starting them.

0. **Live probe** with a real OpenAI key, on a throwaway server. The human
   connects the key; agents never type it. Prove one WebRTC session with
   Responses delegation and one custom function: the page receives the call on
   the data channel, returns the result there, and the model continues and
   speaks. Also check: a disconnect during an accepted call, whether session
   config has a duration limit, and a `delegation_id: null` commentary. Record
   the results here.
1. **Shared tools** (web): Auto-research authorization and navigation tools,
   in WebMCP too. Starts now from `main`.
2. **Session backend** (Python): voice purpose, routes, member voice settings,
   hard cap. Starts once the dictation connection store lands; merge that
   branch in first.
3. **Voice panel and executor** (web). Starts with slice 1, against slice 2's
   route contract.

## Tests

One test per invariant, no wording assertions:

- the executor refuses a function outside the shared list;
- in tap mode, a confirm-required call runs only after Confirm; in the other
  mode it runs at once; reads never wait;
- a repeated `call_id` runs once, and an unknown-outcome Send is not replayed;
- a session cannot open without a voice-enabled connection, and the key never
  appears in a response;
- the hard cap closes the session;
- the Auto-research tool refuses wherever the visible control would.

## Docs to update when this lands

- API, Web, and desktop projections spec: the WebMCP section (new tools, a
  shorter exclusion list) and a voice section.
- Authority and Proposals spec: a human action includes an agent the member
  runs in their own page, per the decision record.
- `docs/design.md`: the WebMCP paragraph names Auto-research and voice.
