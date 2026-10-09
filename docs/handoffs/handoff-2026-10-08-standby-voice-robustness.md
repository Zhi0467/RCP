# Standby voice robustness

Date: 2026-10-08
Status: slices 1 to 6 implemented. Slice 1 is partly
live-checked (2026-10-08): talking on another Space stayed connected, and a
minimized, silent session released the microphone at a 1-minute idle limit.
The fully occluded and app-hidden cases, speech in each state, and timer and
media measurements remain in the final live check; the native deadline
backstop stays deferred until that check, not ruled out. Each slice had one cross-model review and one
fix round.

Implemented: slices 1 to 6.
Remaining: the live checks below for slices 1 to 6.

Settled with the human on 2026-10-08:

- Goal: start voice, go anywhere on the computer, and have it explain the
  project and route work. Staying connected while hidden is priority one;
  broad reads are priority two.
- Desktop on macOS 14+ keeps voice open while the window is hidden. On macOS
  13 and in browsers, hiding still ends it, with a notice. Slice 1 is built
  and live-checked before the other slices. A native deadline backstop is
  added only if the check shows page timers lag while hidden.
- One idle rule, set per member in voice settings: default 5 minutes, range
  1 to 60. Running work does not extend it. The 30-minute hard cap stays,
  fixed.
- Resume seeds a new session from a text transcript RCP saves. It is not an
  OpenAI fork. RCP keeps sending `store: false`.
- Transcripts: text only, on the current space's backend (the team server for
  a team space), behind member-private routes. The last 20 sessions, each
  kept up to 30 days. They survive sign-out, are deleted on member removal,
  and each has Delete. Disconnecting the voice provider keeps them.
- History is a short list opened from the voice button, each row with Resume.
  It does not appear in the Agents panel.
- The same conversation resumed on two devices: the newest wins, and the
  older session ends with a notice.
- Resume gives one summary line, built by code, of what earlier work finished
  and what still runs, and keeps watching the running ones. Nothing opens on
  its own. A finish during a session is announced, and the result opens only
  on the member's yes.
- Reads are broad: every project GET, repository files included, minus a
  credential-file denylist. Writes stay named and card-gated. No page-driving
  or browser control: it would let the agent press Confirm itself and send
  the screen to OpenAI.
- A plain-language playbook gives page agents RCP domain knowledge.
- One PR holds all five slices.

## What is wrong today

1. **Hiding ends the session.** `endOnPageSuspend`
   (`web/src/voice/voiceSession.ts`) ends voice on `visibilitychange` to
   hidden, which covers swiping to another desktop, minimizing, and covering
   the window.
2. **No history, no resume.** RCP keeps no transcript. OpenAI has no
   reattach; a dropped session is gone.
3. **Tools with optional fields fail.** In a live session the model said
   `rcp_list_artifacts` requires all four filters, sent several, and was
   refused. Likely cause: Responses delegation normalizes an omitted `strict`
   to strict mode, which makes every property required. `create_session`
   never sends `strict: false`.
4. **Narrow reads.** No tool reads the inbox, a consolidation report, or the
   Artifacts panel's saved artifacts.
5. **No domain knowledge.** `INSTRUCTIONS` in `src/rcp/voice.py` is three
   lines. The agent can't explain how it acts, asks a speaking member to
   paste text, and doesn't know that making something is a Work turn on a
   node.

## Slices

### 1. Keep voice while hidden (desktop, macOS 14+)

- `windows.rs`: build the main window with
  `background_throttling(BackgroundThrottlingPolicy::Disabled)`. Wry maps it
  to `WKInactiveSchedulingPolicy::None` on macOS 14+. It does not suppress
  `visibilitychange` and is no App Nap guarantee.
- A native command, registered through the existing command permission
  machinery (`build.rs`), answers "this window keeps voice while hidden". It
  is resolved from the installed window policy and OS version. Browsers, older
  shells, and a missing command get an explicit unsupported answer.
- `endOnPageSuspend` takes that answer. When kept, `visibilitychange` no
  longer ends voice; `freeze` and `pagehide` still do. When not kept, behavior
  is unchanged and the end notice says hiding ended it.
- Idle and hard-cap deadlines are absolute times, checked on every event and
  timer tick, so a late timer cannot accept more activity past a deadline.
- Idle: one member setting in `VoiceSettings`, `idle_minutes`, default and
  range in `limits.py`. `PUT /api/voice/settings` accepts it.
- Live check (before slice 2): fully occluded, minimized, app-hidden, and on
  another Space, each with quiet periods and speech. Measure timer lateness,
  confirm media and the data channel keep working, and confirm the microphone
  is released at the idle deadline. Note whether a quiet session bills. If
  timers lag, add a native deadline that emits an end event to the page.

### 2. Saved transcripts and Resume

