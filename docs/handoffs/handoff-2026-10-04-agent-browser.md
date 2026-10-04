# Agents use a headless browser through Playwright CLI

Date: 2026-10-04
Status: design settled with the human on 2026-10-04 and reviewed once by an
xhigh design pass the same night. Implemented on this PR: part 1, the host
runtime, launch grants, their wiring, and the Web controls. The
live checks still open are listed under "Implementation checks still open".

Two parts ship together, part 1 first:

1. **The personal backend requires an owner session.** Today any process on the
   machine can call it, including an agent's shell and an agent's browser.
2. **A per-chat headless browser.** Agents run Microsoft's `@playwright/cli` in
   their own shell. RCP installs it, starts each chat's browser, and keeps it.

Why a CLI and not MCP, a command-channel relay, or the providers' own browsers:
[decision](../decisions/2026-10-04-agents-browse-with-playwright-cli.md).
Why the personal backend stops trusting loopback:
[decision](../decisions/2026-10-04-the-personal-backend-requires-an-owner-session.md).

## Settled decisions

- **Browser use, not computer use.** The browser is headless.
- **One CLI, every feature.** Agents call `playwright-cli` directly, including
  `run-code`. RCP does not relay, filter, or rewrite browser commands.
- **No MCP in agent launches.** RCP adds no MCP server and offers no UI to add
  one. Every launch strips the member's own MCP configuration; Claude Discuss
  gains the strict empty MCP flags it lacks today. Skills are unchanged. The
  page-scoped WebMCP surface for agents a member runs in their own page stays.
- **A Browser toggle per chat, off by default.** Continuations inherit it.
  Experiment and Auto-research launch dialogs carry the same toggle, and an
  orchestrator's children inherit the episode's grant.
- **Work, orchestrate, and Discuss** can use the browser. Paper coach,
  ingestion, scratch-patch correction, and recorded-turn replay cannot.
- **The toggle is the human's decision.** Browser commands run outside the
  provider sandbox and can write outside the chat's write roots.
- **The browser runs where the agent runs.** A remote GPU host's localhost is
  reachable from that host's browser. No port forwarding.
- **The execution host needs Node.js 18 or newer and npm.** RCP installs a pinned
  `@playwright/cli` and its headless Chromium into RCP's own folder there.
- **A browser failure never fails a turn.** The turn runs without the browser and
  the chat shows why. The agent may still be unable to finish browser work.
- **Toggle changes apply at the next turn.** A running turn keeps what it was
  launched with. The control says so.
- **Turning the toggle off deletes the chat's browser logins and cookies.**
- **A team server installs Chromium's system libraries** through its root install
  and update path, best effort: a failure is a warning and never blocks an update.
- **Personal non-loopback binds keep working.** They use the same owner session.
  That is strictly better than today's no authentication.

## Evidence

Probed on 2026-10-04 with Claude Code 2.1.288, Codex 0.160.0 (macOS) and 0.157.0
(Linux team host), OpenCode 1.18.30, and `@playwright/cli` 0.1.22 with Chrome
Headless Shell 155.

| Check | Result |
| --- | --- |
| Agent starts Chromium itself inside Codex's macOS Work sandbox | Fails: Mach port registration is denied (WebKit fails too) |
| One browser across separate `playwright-cli` invocations | Yes |
| RCP-side `open` outside the sandbox, then `goto` and `eval` inside Codex's Work sandbox | Works on macOS and on the Linux team host |
| `state-save` and `screenshot --filename` outside the workspace, from inside Codex's sandbox | Both wrote the file |
| Turn Playwright's file checks back on, or remove `run-code`, by config | Not possible: CLI mode hard-codes `skillMode: true`; `run-code` is core |
| Claude `Bash(playwright-cli:*)`; OpenCode `{"*":"deny","playwright-cli *":"allow"}` | Bare calls run; chaining into other programs refused. Claude's own safe-command rule still allows chaining harmless commands such as `echo` |
| Persistent profile across browser restarts | localStorage survives; cookies survive a graceful close |
| `playwright-cli open` on a live session | Stops it first (tabs lost). Check liveness before `open` |
| Session registry | Keyed by the nearest ancestor of the cwd that holds `.playwright`, else the CLI install root |
| Codex Work sandbox calls the running personal backend | `GET /api/health` and `GET /api/projects` return 200 with no credential |
| Linux host without `libasound2` | Chromium refuses to start |

