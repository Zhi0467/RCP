# Declare every external program as required or optional

Date: 2026-10-05
Status: design settled with the human on 2026-10-05. Not started. Version 0.4.12
has not been promoted; promotion waits for this PR and a fresh candidate check.

## Problem

RCP starts about 40 external programs (git, ssh, rsync, bwrap, node, python3,
tar, …). The spec table in
[server-and-machine-operations.md](../specs/server-and-machine-operations.md#external-dependencies)
lists them, and `tests/test_external_dependencies.py` keeps its names in step
with the source. Nothing else is held to it:

- No one says which programs are required and which are optional.
- Server `install` and `doctor` each keep their own hand-written list.
- The server guide's apt line is edited by hand; `bwrap` was missing until #265.
- Adding a machine runs nothing against the host. Each feature probes its own
  tools when used; python3, rsync, git, and tar on an execution host are never
  checked, so a missing one surfaces as a shell error halfway through a run.
- The Mac app checks no external program up front. Git comes from Apple's
  Command Line Tools, not from macOS itself.

## Settled decisions

1. **One registry in code** declares each external program once: where it runs
   (desktop, team server, execution host), whether it is required or optional
   there, and an install hint (apt package; macOS step).
2. **Required** means checked at setup. A host missing a required program is
   refused for agent launches, with the missing names and the install command.
3. **Optional** means the entry names the feature it gates and the visible
   fallback (for example: no `bwrap`, agents run without secret hiding and a
   dismissible warning says so; no Node 20+, browser use cannot be turned on).
4. **The machine card offers a copyable install command**, never an install
   button. RCP normally has no root on someone's GPU host. This matches the
   browser's missing-library command and the linger admin command.
5. **The basic tools that are never checked today become required** on the
   hosts that run them, and the presence check covers them.

## Design

### Registry

A new module `src/rcp/dependencies.py` holds one frozen entry per program:

- `name`
- `required_on` and `optional_on`: sets of `desktop`, `server`, `execution_host`
- for optional hosts: `feature` and `fallback`, both required text
- `apt_package` (or none when Ubuntu's base system always has it) and
  `macos_hint` (for example `xcode-select --install` for git)

Version and feature contracts stay with their owners (git ≥2.38 in the worktree
code, the rsync contract in `state_transfer.py`, Node 20 in the browser). The
registry only declares presence and tier.

### Presence check

One function runs `command -v` for every program required on a host, in one
call: locally for the desktop, over the existing SSH route for an execution
host, and as the service account for the team server. It returns the missing
names and the install command for that OS (an apt line on Ubuntu; the macOS
hints on a Mac; the bare names elsewhere).

- **Machine card:** a **Dependencies** row on every machine card, local and
  remote. It runs when a machine is added and on **Check again**, and shows
  missing programs with a copy button for the command.
- **Launch:** the verdict is cached per host per backend process, next to
  provider readiness. A launch onto a host whose verdict lists a missing
  required program is refused with the reason, before a task is allocated.
  A refused launch probes again first, so a fixed host is not stuck.
- **Server:** `install` and `doctor` read their program list from the registry
  instead of their local tuples. Their version checks stay as they are.

### What CI enforces

`tests/test_external_dependencies.py` grows from a name check into:

1. Every program the source starts, or looks up with `shutil.which`, is in the
   registry, and every registry entry is used.
2. The spec table matches the registry, including a new Required/Optional column.
3. The server guide's apt line contains the `apt_package` of every program
   required on the server.
4. Every optional entry names its feature and fallback.

Behavior tests, one per rule:

- A host missing one required program: the card lists it with the command, and
  a launch onto it is refused with that reason.
- Doctor reports a missing required program from the registry.

### Agent rule

AGENTS.md gains one line under cross-cutting rules: a new external program is
declared in `rcp/dependencies.py` as required or optional per host; required
programs are checked at setup and refuse launches when missing; optional ones
name their feature and a visible fallback. CI holds the source, the spec table,
and the server install line to the registry.

## Not in scope

- Installing packages on any host.
- Bundling system tools into the Mac app.
- Checking that release notes mention a new prerequisite (the release checklist
  keeps that as a manual step).

## Close criteria

- The CI checks above pass, and each fails when its rule is broken.
- On a disposable data directory, a remote machine with `rsync` hidden from
  PATH shows it under **Dependencies** with the apt command, and a chat launch
  onto it is refused with that reason. Restoring PATH and **Check again**
  clears it.
- The desktop machine card shows **Dependencies** as ready on this Mac.
- `rcp server doctor` on a disposable server reports a missing required program.
- A fresh 0.4.12 candidate passes the desktop checks before promotion.
