# Standby voice robustness

Date: 2026-10-08
Status: design, waiting for the human's go. Nothing is implemented yet.

Implemented: nothing.
Remaining: slices 1 to 5 below, then the live checks.

Settled with the human on 2026-10-08:

- In the desktop app on macOS 14 or later, hiding the window keeps voice
  open. The idle limit and hard cap still apply. On macOS 13 and in browsers,
  hiding still ends it, and the panel says why.
- Resume uses a text transcript RCP saves, not an OpenAI fork. RCP keeps
  sending `store: false`. Nothing stays on OpenAI's side.
- One PR holds all five slices.
- Priority order: staying connected while hidden first, broad reads second.
- A code-owned playbook (slice 5) tells page agents how RCP work is routed.
- Reads become broad (one generic project GET tool). Writes stay named and
  card-gated. No page-driving or browser control: it would let the agent press
  Confirm itself and send the screen to OpenAI.

## What is wrong today

1. **Hiding ends the session.** `endOnPageSuspend`
   (`web/src/voice/voiceSession.ts`) ends voice on `visibilitychange` to
   hidden. Swiping to another desktop, minimizing, or covering the window all
   count as hidden. The spec asks for this because a hidden WebKit page may stop
   running timers, which would freeze the idle limit and the hard cap.
2. **No history, no resume.** RCP keeps no transcript, so a voice session
   never shows in the Agents panel. OpenAI has no reattach. A dropped session
   is gone (a live probe got 404 on `/live/sessions/{id}/attach`).
3. **Tools with optional fields fail.** In a live session the model said
   `rcp_list_artifacts` *requires* all four filters. It sent several, and
   `artifactFilter` refused with "at most one". Likely cause: Responses
   delegation treats tool schemas as strict, and strict mode makes every
   property required. `create_session` never sends `strict: false`. Every
   catalog tool with optional fields is exposed to this.
4. **No tool reads the inbox.** `rcp_open_view` can show the inbox, but no tool
   returns its items or a consolidation report's text. So voice can't answer
   "what did last night's consolidation find?"
5. **Thin instructions.** `INSTRUCTIONS` in `src/rcp/voice.py` is three lines.
   It doesn't say the agent acts through RCP's page tool catalog (the same one
   WebMCP gets) inside the member's page. It doesn't say the member can only
   speak. In the live session the agent dodged "how do you work?" and asked
   for pasted text three times.

## Slices

### 1. Keep voice while hidden (desktop, macOS 14+)

- `web/src-tauri/src/windows.rs`: build the main window with
  `background_throttling(BackgroundThrottlingPolicy::Disabled)`. Tauri 2.11
  applies it on macOS 14+ and ignores it on 13.
- A native command tells the page whether hidden timers keep running: true
  only for the desktop app on macOS 14+. The page reads it once.
- `endOnPageSuspend` takes that answer. When kept, `visibilitychange` no
  longer ends voice. `freeze` and `pagehide` still do. When not kept,
  behavior is unchanged, and the end notice says hiding ended it.
- The idle limit and hard cap stay page timers. They work because the page is
  no longer throttled.
- Check: a web unit test for the policy branch, plus the live check below.

### 2. Saved transcripts, Agents panel, Resume

- Storage: a private per-member file beside the member's voice settings,
  owned by the same store (`rcp/service_connections.py`) under its lock. It
  isn't canonical state, isn't visible to the team, and holds no audio. Each
  record holds the id, start and end time, end reason, and the panel's lines
  (member, agent, and tool activity). A count limit and a per-session line
  limit live in `limits.py`. The oldest records drop first.
- API: `PUT /api/voice/sessions/{id}/transcript` (the page saves as lines
  land, throttled, plus once at end). `GET /api/voice/sessions` lists them.
  `DELETE` removes one. All are member-private and use the existing service
  connection route.
- Agents panel: a **Voice** group lists the member's recent sessions. A row
  opens a read-only transcript with **Resume**.
- Resume: `POST /api/voice/sessions` takes an optional `resume_id`. The
  backend reads the saved lines and passes them as `session.input` (user /
  assistant text, inside OpenAI's 128-message / 8,192-token limit, newest
  kept). Tool lines become short assistant text. The new session continues
  the same record. Watches and in-flight calls are not restored. The agent is
  told to check status again before claiming anything.
- A dropped connection keeps the record, so Resume works right after a drop.
- Docs: replace the "keeps no transcript" line in
  `docs/specs/api-web-and-desktop-projections.md`. Add a decision record for
  the privacy change (text kept locally, private to the member, no audio).

### 3. Non-strict tool schemas

- `create_session` sends each delegated tool with `strict: false`. The
  page-side validators stay the authority for argument shape.
- First, confirm the cause live. Run one session that asks for artifacts with
  no filter, before and after the change.
- Check: a backend test that the outgoing tool payload carries `strict: false`
  (structure, not wording).

