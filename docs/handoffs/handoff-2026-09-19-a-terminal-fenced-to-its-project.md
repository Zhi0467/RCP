# A terminal in a project, fenced to its repositories

Date: 2026-09-19
Status: REFUTED as written. A gpt-6-astra review on 2026-09-19 broke the
containment mechanism; two of its findings were reverified directly. Nothing is
implemented, and one blocking question must be answered before a redesign.

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

## Where this leaves the design

The expensive part was never PTY transport. It is separating member-controlled
execution from RCP's machine authority. Any workable version needs a distinct
execution identity that is not the service account, an independent clone rather
than a linked worktree sharing `.git`, no reachable control or agent socket, and
Git authentication that does not hand over the central deploy key.

That is a materially larger feature than this handoff described, and its shape
depends entirely on the question below.

## The question that precedes the others

**What authority may member-controlled code hold on the server?**

RCP's existing provider containment assumes cooperative inputs
([`providers-and-containment.md`](../specs/providers-and-containment.md)). A
member terminal either keeps that assumption — convenience for colleagues who
could already ask an agent to run anything — or breaks it, and then needs
hostile-code isolation, credential delegation with revocation, and adversarial
verification of its own.

Answer that and the remaining questions resolve:

1. **Execution identity.** Separate account, container, or VM.
2. **Git credential.** Member credentials, a scoped auth broker, or none.
3. **Repository ownership.** Independent clone versus shared `.git`, and what
   happens to refs, config, and hooks a later service-account operation runs.
4. **What survives membership loss.** A copied key does not expire.
5. **Audit.** Session metadata — member, target, profile, termination — is
   available without recording terminal bytes.

Remote execution machines stay out of scope until the local case is settled.

## Closure condition

Deliberately not yet written. The previous one tested three reads and a rebase,
and would have passed a shell that could still reach `/run/rcp/control.sock` and
rewrite `.research`.
