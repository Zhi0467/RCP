# Agents use a headless browser through Playwright CLI

Date: 2026-10-04
Status: design settled with the human on 2026-10-04. Not started. Implementation
waits for an explicit start. Nothing below is implemented.

Two parts ship in this order:

1. **The personal backend requires an owner session.** Today any process on the
   machine can call it, and that includes an agent's shell and an agent's browser.
   This part is a prerequisite, not an afterthought.
2. **A per-chat headless browser.** Agents run Microsoft's `@playwright/cli` in
   their own shell. RCP installs it, starts each chat's browser, and keeps it.

Why a CLI and not MCP, a relay through the command channel, or the providers' own
browsers: [decision](../decisions/2026-10-04-agents-browse-with-playwright-cli.md).
Why the personal backend stops trusting loopback:
[decision](../decisions/2026-10-04-the-personal-backend-requires-an-owner-session.md).

## Settled decisions

- **Browser use, not computer use.** The browser is headless. No agent moves a
  real mouse or sees a real screen.
- **One CLI, every feature.** Agents call `playwright-cli` directly, including
  `run-code`. RCP does not relay, filter, or rewrite browser commands.
- **No MCP in agent launches.** RCP adds no MCP server and offers no UI to add
  one. Every launch keeps stripping the member's own MCP configuration, as today.
  Skills are unchanged: provider-native skills plus the fixed official packages.
  The page-scoped WebMCP surface for agents a member runs in their own page is a
  separate thing and stays.
- **A Browser toggle per chat, off by default.** Continuations inherit it.
  Experiment and Auto-research launch dialogs carry the same toggle, and an
  orchestrator's children inherit it from their episode.
- **Work, orchestrate, and Discuss** can use the browser. Paper coach,
  ingestion, and scratch-patch correction cannot.
- **The toggle is the human's decision.** Browser commands run outside the
  provider sandbox and can write files outside the chat's write roots. RCP
  records that gap and does not hide the feature behind it.
- **The browser runs where the agent runs.** On a remote GPU host, the agent's
  browser reaches services the agent started on that host's localhost. No port
  forwarding is needed.
- **The execution host needs Node.js 18 or newer.** RCP installs a pinned
  `@playwright/cli` and its headless Chromium into RCP's own folder there.
- **A browser failure never fails a turn.** The turn runs without the browser,
  and the chat shows why.

## Evidence

Probed on 2026-10-04 with Claude Code 2.1.288, Codex 0.160.0 (macOS) and 0.157.0
(Linux team host), OpenCode 1.18.30, and `@playwright/cli` 0.1.22 with Chrome
Headless Shell 155.

| Check | Result |
| --- | --- |
| Agent starts Chromium itself inside Codex's macOS Work sandbox | Fails: the sandbox denies Chromium's Mach port registration (WebKit fails too) |
| `playwright-cli` keeps one browser across separate invocations | Yes |
| RCP-side `open` outside the sandbox, then `goto` and `eval` from inside Codex's Work sandbox | Works on macOS and on the Linux team host |
| Codex's Linux sandbox still blocks a direct write outside the workspace | Yes |
| `state-save` and `screenshot --filename` to a path outside the workspace, from inside Codex's sandbox | Both wrote the file. The daemon writes for the agent |
| Turn off Playwright's file checks or `run-code` by config | Not possible: CLI mode hard-codes `skillMode: true`, and `run-code` is a core command |
| Claude `Bash(playwright-cli:*)` and OpenCode `{"*":"deny","playwright-cli *":"allow"}` | Bare calls run; `&&`, `;`, and `$(…)` chaining are refused |
| Persistent profile across browser restarts | localStorage survives; cookies survive a graceful close (`SIGTERM` or close) |
| Codex Work sandbox calls the running personal backend | `GET /api/health` and `GET /api/projects` return 200 with no credential |
| Linux host without `libasound2` | Chromium refuses to start until the library is present |

## Part 1: the personal backend requires an owner session

### Problem

