# Agent secrets: usable but not readable

Status 2026-10-04: design settled; implementation not started. One branch and
one PR carry all of it. Implemented on this branch: the production CORS
allowlist is gone (Vite proxies `/api`, so no origin needed it). Remaining:
everything under "Plan".

## Threat model

A generic network attacker. RCP exposes no public listener (a team server is
tailnet-only), so the only untrusted input is agents fetching web content
(search, fetch, browser). An untargeted prompt injection asks the agent to read
a secret, such as `~/.ssh/id_*`, and send it somewhere. Such an attack needs
untrusted content, private data, and a way out. Agents keep the web and the
network, so this work removes the private data: tools can still *use* a
secret but cannot *read* it.

Out of scope: an agent that deliberately escapes through `systemd-run`,
`launchctl`, `ssh localhost`, or a scheduler job; same-account isolation;
targeted poisoning of RCP content; egress filtering.

## Settled decisions

- No separate OS account, no egress filtering, no RCP-managed full sandbox.
  Agents keep network, Slurm, GPUs, SSH, systemd/launchctl, and the browser.
- Browser and Git capability never shrinks. A change may remove only the
  ability to read a hidden secret; everything a task legitimately does with the
  browser or Git keeps working.
- Browser: on Linux the host starts the daemon and Chromium inside the same
  hidden-path policy (Chromium's own sandbox is already off there), so an
  `upload` of a hidden file fails while every other upload and download works.
  On macOS the daemon stays unwrapped, because Chromium's sandbox cannot nest
  inside Seatbelt; browser file operations stay unhidden there and the turn
  says so.
- Git: the backend owns one long-lived `ssh-agent` per account at a stable
  socket path, loaded with that account's deploy keys and restarted with the
  backend. `core.sshCommand` keeps its transport and adds
  `-o IdentityAgent=<stable path>`. A deploy key is hidden only when that agent
  is confirmed to hold it at launch; otherwise the key stays readable for that
  turn with a visible warning, so Git always works. Local keys first; remote
  execution machines follow the same rule if their agent ownership stays small.
- `~/.ssh/id_*` files are hidden only when that key is loaded in the account's
  `ssh-agent`, so SSH keeps signing through the agent; keys not in an agent stay
  readable.
- Files use a deny list: code-owned defaults plus per-machine **hidden folders**
  stored beside the existing machine writable paths. Settings lists the
  defaults read-only; users can add folders, not remove defaults. Any member
  may edit a team machine's list (RCP has no admin role). A path that overlaps
  a project checkout, a stage, `known_hosts`, or the command sockets is refused
  with a clear message.
- Tool-call environments use an allow list (home, path, user, locale, terminal,
  temporary directory, SSH agent socket, runtime directory, and RCP's own Git and
  browser variables). Providers keep their own authentication.
- When a host cannot enforce hiding (no `bwrap`, or user namespaces blocked as
  on Ubuntu 24.04), the launch runs unhidden with a visible warning in the turn,
  Settings, and doctor. It never blocks.
- The claim is "selected secrets are unreadable on supported launches", never
  "private data is removed": research data stays readable by design.

## Plan

1. `Host` header check on the backend (DNS rebinding). The owner session (#256)
   already keeps a rebound page from acting.
2. `HiddenReadScope`: one resolved, provider-neutral object per launch, carried
   on `ProviderTurnRequest` (`src/rcp/providers/base.py`) for every capability
   (Discuss, Work, Experiment, orchestrate, paper coach, Seed/Refresh,
   consolidation, correction). Sibling of `ProjectWriteScope`, which exists only
   for Work-like launches. Reuse storage-owner discovery from
   `agents/write_scope.py`, but hide only secrets, not the stages, tools, and
   sockets agents need.
   Defaults: `rcp.sqlite3*` and backup or checkpoint copies (including
   `run-stage/backup-*`), provider credential stores under the data directory,
   service-connection keys, deploy keys held by RCP's agent, the remote Claude
   setup token
   (`~/.config/rcp/claude-setup-token`), the control-socket directory,
   `~/.ssh/id_*` keys loaded in `ssh-agent` (never `known_hosts`, `config`, or
   `.pub`), resolved provider login files,
   well-known credential files (`~/.aws`, `~/.config/gh`, `~/.netrc`, ...), and
   the WebKit storage entries now in `CODEX_READ_DENY_PATHS`.
3. One staged, stdlib-only wrapper, shipped from its source module like
   `agents/staged_command_client.py`: an environment allow list, then a Seatbelt
   profile on macOS (`allow default`, deny the hidden paths, deny the Keychain
   service lookup) or `bwrap --dev-bind / /` with the hidden paths masked on
   Linux (no network namespace). A readiness probe per host chooses between
   enforced and the visible-warning fallback.
4. Provider wiring, all rendered from the scope:
   - Claude: `CLAUDE_CODE_SHELL_PREFIX` set to the wrapper (local environment
     and SSH `remote_prefix`), plus `Read(...)` denies for its in-process tools,
     on every capability's settings branch. Exact files and globs render
     differently from directories.
   - Codex: deny entries in every permission profile, including the
     ingestion/paper sandbox path and `app_server` thread and turn policies;
     `shell_environment_policy` set to the allow list (`app_server` currently
     resets it to `{}`).
   - OpenCode: `SHELL` set to the wrapper acting as a shell.
5. Browser (Linux): `browser/host.py` starts the daemon through the wrapper,
   records the policy fingerprint, and restarts a live daemon when it changes.
   Wrapped tool calls keep the browser CLI socket, path prefix, and session
   variable.
6. Git: a backend-owned `ssh-agent` with a stable socket loads deploy keys;
   `git_access.py` adds `IdentityAgent`; the scope hides a deploy key only after
   confirming the agent holds it. Wrapped tool calls keep `SSH_AUTH_SOCK` and
   RCP's Git variables.
7. Prompt text renders the effective scope, including any fallback and the
   unhidden macOS browser.
8. Docs: `providers-and-containment.md` (cooperative model, provider and
   browser sections; network behavior unchanged), Settings in the interface spec,
   and a decision record for the threat model.

## Probe evidence (2026-10-04)

Claude Code 2.1.288 and Codex 0.160 on macOS; Claude 2.1.288 and Codex 0.157
on an Ubuntu 22.04 host (kernel 5.15, bwrap 0.6.1); OpenCode 1.18.30 on macOS.

- Claude through the wrapper (both OSes): hidden files and private keys give
  EPERM or appear absent; injected tokens are gone; HTTPS, `ssh-agent`, Slurm,
  GPU, writes, and `git commit` work. Background Bash goes through the prefix;
  `Read` denies block the Read tool, Grep, Glob, and subagents.
- `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` is unusable: it kept `GH_TOKEN` and removed
  `SSH_AUTH_SOCK`.
- Codex deny entries are kernel-enforced for directories, files, and globs; a
  Unix socket to `ssh-agent` works under both OS sandboxes; by default every
  environment variable passes until `shell_environment_policy` excludes it.
- `ssh -i key.pub -o IdentitiesOnly=yes` signs through the agent while the
  private key is unreadable.
- OpenCode runs shell commands through `$SHELL`; the wrapper there hides files
  and drops variables.
- `bwrap` works under `NoNewPrivileges`, as in `rcp.service`.
- Browser: `file://` is already blocked by playwright-cli, but `upload` of a
  hidden file through a daemon outside the wrapper leaks its content. Chromium
  runs inside `bwrap`; inside Seatbelt it runs only with `--no-sandbox`.
- Keychain: an item trusting `/usr/bin/security` (as RCP's owner and team items
  do) is readable by a plain `security find-generic-password -w`, also under an
  allow-default Seatbelt profile. Denying the `com.apple.SecurityServer` lookup
  blocks it while network, `ssh-agent`, and public HTTPS Git keep working;
  Keychain-backed HTTPS Git credentials stop working inside tool calls.

## Open checks

- Ubuntu 24.04 `bwrap` behavior and the fallback warning.
- A full Work turn under the wrapper: command broker `launch`, `patch.json`,
  watcher, and Apply, local and over SSH.
- The team server's service account under the real unit (`PrivateTmp`).
- Browser grant from a wrapped tool call; Linux daemon restart on a policy
  change.
- Deploy-key Git push through the backend agent, and the readable-key fallback
  when the agent is missing.