### 4. Broad reads and instructions

Writes stay as named, card-gated tools. Reads become broad, so the agent can
answer about anything the member can see without a hand-written tool per
screen. This replaces an inbox-only tool. It adds no authority: every read is
a GET the member's page could already make.

- `rcp_list_read_routes`: the open project's GET route templates, taken from
  the backend's OpenAPI schema and filtered by the same rule `rcp_read` uses.
  The tool description and enforcement share one resolved object.
- `rcp_read`: one GET under `/api/projects/{open project}/`, as the member.
  It refuses other prefixes, other methods, streams (SSE), and non-JSON or
  non-text bodies. Results are size-limited (limit in `limits.py`) and marked
  untrusted. This covers the inbox, consolidation reports, artifacts, tasks,
  and episodes.
- Both are available to voice and WebMCP, in a new `webmcp/` module beside
  `artifacts.ts`.
- `INSTRUCTIONS`: the agent acts through RCP's page tools in the member's own
  page (the WebMCP catalog), as the member. It doesn't see the screen. It uses
  `rcp_read` when no named tool fits. The member can only speak, so it never
  asks for pasted or typed text. It names the exact tool that refused and
  retries with corrected arguments. Prompt tests check data and enforcement
  only, never wording.
- Checks: tests that try a path outside the open project, a non-GET, an SSE
  route, and an oversized body, and that the route list matches the
  enforcement filter.

### 5. Playbook: how RCP work gets done

Today a page agent gets three lines of instructions and one-line tool
descriptions. Nothing says that making something (a visualization, a live
dashboard, an analysis, code) means a Work turn on the relevant node, whose
node agent already knows the artifact contract. So "make me a live dashboard
for this experiment" depends on luck.

- The playbook is domain knowledge in plain language, about 25 lines. It
  names no tools; tool descriptions map each step to a call. It describes
  workflows, not enforcement, so it is not rendered from an enforcement
  object. One module in `web/src/webmcp/` holds it.
- Contents, settled 2026-10-08:
  - What RCP is: the node graph, conversations, episodes (Experiment loops,
    Auto-research on a branch), artifacts, and the Inbox (proposals, nightly
    consolidation reports).
  - Role: the member's voice in their own page, acting as them. Node agents do
    the research; the voice agent finds, reads, explains, routes, and reports.
  - Catch me up or explain: overview first, then only as deep as asked.
  - Make something: pick the node and say which one, then send at once (the tap
    card, when on, is the guard). Continue the node's recent conversation if it
    has one; otherwise start a fresh node conversation. Work mode, with the
    member's words plus what they want to see. Announce the finish and open the
    result.
  - Think or plan: Discuss on the node.
  - Run or keep going: start the Experiment or authorize Auto-research, saying
    the budget aloud first. Stop means a graceful stop.
  - Show me: open the view or artifact.
  - Member-only: proposal judgment, Decision choice, standing, Hypothesis
    status, branch merge. Say where to tap.
  - Voice manners: speaking only, maybe not looking. Never ask to paste or type.
    Gist first, names not ids, one short question when the node or project is
    unclear, nothing claimed until a result confirms it.
- Voice: the page sends the playbook with the session request. The backend
  appends it to its fixed instructions under a size limit.
- WebMCP: a read-only `rcp_get_playbook` tool returns the same text.
- Checks: voice and WebMCP get the same object, and it stays under its size
  limit. No wording tests.

## Ownership and checks

| Slice | Files | Focused check |
| --- | --- | --- |
| 1 | `windows.rs`, a native command, `voiceSession.ts`, `useVoiceAgent.ts` | web voice tests, Tauri build |
| 2 | `service_connections.py`, `api/voice.py`, `voice.py`, `limits.py`, `useVoiceAgent.ts`, Agents panel, `core/types.ts` | `tests/test_voice*.py`, web voice tests |
| 3 | `voice.py` | `tests/test_voice*.py` |
| 4 | `webmcp/` new module, `webmcp/index.ts`, `voice.py`, `limits.py` | web webmcp tests |
| 5 | `webmcp/playbook.ts`, `webmcp/index.ts`, `voice.py`, `api/voice.py` | web webmcp tests, `tests/test_voice*.py` |

`core/types.ts` and `voice.py` are shared, so they are edited serially.

## Live checks

Run with a real OpenAI key in a rebuilt `RCP Dev.app` on disposable data:

- Open voice, swipe to another desktop for 2 minutes, and keep talking. It
  stays open. A forgotten hidden session still ends at the idle limit.
- On macOS 13 (if available), hiding still ends it, with the notice.
- Kill the network mid-session, then Resume from the Agents panel. The agent
  remembers the earlier topic.
- Ask for artifacts and for last night's consolidation report. Both answer.
- Ask "make me a live dashboard for experiment X" with no other hint. It sends
  a Work turn on that node, announces the finish, and opens the result.