A personal backend listens on a fixed loopback port and has no session. It
refuses only cross-site simple-content POSTs. Any local process can call it with
JSON, and agents are local processes. A Codex Work turn read the project list
from inside its sandbox. The same route lets an agent approve a Proposal, which
invariant 3 forbids. A browser makes this one click easier, but the shell already
reaches it.

### Requirement

Every personal `/api` request carries an owner session, except `/api/health` and
the phone-pairing listener's own routes. No agent launch receives an owner
credential.

### Approach

Reuse the team session machinery: a session row, a hashed token, a cookie, and an
exchange route.

- **Desktop app.** The app keeps an owner secret in its Keychain, as it keeps team
  member tokens. The backend stores only the secret's hash in its data directory.
  The app exchanges the secret for a session cookie when it starts or adopts the
  backend. The first desktop start creates the secret and hands it to the backend
  it spawns over stdin, never argv or the environment.
- **Plain browser.** `rcp serve` prints a one-time sign-in URL to its own
  terminal. The code is single use, expires in ten minutes, and the server keeps
  only its hash, like device pairing codes.
- **`rcp` CLI commands that open a project.** They stop calling the API. They open
  the signed-in UI at a route that names the project, and the human confirms
  there.
- **Agents.** Launch environments never carry the secret or a cookie. Agent
  browser profiles start empty, so the agent's browser is signed out of RCP.

### What it does not close

Agents run as the same OS account. A Claude or OpenCode Work shell is unbounded,
so an agent that deliberately reads the desktop's cookie store from disk could
still act as the owner. Part 1 closes the accidental and the trivial path: plain
`curl`, and opening RCP in the agent's browser. Slice 1 also denies Codex reads of
the desktop's web storage folder, where the profile shape allows it.

## Part 2: the per-chat browser

### A turn with the Browser toggle on

1. **Readiness.** The launch reads the execution host's browser readiness. Not
   ready: the turn launches without the browser, the prompt omits it, and the
   chat shows the reason.
2. **Ensure the session.** Outside any provider sandbox, RCP runs
   `playwright-cli open` for the chat's session with `--persistent` and the chat's
   profile folder. A session that is already running is reused. Local hosts use a
   subprocess; remote hosts use the existing SSH transport. A failure here is the
   same as step 1: no browser, visible reason, the turn still runs.
3. **Launch.** The provider gets `PLAYWRIGHT_CLI_SESSION` set to the session id
   and RCP's tools folder first on `PATH`. The prompt gains one line, rendered from
   the same resolved browser grant the launch uses.
4. **Use.** The agent runs `playwright-cli` as it likes. Snapshots and screenshots
   default to `.playwright-cli/` under its working folder.
5. **Turn end.** The session stays up. The next turn finds the same tabs, page,
   cookies, and logins.
6. **Close.** The CLI's idle timeout closes an unused session. Toggle off, chat
   archive, and chat delete close it. Chat delete also deletes the profile.

### Sessions and profiles

- **Session id.** The stable chat id, or the episode id for Experiment and
  Auto-research owners. Each child Work or worker gets its own.
- **Profile folder.** RCP-owned storage on the execution host, keyed by session
  id, outside backups because it holds logins. On a remote host it lives under
  RCP's remote state root.
- **Concurrency.** A host keeps at most `BROWSER_MAX_SESSIONS_PER_HOST` sessions.
  Starting one more closes the least recently used idle session first. Its profile
  stays, so only its open tabs are lost.
- **Shared account.** On a team server every agent runs as the service account. An
  agent can address another chat's session by name. This matches the existing
  trust in that account, recorded in the transcription-key decision.

### Install and readiness

- The machine card and doctor gain a **Browser** row: ready, Node missing or too
  old, not installed (with **Install**), or system libraries missing.
- **Install** runs on the execution host: `npm install` of the pinned
  `@playwright/cli` into RCP's tools folder, then
  `playwright-cli install-browser chromium` (about 120 MB to download, 270 MB on
  disk). The pin lives in code. Nothing runs `@latest` at launch.