The xhigh review's evidence lines (paths and line numbers for every point below)
are kept with the implementation briefs, not here.

## Part 1: the personal backend requires an owner session

### Admission

- Personal identity resolution requires an owner session for every HTTP route and
  for the terminal WebSocket upgrade. Terminal sockets keep their existing
  periodic revocation checks.
- **Public without a session:** `/api/health` (trimmed to adoption identity:
  version, instance, data directory id; no space name, project names, or counts),
  the static Web shell and sign-in assets, the owner exchange and one-time code
  redemption routes, and `OPTIONS`. The phone-pairing listener stays separate and
  unchanged. Team enrollment and pairing stay team policy.
- New public routes share the bounded auth-body handling of team exchange.

### Storage and cookie

- Reuse the session storage primitives (hashed token, lifetime, revoke) through
  shared mint, resolve, and revoke operations. Owner admission and team-member
  admission stay separate policies. No table renames.
- The owner secret is stored only as a hash. Owner sessions expire, log out, and
  revoke like team sessions. A restore requires signing in again, as provider
  logins do. Authentication stays separate from the display-name prompt.
- **Cookie:** a separate host-only, HttpOnly, `SameSite=Strict` owner cookie.
  `Secure` only on HTTPS origins. The team `__Host-` cookie and the native team
  cookie validator do not change.

### How the human's clients sign in

