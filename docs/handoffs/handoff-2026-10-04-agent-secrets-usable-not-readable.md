# Agent secrets: usable but not readable

Status 2026-10-05: code complete on this branch, and the live provider and browser
checks pass. One branch and one PR carry all of it. Implemented: the `Host` check,
CORS removal, slices A–F, remote account agents and key confirmation, the wrapper
staged in a hidden `~/.rcp/hidden-read/<fingerprint>` folder, linger detection, and
the pinned browser CLI launcher. Remaining: the checks listed under "Live checks".

## Threat model

A generic network attacker. RCP exposes no public listener (a team server is
tailnet-only), so the only untrusted input is agents fetching web content
(search, fetch, browser). An untargeted prompt injection asks the agent to read
a secret, such as `~/.ssh/id_*`, and send it somewhere. Such an attack needs
untrusted content, private data, and a way out. Agents keep the web and the
network, so this work makes selected secrets unreadable on supported launches:
tools can still *use* them but cannot *read* them.

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
  inside Seatbelt; browser file operations stay unhidden there (shown in
  Settings and doctor).
- Git: the backend owns one long-lived `ssh-agent` per account at a stable
  socket path, loaded with that account's deploy keys and restarted with the
  backend. `core.sshCommand` keeps its transport and adds
  `-o IdentityAgent=<stable path>`. A deploy key is hidden only when that agent
  is confirmed to hold it at launch; otherwise the key stays readable for that
  turn (shown in Settings and doctor), so Git always works. The agent is owned per OS
  account (an account lock, since two data directories can share an account),
  lives in a private stable directory under `~/.rcp` (not `/tmp`: the service
  unit has `PrivateTmp`), starts after the startup-effect fence opens and before
  recovery launches work, and stops after workers drain.
- Remote execution machines get the same rule in this PR. Each remote account
  runs one long-lived `ssh-agent` at a stable socket under its `~/.rcp`, owned
  by the existing generic launch helper (systemd user unit on Linux), started
  on the first launch that needs it and loaded with that account's deploy keys.
  Each launch confirms deploy keys against it, and user keys against the
  remote account's own agent, through the staged remote helper. If either
  agent is missing or a check fails, the key stays readable for that turn.
- "Holds the key" means: the SHA256 fingerprint of the decoded public key is
  listed by that agent on the launch's host and a bounded signing check passes.
  A missing or stale `.pub`, an unavailable agent, or a failed check means
  readable fallback. Confirmation runs before scope, browser, and provider
  policy are resolved. A parent folder holding an exempt key is never masked.
  The user's `SSH_AUTH_SOCK` stays separate from RCP's deploy-key agent.
- `~/.ssh/id_*` files (never `.pub`) are hidden only when that key is loaded
  in the account's `ssh-agent` by the same fingerprint rule, so SSH keeps
  signing through the agent; keys not in an agent stay readable.
- OpenCode's native `read`, `grep`, `glob`, and `list` tools bypass `$SHELL`.
  They get path denies from the scope where OpenCode's permission rules can
  express them; otherwise that launch reports itself unhidden.
- Files use a deny list: code-owned defaults plus per-machine **hidden folders**
  stored beside the existing machine writable paths (`hidden_folders_json`
  beside `writable_paths_json`, `SpaceMachineRecord.hidden_folders`, the
  existing machine PATCH, and `MachineCard`; never a project manifest). One
  backend resolver validates bounded absolute folders on the execution host,
  checks overlap both ways against checkouts, stages, tools, and sockets, and
  rechecks at launch. Settings lists the
  defaults read-only; users can add folders, not remove defaults. Any member
  may edit a team machine's list (RCP has no admin role). A path that overlaps
  a project checkout, a stage, `known_hosts`, or the command sockets is refused
  with a clear message.
- Tool-call environments use a deny list (decided 2026-10-05): every variable
  passes except credential-looking names (`*_TOKEN*`, `*SECRET*`, `*PASSWORD*`,
  `*_KEY`, `AWS_*`, ...), so a lab's own variables, proxies, Slurm, and GPUs keep
  working. Providers keep their own authentication.
- When a host cannot enforce hiding (no `bwrap`, or user namespaces blocked as
  on Ubuntu 24.04), the launch runs unhidden. It never blocks.
