# Source server uses staged releases and split operator/service privilege

**Status:** accepted on 2026-08-27.

## Decision

The first team server is installed from source through one disposable bootstrap
checkout and one separately managed production checkout.

A normal machine operator clones the bootstrap checkout under their own account
and runs the required `npm ci`, Web build, and `uv sync` without privilege. The
first privileged RCP command is the bootstrap checkout's absolute
`.venv/bin/rcp server install` path under `sudo`. The `rcp` account may not exist
before that command.

The installer creates or validates the dedicated `rcp` account and a separate
clean managed checkout of GitHub `main`. It never adopts the operator's
bootstrap checkout. Root performs only operating-system work: service-account
and directory setup, the stable CLI wrapper, systemd unit/timer installation,
and systemd start/restart. Git fetch, npm, the Web build, `uv sync --frozen`, and
the service process run as `rcp`. The disposable bootstrap checkout may be
removed after installation without affecting the service.

Later updates are invoked by an authorized machine operator as:

```bash
sudo rcp server update
```

The update coordinator validates its root invocation, then runs managed
checkout fetch/fast-forward as `rcp`. It creates a separate clean release
directory for the exact target commit, then runs npm, the Web build, `uv sync
--frozen`, and readiness preflight there as `rcp`. The currently running release
and its environment remain untouched throughout candidate preparation.

Only a successful candidate may replace the service's `current` release pointer.
The coordinator returns to its narrow root portion for that switch and the
systemd restart, then reads back the running commit. A failed candidate remains
diagnosable or is removed through an explicit safe cleanup; it never changes the
running release. A failed post-switch start is reported loudly and never causes
a silent rollback. The `rcp` account receives no general sudo rule or general
permission to control systemd.

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

Building in the live source directory would let a failed npm, Web, or Python
sync mutate files and dependencies beneath the old process. Per-commit release
directories keep the running source coherent until the replacement has passed
every check, while remaining entirely source-built from GitHub `main`.

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