- **Linux system libraries.** Readiness runs `ldd` on the headless shell and names
  the missing packages. A team server installs them through its root-run install
  and update path. A personal SSH host without root shows the exact `apt` command
  for its administrator.

### Provider launch changes

| Capability | Codex | Claude | OpenCode |
| --- | --- | --- | --- |
| Work, orchestrate | env and `PATH` only; shell exists | env and `PATH` only; `Bash` is allowed | env and `PATH` only; shell exists |
| Discuss | env and `PATH` only; workspace-write shell exists | add `Bash(playwright-cli:*)` to the allowed tools | allow `playwright-cli *` after `*` deny in the RCP agent's bash rules |

The Discuss rules are the only shell Discuss gains, and only while the toggle is
on.

### Containment

Browser commands run in an RCP-started process outside the provider sandbox. The
CLI turns off Playwright's own file-path checks, and `run-code` runs any
Playwright code. A turn with the browser on can therefore write files anywhere the
RCP account can, and reach any address its execution host can. Graph authority is
unchanged: `patch.json` stays the only graph-change channel. The toggle is the
consent, and its control says so in plain words.

### UI

- The chat header gets a **Browser** toggle with one sentence: the agent can use a
  headless browser, and browser actions can run agent-written code and write
  files outside this chat's folders.
- A turn that wanted the browser and did not get it shows a notice with the reason.
- The Experiment and Auto-research launch dialogs carry the same toggle.

### Limits

New entries in `limits.py`: `BROWSER_SESSION_IDLE_SECONDS` (proposed 1800),
`BROWSER_SESSION_START_TIMEOUT_SECONDS` (proposed 60), and
`BROWSER_MAX_SESSIONS_PER_HOST` (proposed 8).

## Slices

1. **Owner session.** Backend sessions for the personal space, the desktop
   exchange, the `rcp serve` sign-in URL, the CLI hand-off, the Codex read deny, and
   a shared test fixture that signs in. Ships first.
2. **Install and readiness.** Tools folder, pinned install, `ldd` check, machine
   card row, doctor output, and the team server's root install path.
3. **Session manager.** Ensure, reuse, idle close, eviction, toggle-off and delete
   close, and profile storage, on local and SSH hosts.
4. **Launch wiring.** Toggle state on chat and episode records, the launch request
   field, env and `PATH`, the Discuss rules, the prompt line, and turn notices.
5. **Web and docs.** The toggle, launch dialogs, and notices. Update
   `providers-and-containment.md`, `api-web-and-desktop-projections.md`,
   `server-and-machine-operations.md`, invariant 4 in `AGENTS.md`, and the
   decision index.

## Implementation checks still open

- Whether the CLI's idle shutdown closes Chromium gracefully. Cookies persist
  only on a graceful close.
- That a session started over SSH survives the SSH session ending and an RCP
  restart. A session survived separate SSH logins on the Linux team host.
- That Codex's shell passes `PLAYWRIGHT_CLI_SESSION` and `PATH` to commands under
  RCP's environment-policy override.
- That RCP's real OpenCode agent rules accept the `playwright-cli *` pattern, not
  only the plain config probed here.
- Which cookie the owner session uses on the plain-HTTP personal origin, since a
  `Secure` cookie is lost there. Decide in slice 1 with the existing loopback
  origin probes.

## Close criteria

Close this handoff when all of these hold:

- An unauthenticated request to the personal API from a Codex Work sandbox gets
  401. The desktop app, a plain browser through the sign-in URL, and the `rcp`
  open flow all still work.
- On each provider, a Work turn uses the browser on a real page, and the next turn
  in that chat finds the same page and login.
- On each provider, a Discuss turn uses the browser, and chained shell commands
  are still refused.
- A Codex Work turn on a Linux team server and on an SSH GPU host uses the browser
  and opens a localhost service the agent started there.
- A missing browser and a daemon killed mid-turn each leave the turn complete with
  a visible notice.
- Toggle off and chat delete close the session, and delete removes the profile.
