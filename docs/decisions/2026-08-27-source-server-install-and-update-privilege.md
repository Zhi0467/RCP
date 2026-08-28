# Source server uses staged releases and split operator/service privilege

**Status:** accepted on 2026-08-27.

## Decision

The first team server is installed from source through one disposable bootstrap
checkout and one separately managed production checkout.

The first supported server platforms are Ubuntu 22.04 LTS and Ubuntu 24.04 LTS
on x86-64 with systemd. The server build uses Node.js 24 and Python 3.12 managed
through `uv`, plus Git, OpenSSH, and the upstream `age` CLI in the range
`>=1.0.0,<2.0.0`. The first backup format accepts only native X25519 `age1...`
recipients; plugin, SSH, passphrase, and post-quantum recipient behavior is not
part of the two-Ubuntu compatibility promise. The operator guide provides tested
prerequisite commands for both Ubuntu releases. The RCP installer validates
exact system tools and versions but does not modify apt repositories or install
general system software: the bootstrap CLI cannot exist until its own
prerequisites are already present. Python is the one application-runtime
exception. After creating `rcp`, the installer invokes the required system-wide
`uv` as that account to install and revalidate its isolated managed Python 3.12;
an operator cannot sensibly pre-provision files in an account that does not
exist yet. Other systemd Linux
distributions and CPU architectures are unverified, not silently claimed as
supported.

A normal machine operator clones the bootstrap checkout under their own account
and runs `npm --prefix web ci`, `npm --prefix web run build`, and `uv sync`
without privilege. The first privileged RCP command is the bootstrap checkout's
absolute `.venv/bin/rcp server install --team-name "<team name>"` path under
`sudo`. The `rcp` account may not exist before that command.

The installer creates or validates the dedicated `rcp` account, installs or
validates its application-owned uv-managed Python 3.12, and creates a separate
clean managed checkout of GitHub `main`. It never adopts the operator's
bootstrap checkout. Root performs only operating-system work: service-account
and directory setup, the stable CLI wrapper, systemd unit/timer installation,
and systemd start/restart. Git fetch, npm, the Web build, `uv sync --frozen`, and
the service process run as `rcp`. On a fresh data directory the installed unit
remains stopped and disabled while the operator runs
`sudo -u rcp -H /usr/local/bin/rcp space init --team --name ...` interactively.
Only then does root enable/start systemd and read back health. This keeps the
one-time bootstrap code out of service logs and prevents initialization from
opening SQLite beside a running lock owner. The disposable bootstrap checkout
may be removed after installation without affecting the service.

All ordinary server-owned state is grouped below
`/home/rcp/rcp-server/`: the managed checkout at `source/`, clean per-commit
builds at `releases/<commit>/`, `RCP_DATA_DIR` at `data/`, server-local central
checkouts at `projects/<project-id>/repositories/<alias>/`, the source and
server-local project keys at `credentials/`, update rollback state at
`update-checkpoints/`, and disaster-restore journals and protected candidates at
`restore-operations/`. A checkout on an SSH machine keeps its repository key on
that same configured account under the verified absolute
`<remote-home>/.local/share/rcp/credentials/` root; the private key never moves
through the server. Every provider keeps its native account state in its
ordinary home path (currently `/home/rcp/.codex` and `/home/rcp/.claude`), while
SSH uses `/home/rcp/.ssh`; RCP does not relocate or manage provider
authentication.

