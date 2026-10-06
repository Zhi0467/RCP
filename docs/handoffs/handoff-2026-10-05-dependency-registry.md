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
- No setup or admission check covers a machine's basic tools. Some owners
  check their own tool when it is used (the rsync transfer contract, the git
  worktree version gate); python3, tar, and mkdir on a remote machine are not
  checked at all. A missing tool surfaces as a shell error partway through a run.
- The desktop backend checks provider CLIs during project setup, but not git
  or the other programs it runs.

## Settled decisions

1. **One registry in code** declares each external program once: what it is
   for, the machines and operating systems it applies to, whether it is
   required or optional there, and how to install it.
2. **Required means an agent run on that machine cannot finish without it.**
   An agent launch onto a machine missing a required program is refused
   before anything is created, with the missing names and the install command.
3. **Optional** means the entry names the feature it gates and the visible
   fallback. A missing optional program never refuses a run. Feature owners
   keep their own readiness probes.
4. **The machine card offers a copyable install command**, never an install
   button. RCP normally has no root on someone's GPU machine.
5. **Every missing program is visible on the machine card.** The
   Dependencies row lists missing required programs and missing optional
   ones with their feature and fallback. No per-run warning is added; missing
   secret hiding also stays in Settings and `doctor`, as today.
6. **The refusal covers agent runs only.** Terminals, compute jobs, and state
   sync keep their existing owner checks (terminal unavailable, the compute
   probe, the rsync contract with its tar fallback).
