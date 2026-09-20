# A terminal in a project, fenced to its repositories

Date: 2026-09-19
Status: design proposed, not yet confirmed by a human. Nothing is implemented.
The open questions at the end are unanswered and block the plan.

Close this handoff when a project member can open an interactive shell on a
registered repository from the project UI, that shell runs every ordinary Git
command including the interactive ones, and the kernel — not a command filter —
keeps it out of the control plane, other projects, and provider credentials.

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
- `rcp-server/credentials` — every project's Git deploy key, and the Claude
  setup token.
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

## The shape

One interactive shell, per registered repository, opened from the project UI by
a project member, running inside a namespace whose writable set is that one
repository root.

Containment reuses the primitive the compute backend already runs:
[`systemd_user.py`](../../src/rcp/compute_jobs/backends/systemd_user.py) starts
jobs under `systemd-run` with `PrivateUsers=yes`, `ProtectSystem=strict`,
`ProtectHome=read-only`, an explicit `ReadWritePaths` per writable root, and
`ReadOnlyPaths` for protected paths. `rcp server compute probe` already verifies
cgroup separation for it.

The terminal's profile differs from the compute profile in one deliberate way,
and this is the decision the whole design rests on.

**`ProtectHome=yes`, not `read-only`.** Read-only still means readable, and the
provider credentials are under `/home/rcp`. A compute job is RCP's own work; a
member shell is a human with a keyboard. The repository root is re-exposed
through its `ReadWritePaths` entry, which takes precedence over `ProtectHome`.
The deploy key is re-exposed through `ReadOnlyPaths`, and nothing else under
`/home/rcp` is visible at all.

Network stays on. `PrivateNetwork` is not set, because `git push` is the point.

`.research` is never in the writable set. It is not excluded by a rule that
someone could forget to apply; it is excluded because the writable set is built
from the repository roots in the project manifest and `.research` is not one of
them.

## Transport

This is the expensive half. The repository has no websocket and no
server-sent-events endpoint — `grep` over `src/rcp` and `web/src` finds neither.
Every existing stream is polled. An interactive PTY needs:

- a PTY allocated inside the fenced unit;
- a bidirectional byte channel from the browser to that PTY;
- a session lifecycle, so a closed tab does not leak a shell;
- an idle timeout in [`limits.py`](../../src/rcp/limits.py);
- a terminal emulator in the Web app.

A `RepositoryConfig` is `(alias, machine, path)` and a `MachineConfig` carries a
`host` that is empty for the local machine
([`config.py`](../../src/rcp/config.py)). So the target may be the server itself
or a remote execution account over SSH, and the remote case reuses the existing
pattern of shipping a module's source to the host, as
[`transport/`](../../src/rcp/transport/) already does throughout.

## What this is not

- Not a machine console. The fence is per repository, not per server.
- Not a graph channel. `patch.json` in the task stage remains the only way a
  graph changes (invariant 4b). A terminal writes files and nothing else.
- Not an administrator role. It grants no capability over projects the member is
  not a member of.
- Not a replacement for the operator's own SSH access. Installation, backup,
  restore, and release update stay where they are.

## Open questions

1. **Remote machines.** `systemd-run --user` is Linux-only, and the compute
   backend already treats macOS as a separate ownership case. A repository on a
   remote machine with no comparable fence can be refused, or offered unfenced
   with the difference stated. Refusing is the safer default and the less useful
   product.
2. **Concurrency with agent work.** A member editing the working tree while a
   Work turn runs in the same checkout will collide. The conversation-worktree
   machinery already exists; whether the terminal gets its own worktree or
   shares the checkout changes the design materially.
3. **Audit.** Whether a session records anything durable, and if so what. A
   transcript is a credential-leak surface; no record at all means a
   corrupted working tree has no history.

## Closure condition

A member opens a terminal on a registered repository from the project UI,
completes a real conflicted `git rebase -i` in it, and a check proves that the
same shell cannot read `rcp-server/data`, another project's checkout, or
`/home/rcp/.claude`.