- **Fresh desktop start.** The desktop creates a 32-byte secret (base64url, within
  the Keychain helper's 64-byte limit), keyed by data directory id. It spawns the
  backend and passes the secret on stdin. The backend enrolls the hash only while
  holding the data-directory ownership lock, and only if no owner secret exists.
- **Start or adopt with a matching secret.** With a captured startup code, the
  desktop redeems it once with the saved Keychain secret or the spawned secret;
  without a code, it exchanges the secret. It writes the Keychain only when the
  secret did not come from there. It installs the cookie through the native
  WKWebView bridge before it calls the UI ready. "Ready" includes the session,
  not just health.
- **One-time sign-in code.** At start the backend prints a single-use code to its
  stdout: 10 minutes, hash stored only, never written to a log file. `rcp serve`
  shows it as a sign-in URL in human mode. Machine-readable stdout stays one JSON
  object carrying `owner_sign_in_code`; native diagnostics exclude that field.
  A desktop-spawned backend's stdout belongs to the desktop, which redeems the code itself, so a missing or reset Keychain entry
  recovers by restarting the backend. A backend started from a terminal and
  adopted by the desktop needs the human to paste that code into the desktop.
  Redeeming a code may enroll a new owner secret. An unauthenticated exchange can
  never register or replace one.
- **Authentication failure never stops or replaces an adopted backend.**
- **`rcp open` and the other project-opening CLI paths** stop calling the API.
  They open the UI at a route that carries the project locator, preserved across
  sign-in, and the human confirms there. Fresh startup that passes a project to
  `create_app` keeps working.

### Every client that must carry the session

| Client | Change |
| --- | --- |
| Web UI, polling, uploads, provider and service settings, transcription | Same-origin cookie. A personal 401 opens the sign-in boundary before protected boot requests |
| Tauri WebView | Native exchange installs the cookie for the verified personal origin |
| Native downloads and PDF preview | One shared native authenticated personal client |
| Native project transfer and archive streaming | The same client, through every step; keep bounded streaming and instance pinning |
| Native desktop notifications | The same client; personal devices stay owner-bound |
| Native update notice | The same client; a 401 is no longer silent |
| Artifact viewer, previews, reports, images, PDFs, downloads | Same-origin cookie on the outer viewer and content routes; agent HTML keeps its opaque sandbox and never receives credentials |
| Terminals, including voice terminals | Cookie on the WebSocket upgrade |
| Page WebMCP and voice | The page's cookie through existing API owners; never sent to a model |
| Health probes, doctor, control socket | Unchanged |

There is no public SSE route. The native archive download is the one streaming
response.

### Tests

A signed-in client factory in `tests/helpers.py` mints and exchanges credentials
through production session code against each test's disposable app. Async
clients get the matching cookie helper. Negative tests keep a raw client. No
autouse fixture bypasses authentication. Served-app tests sign in on their own
disposable ports.

### Codex read deny

Codex profiles gain a read-deny list separate from write protection, because
protected write paths render as `"read"`. It denies the desktop's web storage
folders for each bundle identifier, for Work and Discuss profile shapes. A real
shell read proves the denial.

### What it does not close

Claude and OpenCode Work shells are unbounded. An agent that deliberately reads
the desktop's cookie store from disk could still act as the owner. Part 1 closes
the accidental and trivial paths.

## Part 2: the per-chat browser

### Owner and session

- **Browser owner = the stable run stage** the native session already uses: the
  chat's stage, an Experiment lineage's stage, an Auto-research root's stage.
  Episode ids change across continuations; stages do not. Each RCP-managed child
  has its own stage and owner, kept across its retries and wakes. Owners are
  namespaced by space, project, and execution host and account; a repointed
  machine alias never adopts or deletes another host's profile.
- **Registry.** RCP starts the session from the owner's stage workspace, and the
  agent starts in that folder, so both resolve the same registry. The prompt line
  tells the agent to run `playwright-cli` from its starting folder. RCP adds no
  `.playwright` marker.
- **Ensure, never reopen.** RCP checks the session's liveness first and runs
  `open` only when it is absent or dead.
- **Pinned configuration.** RCP writes an owner config file and passes it to
  `open`. It names RCP's installed Chromium executable, headless, the owner
  profile as user data dir, and the output dir, and it explicitly overrides every
  ambient key that could attach an extension, a CDP endpoint, storage state, or a
  headed browser. A test runs it against a hostile global config. Readiness and
  launch resolve the same executable and config.
- **Process owner.** The daemon starts under the OS owner primitives the compute
  launch helper uses (a systemd user unit on Linux, a launchd job on macOS),
  without compute authorization, so a service restart or SSH logout does not kill
  it. RCP keeps a durable handle, adopts live sessions after its own restart, and
  closes with `playwright-cli close`. Liveness is checked outside any provider
  PID namespace. Where that owner is unavailable, the browser is unavailable with
  a visible reason.

### Lifecycle

- Turning the toggle off closes the session and deletes the profile right after
  the response; a turn still running keeps its browser, and the deletion happens
  when it ends. Turning it back on before cleanup cancels that pending close and
  deletion, except for archived chats or project deletion. Archiving a chat closes
  it and keeps the profile. Removing a project closes and deletes all of its owners' profiles. There is no chat
  delete route, so there is no chat-delete hook.
- The CLI's idle timeout closes unused sessions. It closes the browser
  gracefully; a smoke test proves cookies survive it.
- A host keeps at most `BROWSER_MAX_SESSIONS_PER_HOST` RCP-managed sessions. Idle
  means no active invocation uses it. Ensure, close, and eviction are serialized
  per owner. A new session evicts the least recently used idle one; if all are
  busy, the new turn runs without the browser and says why.
- A remote cleanup that cannot reach its host stays pending and retries.

### Storage

- Local and team hosts: `<data_dir>/browser/`, explicitly excluded from backups.
- SSH hosts: `~/.rcp/browser/`, mode 0700.
- Tools (Node package and Chromium) live in a sibling `tools` folder, outside
  backups. Write-scope protection covers both as RCP-owned storage.
- Playwright keeps its registry and sockets in its default cache folder, which
  Codex's sandbox can read.

### Intent, grant, and propagation

- **Chat preference** lives in app-local chat settings beside chat display state.
  Existing chats default to off. Toggling never rewrites transcript records.
- **Turn admission** snapshots the preference as a requested value. Launch
  resolution produces one immutable grant (session id, invocation folder, or the
  unavailable reason) before the prompt renders. The grant travels through the
  launcher into `ProviderTurnRequest`. Stage objects do not carry it.
- **Episodes** persist the launch preference on the generic episode, through SQL,
  row conversion, Experiment projections, the API, and Web types, as one field.
- **Auto-research children** inherit the human's episode grant during admission.
  The agent's route payload never grants it.
- **Every continuation path** carries it: retry, human Continue, watcher wakes,
  queued follow-ups, child wakes, and bounded Experiment turns. Strict request
  models and continuation snapshots are updated deliberately.
- **Prompt continuation** sends the browser value when it differs from the
  session's last committed turn, so a session hears "off" after the toggle went
  off and nothing while it stays the same. A fresh session or episode mentions
  the browser only when a human asked for it.
- Transfer and replay may carry the preference, never live handles or profiles.
  Imported history needs the human to turn the toggle on again.

### Provider changes

| Capability | Codex | Claude | OpenCode |
| --- | --- | --- | --- |
| Work, orchestrate | env and `PATH` | env and `PATH` | env and `PATH` |
| Discuss | env and `PATH` | add `Bash(playwright-cli:*)`; add strict empty MCP | bash rules `*` deny then `playwright-cli *` allow, generated once for both the command and the environment |

- Local launches merge the environment. SSH launches prefix the remote `PATH` on
  the host after login-shell initialization; the controller's `PATH` never
  substitutes for the remote one.
- Codex keeps `shell_environment_policy={}`. Its default inherits the environment;
  a real shell check proves it per version.
- The grant is threaded through both launcher methods, provider request
  construction, legacy profile commands, and the acceptance launcher. Test doubles
  get it explicitly, without `getattr` fallbacks.

### Notices

Each turn records a durable browser status: not requested, granted, or
unavailable with a reason. RCP checks session liveness after the turn and at the
next ensure, so a daemon killed mid-turn shows on that turn. RCP never intercepts
CLI commands to observe them.

### Readiness and install

- One readiness service feeds the machine card and doctor. It resolves Node, npm,
  the pinned CLI, Chromium, and permissions in the same environment the execution
  account uses, since a GUI or service `PATH` differs from a login shell.
- States: host unreachable, Node missing or too old, npm missing, not installed,
  unsupported platform, system libraries missing, ready.
- Install is explicit, bounded, serialized, and never part of turn startup:
  pinned `npm install` into the tools folder, then
  `playwright-cli install-browser chromium --no-shell` (RCP launches full
  Chromium headless, so the separate headless shell is not downloaded), then a
  headless smoke launch.
- Linux libraries: map `ldd` misses to packages for the supported Ubuntu releases.
  A team server installs that fixed package set through its root install and
  update path. This is a deliberate change to the server contract that
  installation adds no OS software. A personal SSH host without root shows the
  exact `apt` command.
- RCP starts without Node when no chat uses the browser.

### Containment

Browser commands run in an RCP-started process outside the provider sandbox. The
CLI turns off Playwright's own file-path checks, and `run-code` runs any
Playwright code. A turn with the browser on can write anywhere the RCP account
can and reach any address its host can. Graph authority is unchanged:
`patch.json` stays the only graph-change channel.

### UI

- The chat header gets a **Browser** toggle. Its consent sentence is primary
  content, not a muted subtitle: the agent can use a headless browser, and
  browser actions can run agent-written code and write files outside this chat's
  folders. It also says changes apply from the next turn.
- A turn that wanted the browser and did not get it shows the reason.
- Experiment and Auto-research launch dialogs carry the same toggle.
- The machine card shows the Browser readiness row with **Install**.

### Limits

In `limits.py`, in seconds, converted to milliseconds at the CLI boundary:
`BROWSER_SESSION_IDLE_SECONDS` (1800), `BROWSER_SESSION_START_TIMEOUT_SECONDS`
(60), `BROWSER_SESSION_CLOSE_TIMEOUT_SECONDS` (30),
`BROWSER_READINESS_TIMEOUT_SECONDS` (60), `BROWSER_INSTALL_TIMEOUT_SECONDS` (900),
and `BROWSER_MAX_SESSIONS_PER_HOST` (8).

## Slices

Three runs start in parallel worktrees, then integrate, then the Web slice. The
integrator owns `storage/models.py`, `storage/base.py`, `limits.py`,
`web/src/types.ts`, `web/src/api.ts`, and `web/src/App.tsx` at merge time; a run
that must touch them keeps its hunks small and reports them. Each run takes the
next free migration number in its worktree; the integrator renumbers at merge, so
no test hard-codes a migration number. Each run updates the current-behavior
docs for what it changes.

1. **A: owner session.** Backend admission, storage, cookie, exchange, codes,
   health trim, terminals; Tauri spawn, adopt, exchange, cookie bridge, shared
   native client and every native caller; Web sign-in boundary; `rcp open`
   locator route; the signed-in test factory and test migration; Codex read deny.
2. **B: host runtime.** A new `src/rcp/browser/` package: install, readiness,
   pinned config, the process owner, ensure, close, eviction, cleanup, storage
   and backup exclusion, doctor and machine-card API rows, server install path.
   Unreachable from launches until integration.
3. **C: intent, grant, and providers.** Chat preference storage and API, episode
   field, request snapshots, every continuation path, the grant through launcher
   and providers, the three provider adapters, the prompt line and off delta,
   notices, and capability admission. It reaches the runtime through one seam
   function that integration wires to B.
4. **Integration.** Merge A, B, and C; wire the seam; run the affected suites.
5. **D: Web controls.** The toggle, launch dialogs, notices, and the machine-card
   Browser row and Install.

## Implementation checks still open

Proven on 2026-10-04: on macOS, a served branch backend ran real Codex 0.160.0
Work turns with the browser on. The sandboxed shell saw `PLAYWRIGHT_CLI_SESSION`
and the CLI on `PATH`, loaded a page, and the next turn found the same page and
cookie. Toggling off deleted the profile within seconds. Cookies survived the
CLI's idle close. A real `codex sandbox` refused reads of the desktop app's
storage. Run C drove real Claude and OpenCode Discuss launches with a granted
shim (OpenCode including a resumed session). Over SSH, remote readiness and
install ran on the Linux GPU host under a member account and reported the
missing `libasound2` with its `apt` command. Real Claude Work and Discuss turns
with a granted browser reused a page and cookie, released their leases, and
toggling off deleted the chat's browser folder. The desktop app from a source
build and from a frozen candidate passed fresh start, adopt, and restart with
one session each; the source build also passed reload, and an expired session
showed the sign-in screen until a relaunch. The frozen candidate's native
notification client registered the Mac through the owner session.

Still open:

- A Linux execution account with linger: a session that survives an SSH
  disconnect and an installed-service restart, and a Work turn that opens a
  localhost service the agent started there. The member account used had no
  linger, so ensure correctly refused.
- The desktop app's native PDF preview, project transfer, terminals, and a
  manual sign-in with a pasted code. Each needs the human's running app quit.

## Close criteria

Close this handoff when all of these hold:

- An unauthenticated request to the personal API from a Codex Work sandbox gets
  401. The source and frozen desktop apps (fresh, adopt, restart, reload), a plain
  browser through the sign-in URL, native PDF, transfer, and notifications, the
  terminals, and the `rcp open` flow all still work.
- On each provider, a Work turn uses the browser on a real page, and the next turn
  in that chat finds the same page and login.
- On each provider, a Discuss turn uses the browser, and chained shell commands
  are still refused.
- A Codex Work turn on a Linux team server and on an SSH GPU host uses the browser
  and opens a localhost service the agent started there; the session survives an
  SSH disconnect and a service restart.
- A missing browser and a daemon killed mid-turn each leave the turn complete with
  a visible notice.
- Toggle off deletes the profile; archive closes the session.
