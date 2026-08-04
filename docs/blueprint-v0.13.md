# Research Control Panel blueprint v0.13

This amendment supersedes only remote canonical-state lock ownership and
contention behavior described by earlier blueprint versions. All other sections
remain in force.

## Process-held canonical-state locks

Remote `.research/.agent-run.lock` and `.research/.refresh.lock` are regular
advisory-lock files. Existence is not ownership. RCP holds ownership through a
dedicated SSH child whose remote process owns an operating-system `flock`; normal
exit, local process death, or connection loss closes that process and releases
ownership without cleanup of a marker path.

Remote canonical publication is fenced by that same owner. RCP may stage bytes
under `.research/.publish/` with rsync, but only the process holding
`.refresh.lock` moves staged files to canonical paths. If its channel disappears
during a history commit, RCP observes the append-only patch commit point as
present, absent, or unknown and follows the existing reconciliation rules; it
never falls back to a separate unfenced SSH apply process. Ordinary-file apply
loss reports that a prefix may have landed and requires a fresh transaction to
restage the complete file set.

Live contention waits and remains non-terminal. Seed and Refresh record the wait
against the durable task, launch no duplicate provider, and can pause before the
provider starts. The short refresh/transaction lock follows the same ownership
primitive.

RCP never automatically replaces an entry whose type prevents safe ownership
proof. A directory from a pre-v0.13 deployment, a symlink, or a special file is
preserved. The task reports the exact canonical location, the unverifiable fact,
and why replacement was refused; it never instructs the human to delete a lock
path manually. This is the only refusal branch. An unowned regular lock file is
harmless and is acquired normally.