- Gaps are shown per machine in Settings and in doctor, never as a per-turn
  trace or badge. The agent's prompt still states its effective status.
- The macOS Keychain stays open to tool calls. RCP's own items (owner secret,
  team credentials, local-HTTPS sealing key) trust Apple's `security` tool, so an
  agent can read them, but they work only against the loopback backend, the
  tailnet-only team server, or the RCP window, so they are worth little to a
  remote attacker. Blocking the Keychain would break `gh` and Keychain-backed
  Git. Developer ID signing with app-scoped items is the future fix.
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
   `.pub`), resolved provider login files (the provider process reads them
   outside the wrapper), and the WebKit storage entries now in
   `CODEX_READ_DENY_PATHS`. Credential files that agent tools read directly
   (`~/.config/gh`, `~/.netrc`, `~/.aws`, ...) are not defaults, since hiding
   them would break `gh`, Git, or cloud CLIs; a user may add them as hidden
   folders.
3. One staged, stdlib-only wrapper, shipped from its source module like
   `agents/staged_command_client.py`: an environment deny list, then a Seatbelt
   profile on macOS (`allow default`, deny the hidden paths) or `bwrap --dev-bind / /` with the hidden paths masked on
   Linux (no network namespace). A readiness probe per host chooses between
   enforced and the visible-warning fallback.
4. Provider wiring, all rendered from the scope:
   - Claude: `CLAUDE_CODE_SHELL_PREFIX` set to the wrapper (local environment
     and SSH `remote_prefix`), plus `Read(...)` denies for its in-process tools,
     on every capability's settings branch. Exact files and globs render
     differently from directories.
   - Codex: deny entries in every permission profile, including the
     ingestion/paper sandbox path and `app_server` thread and turn policies;
     `shell_environment_policy` excluding the deny list (`app_server` currently
     resets it to `{}`).
   - OpenCode: `SHELL` set to the wrapper acting as a shell.
5. Browser (Linux): `browser/host.py` runs the wrapper inside the systemd job
   command (wrapping `systemd-run` itself would not bind the daemon), persists
   the policy fingerprint, and restarts a live daemon under `host_lock` when it
   changes, keeping the profile and active leases.
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

## Slices

Codex workers in sibling worktrees after the serial contract slice; commits
merge into this one branch.

- **A, contracts (serial):** `HiddenReadScope`, key evidence, effective status,
  fingerprint, machine payloads, and seams in `core/models.py`, `config.py`,
  `providers/base.py`, `storage/models.py`, `agents/hidden_read_scope.py`,
  `limits.py`, `web/src/types.ts`.
- **B, policy:** defaults, host validation, environment deny list, the staged
  wrapper, readiness, and fallback.
- **C, backend and Git:** the account `ssh-agent`, identity confirmation,
  `IdentityAgent` transport, the `Host` check, doctor warnings.
- **D, launch and providers:** resolve once before browser and provider
  preparation; Claude, Codex (`exec` and `app_server`), OpenCode; prompts and
  warnings.
- **E, browser:** Linux daemon policy, fingerprint restart, lease preservation,
  macOS and fallback visibility.
- **F, Settings and docs:** migration, `MachineCard`, specs, decision record.

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

## Live checks

Done 2026-10-05 against `main`, with served Work turns through the real routes:
- Every provider, Discuss and Work, on a macOS desktop and on an Ubuntu 22.04 host,
  local and over SSH: command broker, `patch.json`, and Apply work.
- No capability changed from allowed to blocked. Hidden secrets read on `main` and
  fail on this branch.
- On Linux, uploading a hidden file through the browser fails with `ENOENT` while
  ordinary uploads work.
- Missing linger on systemd 249 kills helper jobs ten seconds after logout. This
  branch now detects it and offers **Allow background processes**.

Remaining:
- After merge, update the team server; run doctor and one Work turn under the real
  unit (`PrivateTmp`, `NoNewPrivileges`) as the service account.
- A deploy-key Git push through the backend agent, and the readable fallback when
  that agent is missing.
- Ubuntu 24.04 `bwrap` behavior and the fallback warning.
- The frozen candidate build.