- Owner: `ServiceConnections` (`rcp/service_connections.py`), under its
  member lock, beside the voice settings. It keeps the existing backup and
  project-transfer exclusions and the member-removal deletion path.
- Record and attempt: `POST /api/voice/sessions` allocates the record and a
  generation. Every save and end names the record, generation, and a rising
  revision. Stale revisions, deleted records, and superseded generations are
  rejected. Resume claims a new generation, so the newest device wins; the
  older page sees "superseded" on its next save and ends with a notice.
- Client saves are serialized and coalesced, and bound to the captured member
  and space, as the voice-settings save already is. Resume recovers the last
  acknowledged transcript; an unsaved tail is not promised.
- Transcript model, separate from the panel buffer: entries with speaker
  (member or agent only), text, and provider order where available. Bounds on
  request bytes, text per entry, bytes per session, and the 20-session cap,
  all in `limits.py`. The list returns metadata with paging, not bodies.
- Action receipts beside the transcript: tool, target, call id, argument
  fingerprint, accepted task or episode id, and outcome (accepted, refused,
  unknown). Resume keeps the unknown-outcome fence, so a lost-response Work
  request is reconciled before it can be sent again. A historical
  confirmation never authorizes a new write.
- Resume: `session.input` gets user and assistant text only, cut to OpenAI's
  128-message and 8,192-token limits after conversion, newest kept, with
  truncation noted. Project content the agent quoted keeps its provenance and
  is not turned into assistant assertions. Seeded history is context, never
  authorization or proof of current status. Resume reloads the receipts'
  task and episode ids into the existing watch loop and speaks one code-built
  summary line.
- UI: the voice button opens a short list of recent sessions with Resume and
  Delete.
- Docs: replace the stateless and "keeps no transcript" lines in
  `docs/specs/api-web-and-desktop-projections.md`. Add a decision record: text
  stored on the space's backend behind member-private routes, resent to
  OpenAI on Resume, no audio. OpenAI's own abuse-monitoring retention still
  applies, since `store: false` is not zero data retention.

### 3. Non-strict tool schemas

- `create_session` sends each delegated tool with `strict: false`. The page
  validators stay the authority for argument shape.
- Live check: inspect real delegated arguments for no filter, one filter,
  conflicting filters, and a malformed write. Each malformed call is refused
  before any side effect.

### 4. Broad reads and instructions

- `rcp_list_read_routes` and `rcp_read`, for voice and WebMCP, in a new
  `webmcp/` module. Discovery uses the OpenAPI schema intersected with the
  code-owned policy below; enforcement uses the same policy object.