7. **The code is the only internal record.** The registry replaces the
   spec's dependency table; the spec keeps a pointer to it. Public docs
   still list what a user installs (the server guide's apt line, the
   README's Mac prerequisites), and CI holds them to the registry.
8. **Supported operating systems.** The local machine, where the RCP backend
   runs, is macOS or Linux: the Mac app, a source run, or a team server. A
   remote machine must be Linux. Anything else is refused as unsupported.
   Linux distributions RCP has not been tested on (for example Rocky or CentOS
   on a GPU cluster) are allowed with a "not tested" note, never refused for
   that reason.
9. **Refuse only when sure.** An unreachable machine or an unclear answer is
   *not checked*, which never refuses. Dropped connections are retried with a
   bounded backoff first.
10. **One PR**, landing before 0.4.12 is promoted.

## Design

### Registry

A new module `src/rcp/dependencies.py` holds one frozen entry per program,
written to be read by people:

- `name`
- `purpose`: one line on what RCP uses it for, on every entry
- `required_on` and `optional_on`: sets of roles
- for optional roles: `feature` (what stops working) and `fallback` (what the
  user sees instead)
- `platforms`: `linux`, `darwin`, or both
- `install`: per platform, an apt package name or a short instruction (for
  example `xcode-select --install` for git on macOS, or "see the server
  guide's uv step" for uv)

The roles:

- `local`: the machine running the RCP backend, macOS or Linux.
- `remote`: a Linux machine RCP reaches over SSH to run agents and jobs.
- `server`: the extra programs an installed team server's service account needs.
- `server_install`: programs the root installer needs before that account exists.

The registry declares presence and tier only. Version and feature contracts
stay with their owners: the git ≥2.38 worktree gate, the rsync transfer
contract, Node 20 for the browser, the bwrap and Seatbelt execution probes,
the compute probe job, and browser readiness as a whole.

The tier of each program is in the table at the end.

### Presence check

**Local machine.** No script and no shell: the backend reads
`platform.system()`, `/etc/os-release` on Linux, and `shutil.which` for each
program, with its own PATH (the Mac app repairs it at startup).

**Remote machine.** One SSH command over the same plain route the state
transport uses, so it sees the account and PATH that real runs see. It sends a
short POSIX `sh` script from a source module on stdin to `sh -s`. The script
prints, between begin and end lines carrying a per-call nonce, the OS family
(`uname -s`), the distribution from `/etc/os-release`, and each required
program that `command -v` cannot find. A non-Linux answer is *unsupported*.

**Team server.** `install` runs the local check as root for
`server_install`, then as the service account for `local` and `server`.
`doctor` runs the service account's check. Their version checks stay as they
are.

The tested distributions are one constant in the registry module. The apt
install command is offered only on Debian and Ubuntu; elsewhere the card lists
the package names.

### Robustness on remote machines

Remote machines differ: login shells print banners, some accounts use tcsh or
fish, HPC sites load tools with `module load`, and SSH drops. A false
"missing" blocks runs that would have worked, so it is worse than no check.

1. **Three outcomes.** *Ready*, *missing: …* (or *unsupported*), and *not
   checked*. Only a definite *missing* or *unsupported* refuses a run. *Not
   checked* lets the run proceed as today and shows the reason on the card.
2. **Bounded retry.** A dropped connection or timeout is retried with the
   state transfer's backoff and its transient-failure classifier
   (`transport/state_transfer.py`), within limits kept in `limits.py`. When
   the attempts run out, the result is *not checked*.
3. **POSIX `sh`, shipped from source.** The script never relies on the
   account's login shell syntax.
4. **Framed output.** Only the lines between the begin and end lines count;
   rc-file banners outside them are ignored. A missing frame is *not checked*.
5. **Absolute paths.** Programs the code starts by absolute path
   (`/bin/bash`) are checked at that path, not on PATH.
6. **Fresh before refusing.** Results are cached per machine route and
   account with a short time-to-live. A cached *missing* is rechecked before
   it refuses, so a fixed machine is never stuck.

Tests feed the parser recorded outputs: a banner before the frame, a
truncated frame, a timeout, exit 255, and a tcsh-style login shell. Each must
produce *not checked*, never *missing*.

### Machine card

Every machine card, local and remote, gets a **Dependencies** row next to the
browser row, following the same pattern: it requests the check when the card
mounts and on **Check again**. It shows the OS (with "not tested" when that
applies), missing required programs, missing optional programs with their
feature and fallback, and a copy button for the install command.

### Launch refusal

`BackgroundAgentTasks.admit_provider_task` in `src/rcp/background.py` already
refuses an ineligible execution account before any durable allocation. It
gains the dependency check for the run's machine: a fresh cached result, or a
new check. A definite *missing* or *unsupported* refuses with the reason.

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
   required on `server` or `server_install`.
3. Every entry has a purpose, and every optional entry has feature and
   fallback text.

The spec's dependency table is deleted; its contract and probe notes that
still matter move into the owning modules' docstrings or registry entries.
The README's Mac section names Git through the Command Line Tools, the one
required local program macOS does not ship.

Behavior tests, one per rule:

- Admission refuses a run onto a machine missing a required program, from a
  cold cache; an unreachable machine is *not checked* and is not refused.
- A remote macOS machine is refused as unsupported.
- The chat question follow-up is refused before its task exists.
- The card's check returns the OS, the missing names, and the install command.
- `doctor` reports a missing required program.

### Code structure

Settled with the human, mirroring hidden-read readiness:

- `src/rcp/dependencies.py`: data only. The `Dependency` entries and pure
  lookups such as "required for a remote Linux machine". No I/O.
- `src/rcp/staged_dependency_check.sh`: a fixed POSIX script shipped in the
  package and read through `importlib.resources`. Program names arrive as
  arguments (`sh -s -- python3 rsync …`); the script text never varies.
- `src/rcp/dependency_check.py`: the behavior. Local check, remote check with
  retry, output parsing, and the cache.
- `DependencyStatus` in `src/rcp/core/models.py` and its type in
  `web/src/core/types.ts`, edited serially as shared contracts.

`install`, `doctor`, admission, and the machine card API all call the check
module; none keeps its own list.

**Ownership and concurrency.** The app creates one `DependencyChecker` at
startup and passes it to admission and the machine API, as it does
`AgentLauncher`; tests inject a fake runner and a fake clock. Background tasks
run on their own threads, so it uses threading locks: one lock per machine
route and account, so concurrent runs on one machine share one check and
different machines never wait on each other. Admission waits for at most one
check, under a total deadline in `limits.py`. The project POST routes are
synchronous handlers, so a check runs in the server's thread pool, never on
the event loop.

**Tests**, each layer proving what the others cannot, with no wording
assertions and a test diff no larger than the source diff:

1. The real script under real `sh` (dash on Linux CI) with PATH pointed at a
   temporary folder of fake programs.
2. The parser as a pure function, table-driven over recorded outputs.
3. The checker with a fake runner and clock: retry then success, exhausted
   attempts, single-flight across two threads, recheck before refusing.
4. Admission, doctor, and the card row on behavior and data.

The real SSH hop stays a manual close criterion.

### Agent rule

AGENTS.md gains one line under cross-cutting rules: a new external program is
declared in `rcp/dependencies.py` with its purpose, roles, platforms, and tier;
required programs are checked up front and refuse agent runs when missing;
optional ones name their feature and a visible fallback, and their owner keeps
the readiness probe.

## Not in scope

- Installing packages on any machine.
- Bundling system tools into the Mac app.
- Remote macOS machines. The remote launch wrapper uses Linux `setsid`.
- Dependency refusal for terminals, compute jobs, and state sync.
- Checking that release notes mention a new prerequisite.

## Close criteria

- The CI checks above pass, and each fails when its rule is broken.
- On a disposable data directory, a remote machine with `rsync` hidden from
  the SSH PATH shows it under **Dependencies** with the apt command, and a
  chat launch onto it is refused with that reason. Restoring PATH and
  **Check again** clears it. Blocking SSH to it shows *not checked* and does
  not refuse.
- The Mac app's local machine card shows **Dependencies** as ready.
- `rcp server doctor` on a disposable server reports a missing required program.
- A fresh 0.4.12 candidate passes the desktop checks before promotion.

## Tiers

R = required, O = optional (feature → fallback in the last column), — = not
used there. L/M = Linux or macOS only. Local is macOS or Linux; remote is
Linux. Server lists only what an installed team server adds; "install" marks
`server_install`.

| Program | Local | Server | Remote | Purpose; optional feature → fallback |
|---|---|---|---|---|
| `age` | — | R | — | Protected backup encryption |
| `age-keygen` | — | R | — | Backup identity creation and readback |
| `apt-get` | — | O | — | Browser system libraries → failure shown with the admin command |
| `bash` | R | — | R | Provider and watcher shells, terminal shell |
| `bwrap` | O/L | — | O | Secret hiding → runs unhidden; Settings, doctor, and card say so |
| `cat` | — | — | R | Run folder reads |
| `curl` | — | R install | — | Server install and provider updates |
| `env` | R | — | R | Account and command environments |
| `findmnt` | O/L | — | O | Mirrored terminals → terminal unavailable |
| `getent` | — | R install | — | Account policy proof |
| `git` | R | — | R | Repository operations; owner keeps the version gate |
| `id` | R | — | R | Execution-account identity |
| `launchctl` | O/M | — | — | Browser, helper jobs, keep-awake → that feature unavailable |
| `ldd` | O/L | — | O | Browser libraries → browser unavailable with the reason |
| `loginctl` | O/L | R | O | Server account lifecycle; elsewhere linger → feature unavailable with the admin command |
| `mkdir` | — | — | R | Run folder preparation |
| `node` | O | — | O | Browser → cannot be turned on without Node 20+ |
| `npm` | O | — | O | Browser install, source web build → that operation fails with the reason |
| `osascript` | O/M | — | — | Keep-awake install → error shown, nothing changed |
| `printenv` | — | — | O | Hidden-read home lookup → runs unhidden with the reason |
| `ps` | O | — | O | Stopping a run → stop reported as unconfirmed |
| `python3` | — | — | R | Shipped remote helpers |
| `rm` | — | — | R | Run folder cleanup |
| `rsync` | R | — | R | Transfers; owner keeps its contract and tar fallback |
| `runuser` | — | R install | — | Privileged account operations |
| `sandbox-exec` | O/M | — | — | Secret hiding → runs unhidden; Settings says so |
| `setsid` | — | — | R | Remote provider launch wrapper |
| `sh` | R | — | R | Command wrappers and scripts |
| `sleep` | — | — | O | Compute probe job → compute route unavailable with the reason |
| `ssh` | R | — | R | Remote transport and Git transport |
| `ssh-add` | O | — | O | Agent-held deploy keys → keys stay readable, with the reason |
| `ssh-agent` | O | — | O | Agent-held deploy keys → keys stay readable, with the reason |
| `ssh-keygen` | O | R | O | Repository key setup → refused with the reason |
| `sudo` | — | R install | — | Privilege policy proof |
| `systemctl` | O/L | R | O | Server service lifecycle; elsewhere terminals and helper jobs → unavailable |
| `systemd-run` | O/L | — | O | Terminals, browser, helper jobs → unavailable |
| `tar` | — | — | R | Remote archive extraction |
| `test` | — | — | R | Remote shell predicates |
| `true` | R | — | R | Route and wrapper probes |
| `uname` | R | — | R | OS identity |
| `useradd` | — | R install | — | Service account creation |
| `uv` | — | R | — | Managed server runtime; installed by the server guide's uv step |
