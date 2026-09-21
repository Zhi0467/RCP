# A terminal in a project, fenced to its repositories

Date: 2026-09-19
Status: implemented; the Linux merge qualification passed on 2026-09-20. The
design was confirmed on
2026-09-19 after a gpt-6-astra review refuted the original containment
mechanism and two of its findings were reverified directly. The trust model and
the collision behavior are settled.

Implemented: the Terminals destination, local and remote backends, the mirrored
mount profile and its preflight, the capability probe, the session lifetime and
its cleanup, and Git access including a remote team checkout's deploy key. A
live run against a real Linux execution machine opened a shell on every
registered repository and returned real `git status` output.

Qualified on 2026-09-20 on a Linux machine with systemd, in a personal space,
driven over the API and WebSocket against a throwaway data directory: the
mirrored mounts hold (canonical `.research` and the account's SSH directory
refuse writes, home is the empty tmpfs with only the bound entries, the ambient
environment is scrubbed); Git reaches its remote over SSH with the account's key
and an interactive `git add -p` stages a hunk; a PTY resize reaches the shell;
the unit survives the server being killed, and the restart stops it and finishes
its record; a record naming an unreachable machine is retained, refuses a
reopening on that repository, and stops refusing once it is gone.

Remaining: the closure condition below driven from the Terminals destination
rather than the API — a conflicted `git rebase -i` completed after navigating
away and back — and one live run in a team space, where Git uses the
repository's deploy key instead of the account's key.

Close this handoff when a project member can open an interactive shell on a
registered repository from the project UI, that shell runs every ordinary Git
command including the interactive ones, and the mount profile gives the member
accident resistance against the control plane, other projects, and provider
credentials — which is the honest claim the
[decision](../decisions/2026-09-19-a-member-terminal-inherits-the-work-trust-boundary.md)
settles, not isolation from a deliberate member.

## The problem

A team project's repositories live on the server under the `rcp` service
account. Members do not have that account and are not meant to: whoever holds
operating-system authority on the server is a separate role, and
[`projects-spaces-and-operations.md`](../specs/projects-spaces-and-operations.md)
states that RCP defines no administrator member role.

So a member who wants to run `git pull` has exactly one route today: ask an
agent to run it inside a Work turn. That is a poor fit for three reasons.

1. Git's hard cases are interactive. Conflict resolution, `rebase -i`, and
   `add -p` are conversations with the working tree, not single commands. An
   agent relaying them is slower than doing it and less predictable.
2. A Work turn on Claude has an unbounded `Bash`
   ([`providers-and-containment.md`](../specs/providers-and-containment.md), and
   the [no-OS-sandbox decision](../decisions/2026-09-13-claude-work-runs-without-the-os-sandbox.md)).
   The member is already paying for an unfenced shell on that machine; they just
   cannot drive it themselves.
3. Ordinary repository work is not graph work. Routing it through the agent task
   lifecycle produces episodes and receipts for something that should leave no
   research trace at all.

## Why a bare service-account shell is not the answer

`rcp` cannot become root. Install writes no sudoers entry, runs no
`usermod -aG`, and the account has an unusable password; verified in
[`install.py`](../../src/rcp/server_ops/install.py). Escalation to root is not
the risk.

The risk is that `rcp` already owns everything RCP protects. From
[`layout.py`](../../src/rcp/server_ops/layout.py), with no escalation at all, a
shell as that account reads and writes:

- `rcp-server/data` — the SQLite control plane. Writable, so membership and
  sessions can be forged, not merely read.
- `rcp-server/projects` — every project's checkout, including projects the
  member does not belong to, and their `.research` history. Invariants 1, 2,
  and 6 are bypassed by writing the files directly.
- `rcp-server/credentials` — every project's Git deploy key (the Claude
  setup token lives under the data directory; see the correction below).
- `/home/rcp/.codex` and `/home/rcp/.claude` — provider logins, usable off the
  machine.
- `/home/rcp/.ssh` — the machine's outbound identities, including configured
  remote execution accounts.

Membership stops being authority the moment one member holds that shell.

## Why the guardrail cannot be a command filter

A shell cannot be fenced from inside the shell. An allowlist or blocklist of
commands is defeated by `python3 -c`, by `git config core.pager`, by `!` from
inside Git's own pager, and by any editor's shell escape. Every such filter is a
puzzle, and it only has to be solved once.