- Policy: every GET under `/api/projects/{open project}` (the root included),
  except:
  - repository files whose name matches a credential denylist (`.env*`,
    `*.pem`, `*.key`, `.npmrc`, `.netrc`, `id_*`, and similar), kept in one
    list in code;
  - GETs with side effects (the terminal listing's reconcile) and download
    routes.
- Transport: requests are built from templates and validated parameters. The
  normalized URL must stay same-origin and in the open project. Redirects are
  rejected. The real response MIME type is checked, SSE is refused, bodies
  are read incrementally to a byte cap, and a timeout applies. Calls are
  cancelled on identity or space loss. Routes with unbounded windows
  (`/history`) get a required window. The shared API client keeps its
  401/403 access-loss handling.
- Graph target: graph and history reads get the displayed branch injected;
  project-wide results are labelled.
- Untrusted results: the result voice receives carries its source and an
  explicit untrusted-data envelope. Today the voice catalog drops the tool
  annotations (`toolCatalog.ts`), so this is new.
- `INSTRUCTIONS`, for both the live and delegated model: the agent acts
  through RCP's page tools as the member and does not see the screen. It
  separates the member's current words, historical speech, quoted project
  content, and action receipts. It retries corrected arguments only after an
  argument-validation refusal, never after a declined card, identity loss,
  or an unknown outcome.
- Checks: attempts to escape the project (dot segments, encoded dots, another
  project), a non-GET, an SSE route, a denylisted file, an oversized body, and
  that discovery matches enforcement.

### 5. Playbook and artifacts

- The playbook is plain-language domain knowledge, about 25 lines, in one
  `webmcp/` module. It names no tools. Voice sends it with the session request,
  and the backend appends it to its fixed instructions under a size limit.
  WebMCP gets it from a read-only `rcp_get_playbook`.
- Contents:
  - What RCP is: the node graph, conversations, episodes (Experiment loops,
    Auto-research on a branch), artifacts, and the Inbox (proposals, nightly
    consolidation reports).
  - Role: the member's voice in their own page, acting as them. Node agents do
    the research; the voice agent finds, reads, explains, routes, and reports.
  - Catch me up: overview first, then only as deep as asked.
  - Think, plan, or explain: answer from reads. Hand off to a Discuss turn on
    the node only when it needs code, data, or long analysis, and say so.
  - Make something: name the node it picked, then send at once (the tap card,
    when on, is the guard). Continue the node's recent conversation, or start
    one. Work mode, with the member's words plus what they want to see. Say it
    started. Finishes are announced; offer to open the result.
  - Run or keep going: start the Experiment or authorize Auto-research, saying
    the budget aloud first. Stop means a graceful stop.
  - Where things are: artifacts live in the Artifacts panel and in the
    conversations and episodes that made them. Search both without asking
    which agent made it; ask only when several match.
  - Member-only: proposal judgment, Decision choice, standing, Hypothesis
    status, truth membership, branch merge. Say where to tap.
  - Voice manners: speaking only, maybe not looking. Never ask to paste or
    type. Gist first, names not ids, one short question when the target is
    unclear, nothing claimed until a result confirms it.
- One artifact search: `rcp_list_artifacts` also reads the Artifacts panel
  (`GET /api/projects/{id}/artifacts`). Both inventories become one stable
  tool identity, deduplicated by the underlying artifact, and exact items
  outside the recent windows resolve. Each result says where it lives.
- PDFs: `can_open` keeps its meaning (an RCP viewer). A separate branch opens
  a desktop PDF through `openDesktopArtifactPdf`, the panel's own path, with
  validated ids. In the browser and for download-only files, the refusal
  says where to tap Download. Page agents never download.
- Finish handling: while voice is open, a watched task's finish speaks the
  fixed line and offers the result; on yes it resolves that task's artifacts
  and opens them under the current project and graph guards. WebMCP hosts get
  no watches; they read the returned task id.
- Checks: voice and WebMCP get the same playbook object under its size limit;
  artifact identity, dedup, and PDF eligibility tests. No wording tests.

### 6. Request ids for voice writes (added 2026-10-08)

A Work send whose reply is lost leaves an "unknown" receipt, and today Resume
can only block the identical send. The human chose to fix this in this PR.

- The page mints one request id (UUID4) per voice write before dispatch: a
  conversation Send, an Experiment Start, and an Auto-research authorization.
  It stores the id in the receipt first, then sends it as an
  `Idempotency-Key` header.
- Each of the three admission routes accepts the header. The backend records
  (project, member, key) with the admitted task or episode id in one AppStore
  table, written in the same transaction as the admission. A repeat of the
  same key by the same member returns the original result and admits nothing
  new; the same key from another member or another route is refused. Rows
  older than the transcript retention plus one hard cap are pruned.
- `GET /api/projects/{id}/client-requests/{key}` returns the admitted task or
  episode id, or 404 when nothing was admitted under that key.
- Resume reconciles each unknown receipt through that lookup: accepted becomes
  an accepted receipt with its id (and a watch); not found (possibly still in
  flight) or a lookup that cannot answer leaves it unknown. A
  member who asks again resends with the receipt's same key, so a send still
  in flight can never be admitted twice.
- Terminal commands keep their per-run card and are out of scope.

## Ownership and checks

| Slice | Files | Focused check |
| --- | --- | --- |
| 1 | `windows.rs`, a native command, `build.rs` permissions, `voiceSession.ts`, `useVoiceAgent.ts`, `limits.py`, `service_connections.py` (idle setting) | web voice tests, `tests/test_voice*.py`, Tauri build, live check |
| 2 | `service_connections.py`, `api/voice.py`, `voice.py`, `limits.py`, `useVoiceAgent.ts`, `voiceExecutor.ts`, `VoicePanel.tsx`, `core/types.ts` | `tests/test_voice*.py`, web voice tests |
| 3 | `voice.py` | `tests/test_voice*.py`, live check |
| 4 | `webmcp/` new module, `webmcp/index.ts`, `toolCatalog.ts`, `voiceExecutor.ts`, `voice.py`, `limits.py` | web webmcp tests |
| 5 | `webmcp/playbook.ts`, `webmcp/artifacts.ts`, `webmcp/index.ts`, `App.tsx`, `useVoiceAgent.ts`, `voice.py`, `api/voice.py`, specs | web webmcp and artifact tests, `tests/test_voice*.py` |

`core/types.ts`, `voice.py`, and `useVoiceAgent.ts` are shared, so their edits
are serialized.

## Live checks

With a real OpenAI key in a rebuilt `RCP Dev.app` on disposable data:

- Slice 1's hidden-window check, before slice 2 starts.
- On macOS 13 (if available), hiding still ends voice, with the notice.
- Kill the network mid-session, then Resume from the voice button. The agent
  remembers the topic; work started before the drop is summarized once.
- Resume the same session on a second device; the first ends with a notice.
- Ask for artifacts, a saved Artifacts-panel item, and last night's
  consolidation report. All answer. Open a PDF artifact.
- Ask "make me a live dashboard for experiment X" with no other hint. It names
  the node, sends a Work turn, announces the finish, and opens it on yes.