The dedicated account itself has fixed home `/home/rcp`, a real `/bin/bash`
shell, and no usable password. Its Ubuntu shadow entry uses an unusable
non-locking value such as `*NP*`, following Ubuntu's `sshd(8)` guidance for
[22.04](https://manpages.ubuntu.com/manpages/jammy/man8/sshd.8.html) and
[24.04](https://manpages.ubuntu.com/manpages/noble/man8/sshd.8.html)
for denying password authentication without a leading `!` account lock that can
also prevent public-key authentication. A nologin account would
prevent the operator from running provider-native login commands as that
execution account and would conflict with the accepted optional direct
`rcp@server` route. Installation
does not enable password SSH or change global `sshd_config`; direct
service-account SSH is key-only and exists only when an operator deliberately
provisions that route. The preferred named-operator path uses narrow sudo, and
`rcp` receives no general sudo or supplemental privileged group membership.
The operator guide provides a root-owned, `visudo`-validated example for that
narrow service-account command family, but RCP neither guesses the named account
nor silently edits sudo policy.

Only root/system integration lives outside that home: the versioned machine
configuration at `/etc/rcp/server.toml`, root-owned current-release pointer at
`/etc/rcp/current`, private runtime socket at `/run/rcp/control.sock`, stable
wrapper at `/usr/local/bin/rcp`, systemd units, and journald. The configured
backup destination may be elsewhere. The installer records and validates these
absolute paths rather than rediscovering them from platform defaults.

The machine config also retains one immutable random nonsecret
`installation_id`. A private source origin's read-only deploy key is labelled
`rcp-source:<installation-id>` and only its public fingerprint is recorded, so a
restore can name the old grant to revoke without preserving its private key.

Later updates are invoked by an authorized machine operator as:

```bash
sudo rcp server update
```

The update coordinator validates its root invocation, then runs managed
checkout fetch/fast-forward as `rcp`. It creates a separate clean release
directory for the exact target commit, then runs `npm --prefix web ci`,
`npm --prefix web run build`, `uv sync --frozen`, and readiness preflight there
as `rcp`. The currently running release and its environment remain untouched
throughout candidate preparation.

Only a successful candidate rehearsal may reach the cutover. That rehearsal
uses a consistent copy of actual state but runs behind an offline fence: it may
migrate, replay, plan recovery, and serve diagnostic reads, while provider
turns, watchers, timers, Git writes, and other external effects are forbidden.
An attempted effect fails the rehearsal. The coordinator then closes mutation
and machine-operation admission, waits for in-flight provider turns, mutations,
backups, provisioning steps, and transfer uploads to reach a durable boundary,
and enters a short maintenance window.
Long-lived watchers do not have to finish: their durable state is recovered by
the replacement process. With no new mutation admitted, the coordinator takes a
final local rollback checkpoint of every RCP-owned state surface the candidate
startup may change.

The narrow root portion switches the service's `current` release pointer and
starts the candidate with normal work and all startup external effects still
closed behind the rehearsal's same fence. In particular, provider warming,
watcher activity, timers, recovery dispatch, remote-stage cleanup, and Git work
remain deferred while rollback is possible. It reads back the running commit
and verifies startup, ownership, replay/recovery, and representative API reads
before releasing the fence and reopening mutation admission. The local
checkpoint includes current local run stages and attachment sets through their
concrete owners; it does not pretend to snapshot remote run stages that the
fenced candidate cannot yet touch. If any post-switch verification
fails, the coordinator stops the candidate, restores the final checkpoint and
previous release pointer, starts and verifies the previous release, and then
reopens service. That restoration is automatic but never silent: the command,
server status, and durable operation receipt report the failed target and the
restored commit. A failed pre-switch candidate remains diagnosable or is removed
through explicit safe cleanup and never changes the running release. The `rcp`
account receives no general sudo rule or general permission to control systemd.

The restoration itself is crash-safe. Before moving candidate app data or local
canonical roots aside, the coordinator fsyncs a phase journal beside the
checkpoint. Re-entry keeps the service stopped and idempotently completes and
verifies the previous bytes/release; it never starts from a mixed overlay or
silently drops candidate-created unknown roots. Those roots remain in the exact
operation quarantine until explicit safe cleanup.

## Why

The service account cannot perform the initial installation because it does not
exist yet, and a system service cannot normally install or restart itself without
machine authority. Conversely, builds and Git operations should not run as root:
doing so creates root-owned source state, widens credential exposure, and makes
ordinary updates depend on root's home and caches.

A disposable bootstrap avoids turning an operator's home checkout, branches,
dirty files, or personal Git credentials into production state. One privileged
coordinator keeps the operator command simple while preserving the narrow
execution identity of every source/build step.

An explicit two-release Ubuntu matrix turns "Linux" into a testable promise.
Pinning the service build's Node and Python versions prevents an OS default or a
future language release from changing one server update while leaving another
unchanged. Keeping OS prerequisite installation outside RCP also avoids making a
research application an implicit operating-system package manager.

Building in the live source directory would let a failed npm, Web, or Python
sync mutate files and dependencies beneath the old process. Per-commit release
directories keep the running source coherent until the replacement has passed
every check, while remaining entirely source-built from GitHub `main`.

The final maintenance barrier is what makes automatic restoration lossless. A
snapshot taken while new work can still commit would not identify one coherent
state to restore. Candidate preparation and rehearsal therefore remain online,
while only the final checkpoint, switch, verification, and possible restoration
briefly stop new mutations. This local checkpoint is an update safety boundary,
not a substitute for the encrypted off-server backup workflow.

## Rejected alternatives

- Adopt the bootstrap checkout as production: binds service correctness to an
  operator-owned working tree and credentials.
- Run the complete install or update as root: creates root-owned build state and
  exposes source credentials to the wrong account.
- Run update directly as `rcp`: cannot safely restart the system service without
  an additional privilege mechanism.
- Give `rcp` broad sudo or systemd rights: unnecessarily lets the long-running
  service identity control machine services.
- Pull and build in the running checkout: a partial build can corrupt the next
  restart even while the old in-memory process appears healthy.
- Package or download release artifacts: outside the source-built team target.
- Claim generic Linux support after testing one hosted runner: leaves package,
  service, and filesystem differences outside the stated evidence.
- Let the installer add apt repositories or install arbitrary prerequisites:
  broadens its root authority and still cannot solve the pre-bootstrap tools.
