# Declare every external program as required or optional

Date: 2026-10-05
Status: design settled with the human on 2026-10-05 and revised after one
astra xhigh design review the same day. Not started. Version 0.4.12 has not
been promoted; promotion waits for this PR and a fresh candidate check.

## Problem

RCP starts about 40 external programs (git, ssh, rsync, bwrap, node, python3,
tar, …). The spec table in
[server-and-machine-operations.md](../specs/server-and-machine-operations.md#external-dependencies)
lists them, and `tests/test_external_dependencies.py` keeps its names in step
with the source. Nothing else is held to it:

- No one says which programs are required and which are optional.
- Server `install` and `doctor` each keep their own hand-written list.
- The server guide's apt line is edited by hand; `bwrap` was missing until #265.
- No setup or admission check covers a host's basic tools. Some owners check
  their own tool when it is used (the rsync transfer contract, the git worktree
  version gate); python3, tar, and mkdir on an execution host are not checked
  at all. A missing tool surfaces as a shell error partway through a run.
- The desktop backend checks provider CLIs during project setup, but not git
  or the other programs it runs.

## Settled decisions

1. **One registry in code** declares each external program once: the roles and
   platforms it applies to, whether it is required or optional there, and how
   to install it.
2. **Required** means checked up front. An agent launch onto a host missing a
   required program is refused before allocation, with the missing names and
   the install command.
3. **Optional** means the entry names the feature it gates and the visible
   fallback. Feature owners keep their own readiness probes.
4. **The machine card offers a copyable install command**, never an install
   button. RCP normally has no root on someone's GPU host.
5. **Basic tools that no setup check covers today become required** on the
   hosts that run them.
6. **Every missing optional program is visible on the machine card.** The
   Dependencies row lists it with the feature it gates and its fallback.
   No per-run warning is added; missing secret hiding also stays in
   Settings and `doctor`, as today.
7. **The refusal covers agent runs only.** Terminals, compute jobs, and state
   sync keep their existing owner checks (terminal unavailable, the compute
   probe, the rsync contract with its tar fallback).
8. **The code is the only internal record.** The registry replaces the
   spec's dependency table; the spec keeps a pointer to it. Public docs
   still list what a user installs (the server guide's apt line, the
   README's Mac prerequisites), and CI holds them to the registry.
9. **One PR**, landing before 0.4.12 is promoted.
10. **The check learns the OS first.** A machine that is neither Linux nor
    macOS is refused as unsupported. Linux distributions RCP has not been
    tested on (for example Rocky or CentOS on a GPU cluster) are allowed
    with a "not tested" note on the card, never refused for that reason.

## Design

### Registry

A new module `src/rcp/dependencies.py` holds one frozen entry per program:

- `name`
- `required_on` and `optional_on`: sets of roles. Roles are `desktop`,
  `server_install` (root bootstrap before the service account exists),
  `server` (the service account at runtime), and `execution_host`.
- `platforms`: `linux`, `darwin`, or both
- for optional roles: `feature` and `fallback` text
- `install`: per platform, either an apt package name or a short instruction
  (for example `xcode-select --install` for git on macOS, or "see the server
  guide's uv step" for uv)

The registry declares presence and tier only. Version and feature contracts
stay with their owners: the git ≥2.38 worktree gate, the rsync transfer
contract, Node 20 for the browser, the bwrap and Seatbelt execution probes,
the compute probe job, and browser readiness as a whole.

The tier of each program is in the table at the end.

### Presence check

One shipped helper runs `command -v` for the programs a role requires on that
platform and returns the missing names. It runs through each existing
execution adapter, so it sees the same account and PATH as the real work:

- **Desktop:** in the backend process, with its own PATH (the desktop shell
  already repairs it at startup).
- **Execution host:** over the same plain SSH route the state transport uses,
  not the provider's login shell.
- **Server:** `install` runs it as root for `server_install`, then as the
  service account for `server`. `doctor` runs the `server` set as the service
  account. Their version checks stay as they are.

The helper reports the OS family (`uname -s`) and, on Linux, the
distribution from `/etc/os-release`, then checks the programs required for
that platform. The tested distributions are one constant in the registry
module. The apt install command is offered only on Debian and Ubuntu;
elsewhere the card lists the package names.

### Robustness on remote hosts

Remote hosts differ: login shells print banners, some accounts use tcsh or
fish, HPC sites load tools with `module load`, and SSH drops. A false
"missing" blocks launches that would have worked, so it is worse than no
check. The rules:

1. **Refuse only on a definite answer.** The check has three outcomes:
   *ready*, *missing: …*, and *not checked* (unreachable, timeout, or output
   that does not parse). Only *missing* refuses a launch. *Not checked* lets
   the launch proceed as today and shows the reason on the card.
2. **POSIX `sh`, shipped from source.** The helper is a script in a source
   module, sent on stdin to `sh -s`. It never relies on the account's login
   shell syntax.
3. **Framed output.** The script prints its result between begin and end
   lines carrying a per-call nonce. Anything outside them, such as rc-file
   banners, is ignored. A missing frame is *not checked*, never *missing*.
4. **The real route's PATH.** It runs over the same plain SSH route the state
   transport uses, which is where python3, rsync, tar, and mkdir run. Programs
   the code starts by absolute path (`/bin/bash`, `/usr/bin/sandbox-exec`)
   are checked at that path, not on PATH.
5. **Fresh before refusing.** Results are cached per host route and account
   with a short time-to-live. A cached *missing* is rechecked before it
   refuses, so a fixed host is never stuck and a stale result never blocks.

Tests feed the parser recorded outputs: a banner before the frame, a
truncated frame, a timeout, exit 255, and a tcsh-style login shell. Each must
produce *not checked*, never *missing*.

### Machine card

Every machine card, local and remote, gets a **Dependencies** row next to the
browser row, following the same pattern: it requests the check when the card
mounts and on **Check again**, and shows missing required programs, missing
optional programs with their fallback, and a copy button for the install
command.

### Launch refusal

`BackgroundAgentTasks.admit_provider_task` in `src/rcp/background.py` already
refuses an ineligible execution account before any durable allocation. It
gains the dependency check for the launch's execution host: a cached verdict
if fresh, otherwise a probe. A host with a missing required program is refused
with the reason. An unreachable host keeps today's behavior.

This covers Discuss, Work, Experiment, Auto-research roots, children, wakes,
and retries, ingestion, coaching, artifact edits, and reports. One path
allocates first: chat question follow-ups create their task before
`launch_admitted` (`src/rcp/runs/chat.py`, `answer` handling). That path moves
behind admission. Already-reserved report allocations keep their reservation.
A refusal never reroutes an episode or replaces its session (invariant 10g).

### What CI enforces

`tests/test_external_dependencies.py` stays a bounded static check of literal
launches; it does not claim to find every program. It grows to:

1. Every literal program the source starts or looks up is in the registry, and
   every registry entry is used. The scanner learns the forms it misses today
   (`setsid` in the remote launch wrapper, `sleep` in the compute probe,
   remote `ssh-agent`), each with a fixture.
2. The server guide's apt line contains the apt package of every program
   required on `server` or `server_install` for Linux.
3. Every optional entry has feature and fallback text.

The spec's dependency table is deleted; its contract and probe notes that
still matter move into the owning modules' docstrings or registry entries.
The README's Mac section names Git through the Command Line Tools, the one
required desktop program macOS does not ship.

Behavior tests, one per rule:

- Admission refuses a launch onto a host missing a required program, from a
  cold cache, and reports unreachable separately.
- The chat question follow-up is refused before its task exists.
- The card's check returns the missing names and the install command.
- `doctor` reports a missing required program.

### Agent rule

AGENTS.md gains one line under cross-cutting rules: a new external program is
declared in `rcp/dependencies.py` with its roles, platforms, and tier; required
programs are checked up front and refuse agent launches when missing; optional
ones name their feature and a visible fallback, and their owner keeps the
readiness probe.

## Known gap found by the review

Every remote provider launch with a pid file runs `setsid --wait`
(`src/rcp/agents/launcher.py`, `_remote_login_command`, since 2026-09-10).
`setsid` is util-linux and absent on macOS. With this PR, a remote macOS
execution host will be refused with "missing: setsid" instead of failing at
launch. Making remote macOS launches work again is a separate fix.

## Not in scope

- Installing packages on any host.
- Bundling system tools into the Mac app.
- Dependency refusal for terminals, compute jobs, and state sync.
- Checking that release notes mention a new prerequisite.

## Close criteria

- The CI checks above pass, and each fails when its rule is broken.
- On a disposable data directory, a remote machine with `rsync` hidden from
  the SSH PATH shows it under **Dependencies** with the apt command, and a
  chat launch onto it is refused with that reason. Restoring PATH and
  **Check again** clears it.
- The desktop machine card shows **Dependencies** as ready on this Mac.
- `rcp server doctor` on a disposable server reports a missing required program.
- A fresh 0.4.12 candidate passes the desktop checks before promotion.

## Tiers

R = required, O = optional (fallback in the last column), — = not used in that
role. L/M = Linux/macOS only. The three columns are desktop, server
(`server_install` marked "install"), and execution host.

| Program | Desktop | Server | Execution host | Required for / optional feature → fallback |
|---|---|---|---|---|
| `age` | — | R | — | Protected backup encryption |
| `age-keygen` | — | R | — | Backup identity creation and readback |
| `apt-get` | — | O | — | Browser library install → failure reported with the admin command |
| `bash` | R | R | R | Provider and watcher shells, terminal shell |
| `bwrap` | O/L | O | O/L | Secret hiding → launch runs unhidden; Settings and doctor say so |
| `cat` | — | — | R | Remote setup and stage reads |
| `curl` | — | R install | — | Server install and provider updates |
| `env` | R | R | R | Account and command environments |
| `findmnt` | O/L | O | O/L | Mirrored terminals → terminal unavailable |
| `getent` | — | R install | — | Account policy proof at install |
| `git` | R | R | R | Repository operations; owner keeps the worktree version gate |
| `id` | R | R | R | Execution-account identity |
| `launchctl` | O/M | — | O/M | Browser, helper jobs, keep-awake → that feature unavailable |
| `ldd` | O/L | O | O/L | Browser libraries → browser unavailable with the reason |
| `loginctl` | O/L | R | O/L | Server account lifecycle; elsewhere linger → feature unavailable with the admin command |
| `mkdir` | — | — | R | Remote stage and state preparation |
| `node` | O | O | O | Browser → cannot be turned on without Node 20+ |
| `npm` | O | O | O | Browser install, source web build → that operation fails with the reason |
| `osascript` | O/M | — | — | Keep-awake install → error shown, nothing changed |
| `printenv` | — | — | O | Hidden-read home lookup → launch runs unhidden with the reason |
| `ps` | R | R | R | Process identity and stopping |
| `python3` | — | — | R | Shipped remote helpers |
| `rm` | — | — | R | Remote staged handoff cleanup |
| `rsync` | R | R | R | Transfers; owner keeps its contract and tar fallback |
| `runuser` | — | R install | — | Privileged account operations |
| `sandbox-exec` | O/M | — | O/M | Secret hiding → launch runs unhidden; Settings says so |
| `setsid` | — | — | R/L | Remote provider launch wrapper (see known gap) |
| `sh` | R | R | R | Command wrappers and scripts |
| `sleep` | — | — | R | Compute probe job |
| `ssh` | R | R | R | Remote transport and Git transport |
| `ssh-add` | O | O | O | Agent-held deploy keys → keys stay readable, with the reason |
| `ssh-agent` | O | O | O | Agent-held deploy keys → keys stay readable, with the reason |
| `ssh-keygen` | O | R | O | Repository key setup on that host → refused with the reason |
| `sudo` | — | R install | — | Privilege policy proof at install |
| `systemctl` | O/L | R | O/L | Server service lifecycle; elsewhere terminals and helper jobs → unavailable |
| `systemd-run` | O/L | O | O/L | Terminals, browser, helper jobs → unavailable |
| `tar` | — | — | R | Remote archive extraction |
| `test` | — | — | R | Remote shell predicates |
| `true` | R | R | R | Route and wrapper probes |
| `uname` | R | R | R | Host OS identity |
| `useradd` | — | R install | — | Service account creation |
| `uv` | — | R | — | Managed server runtime; installed by the server guide's uv step |