A mount namespace is not a puzzle. The paths are absent.

## What the first design got wrong

A `gpt-6-astra` review on 2026-09-19 refuted the mechanism this handoff
originally proposed: a shell still running as `rcp`, fenced by `systemd-run`
with `ProtectHome=yes` and a `ReadWritePaths` entry per repository root. Three
findings are load-bearing, and the first two were reverified directly.

**The service UID is itself machine authority.** The installed control socket is
`/run/rcp/control.sock` ([`layout.py`](../../src/rcp/server_ops/layout.py)), not
a path under the data directory, so no `ProtectHome` setting hides it. Its
handler admits any peer whose uid is root *or the owning service uid*
([`control.py`](../../src/rcp/server_ops/control.py)), which is exactly the
proposed shell's identity; `PrivateUsers=yes` does not change the host uid.
Member removal is reachable there, and the instance id the socket wants is
published by `/api/health`. Hiding credential *files* while keeping the service
*identity* is not containment.

**`.research` is inside a registered repository, not outside it.** The manifest
requires `state.repository` to name a registered repository
([`config.py`](../../src/rcp/config.py)) and canonical state is written to
`<that repository>/.research/manifest.toml`
([`setup.py`](../../src/rcp/setup.py)). A writable root therefore contains the
canonical history, its projections, and its advisory lock namespace. The claim
that `.research` was structurally excluded was simply false.
[`agents/write_scope.py`](../../src/rcp/agents/write_scope.py) already builds
explicit protected `.research` paths, including canonicalized ones; the original
design ignored machinery that exists precisely for this.

**`ProtectHome=yes` cannot be reopened.** It makes `/home`, `/root`, and
`/run/user` inaccessible, and systemd drops mounts beneath an inaccessible
entry, so nested `ReadWritePaths` exceptions do not re-expose a repository under
`/home/rcp`. Selective exposure is `ProtectHome=tmpfs` with `BindPaths`. Not
retested here — this checkout is macOS — but it is documented systemd behavior.

The review also corrected three smaller claims. The compute backend applies
those properties only for `mirrored` containment and also admits `job_root`; its
probe can succeed in cooperative-only mode, and
[`compute-jobs.md`](../specs/compute-jobs.md) explicitly disclaims hostile
same-account isolation, so a compute readiness result cannot authorize this
feature. A readable deploy key is a *write* credential that survives being
copied, so exposing it read-only is possession, not containment. And the Claude
setup token lives under `<data_dir>/providers/...`, not in `rcp-server/credentials`
as stated above.

## The settled trust model

A member terminal inherits the Work turn's trust boundary. It is convenience
for a colleague who is already trusted on this machine, not isolation against
one who is not. The rationale is in
[the decision record](../decisions/2026-09-19-a-member-terminal-inherits-the-work-trust-boundary.md).

This resolves the review's blocking question and discards most of what it
costed. There is no separate execution identity, no independent clone, and no
Git credential broker, because none of those defend against a threat this
product has decided it does not face.

What survives from the review is everything about **accidents** rather than
attackers, and one honesty requirement: the feature must not describe itself as
containment.

## The design

One interactive shell, per registered repository on the local machine, opened by
a project member from the project UI, running as the service account in the
same checkout agents use.

**Authorization** is project membership, checked on the route like every other
project-scoped route. The WebSocket needs its own admission check: the existing
authentication is HTTP middleware
([`api/app.py`](../../src/rcp/api/app.py)) and does not cover an upgraded
connection. Losing membership closes live sessions.

**Canonical state is refused where the OS can refuse it, and reported where it
cannot.** Invariants 1, 2, and 6 are about corruption, and a slipped `rm`
corrupts history exactly as well as malice does. The terminal reuses the
protected-path construction in
[`agents/write_scope.py`](../../src/rcp/agents/write_scope.py), including its
canonicalized `.research` entries, rather than growing a second list. This is a
code contract, not manifest configuration.

A machine either can make those paths read-only or cannot, and that is an OS
capability, not a policy choice. This follows the vocabulary compute already
uses in [`compute-jobs.md`](../specs/compute-jobs.md): **mirrored** containment
on local Linux with systemd, **cooperative** elsewhere, and a cooperative
session states that canonical-state protection is unavailable on that machine
rather than implying a guarantee it does not keep. Confirmed by the human on
2026-09-19.

Cooperative is chosen by what the machine is, never by a mirrored launch
failing. A machine that should support mirrored containment and does not
produce it is a failure, not a downgrade.

**Accident resistance, named as such.** The shell runs under `systemd-run` with
`ProtectHome=tmpfs` plus `BindPaths` for the repository root — the idiom that
actually works, unlike the refuted profile — and `ReadOnlyPaths` for what Git
needs. This stops a wrong-directory mistake. It stops nothing deliberate, and
the spec text must say so in the same breath, the way
[`providers-and-containment.md`](../specs/providers-and-containment.md) already
names Claude's unbounded `Bash` an accepted gap rather than a boundary.

**Shared checkout.** A pull that does not update the tree agents run in fails
the purpose. Agents and the terminal share it. Git's own index locking handles
the common collision.

A live Work turn on that repository does not block a terminal. The session
opens, and the UI states that a turn is running and what it is. Confirmed by
the human 2026-09-19: watching an agent work is a real use for this feature,
and an interlock would forbid the case a human most wants. The member owns the
consequence of touching the index underneath a running turn.

This follows from the settled trust model rather than sitting beside it. A
product that refuses to fence a trusted member's shell has no reason to fence
their timing either.

**Session lifecycle.** An idle timeout belongs in
[`limits.py`](../../src/rcp/limits.py). A closed tab must not leak a shell;
orphan cleanup is required, not optional.

**Audit is metadata only.** Member, repository, start, end, termination reason.
No byte transcript: a transcript is a credential-leak surface and buys little
when the working tree is the thing that changed.

**Terminals is a destination, not a Settings panel.** It joins the project tab
row in [`App.tsx`](../../web/src/App.tsx) beside Overview, Inbox, Research,
Runs, Artifacts, Paper, Settings, and Chats. Settings keeps the repository list
and grows no terminal control, so the feature has one owner. Confirmed by the
human on 2026-09-19 against a rendered mockup.

Its empty state is the repository list itself: each server-local repository is
the control that opens a session on it, showing the path it will start in. No
instructional empty-state copy, per the no-commentary rule in
[`interface-and-visual-design.md`](../specs/interface-and-visual-design.md).

Two consequences follow, and neither existed while this lived in Settings.
Sessions now **outlive the view**, so leaving for Research and returning must
find the same shell; session lifetime belongs to the idle timeout, not to the
panel being mounted. And **concurrent sessions are expected**, so the rail lists
them with live or idle state, one session per repository, each ended on its own.
A running Work turn is marked twice: a strip above the active terminal and a
mark on that session's rail row, so it is visible from a session the member is
not looking at.

The Terminals tab appears when at least one project machine can host a session,
and a remote probe that is pending or has failed also keeps it visible so its
status and Refresh control stay reachable. It is hidden for an empty project,
or one whose repositories are all on unavailable local machines, where there is
nothing the tab could explain. Predictability won where a probe result would
otherwise make the tab come and go; an unavailable local machine is settled,
not pending, so hiding it there costs nothing.

**Scope.** Repositories on the server itself and on remote execution machines.
Remote arrived in this work rather than a later handoff, because every
repository in the requesting human's projects is remote and a server-only
terminal would not have been usable by the person who asked for it. A machine
that cannot host a terminal says why instead of offering a weaker one.

## What this is not

- Not a machine console. One repository, not one server.
- Not a graph channel. `patch.json` in the task stage stays the only way a graph
  changes (invariant 4b). A terminal writes repository files and nothing else.
- Not an administrator role. It confers nothing over projects the member does
  not belong to — but see the decision record: this is enforced by the route's
  membership check, not by the shell's identity.
- Not a containment claim. Said twice on purpose.

## Closure condition

A project member opens the Terminals destination, starts a session on a
server-local repository, and completes a real conflicted `git rebase -i` in it,
including after navigating away and back. A regression test
proves the session cannot write under `.research` on any registered repository,
including through a canonicalized path. The served-app journey is driven on a
throwaway data directory, and the spec text that lands with it states plainly
that the shell is not contained against a member acting deliberately.
